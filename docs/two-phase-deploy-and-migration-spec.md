# Spec: 兩階段部署 + Migration 機制（Phase 1 v1.1）

**狀態**: 📋 設計中，待 review
**目標 platform 版本**: v1.1
**動機**:
1. 替代 `OPTIONS(strict_mode=false)` 的開發者負擔
2. 為 Phase 2 table 處理鋪基礎
3. 提供 DROP SP / 未來 ALTER table 的標準路徑

---

## 1. 整體架構

```
deploy-test / deploy-prod 流程：

  1. Auth GCP via WIF                     ← 既有
  2. apply-routines.sh                    ← 改成兩階段
       ├─ Pass 1: 試所有 SP CREATE OR REPLACE
       ├─ Pass 2: 對 Pass 1 失敗的再試一輪
       └─ Pass N: 一輪沒進展 → 真有問題 → exit 1
  3. apply-migrations.sh                  ← 新增
       ├─ 查 ledger 取已套用清單
       ├─ 對未套用的 migration 依檔名順序套用
       ├─ 套用後 INSERT ledger
       └─ 失敗中止
  4. write manifest                       ← 既有，加 migrations_applied 欄位
  5. notify TG                            ← 既有
```

---

## 2. 兩階段部署 spec

### 2.1 演算法

```bash
# 偽碼，實際在 apply-routines.sh
todo=( $(find $ROOT -path '*/routines/*.sql' | sort) )
attempt=1
last_remaining=-1
DEPLOYED=()

while [[ ${#todo[@]} -gt 0 ]]; do
  echo "=== Pass $attempt: ${#todo[@]} files to try ==="
  failed_this_round=()
  last_error=""

  for f in "${todo[@]}"; do
    rel="${f#$ROOT/}"
    if bq query --project_id=$PROJECT --use_legacy_sql=false < "$f" 2>/tmp/err; then
      echo "  ✓ $rel"
      DEPLOYED+=("bigquery/$rel")
    else
      echo "  ✗ $rel (will retry)"
      failed_this_round+=("$f")
      last_error=$(cat /tmp/err)
    fi
  done

  if [[ ${#failed_this_round[@]} -eq $last_remaining ]]; then
    echo "❌ Cannot deploy after $attempt passes:"
    printf '   %s\n' "${failed_this_round[@]}"
    echo "Last error:"
    echo "$last_error"
    exit 1
  fi

  last_remaining=${#failed_this_round[@]}
  todo=( "${failed_this_round[@]}" )
  attempt=$((attempt + 1))
done

echo "✅ ${#DEPLOYED[@]} routines deployed in $((attempt-1)) pass(es)"
```

### 2.2 設計取捨

| 取捨 | 決定 | 為什麼 |
|---|---|---|
| 是否 set -e 全程開 | ❌ 不開 | 必須容忍單一 SP 失敗繼續處理其他 |
| 是否解析 SP 依賴 | ❌ 不解析 | sqlglot 那條路保留給未來 |
| 失敗的 SP 多久才放棄 | 一輪沒進展即放棄 | 避免無限 loop |
| TG 通知時機 | 整個跑完才推 | 中間 retry 不打擾 |
| 重試上限 | 隱含（直到無進展） | 一般 1-2 pass，最多 N pass |

### 2.3 對 SP 的副作用評估

| 顧慮 | 結論 |
|---|---|
| 同一個 SP CREATE 兩次有副作用嗎 | ❌ 沒有，CREATE OR REPLACE 冪等 |
| 部署時間影響 | 平常 1 pass，最差 2-3 pass，影響輕微 |
| Quota 影響 | 每個 CREATE PROCEDURE 是 query job，同 quota 限制；對 sandbox 不會撞 |

### 2.4 對 table（未來）的適用性

