# lineage-extract.py — Phase 1 Lineage 雛形工具

> 不在原 Phase 1 SP-only 範圍內，但**完全不需 GitHub 整合**就能跑，適合 GitHub 卡關時並行做。
> 設計取材自 Harun Yuksel (2026-04) 的 Claude Code lineage skill。

---

## 它解決什麼問題

| 問題 | 工具能不能答 |
|---|---|
| `sp_x` 讀寫了哪些表？ | ✅ |
| `user_orders` 是被哪些 SP 寫入的？ | ✅ |
| 哪些 SP 含 `EXECUTE IMMEDIATE`（dynamic SQL）→ 靜態解析失敗？ | ✅ |
| 「runtime 跑出來的依賴」vs「程式碼上看起來的依賴」哪裡不一致？ | ✅ |
| `2026-03-15 14:30 誰刪了 user_orders`？ | ❌（這是 audit log sink 的事，不是 lineage） |
| 完整的 IAM / DDL / API 操作軌跡 | ❌（同上）|

---

## 跟 audit log sink 的分工

```
本工具                            audit log sink
─────────                         ──────────────
資料源: INFORMATION_SCHEMA.JOBS    Cloud Logging
保留:   180 天（BQ 內建）          1 年（自設 partition expiry）
成本:   免費                      log volume × Cloud Logging 費率
解析:   sqlglot 加值靜態分析        無（原始 protoPayload）
產出:   SQLite 依賴圖             BQ dataset 巨大 schema
適合:   開發者問「sp_x 讀寫什麼」    稽核 / 鑑識「誰做了什麼」
```

**結論**：兩個都要做，分工不重疊。本工具現在做（不卡 admin），sink 等 GitHub 通了再做。

---

## 安裝

依賴用 Poetry 管。詳見 [`docs/dev-setup.md`](../docs/dev-setup.md)，最短路徑：

```bash
cd D:/Claude/BQ_Governance/phase1/platform
poetry install
gcloud auth application-default login
```

---

## 使用流程

> 所有命令都假設你在 `phase1/platform/` 目錄底下執行，並用 `poetry run` 開頭走 venv。

### 1. 從 BigQuery runtime 抽 lineage（最近 30 天的真實執行）

```bash
poetry run python scripts/lineage-extract.py from-jobs \
  --project tapirus-test-384312 \
  --region US \
  --days 30 \
  --db ./lineage.db
```

行為：
- 查 `region-us.INFORMATION_SCHEMA.JOBS_BY_PROJECT` 過去 30 天所有 DONE jobs
- 對每個 query，看 `query` 文字是不是 `CALL dataset.routine(...)` 形式
  - 是 → 該 routine 為「源頭」
  - 不是 → 跳過（adhoc query 暫不記錄，避免噪音）
- 從 `referenced_tables` 抽讀取依賴
- 從 `destination_table` 抽寫入依賴（只在 statement 是 INSERT/MERGE/UPDATE 等）
- 寫進 SQLite

### 2. 從 git 上的 SP body 抽 lineage（靜態解析）

```bash
poetry run python scripts/lineage-extract.py from-repo \
  --git-root ../pilot/bigquery \
  --db ./lineage.db
```

> 注意 `--git-root` 指向你 pilot repo 的 `bigquery/` 目錄（裡面是 `{dataset}/routines/*.sql`）。
> 若 pilot repo 還沒推上 GitHub 也可以指本地路徑，例如 `D:/Claude/BQ_Governance/phase1/pilot/bigquery`。

行為：
- 對每個 routine 檔，先剝掉 metadata header
- 偵測有沒有 `EXECUTE IMMEDIATE` → 標記 `has_dynamic_sql = 1`
- sqlglot parse → 找出所有 `Insert / Update / Delete / Merge` 的 target → 寫入依賴
- 其餘 `Table` 引用 → 讀取依賴
- 跨專案 ref（`other.ds.tbl`）也會記錄，但 schema 名會包含 db 部分

### 3. 看 DB 摘要

```bash
poetry run python scripts/lineage-extract.py merge --db ./lineage.db
```

```
DB summary:
  routines : 42
  tables   : 87
  edges    : 312
    - jobs     read   145
    - jobs     write  68
    - sqlglot  read   78
    - sqlglot  write  21
```

### 4. 印出某個 routine 的報告

```bash
poetry run python scripts/lineage-extract.py report \
  --routine analytics.sp_build_daily_summary \
  --db ./lineage.db
```

