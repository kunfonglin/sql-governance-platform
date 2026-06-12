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

# Windows console 預設可能是 cp950/cp1252，遇到 ✓ ⚠ 等 unicode 會 crash。
# 強制 stdout/stderr 改 UTF-8（Python 3.7+ 支援）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
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


# ---------- bq CLI locator ----------

_BQ_PATH_CACHE: str | None = None

def _bq_path() -> str:
    """Locate bq CLI: PATH first, then common Windows install locations."""
    global _BQ_PATH_CACHE
    if _BQ_PATH_CACHE:
        return _BQ_PATH_CACHE
    import shutil
    # Try PATH (handles both Unix bq + Windows bq.cmd)
    for cand in ("bq", "bq.cmd"):
        found = shutil.which(cand)
        if found:
            _BQ_PATH_CACHE = found
            return found
    # Windows common locations
    candidates = [
        Path.home() / "AppData/Local/Google/Cloud SDK/google-cloud-sdk/bin/bq.cmd",
        Path("C:/Program Files (x86)/Google/Cloud SDK/google-cloud-sdk/bin/bq.cmd"),
        Path("C:/Program Files/Google/Cloud SDK/google-cloud-sdk/bin/bq.cmd"),
    ]
    for p in candidates:
        if p.exists():
            _BQ_PATH_CACHE = str(p)
            return _BQ_PATH_CACHE
    raise FileNotFoundError(
        "bq CLI not found. Install Google Cloud SDK or add its bin/ to PATH."
    )


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
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  project TEXT,          -- NULL = same project as the calling routine; non-NULL = cross-project
  schema  TEXT NOT NULL,
  name    TEXT NOT NULL,
  UNIQUE (project, schema, name)
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

CREATE TABLE IF NOT EXISTS routine_calls (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  caller_routine_id INTEGER NOT NULL,
  callee_project    TEXT,
  callee_schema     TEXT NOT NULL,
  callee_name       TEXT NOT NULL,
  source            TEXT NOT NULL CHECK (source IN ('jobs','sqlglot')),
  first_seen        TEXT,
  last_seen         TEXT,
  sample_count      INTEGER DEFAULT 1,
  UNIQUE (caller_routine_id, callee_project, callee_schema, callee_name, source),
  FOREIGN KEY (caller_routine_id) REFERENCES routines(id)
);

CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_routine_id);
CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_table_id);
CREATE INDEX IF NOT EXISTS idx_calls_caller ON routine_calls(caller_routine_id);
CREATE INDEX IF NOT EXISTS idx_calls_callee ON routine_calls(callee_project, callee_schema, callee_name);
"""


# ---------- DB helpers ----------

def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_SQL)
    # Migration for DBs created before `project` column existed
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(tables)").fetchall()]
    if "project" not in cols:
        print("Migrating: adding `project` column to tables", file=sys.stderr)
        conn.execute("ALTER TABLE tables ADD COLUMN project TEXT")
        conn.commit()
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


def upsert_table(conn: sqlite3.Connection, project: str | None, schema: str, name: str) -> int:
    # project=None  → same project as the calling routine
    # project='foo' → cross-project ref to foo.schema.name
    cur = conn.execute(
        "INSERT OR IGNORE INTO tables (project, schema, name) VALUES (?, ?, ?)",
        (project, schema, name),
    )
    if cur.lastrowid:
        return cur.lastrowid
    # IS distinguishes NULL correctly; = would mismatch NULL
    return conn.execute(
        "SELECT id FROM tables WHERE project IS ? AND schema = ? AND name = ?",
        (project, schema, name),
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


def upsert_routine_call(conn: sqlite3.Connection, caller_id: int,
                        callee_project: str | None, callee_schema: str, callee_name: str,
                        source: str, ts: str | None = None) -> None:
    cur = conn.execute(
        "INSERT OR IGNORE INTO routine_calls "
        "(caller_routine_id, callee_project, callee_schema, callee_name, source, first_seen, last_seen, sample_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
        (caller_id, callee_project, callee_schema, callee_name, source, ts, ts),
    )
    if cur.rowcount == 0:
        conn.execute(
            "UPDATE routine_calls SET sample_count = sample_count + 1, "
            "last_seen = CASE WHEN ? IS NULL OR last_seen >= ? THEN last_seen ELSE ? END "
            "WHERE caller_routine_id = ? AND callee_project IS ? AND callee_schema = ? AND callee_name = ? AND source = ?",
            (ts, ts, ts, caller_id, callee_project, callee_schema, callee_name, source),
        )


# ---------- Mode: --from-jobs ----------

JOBS_SQL = r"""
WITH parent_calls AS (
  SELECT
    job_id AS parent_job_id,
    REGEXP_EXTRACT(query, r'(?i)\bCALL\s+`?([\w-]+)\.[\w-]+`?\s*\(') AS routine_schema,
    REGEXP_EXTRACT(query, r'(?i)\bCALL\s+`?[\w-]+\.([\w-]+)`?\s*\(') AS routine_name
  FROM `region-{region}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
  WHERE creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
    AND state = 'DONE'
    AND REGEXP_CONTAINS(IFNULL(query, ''), r'(?i)^\s*CALL\s')
),
all_jobs AS (
  SELECT *
  FROM `region-{region}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
  WHERE creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {days} DAY)
    AND state = 'DONE'
    AND error_result IS NULL
    AND statement_type IS NOT NULL
)
SELECT
  CAST(a.creation_time AS STRING) AS creation_time,
  a.user_email,
  a.job_id,
  a.parent_job_id,
  a.statement_type,
  a.query,
  TO_JSON_STRING(a.referenced_tables) AS referenced_tables_json,
  TO_JSON_STRING(a.destination_table) AS destination_table_json,
  pc.routine_schema AS parent_routine_schema,
  pc.routine_name   AS parent_routine_name