| Table 操作 | 兩階段 work? | 必須 migration? |
|---|---|---|
| `CREATE TABLE IF NOT EXISTS` | ✅ | ❌ |
| `CREATE OR REPLACE TABLE AS SELECT` | ✅ | ❌（但會洗資料）|
| `ALTER TABLE ADD COLUMN` | ❌（重跑會 fail） | ✅ |
| `ALTER TABLE DROP COLUMN` | ❌ | ✅ |
| `DROP TABLE IF EXISTS` | ✅ | ✅（但要走 migration 留 audit） |
| `UPDATE` / `MERGE` (backfill) | ❌（會重複跑壞資料）| ✅ |

→ Phase 2 table 部分會混用：state DDL 走兩階段、mutation 走 migration。

---

## 3. Migration 機制 spec（簡化版）

### 3.1 目錄結構

```
phase1/pilot/
  migrations/
    YYYY-MM-DD-HHMM-{slug}.sql
```

範例：
```
migrations/
  2026-05-07-1400-drop-sp-hello-world.sql
  2026-05-09-1030-drop-sp-deprecated.sql
```

### 3.2 命名規則

```
{ISO date}-{HHMM}-{slug}.sql

YYYY-MM-DD-HHMM 確保字典序 = 時間序
slug 用 kebab-case，不超過 50 字元
```

### 3.3 Migration 檔內容格式

```sql
-- migrations/YYYY-MM-DD-HHMM-{slug}.sql
-- 對應 git 變更: <檔案路徑> 被刪除/修改
-- 操作: <DROP / ALTER / 等>
-- 備註: <為什麼>

DROP PROCEDURE IF EXISTS `analytics.sp_hello_world`;
```

**強制規範**：
- 檔頭必須有 `-- 對應 git 變更`、`-- 操作`、`-- 備註` 三行
- 內容**必須冪等**（用 `IF EXISTS` / `IF NOT EXISTS` 等避免重跑炸）
- 一個 migration 只做一件事
- **合併到 main 後不可修改**（簡化版沒 checksum 強制，但這是規範）

### 3.4 Ledger 表

#### Schema

```sql
-- 每個 GCP project 各建一份在 governance_audit dataset
CREATE TABLE IF NOT EXISTS `governance_audit.migrations_applied` (
  migration_id   STRING NOT NULL,    -- 檔名（不含 .sql）
  applied_at     TIMESTAMP NOT NULL,
  applied_by     STRING NOT NULL,    -- CI SA email
  git_sha        STRING,             -- 對應 commit SHA
  status         STRING NOT NULL,    -- 'applied' | 'failed'
  duration_ms    INT64,
  error_message  STRING,
  -- 可加但不強制
  pr_url         STRING,
  manifest_id    STRING
)
PARTITION BY DATE(applied_at)
CLUSTER BY migration_id;
```

#### 一張表記什麼

```
✓ 已成功套用的 migration_id（CI 跑前查、跑後寫）
✓ 失敗的 migration_id（status='failed'）
✓ 套用的時間、由誰（CI SA）、對應的 git commit
✗ 不記 checksum（簡化版，未來 phase 2 加）
✗ 不記 SQL 內容（git 才是 source of truth）
```

### 3.5 apply-migrations.sh 演算法