範例輸出（markdown）：

```markdown
# Lineage report — `analytics.sp_build_daily_summary`

- Last seen in jobs: 2026-05-04T03:00:12+00:00
- Contains EXECUTE IMMEDIATE: no

## Writes

| Table | Source | Last seen (jobs) | Sample count |
|-------|--------|------------------|--------------|
| `analytics.daily_summary` | jobs | 2026-05-04T03:00:12+00:00 | 7 |
| `analytics.daily_summary` | sqlglot | — | 1 |

## Reads

| Table | Source | Last seen (jobs) | Sample count |
|-------|--------|------------------|--------------|
| `order_data.user_orders` | jobs | 2026-05-04T03:00:12+00:00 | 7 |
| `order_data.user_orders` | sqlglot | — | 1 |
```

---

## 推薦的工作流

每天（或 CI nightly）：

```bash
# 1. 抓昨天的 runtime 紀錄
poetry run python scripts/lineage-extract.py from-jobs \
  --project tapirus-test-384312 --region US --days 1 --db ./lineage.db

# 2. 重新解析 git（如果有改動）
poetry run python scripts/lineage-extract.py from-repo \
  --git-root ../pilot/bigquery --db ./lineage.db

# 3. 看 DB 統計
poetry run python scripts/lineage-extract.py merge --db ./lineage.db
```

需要查某個 routine：

```bash
poetry run python scripts/lineage-extract.py report \
  --routine analytics.sp_x --db ./lineage.db > report.md
```

---

## 跨檢查：揭露「靜態 vs runtime」差異

`report` 模式底部會列出 `⚠ Source mismatch` 區塊。意義：

| 情境 | 解讀 |
|---|---|
| `jobs` only | sqlglot 漏抓了 → 多半是 `EXECUTE IMMEDIATE` 或 cross-project ref |
| `sqlglot` only | 程式碼有但 runtime 沒跑過 → 可能是死碼，或新加的 SP 還沒被排程觸發 |

→ **這份比對結果直接告訴你「未來要不要把 audit log lineage 拉進來」**。  
- 如果 80% routines 純 `sqlglot` 就抓得到 → audit log lineage 優先級低
- 如果一堆 `jobs` only → audit log 是必須

---

## 已知限制

1. **Adhoc query 不入庫**：只認 `CALL routine` 開頭的 job。直接跑 SQL（`INSERT ... SELECT ...`）不會被歸到任何 routine。可以擴充，但會產生噪音。
2. **跨專案 ref**：sqlglot 那邊會把 `other.ds.tbl` 解成 schema=`ds`、name=`tbl`，丟失 project 資訊。Phase 2 再強化。
3. **EXECUTE IMMEDIATE 動態字串**：sqlglot 抓不到（這是 BQ SP 普遍痛點，所有靜態工具都有），只能靠 jobs 模式補。
4. **180 天保留**：`INFORMATION_SCHEMA.JOBS` 是 BQ 系統限制，更長要靠 audit log sink。
5. **不偵測 SP → SP 呼叫鏈**：Harun 原版有，本版先省略。Phase 2 可加（sqlglot 的 `Call` 節點即可）。

---

## 升級成 Claude Code skill（仿 Harun 原始作法）

如果想把這工具包成 `/data-lineage` slash command：

```
.claude/skills/data-lineage/
  SKILL.md
  scripts/lineage-extract.py        ← 本工具
  examples/
    queries.md                      ← 預設查詢題目
```

提示詞範本（給 Claude Code 用）：

```
你有一個 lineage SQLite DB at .claude/skills/data-lineage/lineage.db。
schema 見 lineage-extract.py 的 SCHEMA_SQL。
使用者問「{question}」時，你的工作：
1. 用 sqlite3 直接查 DB 回答
2. 必要時跑 lineage-extract.py from-jobs 更新最近資料
3. 給 markdown 答案
```

→ 這部分非必要，phase1 主流程穩定後再評估。

---

## 跟 phase1-mvp.md 的關係

母設計 §7 「Lineage 切入設計（副線，但架構需好切入）」明確列出：

> Phase 1 就做的（成本≈0）：
> - ✅ Audit log sink 第 1 週開
> - ✅ Repo 路徑慣例穩定
> - ✅ Manifest.json 記錄

本工具相當於**把母設計 §7.2 的「Phase 2-3 才做的」提前做了一個雛形**，不取代 audit log sink，是並行的補充。
