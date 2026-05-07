#!/usr/bin/env bash
# apply-routines.sh — 部署 routines 到目標 BQ project（兩階段自動容錯）
#
# 用法:
#   ./apply-routines.sh \
#       --project sandbox-prod \
#       --root ./bigquery \
#       [--dry-run] \
#       [--manifest-output ./audit/deploys/2026-04-30-run123-manifest.json] \
#       [--logical-project pilot] \
#       [--git-sha abc123def] \
#       [--platform-version v1.1] \
#       [--pr-url https://...] \
#       [--approved-by lin@example.com]
#
# 行為（v1.1 兩階段）:
#   1. 對 ${root}/*/routines/*.sql 嘗試 CREATE OR REPLACE
#   2. 失敗的 SP 留到下一輪重試（依賴可能在第一輪建好了）
#   3. 一輪沒進展 = 真有問題（典型: 循環依賴、語法錯、缺 table）→ exit 1
#   4. 全部成功則 manifest（若有指定路徑）

# 注意: 不開 set -e (要容忍單檔失敗繼續處理其他)
set -uo pipefail

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

echo "==> apply-routines.sh (two-phase)"
echo "    project = $PROJECT"
echo "    root    = $ROOT"
echo "    dry_run = $DRY_RUN"

mapfile -t ALL_FILES < <(find "$ROOT" -path '*/routines/*.sql' -type f | sort)

if [[ ${#ALL_FILES[@]} -eq 0 ]]; then
  echo "    no routines found, nothing to deploy"
  exit 0
fi

# ---------- 兩階段部署 ----------
DEPLOYED=()
todo=("${ALL_FILES[@]}")
attempt=1
last_remaining=-1
LAST_ERROR=""

while [[ ${#todo[@]} -gt 0 ]]; do
  echo ""
  echo "=== Pass $attempt: ${#todo[@]} files to try ==="
  failed_this_round=()

  for f in "${todo[@]}"; do
    rel="${f#"$ROOT"/}"

    if [[ "$DRY_RUN" == "true" ]]; then
      bq_cmd=(bq query --project_id="$PROJECT" --use_legacy_sql=false --dry_run)
    else
      bq_cmd=(bq query --project_id="$PROJECT" --use_legacy_sql=false)
    fi

    if "${bq_cmd[@]}" < "$f" >/tmp/bq.out 2>/tmp/bq.err; then
      echo "  ✓ $rel"
      DEPLOYED+=("bigquery/$rel")
    else
      echo "  ✗ $rel (will retry next pass)"
      failed_this_round+=("$f")
      LAST_ERROR=$(cat /tmp/bq.err)
    fi
  done

  # 沒進展 = 真有問題
  if [[ ${#failed_this_round[@]} -eq $last_remaining ]]; then
    echo ""
    echo "❌ Cannot deploy after $attempt passes. Remaining files:"
    printf '   %s\n' "${failed_this_round[@]}"
    echo ""
    echo "Last error message:"
    echo "$LAST_ERROR"
    exit 1
  fi

  last_remaining=${#failed_this_round[@]}
  todo=("${failed_this_round[@]}")
  attempt=$((attempt + 1))
done

echo ""
echo "✅ ${#DEPLOYED[@]} routines deployed in $((attempt - 1)) pass(es)"

# ---------- Optional manifest output ----------
if [[ -n "$MANIFEST_OUTPUT" && "$DRY_RUN" != "true" ]]; then
  mkdir -p "$(dirname "$MANIFEST_OUTPUT")"
  TS="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  DEPLOY_ID="$(date -u +%Y-%m-%d)-${GITHUB_RUN_ID:-local}"

  ROUTINES_JSON=$(printf '%s\n' "${DEPLOYED[@]}" \
    | python3 -c "import json,sys; print(json.dumps([l for l in sys.stdin.read().splitlines() if l]))")

  cat > "$MANIFEST_OUTPUT" <<EOF
{
  "schema_version": 2,
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
    "routines": $ROUTINES_JSON,
    "migrations_applied": []
  },
  "deploy_passes": $((attempt - 1)),
  "dry_run_passed": true
}
EOF
  echo "📄 wrote manifest: $MANIFEST_OUTPUT"
fi
