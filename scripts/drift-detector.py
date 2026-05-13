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
    kind: str  # "orphan" | "not_deployed" | "content"
    fullname: str
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
        REGEXP_EXTRACT(query, r'(?i)CREATE\\s+(?:OR\\s+REPLACE\\s+)?(?:PROCEDURE|FUNCTION|TABLE\\s+FUNCTION)\\s+`?(?:[\\w-]+\\.)?([\\w-]+\\.[\\w-]+)`?') AS routine_fullname
      FROM `region-{region}`.INFORMATION_SCHEMA.JOBS_BY_PROJECT
      WHERE creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {hours} HOUR)
        AND statement_type IN ('CREATE_PROCEDURE', 'CREATE_FUNCTION', 'CREATE_TABLE_FUNCTION', 'DROP_PROCEDURE', 'DROP_FUNCTION', 'SCRIPT')
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
_BACKTICK_FULL_REF_RE = re.compile(r"`([\w-]+)\.([\w-]+)\.([\w-]+)`")
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

    # 3. Strip project_id from same-project refs (so prod's `proj.ds.obj` matches git's `ds.obj`)
    if source_project:
        def _strip_quoted(m: re.Match) -> str:
            return f"`{m.group(2)}.{m.group(3)}`" if m.group(1) == source_project else m.group(0)

        def _strip_unquoted(m: re.Match) -> str:
            return f"{m.group(2)}.{m.group(3)}" if m.group(1) == source_project else m.group(0)

        text = _BACKTICK_FULL_REF_RE.sub(_strip_quoted, text)
        text = _UNQUOTED_FULL_REF_RE.sub(_strip_unquoted, text)

    # 4. Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    # Remove trailing semicolons for stable comparison
    text = text.rstrip(";").strip()
    # 5. Lowercase
    return text.lower()


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
) -> list[Drift]:
    by_name_live = {r.fullname: r for r in live}
    by_name_git = {r.fullname: r for r in git}

    drifts: list[Drift] = []

    # orphan: in live but not git
    for fullname in sorted(set(by_name_live) - set(by_name_git)):
        drifts.append(Drift(
            kind="orphan",
            fullname=fullname,
            detail="prod 有此 routine 但 git 沒有對應檔案",
        ))

    # not deployed: in git but not live
    for fullname in sorted(set(by_name_git) - set(by_name_live)):
        drifts.append(Drift(
            kind="not_deployed",
            fullname=fullname,
            detail="git 有此 routine 但 prod 沒有",
        ))

    # content drift
    for fullname in sorted(set(by_name_live) & set(by_name_git)):
        live_norm = normalize_for_compare(by_name_live[fullname].ddl, project)
        git_norm = normalize_for_compare(by_name_git[fullname].ddl, project)
        if live_norm != git_norm:
            # find first divergence position for debug
            mismatch_at = next(
                (i for i in range(min(len(live_norm), len(git_norm))) if live_norm[i] != git_norm[i]),
                min(len(live_norm), len(git_norm)),
            )
            window_start = max(0, mismatch_at - 30)
            window_end = mismatch_at + 60
            preview = (
                f"first diff @{mismatch_at}: "
                f"LIVE='...{live_norm[window_start:window_end]}...' | "
                f"GIT='...{git_norm[window_start:window_end]}...'"
            )
            drifts.append(Drift(
                kind="content",
                fullname=fullname,
                detail="內容不一致",
                diff_preview=preview,
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
            "| Kind | Routine | Last Modifier | Last Modified | In Manifest? | Detail |",
            "|------|---------|---------------|---------------|--------------|--------|",
        ]
        for d in report.drifts:
            lines.append(
                f"| {d.kind} | `{d.fullname}` | {d.last_modifier or '-'} | "
                f"{d.last_modified_at or '-'} | {'yes' if d.in_recent_manifest else 'no'} | {d.detail} |"
            )
        lines.append("")

        # diff previews (for content drifts)
        content_drifts = [d for d in report.drifts if d.kind == "content" and d.diff_preview]
        if content_drifts:
            lines += ["## Content diff previews", ""]
            for d in content_drifts:
                lines += [f"### `{d.fullname}`", "```", d.diff_preview, "```", ""]

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
    summary_path = output_dir / "drift-summary.txt"
    if real_drifts:
        lines = []
        for d in real_drifts[:25]:  # 上限 25 條避免 TG 訊息爆字數
            lines.append(f"- [{d.kind}] {d.fullname}")
        if len(real_drifts) > 25:
            lines.append(f"... and {len(real_drifts) - 25} more")
        summary_path.write_text("\n".join(lines), encoding="utf-8")
    else:
        summary_path.write_text("", encoding="utf-8")

    if real_drifts:
        print(f"⚠  {len(real_drifts)} unknown drift(s) — see {out_path}")
        return 1  # signal CI / TG
    print("✅ No unknown drift.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
