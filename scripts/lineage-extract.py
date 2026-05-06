#!/usr/bin/env python3
"""
lineage-extract.py — Extract BigQuery routine ↔ table lineage from runtime jobs + repo SQL

Modes:
  --from-jobs   Query INFORMATION_SCHEMA.JOBS to capture runtime read/write edges
  --from-repo   Use sqlglot to statically parse SP/function bodies in git
  --merge       (no-op for now; both modes write into same SQLite, this is a placeholder
                 for future de-duplication / consolidation logic)
  --report      Print a markdown report for a given routine

Storage:
  SQLite at the path given by --db (default: ./lineage.db)

  Schema:
    routines (id, schema, name, last_seen, has_dynamic_sql)
    tables   (id, schema, name)
    edges    (id, src_routine_id, dst_table_id,
              edge_type ('read' | 'write'),
              source ('jobs' | 'sqlglot'),
              first_seen, last_seen, sample_count)

Why two sources:
  jobs    = ground truth runtime view (catches dynamic SQL too)
            but only reflects what actually ran
  sqlglot = static analysis of SP body (covers untested SP)
            but blind to EXECUTE IMMEDIATE / dynamic strings

Cross-checking the two reveals where audit-log-based lineage is required (i.e. SPs
whose actual writes don't match static parse).

Dependencies:
  pip install pyyaml sqlglot
  bq CLI authenticated (gcloud auth application-default login)

Usage examples:

  # Capture last 30 days of runtime lineage
  python lineage-extract.py --from-jobs \\
      --project tapirus-test-384312 \\
      --region US \\
      --days 30 \\
      --db ./lineage.db

  # Statically parse all routines in git
  python lineage-extract.py --from-repo \\
      --git-root ./bigquery \\
      --db ./lineage.db

  # Print report for one routine
  python lineage-extract.py --report \\
      --routine analytics.sp_build_daily_summary \\
      --db ./lineage.db
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

# Optional imports — only required by certain modes; we lazy-fail with a friendly message.
try:
    import sqlglot                          # type: ignore
    from sqlglot import exp as sqlglot_exp  # type: ignore
    HAS_SQLGLOT = True
except ImportError:
    HAS_SQLGLOT = False


# ---------- SQLite schema ----------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS routines (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  schema          TEXT NOT NULL,
  name            TEXT NOT NULL,
  last_seen       TEXT,
  has_dynamic_sql INTEGER DEFAULT 0,
  UNIQUE (schema, name)
);

CREATE TABLE IF NOT EXISTS tables (
  id     INTEGER PRIMARY KEY AUTOINCREMENT,
  schema TEXT NOT NULL,
  name   TEXT NOT NULL,
  UNIQUE (schema, name)
);

CREATE TABLE IF NOT EXISTS edges (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  src_routine_id  INTEGER NOT NULL,
  dst_table_id    INTEGER NOT NULL,
  edge_type       TEXT NOT NULL CHECK (edge_type IN ('read','write')),
  source          TEXT NOT NULL CHECK (source IN ('jobs','sqlglot')),
  first_seen      TEXT,
  last_seen       TEXT,
  sample_count    INTEGER DEFAULT 1,
  UNIQUE (src_routine_id, dst_table_id, edge_type, source),
  FOREIGN KEY (src_routine_id) REFERENCES routines(id),
  FOREIGN KEY (dst_table_id)   REFERENCES tables(id)
);

CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_routine_id);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_table_id);
"""


# ---------- DB helpers ----------

def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    return conn


def upsert_routine(conn: sqlite3.Connection, schema: str, name: str,
                   last_seen: str | None = None,
                   has_dynamic_sql: bool | None = None) -> int:
    cur = conn.execute(
        "INSERT OR IGNORE INTO routines (schema, name) VALUES (?, ?)",
        (schema, name),
    )
    if cur.lastrowid:
        rid = cur.lastrowid
    else:
        rid = conn.execute(
            "SELECT id FROM routines WHERE schema=? AND name=?", (schema, name)
        ).fetchone()["id"]

    if last_seen is not None:
        conn.execute(
            "UPDATE routines SET last_seen=? WHERE id=? AND (last_seen IS NULL OR last_seen<?)",
            (last_seen, rid, last_seen),
        )
    if has_dynamic_sql is not None:
        conn.execute(
            "UPDATE routines SET has_dynamic_sql=? WHERE id=?",
            (1 if has_dynamic_sql else 0, rid),
        )
    return rid


