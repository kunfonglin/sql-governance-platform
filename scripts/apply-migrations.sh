#!/usr/bin/env bash
# apply-migrations.sh — Phase 1 simplified migration applicator
#
# 用法:
#   ./apply-migrations.sh \
#       --project sandbox-prod \
#       --root ./migrations \
#       [--dry-run] \
#       [--ledger-dataset governance_audit] \
#       [--ledger-table migrations_applied]
#
# 行為:
#   1. 查 ledger 取已套用 migration_id 集合
#   2. 對 ${root}/*.sql 依檔名順序（YYYY-MM-DD-HHMM-... 字典序 = 時間序）
#   3. 已套用的 skip
#   4. 未套用的依序執行：
#       a. 跑 SQL
#       b. 成功 → INSERT ledger (status='applied')
#       c. 失敗 → INSERT ledger (status='failed') + exit 1
#   5. 列出本次新套用的 migration ID 清單到 stdout（給 manifest 用）
#
# 環境變數:
#   GITHUB_SHA       optional，寫入 ledger 的 git_sha 欄位
#   GITHUB_RUN_ID    optional，用於 deploy_id
#   CI_SA_EMAIL      可選，否則用 gcloud config 帶出來
#
# 退出碼:
#   0  全部 migration 成功（含「沒新 migration」情境）
#   1  有 migration 失敗
#   64 參數錯誤
#   65 ledger 表不存在或不可讀

set -uo pipefail

PROJECT=""
ROOT=""
DRY_RUN="false"
LEDGER_DATASET="governance_audit"
LEDGER_TABLE="migrations_applied"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)         PROJECT="$2"; shift 2 ;;
    --root)            ROOT="$2"; shift 2 ;;
    --dry-run)         DRY_RUN="true"; shift ;;
    --ledger-dataset)  LEDGER_DATASET="$2"; shift 2 ;;
    --ledger-table)    LEDGER_TABLE="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 64 ;;
  esac
done

[[ -z "$PROJECT" ]] && { echo "ERROR: --project required" >&2; exit 64; }
[[ -z "$ROOT"    ]] && { echo "ERROR: --root required"    >&2; exit 64; }

# 沒 migrations 目錄 = 沒 migration 要跑（不算錯）
if [[ ! -d "$ROOT" ]]; then
  echo "==> apply-migrations.sh: no migrations directory at $ROOT, skipping"
  echo "[]" > /tmp/migrations_applied.json
  exit 0
fi

LEDGER="${PROJECT}.${LEDGER_DATASET}.${LEDGER_TABLE}"

echo "==> apply-migrations.sh"
echo "    project = $PROJECT"
echo "    root    = $ROOT"
echo "    ledger  = $LEDGER"
echo "    dry_run = $DRY_RUN"

# 取 CI SA email
if [[ -z "${CI_SA_EMAIL:-}" ]]; then
  CI_SA_EMAIL=$(gcloud config get-value account 2>/dev/null || echo "unknown")
fi

GIT_SHA="${GITHUB_SHA:-unknown}"

# ---------- 1. 取已套用清單 ----------
echo ""
echo "Querying ledger for already-applied migrations..."

if ! applied=$(bq query --project_id="$PROJECT" --format=csv --use_legacy_sql=false --max_rows=10000 --quiet \
    "SELECT migration_id FROM \`$LEDGER\` WHERE status='applied' ORDER BY migration_id" 2>/tmp/ledger.err); then
  echo "ERROR: Cannot query ledger. Make sure $LEDGER exists." >&2
  cat /tmp/ledger.err >&2
  exit 65
fi

# 去掉 csv header 行
applied=$(echo "$applied" | tail -n +2)

if [[ -z "$applied" ]]; then
  echo "  (no migrations applied yet)"
else
  echo "  Already applied:"
  echo "$applied" | sed 's/^/    /'
fi

# ---------- 2. 列出 migrations 並排序 ----------
mapfile -t ALL_MIGRATIONS < <(find "$ROOT" -maxdepth 1 -type f -name '*.sql' | sort)

