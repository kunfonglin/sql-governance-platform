#!/usr/bin/env bash
# apply-routines.sh — 部署 routines 到目標 BQ project
#
# 用法:
#   ./apply-routines.sh \
#       --project sandbox-prod \
#       --root ./bigquery \
#       [--dry-run] \
#       [--manifest-output ./audit/deploys/2026-04-30-run123-manifest.json] \
#       [--logical-project pilot] \
#       [--git-sha abc123def] \
#       [--platform-version v1.0] \
#       [--pr-url https://...] \
#       [--approved-by lin@example.com]
#
# 行為:
#   1. 對 ${root}/*/routines/*.sql 逐一跑 bq query --project_id=${project}
#   2. 任一失敗即中止，後續不跑
#   3. 若 --manifest-output 指定，產出 manifest.json 列出所有部署檔案

set -euo pipefail

PROJECT=""
ROOT=""
DRY_RUN="false"
MANIFEST_OUTPUT=""
LOGICAL_PROJECT="${LOGICAL_PROJECT:-unknown}"
GIT_SHA="${GITHUB_SHA:-unknown}"
PLATFORM_VERSION="${PLATFORM_VERSION:-unknown}"
PR_URL="${PR_URL:-}"
APPROVED_BY="${APPROVED_BY:-}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)             PROJECT="$2"; shift 2 ;;
    --root)                ROOT="$2"; shift 2 ;;
    --dry-run)             DRY_RUN="true"; shift ;;
    --manifest-output)     MANIFEST_OUTPUT="$2"; shift 2 ;;
    --logical-project)     LOGICAL_PROJECT="$2"; shift 2 ;;
    --git-sha)             GIT_SHA="$2"; shift 2 ;;
    --platform-version)    PLATFORM_VERSION="$2"; shift 2 ;;
    --pr-url)              PR_URL="$2"; shift 2 ;;
    --approved-by)         APPROVED_BY="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 64 ;;
  esac
done

[[ -z "$PROJECT" ]] && { echo "ERROR: --project required" >&2; exit 64; }
[[ -z "$ROOT"    ]] && { echo "ERROR: --root required"    >&2; exit 64; }
[[ ! -d "$ROOT"  ]] && { echo "ERROR: root not a dir: $ROOT" >&2; exit 64; }

echo "==> apply-routines.sh"
echo "    project = $PROJECT"
echo "    root    = $ROOT"
echo "    dry_run = $DRY_RUN"

mapfile -t FILES < <(find "$ROOT" -path '*/routines/*.sql' -type f | sort)

if [[ ${#FILES[@]} -eq 0 ]]; then
  echo "    no routines found"
fi

DEPLOYED=()
for f in "${FILES[@]}"; do
  rel="${f#"$ROOT"/}"
  echo "  → $rel"
  if [[ "$DRY_RUN" == "true" ]]; then
    bq query --project_id="$PROJECT" --use_legacy_sql=false --dry_run < "$f" \
      || { echo "❌ dry_run failed: $rel" >&2; exit 1; }
  else
    bq query --project_id="$PROJECT" --use_legacy_sql=false < "$f" \
      || { echo "❌ deploy failed: $rel" >&2; exit 1; }
  fi
  DEPLOYED+=("bigquery/$rel")
done

echo "✅ ${#DEPLOYED[@]} routines processed"

# Optional manifest output
if [[ -n "$MANIFEST_OUTPUT" && "$DRY_RUN" != "true" ]]; then
  mkdir -p "$(dirname "$MANIFEST_OUTPUT")"
  TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  DEPLOY_ID="$(date -u +%Y-%m-%d)-${GITHUB_RUN_ID:-local}"

  # Build JSON array of deployed routines
  ROUTINES_JSON=$(printf '%s\n' "${DEPLOYED[@]}" \
    | python3 -c "import json,sys; print(json.dumps([l for l in sys.stdin.read().splitlines() if l]))")

  cat > "$MANIFEST_OUTPUT" <<EOF
{
  "schema_version": 1,
  "deploy_id": "$DEPLOY_ID",
  "logical_project": "$LOGICAL_PROJECT",
  "timestamp": "$TS",
  "target_project": "$PROJECT",
  "trigger": "github-actions",
  "approved_by": "$APPROVED_BY",
  "pr_url": "$PR_URL",
  "git_sha": "$GIT_SHA",
  "platform_version": "$PLATFORM_VERSION",
  "deployed": {
    "routines": $ROUTINES_JSON
  },
  "dry_run_passed": true
}
EOF
  echo "📄 wrote manifest: $MANIFEST_OUTPUT"
fi
