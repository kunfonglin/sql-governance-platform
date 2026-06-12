#!/usr/bin/env python3
"""
exporter.py — 從 BQ prod 匯出 routines 到 git baseline

用法:
  python exporter.py \
      --project sandbox-prod \
      --region asia-east1 \
      --output ./bigquery \
      --config ./config/.governance.yaml \
      [--dry-run]

行為:
  1. 讀 governance.yaml 取 exclude.datasets / exclude.routines
  2. 查 INFORMATION_SCHEMA.ROUTINES（指定 region）
  3. 對每個 routine:
       a. 取 ddl 欄位
       b. 移除 prod project id（保留 dataset.name 形式）
       c. 跨專案引用 → 保留完整路徑 + 在檔頭加 -- cross-project: 註解
       d. 寫到 {output}/{schema}/routines/{name}.sql
  4. 印出匯出報告

依賴:
  - bq CLI 已安裝且已認證（gcloud auth）
  - Python 3.8+
  - PyYAML（pip install pyyaml）

不直接用 google-cloud-bigquery 是為了減少依賴；
透過 bq CLI 子程序執行，輸出格式 = JSON。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

# Windows console UTF-8 (avoid cp950 crash on ✓ ⚠)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

try:
    import yaml  # type: ignore
except ImportError:
    print("ERROR: PyYAML not installed. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


_BQ_PATH_CACHE: str | None = None


def _bq_path() -> str:
    """Locate bq CLI: PATH first, then common Windows install locations."""
    global _BQ_PATH_CACHE
    if _BQ_PATH_CACHE:
        return _BQ_PATH_CACHE
    import shutil
    for cand in ("bq", "bq.cmd"):
        found = shutil.which(cand)
        if found:
            _BQ_PATH_CACHE = found
            return found
    candidates = [
        Path.home() / "AppData/Local/Google/Cloud SDK/google-cloud-sdk/bin/bq.cmd",
        Path("C:/Program Files (x86)/Google/Cloud SDK/google-cloud-sdk/bin/bq.cmd"),
        Path("C:/Program Files/Google/Cloud SDK/google-cloud-sdk/bin/bq.cmd"),
    ]
    for p in candidates:
        if p.exists():
            _BQ_PATH_CACHE = str(p)
            return _BQ_PATH_CACHE
    raise FileNotFoundError("bq CLI not found. Install Google Cloud SDK or add bin/ to PATH.")


def _run_bq_json(project_id: str, sql: str) -> list[dict]:
    """Run a bq query and parse JSON output. Handles Windows .cmd quirks (multi-line SQL, errors on stdout)."""
    sql_single_line = " ".join(sql.split())
    result = subprocess.run(
        [_bq_path(), "--quiet", "query",
         f"--project_id={project_id}",
         "--use_legacy_sql=false",
         "--format=json",
         "--max_rows=10000",
         sql_single_line],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: bq query failed (rc={result.returncode})", file=sys.stderr)
        if result.stderr:
            print(f"  stderr: {result.stderr.strip()}", file=sys.stderr)
        if result.stdout:
            print(f"  stdout: {result.stdout.strip()[:1000]}", file=sys.stderr)
        sys.exit(2)
    stdout = result.stdout
    if not stdout.strip():
        return []
    # bq CLI in Actions can prepend status text; find the JSON array start
    start = stdout.find("[")
    if start < 0:
        return []
    return json.loads(stdout[start:])


@dataclass
class Routine:
    schema: str
    name: str
    routine_type: str  # PROCEDURE / FUNCTION / TABLE_FUNCTION
    ddl: str

    @property
    def fullname(self) -> str:
        return f"{self.schema}.{self.name}"


@dataclass
class TableObj:
    """Represents a BigQuery table OR view (table_type distinguishes them)."""
    schema: str
    name: str
    table_type: str  # BASE TABLE / VIEW / MATERIALIZED VIEW / EXTERNAL
    ddl: str

    @property
    def fullname(self) -> str:
        return f"{self.schema}.{self.name}"

    @property
    def is_view(self) -> bool:
        return "VIEW" in self.table_type.upper()


def load_config(path: Path) -> dict:
    if not path.exists():
        print(f"ERROR: config not found: {path}", file=sys.stderr)
        sys.exit(1)
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def get_excludes(cfg: dict) -> tuple[set[str], list[str]]:
    """
    Returns:
      (exclude_dataset_names, exclude_routine_patterns)
      exclude_routine_patterns format: list of glob patterns matching "{schema}.{name}"
    """
    exclude_block = cfg.get("exclude", {})
    datasets = set(exclude_block.get("datasets", []))
    routines = []
    for entry in exclude_block.get("routines", []):
        if isinstance(entry, dict) and "pattern" in entry:
            routines.append(entry["pattern"])
        elif isinstance(entry, str):
            routines.append(entry)
    return datasets, routines


def query_routines(project_id: str, region: str, exclude_datasets: set[str]) -> list[Routine]:
    """Query INFORMATION_SCHEMA.ROUTINES → list of Routine."""
    sql = f"""
    SELECT
      specific_schema AS schema,
      routine_name AS name,
      routine_type,
      ddl
    FROM `region-{region}`.INFORMATION_SCHEMA.ROUTINES
    WHERE specific_catalog = '{project_id}'
    """
    print(f"Querying INFORMATION_SCHEMA.ROUTINES on {project_id} (region={region})...")
    rows = _run_bq_json(project_id, sql)

    routines: list[Routine] = []
    for row in rows:
        schema = row["schema"]
        if schema in exclude_datasets:
            continue
        routines.append(
            Routine(
                schema=schema, name=row["name"],
                routine_type=row["routine_type"],
                ddl=row["ddl"] or "",
            )
        )
    return routines


def query_tables(project_id: str, region: str, exclude_datasets: set[str]) -> list[TableObj]:
    """Query INFORMATION_SCHEMA.TABLES → list of TableObj (covers BASE TABLE + VIEW)."""
    sql = f"""
    SELECT
      table_schema AS schema,
      table_name AS name,
      table_type,
      ddl
    FROM `region-{region}`.INFORMATION_SCHEMA.TABLES
    WHERE table_catalog = '{project_id}'
      AND table_type IN ('BASE TABLE', 'VIEW', 'MATERIALIZED VIEW')
    """
    print(f"Querying INFORMATION_SCHEMA.TABLES on {project_id} (region={region})...")
    rows = _run_bq_json(project_id, sql)

    tables: list[TableObj] = []
    for row in rows:
        schema = row["schema"]
        if schema in exclude_datasets:
            continue
        tables.append(
            TableObj(
                schema=schema, name=row["name"],
                table_type=row["table_type"],
                ddl=row["ddl"] or "",
            )
        )
    return tables


def filter_excluded(routines: list[Routine], patterns: list[str]) -> tuple[list[Routine], list[Routine]]:
    """Returns (kept, excluded)"""
    kept, excluded = [], []
    for r in routines:
        if any(fnmatch(r.fullname, p) or fnmatch(r.fullname, p.replace("*.", "")) for p in patterns):
            excluded.append(r)
        else:
            kept.append(r)
    return kept, excluded


def normalize_ddl(ddl: str, source_project: str) -> tuple[str, set[str]]:
    """
    Strip project id from same-project references; preserve cross-project refs.

    Returns:
      (normalized_ddl, set_of_cross_project_refs)
    """
    cross_refs: set[str] = set()

    # Match `project.dataset.object` quoted in backticks (BQ standard)
    # Pattern handles both `proj.ds.obj` and `proj`.`ds`.`obj` forms (rare).
    backtick_full_ref = re.compile(r"`([\w-]+)\.([\w-]+)\.([\w-]+)`")

    def replace_backtick(match: re.Match) -> str:
        proj, ds, obj = match.group(1), match.group(2), match.group(3)
        if proj == source_project:
            return f"`{ds}.{obj}`"
        cross_refs.add(f"{proj}.{ds}.{obj}")
        return match.group(0)  # keep cross-project ref intact

    normalized = backtick_full_ref.sub(replace_backtick, ddl)

    # Also handle unquoted project.dataset.object (less common but exists)
    unquoted_full_ref = re.compile(r"(?<![\w`])([\w-]+)\.([\w-]+)\.([\w-]+)(?![\w`])")

    def replace_unquoted(match: re.Match) -> str:
        proj, ds, obj = match.group(1), match.group(2), match.group(3)
        if proj == source_project:
            return f"{ds}.{obj}"
        cross_refs.add(f"{proj}.{ds}.{obj}")
        return match.group(0)

    normalized = unquoted_full_ref.sub(replace_unquoted, normalized)

    return normalized, cross_refs


def render_file_content(routine: Routine, normalized_ddl: str, cross_refs: set[str]) -> str:
    """Compose the final .sql file content with optional cross-project header."""
    lines = [
        f"-- bigquery/{routine.schema}/routines/{routine.name}.sql",
        f"-- routine_type: {routine.routine_type}",
    ]
    for ref in sorted(cross_refs):
        lines.append(f"-- cross-project: {ref}")
    lines.append("")
    lines.append(normalized_ddl.rstrip() + ";")
    lines.append("")
    return "\n".join(lines)


def write_routine(routine: Routine, content: str, output_root: Path, dry_run: bool) -> Path:
    target_dir = output_root / routine.schema / "routines"
    target = target_dir / f"{routine.name}.sql"
    if dry_run:
        return target  # truly dry: no mkdir, no file write
    target_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def render_table_file(table: TableObj, normalized_ddl: str, cross_refs: set[str]) -> str:
    subfolder = "views" if table.is_view else "tables"
    lines = [
        f"-- bigquery/{table.schema}/{subfolder}/{table.name}.sql",
        f"-- table_type: {table.table_type}",
        "-- NOTE: Phase 1 不部署 tables/views, 此檔為文件參考用",
    ]
    for ref in sorted(cross_refs):
        lines.append(f"-- cross-project: {ref}")
    lines.append("")
    lines.append(normalized_ddl.rstrip() + ";")
    lines.append("")
    return "\n".join(lines)


def write_table(table: TableObj, content: str, output_root: Path, dry_run: bool) -> Path:
    subfolder = "views" if table.is_view else "tables"
    target_dir = output_root / table.schema / subfolder
    target = target_dir / f"{table.name}.sql"
    if dry_run:
        return target  # truly dry: no mkdir, no file write
    target_dir.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="Export BQ routines to git baseline")
    parser.add_argument("--project", required=True, help="Source GCP project ID (prod)")
    parser.add_argument("--region", required=True, help="BQ region, e.g. asia-east1")
    parser.add_argument("--output", required=True, help="Output root, e.g. ./bigquery")
    parser.add_argument("--config", required=True, help="Path to governance.yaml")
    parser.add_argument("--dry-run", action="store_true", help="Don't write files, just print plan")
    parser.add_argument("--include-tables", action="store_true",
                        help="Also export table & view DDL into bigquery/{schema}/tables/ and views/ (Phase 1 reference only, not deployed)")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    exclude_datasets, exclude_routines = get_excludes(cfg)

    print(f"Source project : {args.project}")
    print(f"Region         : {args.region}")
    print(f"Output root    : {args.output}")
    print(f"Excluded datasets : {sorted(exclude_datasets) or '(none)'}")
    print(f"Excluded patterns : {exclude_routines or '(none)'}")
    print()

    all_routines = query_routines(args.project, args.region, exclude_datasets)
    kept, excluded = filter_excluded(all_routines, exclude_routines)

    print(f"Found {len(all_routines)} routines (after dataset exclude)")
    print(f"  → {len(kept)} kept")
    print(f"  → {len(excluded)} excluded by routine pattern")
    print()

    output_root = Path(args.output)
    written: list[Path] = []
    review_needed: list[tuple[Path, set[str]]] = []
    for r in kept:
        normalized, cross_refs = normalize_ddl(r.ddl, args.project)
        content = render_file_content(r, normalized, cross_refs)
        path = write_routine(r, content, output_root, args.dry_run)
        written.append(path)
        if cross_refs:
            review_needed.append((path, cross_refs))

    action = "Would write" if args.dry_run else "Wrote"
    print(f"{action} {len(written)} routine files under {args.output}/")
    if review_needed:
        print()
        print(f"⚠  {len(review_needed)} routines have cross-project references — please review:")
        for p, refs in review_needed:
            print(f"   {p}")
            for ref in sorted(refs):
                print(f"     ↳ {ref}")
    if excluded:
        print()
        print(f"Skipped routines (matched exclude pattern):")
        for r in excluded:
            print(f"   {r.fullname}")

    # ----- Tables / Views (optional) -----
    if args.include_tables:
        print()
        all_tables = query_tables(args.project, args.region, exclude_datasets)
        print(f"Found {len(all_tables)} tables/views (after dataset exclude)")
        table_written: list[Path] = []
        for t in all_tables:
            if not t.ddl:
                continue  # external tables sometimes lack ddl
            normalized, cross_refs = normalize_ddl(t.ddl, args.project)
            content = render_table_file(t, normalized, cross_refs)
            path = write_table(t, content, output_root, args.dry_run)
            table_written.append(path)
        base_count = sum(1 for p in table_written if "/tables/" in str(p).replace("\\", "/"))
        view_count = len(table_written) - base_count
        print(f"{action} {base_count} table files + {view_count} view files")
        print(f"  (table/view DDL 預設不部署，請看 .gitignore 是否要納入 git)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