```bash
#!/usr/bin/env bash
# apply-migrations.sh --project=$PROJECT --root=$MIGRATIONS_ROOT
# Phase 1 simplified version

LEDGER="${PROJECT}.governance_audit.migrations_applied"
APPLIED_NEW=()

# 1. 取已套用清單
applied=$(bq query --project_id=$PROJECT --format=csv --use_legacy_sql=false \
  "SELECT migration_id FROM \`$LEDGER\` WHERE status='applied' ORDER BY migration_id" \
  | tail -n +2)  # skip header

# 2. 對檔名排序（YYYY-MM-DD-HHMM-... 字典序 = 時間序）
for f in $(ls migrations/*.sql 2>/dev/null | sort); do
  mid=$(basename "$f" .sql)

  # 3. 已套用 → skip
  if echo "$applied" | grep -q "^$mid$"; then
    echo "  ⊘ $mid (already applied)"
    continue
  fi

  # 4. 套用
  echo "  → $mid"
  start=$(date +%s%3N)
  if bq query --project_id=$PROJECT --use_legacy_sql=false < "$f"; then
    duration=$(($(date +%s%3N) - start))
    bq query --project_id=$PROJECT --use_legacy_sql=false --format=none \
      "INSERT INTO \`$LEDGER\` (migration_id, applied_at, applied_by, git_sha, status, duration_ms)
       VALUES ('$mid', CURRENT_TIMESTAMP(), '$CI_SA', '$GITHUB_SHA', 'applied', $duration)"
    APPLIED_NEW+=("$mid")
    echo "    ✓ applied ($duration ms)"
  else
    bq query --project_id=$PROJECT --use_legacy_sql=false --format=none \
      "INSERT INTO \`$LEDGER\` (migration_id, applied_at, applied_by, git_sha, status, error_message)
       VALUES ('$mid', CURRENT_TIMESTAMP(), '$CI_SA', '$GITHUB_SHA', 'failed', 'see CI log')"
    echo "    ✗ FAILED"
    exit 1
  fi
done

echo "✅ ${#APPLIED_NEW[@]} new migrations applied"
```

### 3.6 設計取捨（vs Phase 2 完整版）

| 功能 | Phase 1 簡化 | Phase 2 完整 |
|---|---|---|
| Ledger 表 | ✅ | ✅ |
| 跑 SQL 失敗中止 | ✅ | ✅ |
| Checksum 防竄改 | ❌ 信任 git | ✅ SHA256 |
| `requires_manual_step` 標記 | ❌ 全自動 | ✅ |
| 跑前 dry_run 預驗 | ❌ | ✅ |
| 失敗 retry 機制 | ❌ 失敗即停 | ✅ 有條件 |
| Migration 之間依賴宣告 | ❌ 靠檔名順序 | ✅ |

→ **簡化版的取捨原則**：「**會走 PR review + git audit trail 的事，不重複實作 ledger 上的對應檢查**」。

---

## 4. 整合點

### 4.1 修改 reusable-deploy.yml

```yaml
# 既有 step（不變）
- name: Deploy routines + write manifest
  uses: ./.platform/.github/actions/deploy-routines
  with: ...

# ⬇ 新增 step
- name: Apply migrations
  uses: ./.platform/.github/actions/apply-migrations
  with:
    project_id: ${{ inputs.project_id }}
    migrations_root: migrations
    logical_project: ${{ inputs.logical_project }}

# 既有 step
- name: Commit manifest
  ...
```

### 4.2 新增 composite action：apply-migrations

```
phase1/platform/.github/actions/apply-migrations/action.yml
```

包含：
- inputs: project_id / migrations_root / logical_project
- step: 跑 apply-migrations.sh 並設定 env vars

### 4.3 Manifest schema 擴充

```json
{
  "schema_version": 2,    // bump from 1
  ...
  "deployed": {
    "routines": [...],
    "migrations_applied": [
      {
        "id": "2026-05-07-1400-drop-sp-hello-world",
        "applied_at": "2026-05-07T14:30:12Z",
        "duration_ms": 234
      }
    ]
  }
}
```

---

## 5. 失敗處理

### 5.1 兩階段部署失敗

```
情境: SP X 在 N 個 pass 都 fail
原因可能:
  - SP body 真有錯（typo / 非預期 reference）
  - 真的循環依賴
  - 跨 dataset reference 但 dataset 不存在
動作:
  - exit 1 + 印出 last error
  - 整個 deploy 中止
  - migration 不會跑
  - TG 推失敗通知
```

### 5.2 Migration 失敗

```
情境: migration M 失敗
動作:
  - INSERT ledger status='failed'
  - 整個 deploy 中止
  - 後續 migration 不跑
  - TG 推失敗通知
修復:
  - 寫 migration M+1 補救（不能改 M，因為合併後 immutable）
  - 或在 git 直接刪 M（簡化版允許，phase 2 完整版會擋）
```

### 5.3 Ledger 寫入失敗