def upsert_table(conn: sqlite3.Connection, schema: str, name: str) -> int:
    cur = conn.execute(
        "INSERT OR IGNORE INTO tables (schema, name) VALUES (?, ?)",
        (schema, name),
    )
    if cur.lastrowid:
        return cur.lastrowid
    return conn.execute(
        "SELECT id FROM tables WHERE schema=? AND name=?", (schema, name)
    ).fetchone()["id"]


def upsert_edge(conn: sqlite3.Connection, routine_id: int, table_id: int,
                edge_type: str, source: str, ts: str | None = None) -> None:
    cur = conn.execute(
        "INSERT OR IGNORE INTO edges "
        "(src_routine_id, dst_table_id, edge_type, source, first_seen, last_seen, sample_count) "
        "VALUES (?, ?, ?, ?, ?, ?, 1)",
        (routine_id, table_id, edge_type, source, ts, ts),
    )
    if cur.rowcount == 0:
        # already existed → bump count + last_seen
        conn.execute(
            "UPDATE edges SET sample_count = sample_count + 1, "
            "last_seen = CASE WHEN ? IS NULL OR last_seen >= ? THEN last_seen ELSE ? END "
            "WHERE src_routine_id=? AND dst_table_id=? AND edge_type=? AND source=?",
            (ts, ts, ts, routine_id, table_id, edge_type, source),
        )


# ---------- Mode: --from-jobs ----------

JOBS_SQL = """
WITH base AS (
  SELECT
    creation_time,
    user_email,
    job_id,
    statement_type,
    query,
    referenced_tables,
    destination_table
  FROM `region-{region}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
  WHERE creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
    AND state = 'DONE'
    AND error_result IS NULL
    AND statement_type IS NOT NULL
)
SELECT
  CAST(creation_time AS STRING) AS creation_time,
  user_email,
  job_id,
  statement_type,
  query,
  TO_JSON_STRING(referenced_tables) AS referenced_tables_json,
  TO_JSON_STRING(destination_table) AS destination_table_json
FROM base
ORDER BY creation_time DESC
LIMIT 50000
"""


# Match `CALL `dataset.routine`(...)` or `CALL dataset.routine(...)`
_CALL_RE = re.compile(r"\bCALL\s+`?([\w-]+)\.([\w-]+)`?\s*\(", re.IGNORECASE)