FROM all_jobs a
LEFT JOIN parent_calls pc ON pc.parent_job_id = a.parent_job_id
ORDER BY a.creation_time DESC
LIMIT 50000
"""


# Match `CALL `dataset.routine`(...)` or `CALL dataset.routine(...)`
_CALL_RE = re.compile(r"\bCALL\s+`?([\w-]+)\.([\w-]+)`?\s*\(", re.IGNORECASE)


def fetch_jobs(project: str, region: str, days: int) -> list[dict]:
    sql = JOBS_SQL.format(region=region.lower(), days=days)
    # Windows bq.cmd 對多行參數會吃掉換行 → 壓成單行
    sql = " ".join(sql.split())
    print(f"Querying INFORMATION_SCHEMA.JOBS_BY_PROJECT on {project} (region={region}, last {days}d)...")
    result = subprocess.run(
        [_bq_path(), "query", f"--project_id={project}", "--use_legacy_sql=false",
         "--format=json", "--max_rows=50000", sql],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        # bq on Windows may write errors to stdout instead of stderr — dump both
        print(f"ERROR: bq query failed (rc={result.returncode})", file=sys.stderr)
        if result.stderr:
            print(f"  stderr: {result.stderr.strip()}", file=sys.stderr)
        if result.stdout:
            print(f"  stdout: {result.stdout.strip()[:1000]}", file=sys.stderr)
        sys.exit(2)
    return json.loads(result.stdout) if result.stdout.strip() else []


def parse_table_ref(blob_json: str | None, source_project: str) -> list[tuple[str | None, str, str]]:
    """
    Parse a BQ table reference JSON (single dict or array of dicts) into list of (project, schema, name).
    project is None if same as source_project, else the cross-project string.
    """
    if not blob_json or blob_json in ("null", ""):
        return []
    try:
        parsed = json.loads(blob_json)
    except json.JSONDecodeError:
        return []

    out: list[tuple[str | None, str, str]] = []
    if isinstance(parsed, dict):
        parsed = [parsed]
    for item in parsed:
        if not isinstance(item, dict):
            continue
        proj = item.get("project_id") or item.get("projectId")
        ds = item.get("dataset_id") or item.get("datasetId")
        tbl = item.get("table_id") or item.get("tableId")
        if ds and tbl:
            cross_project = proj if proj and proj != source_project else None
            out.append((cross_project, ds, tbl))
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
    if not args.skip_cost_warning:
        print(
            "⚠ COST WARNING: INFORMATION_SCHEMA.JOBS_BY_PROJECT scans are NOT cheap on busy projects.\n"
            "  Typical: 10-30 GB / project / day (~$0.05-$0.15 USD per run on on-demand pricing).\n"
            "  BQ's column-pruning on system tables is weak, so SELECT optimisations don't help much.\n"
            "  For ongoing Text2SQL / lineage use, prefer `from-repo` (free, static analysis).\n"
            "  Use `from-jobs` only for: dynamic SQL discovery, runtime confirmation, one-off audits.\n"
            "  (Pass --skip-cost-warning to suppress this message.)\n",
            file=sys.stderr,
        )
    rows = fetch_jobs(args.project, args.region, args.days)
    print(f"Got {len(rows)} job rows. Building edges...")

    parent_call_rows = 0   # 父 CALL job 本身（記 last_seen 用）
    child_edge_rows = 0    # 子 job 對 routine 的 read/write 邊

    write_statements = {"INSERT", "MERGE", "UPDATE", "DELETE",
                        "CREATE_TABLE_AS_SELECT", "CREATE_TABLE",
                        "ALTER_TABLE", "DROP_TABLE", "TRUNCATE_TABLE"}

    for r in rows:
        # Path A: the job IS the parent CALL itself → use its own `query` to identify routine
        own_pair = routine_from_query(r.get("query"))

        # Path B: the job is a child statement of a parent CALL → use joined parent_routine_*
        parent_rs = r.get("parent_routine_schema")
        parent_rn = r.get("parent_routine_name")
        parent_pair = (parent_rs, parent_rn) if parent_rs and parent_rn else None

        if own_pair:
            # 父 CALL：只記 last_seen，不抽 reads/writes（writes 發生在子 job）
            rs, rn = own_pair
            upsert_routine(conn, rs, rn, last_seen=r.get("creation_time"))
            parent_call_rows += 1
            continue

        if not parent_pair:
            # adhoc query 或 CI 部署語句（CREATE OR REPLACE PROCEDURE 等），不歸到任何 routine
            continue

        # 子 job：歸到 parent CALL 的 routine 名下
        rs, rn = parent_pair
        rid = upsert_routine(conn, rs, rn, last_seen=r.get("creation_time"))

        reads = parse_table_ref(r.get("referenced_tables_json"), args.project)
        statement_type = (r.get("statement_type") or "").upper()
        writes = []
        if statement_type in write_statements:
            writes = parse_table_ref(r.get("destination_table_json"), args.project)

        for proj, ds, tbl in reads:
            tid = upsert_table(conn, proj, ds, tbl)
            upsert_edge(conn, rid, tid, "read", "jobs", ts=r.get("creation_time"))
            child_edge_rows += 1

        for proj, ds, tbl in writes:
            tid = upsert_table(conn, proj, ds, tbl)
            upsert_edge(conn, rid, tid, "write", "jobs", ts=r.get("creation_time"))
            child_edge_rows += 1

    conn.commit()
    print(f"✓ {parent_call_rows} parent CALL jobs + {child_edge_rows} read/write edges (from children) recorded.")


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


def _table_ref(t, fallback_schema: str) -> tuple[str | None, str, str] | None:
    """Extract (project, schema, name) from a sqlglot Table node.
    project = catalog if present, else None (same-project assumption).
    schema  = db if present, else fallback_schema (the SP's own schema).
    """
    if not t.name:
        return None
    catalog = t.args.get("catalog")
    db = t.args.get("db")
    proj_name = catalog.name if catalog else None
    schema_name = db.name if db else fallback_schema
    return (proj_name, schema_name, t.name)


def _detect_calls_in_body(text: str) -> set[tuple[str | None, str, str]]:
    """Find SP→SP via regex scan on the body. Returns set of (project, schema, name).

    Supported forms (cover the common BigQuery cases):
      CALL ds.routine(...)
      CALL `ds.routine`(...)
      CALL `ds`.routine(...)
      CALL `proj.ds.routine`(...)         -- triple-segment in one backticks block
      CALL `proj`.`ds`.`routine`(...)     -- each segment in its own backticks
    """
    out: set[tuple[str | None, str, str]] = set()

    # Triple-segment: project.dataset.routine (with optional backticks each segment or whole block)
    triple = re.compile(
        r"\bCALL\s+`?([\w-]+)`?\.`?([\w-]+)`?\.`?([\w-]+)`?\s*\(",
        re.IGNORECASE,
    )
    for m in triple.finditer(text):
        out.add((m.group(1), m.group(2), m.group(3)))

    # Double-segment: dataset.routine (no project, assumed same project)
    # Must NOT match three-segment forms (negative lookahead on a third .ident)
    double = re.compile(
        r"\bCALL\s+`?([\w-]+)`?\.`?([\w-]+)`?\s*\((?!\.)",
        re.IGNORECASE,
    )
    # First strip out matches the triple regex already covers, to avoid double counting
    triple_spans = [(m.start(), m.end()) for m in triple.finditer(text)]
    def _in_triple_span(pos: int) -> bool:
        return any(s <= pos < e for s, e in triple_spans)
    for m in double.finditer(text):
        if not _in_triple_span(m.start()):
            out.add((None, m.group(1), m.group(2)))
    return out


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
    total_calls = 0

    for f in files:
        text = _strip_metadata_header(f.read_text(encoding="utf-8"))
        schema, routine_name = _routine_name_from_path(f)
        is_dynamic = _detect_dynamic_sql(text)
        rid = upsert_routine(conn, schema, routine_name, has_dynamic_sql=is_dynamic)
        if is_dynamic:
            dynamic += 1

        # SP → SP call chain via regex (sqlglot's parse of CALL is uneven across versions)
        calls = _detect_calls_in_body(text)
        for c_proj, c_ds, c_rn in calls:
            upsert_routine_call(conn, rid, c_proj, c_ds, c_rn, source="sqlglot")
        total_calls += len(calls)

        try:
            statements = sqlglot.parse(text, dialect="bigquery")
        except Exception as e:                                                 # noqa: BLE001
            failed.append((f, str(e)[:200]))
            continue

        seen_reads: set[tuple[str | None, str, str]] = set()
        seen_writes: set[tuple[str | None, str, str]] = set()

        for stmt in statements:
            if stmt is None:
                continue

            # Writes: any Insert / Update / Delete / Merge has a "this" table
            for klass in (sqlglot_exp.Insert, sqlglot_exp.Update, sqlglot_exp.Delete, sqlglot_exp.Merge):
                for n in stmt.find_all(klass):
                    target = n.this
                    tables = list(target.find_all(sqlglot_exp.Table)) if hasattr(target, "find_all") else []
                    if not tables and isinstance(target, sqlglot_exp.Table):
                        tables = [target]
                    for t in tables:
                        ref = _table_ref(t, fallback_schema=schema)
                        if ref:
                            seen_writes.add(ref)

            # Reads: every Table that's NOT the direct write target
            all_tables: set[tuple[str | None, str, str]] = set()
            for t in stmt.find_all(sqlglot_exp.Table):
                ref = _table_ref(t, fallback_schema=schema)
                if ref:
                    all_tables.add(ref)
            for entry in all_tables - seen_writes:
                seen_reads.add(entry)

        for proj, ds, nm in seen_reads:
            tid = upsert_table(conn, proj, ds, nm)
            upsert_edge(conn, rid, tid, "read", "sqlglot")
        for proj, ds, nm in seen_writes:
            tid = upsert_table(conn, proj, ds, nm)
            upsert_edge(conn, rid, tid, "write", "sqlglot")

        parsed += 1

    conn.commit()
    print(f"✓ {parsed}/{len(files)} routines parsed, {dynamic} contain EXECUTE IMMEDIATE, {total_calls} CALL edges added")
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

    def _fmt_table(proj: str | None, schema: str, name: str) -> str:
        # Cross-project refs get full prefix so they're visually distinct
        return f"`{proj}.{schema}.{name}`" if proj else f"`{schema}.{name}`"

    def _fmt_routine(proj: str | None, schema: str, name: str) -> str:
        return f"`{proj}.{schema}.{name}`" if proj else f"`{schema}.{name}`"

    def collect(edge_type: str) -> list[sqlite3.Row]:
        return conn.execute(
            """
            SELECT t.project, t.schema, t.name, e.source, e.last_seen, e.sample_count
            FROM edges e JOIN tables t ON t.id = e.dst_table_id
            WHERE e.src_routine_id = ? AND e.edge_type = ?
            ORDER BY (t.project IS NULL) DESC, t.project, t.schema, t.name, e.source
            """,
            (rid, edge_type),
        ).fetchall()

    reads = collect("read")
    writes = collect("write")

    calls_out = conn.execute(
        """
        SELECT callee_project, callee_schema, callee_name, source, last_seen, sample_count
        FROM routine_calls
        WHERE caller_routine_id = ?
        ORDER BY (callee_project IS NULL) DESC, callee_project, callee_schema, callee_name, source
        """,
        (rid,),
    ).fetchall()

    called_by = conn.execute(
        """
        SELECT r.schema, r.name, rc.source, rc.last_seen, rc.sample_count
        FROM routine_calls rc JOIN routines r ON r.id = rc.caller_routine_id
        WHERE rc.callee_schema = ? AND rc.callee_name = ?
        ORDER BY r.schema, r.name, rc.source
        """,
        (schema, name),
    ).fetchall()

    out: list[str] = []
    out.append(f"# Lineage report — `{args.routine}`")
    out.append("")
    out.append(f"- Last seen in jobs: {row['last_seen'] or '—'}")
    out.append(f"- Contains EXECUTE IMMEDIATE: {'⚠ yes — sqlglot view incomplete' if row['has_dynamic_sql'] else 'no'}")
    out.append("")

    def render_table_section(title: str, rows: list[sqlite3.Row]) -> None:
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
                f"| {_fmt_table(r['project'], r['schema'], r['name'])} | {r['source']} | {r['last_seen'] or '—'} | {r['sample_count']} |"
            )
        out.append("")

    def render_calls_section(title: str, rows: list[sqlite3.Row], use_callee_cols: bool) -> None:
        out.append(f"## {title}")
        out.append("")
        if not rows:
            out.append("_(none recorded)_")
            out.append("")
            return
        out.append("| Routine | Source | Last seen | Sample count |")
        out.append("|---------|--------|-----------|--------------|")
        for r in rows:
            if use_callee_cols:
                label = _fmt_routine(r["callee_project"], r["callee_schema"], r["callee_name"])
            else:
                label = _fmt_routine(None, r["schema"], r["name"])
            out.append(f"| {label} | {r['source']} | {r['last_seen'] or '—'} | {r['sample_count']} |")
        out.append("")

    render_table_section("Writes", writes)
    render_table_section("Reads", reads)
    render_calls_section("Calls (downstream SPs invoked by this routine)", calls_out, use_callee_cols=True)
    render_calls_section("Called by (upstream SPs that invoke this routine)", called_by, use_callee_cols=False)

    # Cross-check: edges that appear in jobs but NOT in sqlglot, or vice versa
    discrepancy = conn.execute(
        """
        SELECT t.project, t.schema, t.name, e.edge_type, GROUP_CONCAT(e.source) AS sources
        FROM edges e JOIN tables t ON t.id = e.dst_table_id
        WHERE e.src_routine_id = ?
        GROUP BY t.project, t.schema, t.name, e.edge_type
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
            out.append(f"| {_fmt_table(r['project'], r['schema'], r['name'])} | {r['edge_type']} | {r['sources']} |")
        out.append("")

    print("\n".join(out))


# ---------- Mode: --graph (Mermaid lineage graph) ----------

def _node_id_routine(schema: str, name: str) -> str:
    return "R_" + re.sub(r"[^\w]", "_", f"{schema}_{name}")


def _node_id_table(project: str | None, schema: str, name: str) -> str:
    prefix = f"{project}_" if project else ""
    return "T_" + re.sub(r"[^\w]", "_", f"{prefix}{schema}_{name}")


def run_graph(args: argparse.Namespace, conn: sqlite3.Connection) -> None:
    """Render Mermaid flowchart around a target routine or table.

    --direction upstream   : trace 'who feeds this' (table writers, routine callers)
    --direction downstream : trace 'who uses this' (routine writes / table readers / callees)
    --direction both       : both directions from the target
    --depth N              : max hops (default 3)
    """
    if "." not in args.target:
        print("ERROR: --target should be 'schema.name'", file=sys.stderr)
        sys.exit(64)
    t_schema, t_name = args.target.split(".", 1)

    is_routine = conn.execute(
        "SELECT 1 FROM routines WHERE schema=? AND name=?", (t_schema, t_name),
    ).fetchone() is not None
    is_table = conn.execute(
        "SELECT 1 FROM tables WHERE project IS NULL AND schema=? AND name=?", (t_schema, t_name),
    ).fetchone() is not None

    if not (is_routine or is_table):
        print(f"# {args.target}\n\nNot found in lineage DB.")
        return

    if is_routine and is_table:
        # rare; prefer routine (table can be referenced via reverse query)
        is_table = False

    routines_in_graph: set[tuple[str, str]] = set()
    tables_in_graph: set[tuple[str | None, str, str]] = set()
    edges_in_graph: set[tuple[str, str, str]] = set()  # (from_id, to_id, kind)

    visited_routines: set[tuple[str, str]] = set()
    visited_tables: set[tuple[str | None, str, str]] = set()

    # queue: (kind, schema, name, project_or_none, depth)
    queue: list[tuple] = []
    if is_routine:
        queue.append(("routine", t_schema, t_name, None, 0))
        routines_in_graph.add((t_schema, t_name))
    else:
        queue.append(("table", t_schema, t_name, None, 0))
        tables_in_graph.add((None, t_schema, t_name))

    direction = args.direction
    max_depth = args.depth

    while queue:
        kind, sch, nm, proj, depth = queue.pop(0)
        if depth >= max_depth:
            continue

        if kind == "routine":
            if (sch, nm) in visited_routines:
                continue
            visited_routines.add((sch, nm))

            row = conn.execute(
                "SELECT id FROM routines WHERE schema=? AND name=?", (sch, nm),
            ).fetchone()
            if not row:
                continue
            rid = row["id"]
            r_node = _node_id_routine(sch, nm)

            if direction in ("upstream", "both"):
                for r in conn.execute(
                    "SELECT t.project, t.schema, t.name FROM edges e "
                    "JOIN tables t ON t.id = e.dst_table_id "
                    "WHERE e.src_routine_id=? AND e.edge_type='read'", (rid,),
                ).fetchall():
                    tkey = (r["project"], r["schema"], r["name"])
                    edges_in_graph.add((_node_id_table(*tkey), r_node, "read"))
                    tables_in_graph.add(tkey)
                    queue.append(("table", r["schema"], r["name"], r["project"], depth + 1))

                for c in conn.execute(
                    "SELECT r.schema, r.name FROM routine_calls rc "
                    "JOIN routines r ON r.id = rc.caller_routine_id "
                    "WHERE rc.callee_schema=? AND rc.callee_name=?", (sch, nm),
                ).fetchall():
                    caller_node = _node_id_routine(c["schema"], c["name"])
                    edges_in_graph.add((caller_node, r_node, "call"))
                    routines_in_graph.add((c["schema"], c["name"]))
                    queue.append(("routine", c["schema"], c["name"], None, depth + 1))

            if direction in ("downstream", "both"):
                for w in conn.execute(
                    "SELECT t.project, t.schema, t.name FROM edges e "
                    "JOIN tables t ON t.id = e.dst_table_id "
                    "WHERE e.src_routine_id=? AND e.edge_type='write'", (rid,),
                ).fetchall():
                    tkey = (w["project"], w["schema"], w["name"])
                    edges_in_graph.add((r_node, _node_id_table(*tkey), "write"))
                    tables_in_graph.add(tkey)
                    queue.append(("table", w["schema"], w["name"], w["project"], depth + 1))

                for c in conn.execute(
                    "SELECT callee_project, callee_schema, callee_name FROM routine_calls "
                    "WHERE caller_routine_id=?", (rid,),
                ).fetchall():
                    callee_node = _node_id_routine(c["callee_schema"], c["callee_name"])
                    edges_in_graph.add((r_node, callee_node, "call"))
                    routines_in_graph.add((c["callee_schema"], c["callee_name"]))
                    queue.append(("routine", c["callee_schema"], c["callee_name"], None, depth + 1))

        elif kind == "table":
            tkey = (proj, sch, nm)
            if tkey in visited_tables:
                continue
            visited_tables.add(tkey)

            row = conn.execute(
                "SELECT id FROM tables WHERE project IS ? AND schema=? AND name=?",
                (proj, sch, nm),
            ).fetchone()
            if not row:
                continue
            tid = row["id"]
            t_node = _node_id_table(proj, sch, nm)

            if direction in ("upstream", "both"):
                for w in conn.execute(
                    "SELECT r.schema, r.name FROM edges e "
                    "JOIN routines r ON r.id = e.src_routine_id "
                    "WHERE e.dst_table_id=? AND e.edge_type='write'", (tid,),
                ).fetchall():
                    r_node = _node_id_routine(w["schema"], w["name"])
                    edges_in_graph.add((r_node, t_node, "write"))
                    routines_in_graph.add((w["schema"], w["name"]))
                    queue.append(("routine", w["schema"], w["name"], None, depth + 1))

            if direction in ("downstream", "both"):
                for r in conn.execute(
                    "SELECT r.schema, r.name FROM edges e "
                    "JOIN routines r ON r.id = e.src_routine_id "
                    "WHERE e.dst_table_id=? AND e.edge_type='read'", (tid,),
                ).fetchall():
                    r_node = _node_id_routine(r["schema"], r["name"])
                    edges_in_graph.add((t_node, r_node, "read"))
                    routines_in_graph.add((r["schema"], r["name"]))
                    queue.append(("routine", r["schema"], r["name"], None, depth + 1))

    # ----- Render Mermaid -----
    lines: list[str] = []
    lines.append(f"# Lineage graph — `{args.target}` ({direction}, depth={max_depth})")
    lines.append("")
    lines.append(f"_Nodes: {len(routines_in_graph)} routines + {len(tables_in_graph)} tables_  ")
    lines.append(f"_Edges: {len(edges_in_graph)}_")
    lines.append("")
    lines.append("```mermaid")
    lines.append("graph LR")

    for sch, nm in sorted(routines_in_graph):
        nid = _node_id_routine(sch, nm)
        lines.append(f'    {nid}["{sch}.{nm}"]')

    for proj, sch, nm in sorted(tables_in_graph, key=lambda x: (x[0] or "", x[1], x[2])):
        nid = _node_id_table(proj, sch, nm)
        label = f"{proj}.{sch}.{nm}" if proj else f"{sch}.{nm}"
        lines.append(f'    {nid}[("{label}")]')

    lines.append("")
    for fr, to, kind in sorted(edges_in_graph):
        if kind == "call":
            lines.append(f"    {fr} -.->|calls| {to}")
        elif kind == "write":
            lines.append(f"    {fr} -->|writes| {to}")
        elif kind == "read":
            lines.append(f"    {fr} -->|reads| {to}")
    lines.append("```")
    print("\n".join(lines))


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
    cross_project_count = conn.execute("SELECT COUNT(*) AS c FROM tables WHERE project IS NOT NULL").fetchone()["c"]
    edge_count = conn.execute("SELECT COUNT(*) AS c FROM edges").fetchone()["c"]
    call_count = conn.execute("SELECT COUNT(*) AS c FROM routine_calls").fetchone()["c"]
    by_source = conn.execute(
        "SELECT source, edge_type, COUNT(*) AS c FROM edges GROUP BY source, edge_type"
    ).fetchall()
    calls_by_source = conn.execute(
        "SELECT source, COUNT(*) AS c FROM routine_calls GROUP BY source"
    ).fetchall()

    print("DB summary:")
    print(f"  routines     : {routine_count}")
    print(f"  tables       : {table_count} ({cross_project_count} cross-project)")
    print(f"  edges        : {edge_count}")
    for r in by_source:
        print(f"    - {r['source']:<8} {r['edge_type']:<6} {r['c']}")
    print(f"  routine→routine calls : {call_count}")
    for r in calls_by_source:
        print(f"    - {r['source']:<8} {r['c']}")


# ---------- CLI ----------

def main() -> int:
    p = argparse.ArgumentParser(description="Extract BQ routine ↔ table lineage")
    sub = p.add_subparsers(dest="mode", required=True)

    # --from-repo (PRIMARY mode — free, static analysis)
    p_repo = sub.add_parser("from-repo", help="[PRIMARY] Static parse of git SP/function bodies (free)")
    p_repo.add_argument("--git-root", required=True, help="e.g. ./bigquery")
    p_repo.add_argument("--db", default="./lineage.db")

    # --from-jobs (OPTIONAL mode — costs $$ per run on busy projects)
    p_jobs = sub.add_parser("from-jobs", help="[OPTIONAL/COSTLY] Runtime lineage via INFORMATION_SCHEMA.JOBS")
    p_jobs.add_argument("--project", required=True)
    p_jobs.add_argument("--region", required=True, help="e.g. US, asia-east1")
    p_jobs.add_argument("--days", type=int, default=30)
    p_jobs.add_argument("--db", default="./lineage.db")
    p_jobs.add_argument("--skip-cost-warning", action="store_true",
                        help="Suppress the cost warning banner")

    # --merge (currently summary)
    p_merge = sub.add_parser("merge", help="Show DB summary")
    p_merge.add_argument("--db", default="./lineage.db")

    # --report
    p_report = sub.add_parser("report", help="Print 1-hop lineage report for a routine")
    p_report.add_argument("--routine", required=True, help="schema.name")
    p_report.add_argument("--db", default="./lineage.db")

    # --graph
    p_graph = sub.add_parser("graph", help="Render multi-hop Mermaid lineage graph around a routine or table")
    p_graph.add_argument("--target", required=True, help="schema.name (routine or table)")
    p_graph.add_argument("--direction", choices=["upstream", "downstream", "both"], default="both",
                         help="upstream=who feeds it / downstream=who uses it / both")
    p_graph.add_argument("--depth", type=int, default=3, help="Max hops (default 3)")
    p_graph.add_argument("--db", default="./lineage.db")

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
        elif args.mode == "graph":
            run_graph(args, conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