```
情境: 跑 INSERT INTO migrations_applied 失敗
原因: governance_audit dataset 不存在 / 權限問題
動作:
  - 整個 migration step 失敗
  - 但實際 SQL 已經跑了 → 下次會重跑 → 可能炸
緩解:
  - migration SQL 必須冪等（IF EXISTS / IF NOT EXISTS）
  - onboarding 文件強制要求先建 ledger 表
```

---

## 6. 對 SP 規範的影響

實作完兩階段後，更新 [`sql-rules.md`](sql-rules.md)：

| 規則 | 現況 | 改為 |
|---|---|---|
| Orchestrator 加 `OPTIONS(strict_mode=false)` | 必須 | **不需要**（兩階段自動解） |
| Dynamic SQL 加 `OPTIONS(strict_mode=false)` | 必須 | **仍必須**（runtime 才驗） |
| 一般 SP | 不加 | 不加 |

→ 開發者規範**簡化**為：「只有 EXECUTE IMMEDIATE 才需要 strict_mode=false」。

---

## 7. 升級到 sqlglot 拓樸排序的路徑

未來 Phase 2/3 想做：

```
1. lineage-extract.py 已能解析 SP body 找 CALL / table reference
2. 抽 dependency graph
3. 拓樸排序
4. apply-routines.sh 改成「先跑拓樸排序、再用兩階段當 fallback」
```

兩階段邏輯**不需移除**，當 fallback 用，遇到 dynamic SQL 等場景仍 work。

---

## 8. Pilot 端要做什麼（情境 C 一起驗證）

升級 platform v1.0 → v1.1 後，pilot 端：

```
1. 改 4 個 wrapper workflow: @v1.0 → @v1.1
2. 新增 migrations/ 目錄
3. 寫第一個 migration: drop-sp-hello-world.sql
4. 刪 sp_hello_world.sql
5. （驗證）拿掉 sp_run_daily_pipeline 的 OPTIONS(strict_mode=false)
6. 開 PR development → main
7. Review log:
   a. apply-routines 應該 1-2 pass 跑完
   b. apply-migrations 應該套用 1 個 migration（DROP）
   c. prod 上 sp_hello_world 真的消失
   d. drift detector 不會再報這個 routine
```

---

## 9. 待你 review 的決策

| # | 問題 | 預設 | 你決定 |
|---|---|---|---|
| 1 | Ledger 表 schema 看起來 OK 嗎 | 上面那 8 欄 | ? |
| 2 | Ledger 表叫 `migrations_applied` 還是 `migration_history` | `migrations_applied` | ? |
| 3 | Migration 檔頭規範 3 行 metadata 強制嗎 | 強制（CI 不檢查但規範書寫） | ? |
| 4 | 失敗的 migration 容許用「git 刪檔」的方式 retry 嗎 | 簡化版允許 | ? |
| 5 | Phase 1 結束時要不要把這套 spec 升級到 phase 2 完整版（補 checksum / require_manual_step） | 看屆時情況 | ? |

---

## 10. 工程量估算

| 工作 | 估時 |
|---|---|
| 改 apply-routines.sh 為兩階段 | 30 分鐘 |
| 寫 apply-migrations.sh + composite action | 45 分鐘 |
| 在 prod + test 建 ledger 表 | 5 分鐘（一行 CREATE） |
| 改 reusable-deploy.yml 串入 migration step | 15 分鐘 |
| 更新 manifest schema + write-manifest action | 15 分鐘 |
| Bump platform v1.0 → v1.1 + CHANGELOG | 10 分鐘 |
| **小計** | **~2 小時** |
| Pilot 端驗證情境 C | 15 分鐘 |
| 更新 docs（sql-rules / iam-wif-setup / wif-walkthrough） | 30 分鐘 |
| **總計** | **~3 小時** |

---

## 11. 後續步驟

1. 你 review 本 spec，回答 §9 五個問題
2. 我動手實作 #1-#6
3. 你跑 pilot 驗證情境 C
4. 一起 update docs