def fetch_jobs(project: str, region: str, days: int) -> list[dict]:
    sql = JOBS_SQL.format(region=region.lower(), days=days)
    print(f"Querying INFORMATION_SCHEMA.JOBS_BY_PROJECT on {project} (region={region}, last {days}d)...")
    result = subprocess.run(
        ["bq", "query", f"--project_id={project}", "--use_legacy_sql=false",
         "--format=json", "--max_rows=50000", sql],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: bq query failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(2)
    return json.loads(result.stdout) if result.stdout.strip() else []


def parse_table_ref(blob_json: str | None, source_project: str) -> list[tuple[str, str]]:
    """
    Parse a BQ table reference JSON (single dict or array of dicts) into list of (schema, name).
    Same-project entries strip project; cross-project we ignore for lineage purposes
    (still in graph but tagged differently — kept simple here).
    """
    if not blob_json or blob_json in ("null", ""):
        return []
    try:
        parsed = json.loads(blob_json)
    except json.JSONDecodeError:
        return []

    out: list[tuple[str, str]] = []
    if isinstance(parsed, dict):
        parsed = [parsed]
    for item in parsed:
        if not isinstance(item, dict):
            continue
        ds = item.get("dataset_id") or item.get("datasetId")
        tbl = item.get("table_id") or item.get("tableId")
        if ds and tbl:
            out.append((ds, tbl))
    return out


def routine_from_query(query: str | None) -> tuple[str, str] | None:
    """If the query is essentially `CALL dataset.routine(...)`, return (schema, name)."""
    if not query:
        return None
    m = _CALL_RE.search(query)
    if not m:
        return None
    return (m.group(1), m.group(2))


def run_from_jobs(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    rows = fetch_jobs(args.project, args.region, args.days)
    print(f"Got {len(rows)} job rows. Building edges...")

    matched_calls = 0
    matched_writes = 0

    for r in rows:
        # 1. Try to identify the calling routine
        routine_pair = routine_from_query(r.get("query"))
        if not routine_pair:
            # No detectable routine call. Skip — we don't model "user-issued ad-hoc query as source"
            # in this MVP. (Could be added later if useful.)
            continue
        rs, rn = routine_pair
        rid = upsert_routine(conn, rs, rn, last_seen=r.get("creation_time"))
        matched_calls += 1

        # 2. Reads = referenced_tables (excluding the destination)
        reads = parse_table_ref(r.get("referenced_tables_json"), args.project)

        # 3. Writes = destination_table (only when statement is write-ish)
        statement_type = (r.get("statement_type") or "").upper()
        write_statements = {"INSERT", "MERGE", "UPDATE", "DELETE",
                            "CREATE_TABLE_AS_SELECT", "CREATE_TABLE",
                            "ALTER_TABLE", "DROP_TABLE", "TRUNCATE_TABLE"}
        writes = []
        if statement_type in write_statements:
            writes = parse_table_ref(r.get("destination_table_json"), args.project)

        # 4. Insert edges
        for ds, tbl in reads:
            tid = upsert_table(conn, ds, tbl)
            upsert_edge(conn, rid, tid, "read", "jobs", ts=r.get("creation_time"))

        for ds, tbl in writes:
            tid = upsert_table(conn, ds, tbl)
            upsert_edge(conn, rid, tid, "write", "jobs", ts=r.get("creation_time"))
            matched_writes += 1

    conn.commit()
    print(f"✓ {matched_calls} routine-call rows processed, {matched_writes} write edges added.")


# ---------- Mode: --from-repo ----------

def _strip_metadata_header(text: str) -> str:
    """Drop leading `--` header comment lines (file metadata) before parsing."""
    lines = text.splitlines(keepends=True)
    i = 0
    while i < len(lines) and (lines[i].lstrip().startswith("--") or not lines[i].strip()):
        i += 1
    return "".join(lines[i:])


def _detect_dynamic_sql(text: str) -> bool:
    return bool(re.search(r"\bEXECUTE\s+IMMEDIATE\b", text, re.IGNORECASE))


def _routine_name_from_path(path: Path) -> tuple[str, str]:
    # bigquery/{schema}/routines/{name}.sql
    parts = path.parts
    try:
        idx = parts.index("routines")
        schema = parts[idx - 1]
        name = path.stem
        return schema, name
    except (ValueError, IndexError):
        return ("unknown", path.stem)


def _walk_for_table_refs(node) -> Iterable[tuple[str | None, str]]:
    """Yield (schema, name) for every Table node found in an AST."""
    for table in node.find_all(sqlglot_exp.Table):
        ds = table.args.get("db")
        ds_name = ds.name if ds else None
        tbl_name = table.name
        if tbl_name:
            yield (ds_name, tbl_name)


def run_from_repo(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    if not HAS_SQLGLOT:
        print("ERROR: sqlglot not installed. Run: pip install sqlglot", file=sys.stderr)
        sys.exit(3)

    git_root = Path(args.git_root)
    if not git_root.exists():
        print(f"ERROR: git root not found: {git_root}", file=sys.stderr)
        sys.exit(3)

    files = list(git_root.glob("*/routines/*.sql"))
    print(f"Parsing {len(files)} routine files under {git_root}...")

    parsed = 0
    failed: list[tuple[Path, str]] = []
    dynamic = 0

    for f in files:
        text = _strip_metadata_header(f.read_text(encoding="utf-8"))
        schema, routine_name = _routine_name_from_path(f)
        is_dynamic = _detect_dynamic_sql(text)
        rid = upsert_routine(conn, schema, routine_name, has_dynamic_sql=is_dynamic)
        if is_dynamic:
            dynamic += 1

        try:
            statements = sqlglot.parse(text, dialect="bigquery")
        except Exception as e:                                                 # noqa: BLE001
            failed.append((f, str(e)[:200]))
            continue

        seen_reads: set[tuple[str, str]] = set()
        seen_writes: set[tuple[str, str]] = set()

        for stmt in statements:
            if stmt is None:
                continue

            # Writes: any Insert / Update / Delete / Merge / CreateTableAs / Drop has a "this" table
            for klass, edge_kind in (
                (sqlglot_exp.Insert, "write"),
                (sqlglot_exp.Update, "write"),
                (sqlglot_exp.Delete, "write"),
                (sqlglot_exp.Merge, "write"),
            ):
                for n in stmt.find_all(klass):
                    target = n.this
                    # `target` may be a Table or a Schema → drill to Table
                    tables = list(target.find_all(sqlglot_exp.Table)) if hasattr(target, "find_all") else []
                    if not tables and isinstance(target, sqlglot_exp.Table):
                        tables = [target]
                    for t in tables:
                        ds = (t.args.get("db") or sqlglot_exp.Identifier(this=schema)).name
                        nm = t.name
                        if nm:
                            seen_writes.add((ds, nm))

            # Reads: every Table that's NOT the direct write target
            all_tables = {(((t.args.get("db") or sqlglot_exp.Identifier(this=schema)).name), t.name)
                          for t in stmt.find_all(sqlglot_exp.Table) if t.name}
            for entry in all_tables - seen_writes:
                seen_reads.add(entry)

        for ds, nm in seen_reads:
            tid = upsert_table(conn, ds, nm)
            upsert_edge(conn, rid, tid, "read", "sqlglot")
        for ds, nm in seen_writes:
            tid = upsert_table(conn, ds, nm)
            upsert_edge(conn, rid, tid, "write", "sqlglot")

        parsed += 1

    conn.commit()
    print(f"✓ {parsed}/{len(files)} routines parsed, {dynamic} contain EXECUTE IMMEDIATE")
    if failed:
        print(f"⚠ {len(failed)} routines failed to parse:")
        for p, msg in failed[:10]:
            print(f"   {p}: {msg}")


# ---------- Mode: --report ----------

def run_report(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    if "." not in args.routine:
        print("ERROR: --routine should be '{schema}.{name}'", file=sys.stderr)
        sys.exit(64)
    schema, name = args.routine.split(".", 1)

    row = conn.execute(
        "SELECT id, last_seen, has_dynamic_sql FROM routines WHERE schema=? AND name=?",
        (schema, name),
    ).fetchone()
    if not row:
        print(f"# {args.routine}\n\nNot found in lineage DB.")
        return
    rid = row["id"]

    def collect(edge_type: str) -> list[sqlite3.Row]:
        return conn.execute(
            """
            SELECT t.schema, t.name, e.source, e.last_seen, e.sample_count
            FROM edges e JOIN tables t ON t.id = e.dst_table_id
            WHERE e.src_routine_id = ? AND e.edge_type = ?
            ORDER BY t.schema, t.name, e.source
            """,
            (rid, edge_type),
        ).fetchall()

    reads = collect("read")
    writes = collect("write")

    out: list[str] = []
    out.append(f"# Lineage report — `{args.routine}`")
    out.append("")
    out.append(f"- Last seen in jobs: {row['last_seen'] or '—'}")
    out.append(f"- Contains EXECUTE IMMEDIATE: {'⚠ yes — sqlglot view incomplete' if row['has_dynamic_sql'] else 'no'}")
    out.append("")

    def render_section(title: str, rows: list[sqlite3.Row]) -> None:
        out.append(f"## {title}")
        out.append("")
        if not rows:
            out.append("_(none recorded)_")
            out.append("")
            return
        out.append("| Table | Source | Last seen (jobs) | Sample count |")
        out.append("|-------|--------|------------------|--------------|")
        for r in rows:
            out.append(
                f"| `{r['schema']}.{r['name']}` | {r['source']} | {r['last_seen'] or '—'} | {r['sample_count']} |"
            )
        out.append("")

    render_section("Writes", writes)
    render_section("Reads", reads)

    # Cross-check: edges that appear in jobs but NOT in sqlglot, or vice versa
    discrepancy = conn.execute(
        """
        SELECT t.schema, t.name, e.edge_type, GROUP_CONCAT(e.source) AS sources
        FROM edges e JOIN tables t ON t.id = e.dst_table_id
        WHERE e.src_routine_id = ?
        GROUP BY t.schema, t.name, e.edge_type
        HAVING COUNT(DISTINCT e.source) = 1
        """,
        (rid,),
    ).fetchall()

    if discrepancy:
        out.append("## ⚠ Source mismatch")
        out.append("")
        out.append("Edges seen by only one source. If `jobs` only → sqlglot missed it (likely dynamic SQL). "
                   "If `sqlglot` only → not yet observed at runtime (may be unused / new code).")
        out.append("")
        out.append("| Table | Edge | Source seen |")
        out.append("|-------|------|-------------|")
        for r in discrepancy:
            out.append(f"| `{r['schema']}.{r['name']}` | {r['edge_type']} | {r['sources']} |")
        out.append("")

    print("\n".join(out))


# ---------- Mode: --merge (placeholder) ----------

def run_merge(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    """
    Currently a no-op: --from-jobs and --from-repo both write into the same DB,
    so 'merge' happens implicitly. This entry point exists so future logic
    (e.g. consolidating duplicate aliases, resolving cross-project refs)
    has a clear place to live.
    """
    routine_count = conn.execute("SELECT COUNT(*) AS c FROM routines").fetchone()["c"]
    table_count = conn.execute("SELECT COUNT(*) AS c FROM tables").fetchone()["c"]
    edge_count = conn.execute("SELECT COUNT(*) AS c FROM edges").fetchone()["c"]
    by_source = conn.execute(
        "SELECT source, edge_type, COUNT(*) AS c FROM edges GROUP BY source, edge_type"
    ).fetchall()

    print("DB summary:")
    print(f"  routines : {routine_count}")
    print(f"  tables   : {table_count}")
    print(f"  edges    : {edge_count}")
    for r in by_source:
        print(f"    - {r['source']:<8} {r['edge_type']:<6} {r['c']}")


# ---------- CLI ----------

def main() -> int:
    p = argparse.ArgumentParser(description="Extract BQ routine ↔ table lineage")
    sub = p.add_subparsers(dest="mode", required=True)

    # --from-jobs
    p_jobs = sub.add_parser("from-jobs", help="Extract from INFORMATION_SCHEMA.JOBS")
    p_jobs.add_argument("--project", required=True)
    p_jobs.add_argument("--region", required=True, help="e.g. US, asia-east1")
    p_jobs.add_argument("--days", type=int, default=30)
    p_jobs.add_argument("--db", default="./lineage.db")

    # --from-repo
    p_repo = sub.add_parser("from-repo", help="Static parse of git SP/function bodies")
    p_repo.add_argument("--git-root", required=True, help="e.g. ./bigquery")
    p_repo.add_argument("--db", default="./lineage.db")

    # --merge (currently summary)
    p_merge = sub.add_parser("merge", help="(Placeholder) consolidate / show DB summary")
    p_merge.add_argument("--db", default="./lineage.db")

    # --report
    p_report = sub.add_parser("report", help="Print lineage report for a routine")
    p_report.add_argument("--routine", required=True, help="schema.name")
    p_report.add_argument("--db", default="./lineage.db")

    args = p.parse_args()

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = open_db(db_path)
    try:
        if args.mode == "from-jobs":
            run_from_jobs(args, conn)
        elif args.mode == "from-repo":
            run_from_repo(args, conn)
        elif args.mode == "merge":
            run_merge(args, conn)
        elif args.mode == "report":
            run_report(args, conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