if [[ ${#ALL_MIGRATIONS[@]} -eq 0 ]]; then
  echo ""
  echo "No migration files in $ROOT"
  echo "[]" > /tmp/migrations_applied.json
  exit 0
fi

echo ""
echo "Found ${#ALL_MIGRATIONS[@]} migration file(s)"

# ---------- 3. 套用未套用的 migrations ----------
APPLIED_NEW=()

for f in "${ALL_MIGRATIONS[@]}"; do
  mid=$(basename "$f" .sql)

  # 已套用 → skip
  if echo "$applied" | grep -qx "$mid"; then
    echo ""
    echo "  ⊘ $mid (already applied)"
    continue
  fi

  echo ""
  echo "  → $mid"

  if [[ "$DRY_RUN" == "true" ]]; then
    if bq query --project_id="$PROJECT" --use_legacy_sql=false --dry_run < "$f" >/tmp/m.out 2>/tmp/m.err; then
      echo "    ✓ dry-run OK ($(basename "$f"))"
    else
      echo "    ✗ dry-run FAILED:"
      cat /tmp/m.err | sed 's/^/      /'
      exit 1
    fi
    continue
  fi

  # 真正套用
  start=$(date +%s%3N 2>/dev/null || python3 -c "import time;print(int(time.time()*1000))")

  if bq query --project_id="$PROJECT" --use_legacy_sql=false < "$f" >/tmp/m.out 2>/tmp/m.err; then
    end=$(date +%s%3N 2>/dev/null || python3 -c "import time;print(int(time.time()*1000))")
    duration=$((end - start))

    # 寫 ledger
    bq query --project_id="$PROJECT" --use_legacy_sql=false --quiet \
      "INSERT INTO \`$LEDGER\` (migration_id, applied_at, applied_by, git_sha, status, duration_ms)
       VALUES ('$mid', CURRENT_TIMESTAMP(), '$CI_SA_EMAIL', '$GIT_SHA', 'applied', $duration)" \
      > /dev/null 2>/tmp/ledger.err

    if [[ $? -ne 0 ]]; then
      echo "    ⚠ migration applied but FAILED to record in ledger:"
      cat /tmp/ledger.err | sed 's/^/      /'
      echo "    Migration succeeded but next deploy may try to re-run it. Investigate."
      exit 1
    fi

    APPLIED_NEW+=("$mid")
    echo "    ✓ applied (${duration} ms, recorded in ledger)"
  else
    echo "    ✗ MIGRATION FAILED:"
    cat /tmp/m.err | sed 's/^/      /'

    # 寫 ledger 標 failed
    err_msg=$(head -c 1000 /tmp/m.err | python3 -c "import sys,json; print(json.dumps(sys.stdin.read()))")
    bq query --project_id="$PROJECT" --use_legacy_sql=false --quiet \
      "INSERT INTO \`$LEDGER\` (migration_id, applied_at, applied_by, git_sha, status, error_message)
       VALUES ('$mid', CURRENT_TIMESTAMP(), '$CI_SA_EMAIL', '$GIT_SHA', 'failed', $err_msg)" \
      > /dev/null 2>&1

    exit 1
  fi
done

# ---------- 4. 輸出本次新套用的 migration 列表（給 manifest 合併用） ----------
echo ""
echo "✅ ${#APPLIED_NEW[@]} new migration(s) applied"

if [[ ${#APPLIED_NEW[@]} -gt 0 ]]; then
  printf '%s\n' "${APPLIED_NEW[@]}" \
    | python3 -c "import json,sys; print(json.dumps([l for l in sys.stdin.read().splitlines() if l]))" \
    > /tmp/migrations_applied.json
else
  echo "[]" > /tmp/migrations_applied.json
fi

echo ""
echo "Migrations applied this run (JSON written to /tmp/migrations_applied.json):"
cat /tmp/migrations_applied.json
