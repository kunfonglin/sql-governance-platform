#!/usr/bin/env bash
# apply-views.sh — 部署 views 到目標 BQ project（two-phase，沿用 apply-routines 的容錯模式）
#
# 為什麼 view 跟 routine 同一套、跟 table 不同：
#   View 是「無資料」物件，CREATE OR REPLACE VIEW 非破壞 → 跟 SP/UDF 同性質，
#   可以每次全量重佈、用 git 當 source of truth。Table 因為 CREATE OR REPLACE 會掉資料，
#   才必須走 migration（見 apply-migrations.sh）。
#
# 用法:
#   ./apply-views.sh \
#       --project igs-pig-test \
#       --root ./bigquery \
#       [--dry-run] \
#       [--out-json /tmp/views_deployed.json]
#
# 行為（two-phase，跟 apply-routines.sh 一致）:
#   1. 對 ${root}/*/views/*.sql 嘗試 CREATE OR REPLACE VIEW
#   2. 失敗的留到下一輪重試（view 互相引用時，被依賴的可能在前一輪建好了）
#   3. 一輪沒進展 = 真有問題（循環依賴、語法錯、缺 table/view）→ exit 1
#   4. 把本次部署的 view 清單寫成 JSON 到 --out-json（給 reusable-deploy 補進 manifest）
#
# 退出碼:
#   0  全部成功（含「沒有 views」情境）
#   1  有 view 部署不起來
#   64 參數錯誤

# 注意: 不開 set -e（要容忍單檔失敗繼續處理其他）
set -uo pipefail

PROJECT=""
ROOT=""
DRY_RUN="false"
OUT_JSON="/tmp/views_deployed.json"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --project)   PROJECT="$2"; shift 2 ;;
    --root)      ROOT="$2"; shift 2 ;;
    --dry-run)   DRY_RUN="true"; shift ;;
    --out-json)  OUT_JSON="$2"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 64 ;;
  esac
done

[[ -z "$PROJECT" ]] && { echo "ERROR: --project required" >&2; exit 64; }
[[ -z "$ROOT"    ]] && { echo "ERROR: --root required"    >&2; exit 64; }

# 預設輸出空陣列，確保下游 patch 步驟一定讀得到檔
echo "[]" > "$OUT_JSON"

# 沒有 bigquery root = 沒 view 要部（不算錯）
if [[ ! -d "$ROOT" ]]; then
  echo "==> apply-views.sh: no bigquery root at $ROOT, skipping"
  exit 0
fi

echo "==> apply-views.sh (two-phase)"
echo "    project = $PROJECT"
echo "    root    = $ROOT"
echo "    dry_run = $DRY_RUN"

mapfile -t ALL_FILES < <(find "$ROOT" -path '*/views/*.sql' -type f | sort)

if [[ ${#ALL_FILES[@]} -eq 0 ]]; then
  echo "    no views found, nothing to deploy"
  exit 0
fi

echo "    found ${#ALL_FILES[@]} view file(s)"

# ---------- 兩階段部署 ----------
DEPLOYED=()
todo=("${ALL_FILES[@]}")
attempt=1
last_remaining=-1
LAST_ERROR=""

while [[ ${#todo[@]} -gt 0 ]]; do
  echo ""
  echo "=== Pass $attempt: ${#todo[@]} view(s) to try ==="
  failed_this_round=()

  for f in "${todo[@]}"; do
    rel="${f#"$ROOT"/}"

    if [[ "$DRY_RUN" == "true" ]]; then
      bq_cmd=(bq query --project_id="$PROJECT" --use_legacy_sql=false --dry_run)
    else
      bq_cmd=(bq query --project_id="$PROJECT" --use_legacy_sql=false)
    fi

    if "${bq_cmd[@]}" < "$f" >/tmp/bqv.out 2>/tmp/bqv.err; then
      echo "  ✓ $rel"
      DEPLOYED+=("bigquery/$rel")
    else
      echo "  ✗ $rel (will retry next pass)"
      failed_this_round+=("$f")
      LAST_ERROR=$(cat /tmp/bqv.err)
    fi
  done

  # 沒進展 = 真有問題（循環依賴 / 語法錯 / 缺被引用的 table|view）
  if [[ ${#failed_this_round[@]} -eq $last_remaining ]]; then
    echo ""
    echo "❌ Cannot deploy views after $attempt passes. Remaining:"
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
echo "✅ ${#DEPLOYED[@]} view(s) deployed in $((attempt - 1)) pass(es)"

# ---------- 輸出本次部署清單（給 manifest 合併用） ----------
if [[ "$DRY_RUN" != "true" ]]; then
  printf '%s\n' "${DEPLOYED[@]}" \
    | python3 -c "import json,sys; print(json.dumps([l for l in sys.stdin.read().splitlines() if l]))" \
    > "$OUT_JSON"
  echo "📄 wrote deployed list: $OUT_JSON"
  cat "$OUT_JSON"
fi
