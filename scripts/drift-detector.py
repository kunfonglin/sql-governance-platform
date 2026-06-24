#!/usr/bin/env python3
"""
drift-detector.py — 比對 prod live routines vs git，產出 drift 報告

用法:
  python drift-detector.py \
      --project sandbox-prod \
      --region asia-east1 \
      --git-root ./bigquery \
      --config ./config/.governance.yaml \
      --output ./audit \
      --known-drifts ./audit/known-drifts.yaml \
      [--manifest-dir ./audit/deploys] \
      [--audit-lookback-hours 24]

行為:
  1. dump prod INFORMATION_SCHEMA.ROUTINES（套 exclude）
  2. 讀 git 的 routines/*.sql
  3. 規範化兩邊 DDL（移 project id、空白 / 換行標準化）
  4. 對每個 routine 比對：
       - prod 有 git 沒有 → orphan（未授權新增）
       - git 有 prod 沒有 → not_deployed
       - 兩邊都有但不一致 → diff
  5. 對照 known-drifts.yaml 過濾
  6. 對未知漂移：查 audit log 找最近改動的 user_email
  7. 比對最新 manifest 看是不是 CI 跑的
  8. 寫 audit/drift-YYYY-MM-DD.md
  9. 有未知漂移 → exit code 1（讓 GitHub Actions step fail / 觸發 TG）

依賴:
  - bq CLI 已認證
  - PyYAML
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

try:
    import yaml  # type: ignore
except ImportError:
    print("ERROR: PyYAML not installed. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


# ---------- Data classes ----------

@dataclass
class LiveRoutine:
    schema: str
    name: str
    ddl: str
    last_altered: str | None = None

    @property
    def fullname(self) -> str:
        return f"{self.schema}.{self.name}"


@dataclass
class GitRoutine:
    schema: str
    name: str
    ddl: str  # raw file content
    path: Path

    @property
    def fullname(self) -> str:
        return f"{self.schema}.{self.name}"


@dataclass
class Drift:
    kind: str  # routines/views: "orphan"|"not_deployed"|"content"; tables: "table_orphan"|"table_not_deployed"|"columns"
    fullname: str
    object_type: str = "routine"  # "routine" | "view" | "table"
    detail: str = ""
    last_modifier: str | None = None
    last_modified_at: str | None = None
    in_recent_manifest: bool = False
    diff_preview: str = ""


@dataclass
class Report:
    generated_at: str
    project: str
    drifts: list[Drift] = field(default_factory=list)
    known_drifts_filtered: list[str] = field(default_factory=list)


# ---------- Loaders ----------

def load_config(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_known_drifts(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or []
    return data if isinstance(data, list) else []


def get_excludes(cfg: dict) -> tuple[set[str], list[str]]:
    exclude_block = cfg.get("exclude", {})
    datasets = set(exclude_block.get("datasets", []))
    routines = []
    for entry in exclude_block.get("routines", []):
        if isinstance(entry, dict) and "pattern" in entry:
            routines.append(entry["pattern"])
        elif isinstance(entry, str):
            routines.append(entry)
    return datasets, routines


# ---------- BQ queries ----------

def _parse_bq_json(stdout: str) -> list:
    # bq CLI 在 non-TTY 環境（Actions）可能在 JSON 前面印狀態列，
    # 用 find('[') 跳到真正的 JSON 起點，避免 JSONDecodeError。
    if not stdout.strip():
        return []
    start = stdout.find("[")
    if start < 0:
        print(f"WARN: bq stdout has no JSON array, got:\n{stdout[:500]}", file=sys.stderr)
        return []
    try:
        return json.loads(stdout[start:])
    except json.JSONDecodeError as e:
        print(f"ERROR: failed to parse bq JSON: {e}\nstdout (first 500): {stdout[:500]}", file=sys.stderr)
        sys.exit(2)


def fetch_live_routines(project: str, region: str, exclude_datasets: set[str]) -> list[LiveRoutine]:
    sql = f"""
    SELECT
      specific_schema AS schema,
      routine_name AS name,
      ddl,
      CAST(last_altered AS STRING) AS last_altered
    FROM `region-{region}`.INFORMATION_SCHEMA.ROUTINES
    WHERE specific_catalog = '{project}'
    """
    result = subprocess.run(
        ["bq", "--quiet", "query", f"--project_id={project}", "--use_legacy_sql=false",
         "--format=json", "--max_rows=10000", sql],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: bq query failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(2)
    rows = _parse_bq_json(result.stdout)
    out: list[LiveRoutine] = []
    for r in rows:
        if r["schema"] in exclude_datasets:
            continue
        out.append(LiveRoutine(
            schema=r["schema"], name=r["name"],
            ddl=r["ddl"] or "", last_altered=r.get("last_altered"),
        ))
    return out


def fetch_recent_modifiers(project: str, region: str, hours: int) -> dict[str, dict]:
    """
    Return mapping: 'schema.name' -> { 'user_email': ..., 'creation_time': ... }
    for the latest job that touched each routine in the lookback window.
    """
    sql = f"""
    WITH jobs AS (
      SELECT
        user_email,
        creation_time,
        statement_type,
        REGEXP_EXTRACT(query, r'(?i)(?:CREATE\\s+(?:OR\\s+REPLACE\\s+)?(?:MATERIALIZED\\s+VIEW|VIEW|TABLE\\s+FUNCTION|TABLE|PROCEDURE|FUNCTION)|ALTER\\s+TABLE|DROP\\s+(?:MATERIALIZED\\s+VIEW|VIEW|TABLE|PROCEDURE|FUNCTION))\\s+(?:IF\\s+(?:NOT\\s+)?EXISTS\\s+)?`?(?:[\\w-]+\\.)?([\\w-]+\\.[\\w-]+)`?') AS routine_fullname
      FROM `region-{region}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
      WHERE creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {hours} HOUR)
        AND statement_type IN (
          'CREATE_PROCEDURE', 'CREATE_FUNCTION', 'CREATE_TABLE_FUNCTION',
          'DROP_PROCEDURE', 'DROP_FUNCTION',
          'CREATE_VIEW', 'CREATE_MATERIALIZED_VIEW', 'DROP_VIEW',
          'CREATE_TABLE', 'CREATE_TABLE_AS_SELECT', 'ALTER_TABLE', 'DROP_TABLE',
          'SCRIPT')
        AND state = 'DONE'
    )
    SELECT routine_fullname, user_email, CAST(creation_time AS STRING) AS creation_time
    FROM jobs
    WHERE routine_fullname IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (PARTITION BY routine_fullname ORDER BY creation_time DESC) = 1
    """
    result = subprocess.run(
        ["bq", "--quiet", "query", f"--project_id={project}", "--use_legacy_sql=false",
         "--format=json", "--max_rows=10000", sql],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"WARN: audit-log query failed:\n{result.stderr}", file=sys.stderr)
        return {}
    rows = _parse_bq_json(result.stdout)
    return {r["routine_fullname"]: {"user_email": r["user_email"], "creation_time": r["creation_time"]} for r in rows}


# ---------- Git side ----------

def load_git_routines(git_root: Path) -> list[GitRoutine]:
    """
    Walk git_root looking for {schema}/routines/{name}.sql
    """
    out: list[GitRoutine] = []
    if not git_root.exists():
        return out
    for sql_path in git_root.glob("*/routines/*.sql"):
        schema = sql_path.parent.parent.name
        name = sql_path.stem
        out.append(GitRoutine(
            schema=schema, name=name,
            ddl=sql_path.read_text(encoding="utf-8"),
            path=sql_path,
        ))
    return out


# ---------- Normalization ----------

_HEADER_COMMENT_RE = re.compile(r"^\s*--[^\n]*\n", re.MULTILINE)
# `proj.ds.obj`  → 三段全部一起 backticked
_BACKTICK_FULL_REF_RE = re.compile(r"`([\w-]+)\.([\w-]+)\.([\w-]+)`")
# `proj`.ds.obj  → BQ INFORMATION_SCHEMA 用的格式（project 單獨 backticked）
_SPLIT_BACKTICK_REF_RE = re.compile(r"`([\w-]+)`\.([\w-]+)\.([\w-]+)")
# proj.ds.obj    → 完全沒 backtick
_UNQUOTED_FULL_REF_RE = re.compile(r"(?<![\w`])([\w-]+)\.([\w-]+)\.([\w-]+)(?![\w`])")


def normalize_for_compare(ddl: str, source_project: str | None = None) -> str:
    """
    Normalize a DDL string for diff comparison:
      1. Strip ALL -- line comments (header AND in-body)
      2. Normalize 'CREATE OR REPLACE' → 'CREATE' (BQ INFORMATION_SCHEMA 會把 OR REPLACE 拿掉)
      3. Strip same-project project_id from refs
      4. Collapse whitespace
      5. Lowercase for case-insensitive compare
    """
    text = ddl

    # 1. Strip all -- line comments (whole-line OR trailing comments anywhere)
    text = re.sub(r"--[^\n]*", "", text)

    # 2. Normalize CREATE OR REPLACE → CREATE
    text = re.sub(r"\bCREATE\s+OR\s+REPLACE\b", "CREATE", text, flags=re.IGNORECASE)

    # 3. 統一移除所有 backtick，讓 `proj`.ds.obj / `proj.ds.obj` / `proj`.`ds`.`obj` 等寫法一致
    #    （否則跨專案引用會因 git 與 BQ 的 backtick 風格不同而誤報 drift — 見 #16）
    text = text.replace("`", "")

    # 4. 去掉「同專案」的 project id（backtick 已移除，只剩 unquoted 形式）；跨專案引用保留不動
    if source_project:
        def _strip_unquoted(m: re.Match) -> str:
            return f"{m.group(2)}.{m.group(3)}" if m.group(1) == source_project else m.group(0)

        text = _UNQUOTED_FULL_REF_RE.sub(_strip_unquoted, text)

    # 4. Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    # Remove trailing semicolons for stable comparison
    text = text.rstrip(";").strip()
    # 5. Lowercase
    return text.lower()


def normalize_for_display(ddl: str, source_project: str | None = None) -> list[str]:
    """
    給「人看」用的輕度正規化（保留大小寫與換行）：
      - 移除 -- 註解、CREATE OR REPLACE → CREATE、去掉同專案 project id
      - 每行壓掉多餘空白、去空行
    回傳 list[str] 供 difflib.unified_diff 使用。
    """
    text = ddl
    text = re.sub(r"--[^\n]*", "", text)
    text = re.sub(r"\bCREATE\s+OR\s+REPLACE\b", "CREATE", text, flags=re.IGNORECASE)
    if source_project:
        def _q(m: re.Match) -> str:
            return f"`{m.group(2)}.{m.group(3)}`" if m.group(1) == source_project else m.group(0)

        def _u(m: re.Match) -> str:
            return f"{m.group(2)}.{m.group(3)}" if m.group(1) == source_project else m.group(0)

        text = _BACKTICK_FULL_REF_RE.sub(_q, text)
        text = _SPLIT_BACKTICK_REF_RE.sub(_q, text)
        text = _UNQUOTED_FULL_REF_RE.sub(_u, text)

    out: list[str] = []
    for ln in text.splitlines():
        ln = re.sub(r"[ \t]+", " ", ln).rstrip()
        if ln.strip():
            out.append(ln)
    return out


# ---------- Diff logic ----------

def is_known(fullname: str, known_drifts: list[dict]) -> tuple[bool, str | None]:
    today = dt.date.today()
    for kd in known_drifts:
        pattern = kd.get("pattern", "")
        if fnmatch(fullname, pattern):
            expires = kd.get("expires_at")
            if expires:
                try:
                    expires_date = dt.date.fromisoformat(str(expires))
                    if expires_date < today:
                        continue  # expired, no longer known
                except ValueError:
                    pass
            return True, kd.get("reason", "")
    return False, None


def compute_drift(
    live: list[LiveRoutine],
    git: list[GitRoutine],
    project: str,
    object_type: str = "routine",
) -> list[Drift]:
    """
    DDL-based drift（routines 與 views 共用）：兩者都是「無資料、可全量重佈」物件，
    比的就是規範化後的 DDL 文字。table 的 drift 走 compute_table_drift（比欄位、不比 DDL）。
    """
    by_name_live = {r.fullname: r for r in live}
    by_name_git = {r.fullname: r for r in git}

    drifts: list[Drift] = []

    # orphan: in live but not git
    for fullname in sorted(set(by_name_live) - set(by_name_git)):
        drifts.append(Drift(
            kind="orphan",
            fullname=fullname,
            object_type=object_type,
            detail=f"prod 有此 {object_type} 但 git 沒有對應檔案",
        ))

    # not deployed: in git but not live
    for fullname in sorted(set(by_name_git) - set(by_name_live)):
        drifts.append(Drift(
            kind="not_deployed",
            fullname=fullname,
            object_type=object_type,
            detail=f"git 有此 {object_type} 但 prod 沒有",
        ))

    # content drift
    for fullname in sorted(set(by_name_live) & set(by_name_git)):
        live_norm = normalize_for_compare(by_name_live[fullname].ddl, project)
        git_norm = normalize_for_compare(by_name_git[fullname].ddl, project)
        if live_norm != git_norm:
            # 用 unified diff 呈現「改了哪幾行」（保留大小寫與換行，較好讀）
            git_lines = normalize_for_display(by_name_git[fullname].ddl, project)
            live_lines = normalize_for_display(by_name_live[fullname].ddl, project)
            diff_lines = list(difflib.unified_diff(
                git_lines, live_lines,
                fromfile="GIT", tofile="LIVE(prod)", lineterm="",
            ))
            if len(diff_lines) > 80:
                diff_lines = diff_lines[:80] + [f"... ({len(diff_lines) - 80} more diff lines truncated)"]
            preview = "\n".join(diff_lines)
            drifts.append(Drift(
                kind="content",
                fullname=fullname,
                object_type=object_type,
                detail="內容不一致",
                diff_preview=preview,
            ))

    return drifts


# ---------- Views (DDL-based, 與 routines 同一套) ----------

def fetch_live_views(project: str, region: str, exclude_datasets: set[str]) -> list[LiveRoutine]:
    """
    抓 live views。用 INFORMATION_SCHEMA.TABLES 的 ddl 欄（內含完整 CREATE VIEW 語句），
    這樣能跟 git 的 views/*.sql（CREATE OR REPLACE VIEW ... AS SELECT ...）走同一套 normalize/diff。
    """
    sql = f"""
    SELECT table_schema AS schema, table_name AS name, ddl,
           CAST(NULL AS STRING) AS last_altered
    FROM `region-{region}`.INFORMATION_SCHEMA.TABLES
    WHERE table_catalog = '{project}' AND table_type = 'VIEW'
    """
    result = subprocess.run(
        ["bq", "--quiet", "query", f"--project_id={project}", "--use_legacy_sql=false",
         "--format=json", "--max_rows=10000", sql],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: bq view query failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(2)
    rows = _parse_bq_json(result.stdout)
    out: list[LiveRoutine] = []
    for r in rows:
        if r["schema"] in exclude_datasets:
            continue
        out.append(LiveRoutine(schema=r["schema"], name=r["name"], ddl=r["ddl"] or ""))
    return out


def load_git_views(git_root: Path) -> list[GitRoutine]:
    """Walk git_root looking for {schema}/views/{name}.sql"""
    out: list[GitRoutine] = []
    if not git_root.exists():
        return out
    for sql_path in git_root.glob("*/views/*.sql"):
        out.append(GitRoutine(
            schema=sql_path.parent.parent.name,
            name=sql_path.stem,
            ddl=sql_path.read_text(encoding="utf-8"),
            path=sql_path,
        ))
    return out


# ---------- Tables (column-based, 不比整段 DDL) ----------

def _is_nullable(type_norm: str, nullable_flag: bool) -> bool:
    # ARRAY/REPEATED 欄位 BQ 一律報 NOT NULL，但 git DDL 不會寫 NOT NULL →
    # 強制兩邊都當「非 nullable」，避免 ARRAY 欄位每次誤報。
    if type_norm.startswith("ARRAY"):
        return False
    return nullable_flag


def fetch_live_table_columns(
    project: str, region: str, exclude_datasets: set[str],
) -> dict[str, list[tuple[str, str, bool]]]:
    """
    回傳 {schema.table: [(column_name, normalized_type, nullable), ...]}（依 ordinal_position 排序）。
    只看 BASE TABLE（排除 view / external / snapshot）。
    """
    sql = f"""
    SELECT c.table_schema AS schema, c.table_name AS name,
           c.column_name AS column_name, c.data_type AS data_type,
           c.is_nullable AS is_nullable,
           c.ordinal_position AS ordinal_position
    FROM `region-{region}`.INFORMATION_SCHEMA.COLUMNS AS c
    JOIN `region-{region}`.INFORMATION_SCHEMA.TABLES AS t
      ON c.table_schema = t.table_schema AND c.table_name = t.table_name
    WHERE c.table_catalog = '{project}' AND t.table_type = 'BASE TABLE'
    ORDER BY c.table_schema, c.table_name, c.ordinal_position
    """
    result = subprocess.run(
        ["bq", "--quiet", "query", f"--project_id={project}", "--use_legacy_sql=false",
         "--format=json", "--max_rows=100000", sql],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: bq column query failed:\n{result.stderr}", file=sys.stderr)
        sys.exit(2)
    rows = _parse_bq_json(result.stdout)
    out: dict[str, list[tuple[str, str, bool]]] = {}
    for r in rows:
        if r["schema"] in exclude_datasets:
            continue
        # 跳過 ingestion-time 分區的 pseudo column（git DDL 不會有，否則誤報）
        if r["column_name"].upper().startswith("_PARTITION"):
            continue
        fullname = f"{r['schema']}.{r['name']}"
        ctype = _normalize_bq_type(r["data_type"])
        nullable = _is_nullable(ctype, str(r.get("is_nullable", "YES")).upper() == "YES")
        out.setdefault(fullname, []).append((r["column_name"], ctype, nullable))
    return out


# BQ 型別別名 → 標準名，避免 git DDL 與 INFORMATION_SCHEMA 寫法不同造成誤報
_TYPE_ALIASES = {
    "INT": "INT64", "INTEGER": "INT64", "SMALLINT": "INT64", "BIGINT": "INT64",
    "TINYINT": "INT64", "BYTEINT": "INT64",
    "FLOAT": "FLOAT64", "DOUBLE": "FLOAT64",
    "DECIMAL": "NUMERIC", "BIGDECIMAL": "BIGNUMERIC",
    "BOOL": "BOOLEAN",
}


def _normalize_bq_type(t: str) -> str:
    if not t:
        return ""
    s = re.sub(r"\s+", "", t).upper()
    # 只對「頂層純量型別」做別名（ARRAY<>/STRUCT<> 內部不動，保持原樣比對）
    return _TYPE_ALIASES.get(s, s)


def load_git_table_columns(git_root: Path) -> dict[str, list[tuple[str, str, bool]]]:
    """
    用 sqlglot 解析 git 的 {schema}/tables/{name}.sql，抽出 (column_name, normalized_type, nullable)。
    nullable = 沒寫 NOT NULL（ARRAY 一律當非 nullable，對齊 BQ）。
    CTAS（CREATE TABLE AS SELECT，無明確欄位定義）無法靜態取得欄位 → 略過並警告。
    """
    out: dict[str, list[tuple[str, str, bool]]] = {}
    if not git_root.exists():
        return out

    try:
        import sqlglot
        from sqlglot import exp
    except ImportError:
        print("ERROR: --include-tables 需要 sqlglot。請 pip install sqlglot", file=sys.stderr)
        sys.exit(1)

    for sql_path in git_root.glob("*/tables/*.sql"):
        schema = sql_path.parent.parent.name
        name = sql_path.stem
        fullname = f"{schema}.{name}"
        text = sql_path.read_text(encoding="utf-8")
        try:
            parsed = sqlglot.parse_one(text, read="bigquery")
        except Exception as e:  # noqa: BLE001 — 解析失敗不該炸掉整個 drift run
            print(f"WARN: 無法解析 {sql_path}（略過）: {e}", file=sys.stderr)
            continue
        if parsed is None:
            continue
        cols: list[tuple[str, str, bool]] = []
        for col in parsed.find_all(exp.ColumnDef):
            kind = col.args.get("kind")
            ctype = _normalize_bq_type(kind.sql(dialect="bigquery") if kind is not None else "")
            constraints = col.args.get("constraints", []) or []
            not_null = any(isinstance(c.args.get("kind"), exp.NotNullColumnConstraint) for c in constraints)
            cols.append((col.name, ctype, _is_nullable(ctype, not not_null)))
        if not cols:
            print(f"WARN: {sql_path} 找不到欄位定義（可能是 CTAS），column-drift 略過此表", file=sys.stderr)
            continue
        out[fullname] = cols
    return out


def compute_table_drift(
    live: dict[str, list[tuple[str, str, bool]]],
    git: dict[str, list[tuple[str, str, bool]]],
) -> list[Drift]:
    """
    比欄位（名稱 + 型別 + nullable），不比整段 DDL（partition/cluster/OPTIONS 格式差異會誤報）。
    回報：表級 orphan / not_deployed；欄位級 新增 / 刪除 / 型別變更 / nullable 變更。
    """
    drifts: list[Drift] = []

    for fullname in sorted(set(live) - set(git)):
        drifts.append(Drift(
            kind="table_orphan", fullname=fullname, object_type="table",
            detail="prod 有此 table 但 git 沒有對應 tables/*.sql",
        ))
    for fullname in sorted(set(git) - set(live)):
        drifts.append(Drift(
            kind="table_not_deployed", fullname=fullname, object_type="table",
            detail="git 有此 table 但 prod 沒有",
        ))

    def _null_label(nullable: bool) -> str:
        return "NULLABLE" if nullable else "NOT NULL"

    for fullname in sorted(set(live) & set(git)):
        live_cols = {c[0]: (c[1], c[2]) for c in live[fullname]}   # name -> (type, nullable)
        git_cols = {c[0]: (c[1], c[2]) for c in git[fullname]}
        changes: list[str] = []
        # 有人在 prod 多加的欄位（git 沒有）
        for c in sorted(set(live_cols) - set(git_cols)):
            changes.append(f"+ live 多了欄位 `{c}` {live_cols[c][0]}（git 無）")
        # git 有但 prod 沒有 → 尚未部署 / 被人刪掉
        for c in sorted(set(git_cols) - set(live_cols)):
            changes.append(f"- live 缺欄位 `{c}` {git_cols[c][0]}（git 有）")
        # 型別 / nullable 變更
        for c in sorted(set(live_cols) & set(git_cols)):
            gtype, gnull = git_cols[c]
            ltype, lnull = live_cols[c]
            if gtype != ltype:
                changes.append(f"~ 欄位 `{c}` 型別 git={gtype} → live={ltype}")
            if gnull != lnull:
                changes.append(f"~ 欄位 `{c}` 可空性 git={_null_label(gnull)} → live={_null_label(lnull)}")
        if changes:
            drifts.append(Drift(
                kind="columns", fullname=fullname, object_type="table",
                detail="欄位與 git 快照不一致",
                diff_preview="\n".join(changes),
            ))
    return drifts


# ---------- Manifest correlation ----------

def load_recent_manifests(manifest_dir: Path | None, lookback_days: int = 7) -> list[dict]:
    if not manifest_dir or not manifest_dir.exists():
        return []
    cutoff = dt.datetime.utcnow() - dt.timedelta(days=lookback_days)
    out: list[dict] = []
    for p in manifest_dir.rglob("*manifest.json"):
        try:
            mtime = dt.datetime.utcfromtimestamp(p.stat().st_mtime)
            if mtime < cutoff:
                continue
            with p.open(encoding="utf-8") as f:
                out.append(json.load(f))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def routine_in_manifest(fullname: str, manifests: list[dict]) -> bool:
    schema, name = fullname.split(".", 1)
    expected_path_suffix = f"/{schema}/routines/{name}.sql"
    for m in manifests:
        deployed = m.get("deployed", {})
        for path in deployed.get("routines", []):
            if path.endswith(expected_path_suffix):
                return True
    return False


# ---------- Report rendering ----------

def render_report(report: Report) -> str:
    lines = [
        f"# Drift Report — {report.generated_at[:10]}",
        "",
        f"**Project:** `{report.project}`",
        f"**Generated:** {report.generated_at}",
        "",
    ]
    if not report.drifts:
        lines += ["✅ **No unknown drift detected.**", ""]
    else:
        lines += [
            f"⚠ **{len(report.drifts)} drift(s) detected.**",
            "",
            "| Object | Kind | Name | Last Modifier | Last Modified | In Manifest? | Detail |",
            "|--------|------|------|---------------|---------------|--------------|--------|",
        ]
        for d in report.drifts:
            lines.append(
                f"| {d.object_type} | {d.kind} | `{d.fullname}` | {d.last_modifier or '-'} | "
                f"{d.last_modified_at or '-'} | {'yes' if d.in_recent_manifest else 'no'} | {d.detail} |"
            )
        lines.append("")

        # diff previews（routine/view 的內容 diff + table 的欄位變更）
        preview_drifts = [d for d in report.drifts if d.diff_preview]
        if preview_drifts:
            lines += ["## Diff previews", ""]
            for d in preview_drifts:
                lines += [f"### [{d.object_type}] `{d.fullname}`", "```diff", d.diff_preview, "```", ""]

    if report.known_drifts_filtered:
        lines += [
            "## Filtered (known drifts)",
            "",
        ]
        for fn in report.known_drifts_filtered:
            lines.append(f"- `{fn}`")
        lines.append("")

    return "\n".join(lines)


# ---------- Main ----------

def main() -> int:
    parser = argparse.ArgumentParser(description="Detect drift between prod live routines and git")
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--git-root", required=True, help="e.g. ./bigquery")
    parser.add_argument("--config", required=True, help="path to governance.yaml")
    parser.add_argument("--output", required=True, help="output dir for drift-YYYY-MM-DD.md")
    parser.add_argument("--known-drifts", default=None, help="path to known-drifts.yaml")
    parser.add_argument("--manifest-dir", default=None, help="path to audit/deploys/")
    parser.add_argument("--audit-lookback-hours", type=int, default=24)
    parser.add_argument("--include-views", action="store_true",
                        help="也比對 views（DDL，跟 routines 同一套）")
    parser.add_argument("--include-tables", action="store_true",
                        help="也比對 tables（比欄位名+型別，需 sqlglot）")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    exclude_datasets, exclude_routine_patterns = get_excludes(cfg)
    known_drifts = load_known_drifts(Path(args.known_drifts)) if args.known_drifts else []

    print(f"Fetching live routines from {args.project}...")
    live = fetch_live_routines(args.project, args.region, exclude_datasets)
    # apply routine pattern excludes
    live = [r for r in live if not any(fnmatch(r.fullname, p) for p in exclude_routine_patterns)]

    print(f"Loading git routines from {args.git_root}...")
    git_routines = load_git_routines(Path(args.git_root))
    git_routines = [r for r in git_routines if not any(fnmatch(r.fullname, p) for p in exclude_routine_patterns)]

    print(f"Computing drift...")
    drifts = compute_drift(live, git_routines, args.project)

    # views（DDL-based，與 routines 同一套）
    if args.include_views:
        print(f"Fetching live views from {args.project}...")
        live_views = fetch_live_views(args.project, args.region, exclude_datasets)
        git_views = load_git_views(Path(args.git_root))
        print(f"  live views={len(live_views)}, git views={len(git_views)}")
        drifts += compute_drift(live_views, git_views, args.project, object_type="view")

    # tables（column-based，不比整段 DDL）
    if args.include_tables:
        print(f"Fetching live table columns from {args.project}...")
        live_cols = fetch_live_table_columns(args.project, args.region, exclude_datasets)
        git_cols = load_git_table_columns(Path(args.git_root))
        print(f"  live tables={len(live_cols)}, git tables={len(git_cols)}")
        drifts += compute_table_drift(live_cols, git_cols)

    # filter by known drifts
    filtered_names: list[str] = []
    real_drifts: list[Drift] = []
    for d in drifts:
        is_kd, _reason = is_known(d.fullname, known_drifts)
        if is_kd:
            filtered_names.append(d.fullname)
        else:
            real_drifts.append(d)

    # enrich with audit log info (only on real drifts)
    if real_drifts:
        print(f"Querying audit log for last modifiers...")
        modifiers = fetch_recent_modifiers(args.project, args.region, args.audit_lookback_hours)
        manifests = load_recent_manifests(Path(args.manifest_dir) if args.manifest_dir else None)
        for d in real_drifts:
            mod = modifiers.get(d.fullname)
            if mod:
                d.last_modifier = mod["user_email"]
                d.last_modified_at = mod["creation_time"]
            d.in_recent_manifest = routine_in_manifest(d.fullname, manifests)

    today = dt.date.today().isoformat()
    report = Report(
        generated_at=dt.datetime.utcnow().isoformat() + "Z",
        project=args.project,
        drifts=real_drifts,
        known_drifts_filtered=filtered_names,
    )

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"drift-{today}.md"
    out_path.write_text(render_report(report), encoding="utf-8")
    print(f"Wrote {out_path}")

    # 寫 summary 給 TG / 通知用（不進 git，只在 runner 本地）
    # 最後要有 trailing newline，否則 shell heredoc delimiter 會貼在最後一行末尾、GitHub 找不到
    summary_path = output_dir / "drift-summary.txt"
    if real_drifts:
        lines = []
        for d in real_drifts[:25]:  # 上限 25 條避免 TG 訊息爆字數
            lines.append(f"- [{d.kind}] {d.fullname}")
        if len(real_drifts) > 25:
            lines.append(f"... and {len(real_drifts) - 25} more")
        summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    else:
        summary_path.write_text("", encoding="utf-8")
    print(f"Wrote summary {summary_path} ({summary_path.stat().st_size} bytes)")

    if real_drifts:
        print(f"⚠  {len(real_drifts)} unknown drift(s) — see {out_path}")
        return 1  # signal CI / TG
    print("✅ No unknown drift.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
