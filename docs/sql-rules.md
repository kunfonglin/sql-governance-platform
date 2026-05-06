# SQL 撰寫規範（Phase 1, SP-only）

> 適用於所有 sql-governance-* project repo。Phase 1 範圍：Stored Procedure / Function。Tables / Views / Migrations 屬未來擴充。

---

## 規則 1：檔案路徑

```
bigquery/{dataset}/routines/{routine_name}.sql
```

- `{dataset}` 是 BQ dataset 名（不是 GCP project name）
- 一個檔對應一個 SP / FN
- 檔名 = routine 名（含底線、不含括號）
- 不要有 `tables/` 或 `views/` 子目錄（Phase 1 不處理）

✅ `bigquery/analytics/routines/sp_build_daily_summary.sql`
❌ `bigquery/sandbox-prod/analytics/routines/sp_xxx.sql`（多了 project 層）
❌ `bigquery/analytics/sp_xxx.sql`（少了 routines 層）

---

## 規則 2：使用 `CREATE OR REPLACE`（冪等）

SP / FN 必須用 `CREATE OR REPLACE PROCEDURE` / `CREATE OR REPLACE FUNCTION`，這樣 CI 跑第 N 次都安全。

```sql
CREATE OR REPLACE PROCEDURE `analytics.sp_build_daily_summary`(IN p_date DATE)
BEGIN
  ...
END;
```

❌ 不要用 `CREATE PROCEDURE ...`（沒 OR REPLACE 第二次部署會失敗）

---

## 規則 3：不寫 Project ID（跨專案例外）

**同 project 引用 → 省略 project id**

✅ `INSERT INTO \`analytics.daily_summary\` ...`
❌ `INSERT INTO \`my-prod-proj.analytics.daily_summary\` ...`

部署時 CI 用 `bq query --project_id=$TARGET` 設定預設 project，所以同 project 的物件**不需要也不應該**寫 project id。這讓同一份 SQL 可以同時部署到 test / prod。

**跨 project 引用 → 保留完整路徑 + 在 header 加 `-- cross-project:` 註解**

```sql
-- bigquery/analytics/routines/sp_with_cross_ref.sql
-- cross-project: other-project.shared_data.lookup_table

CREATE OR REPLACE PROCEDURE `analytics.sp_with_cross_ref`()
BEGIN
  SELECT * FROM `other-project.shared_data.lookup_table`;
END;
```

理由：
- header 註解讓 reviewer / lineage 工具一眼看出跨專案依賴
- 完整路徑保留，因為跨 project ref **必須**有 project id

---

## 規則 4：Standard SQL

全部用 Standard SQL。**禁用 Legacy SQL**。

`bq query --use_legacy_sql=false` 由 CI 統一控制；別在 SQL 裡放 `#legacySQL` directive。

---

## 補充：檔頭格式（推薦但非強制）

exporter 匯出時會自動加這幾行（人手寫時可省）：

```sql
-- bigquery/{dataset}/routines/{name}.sql
-- routine_type: PROCEDURE   (可省)
-- cross-project: foo.bar.baz   (有跨 project 才寫)

CREATE OR REPLACE PROCEDURE ...
```

---

## 違反規範會發生什麼

| 違規 | 阻擋層 |
|------|--------|
| 路徑放錯位置 | drift detector / exporter 找不到，report 列為孤兒 |
| 沒 `OR REPLACE` | 第二次 deploy 失敗，CI 中止 |
| 寫了同 project 的 project id | drift detector 規範化後仍可比對；但易造成跨環境問題 → reviewer 應 block |
| Legacy SQL | `bq query --use_legacy_sql=false` 直接 syntax error |

---

## FAQ

### Q: SP 之間 CALL 別的 SP 要寫 project id 嗎？

不用。`CALL \`analytics.sp_helper\`()` 即可。同 project 內不寫 project id 規則一致。

### Q: 我有 SP 是 prod 才有的（SQL Server 遷移工具），怎麼辦？

加進 `config/.governance.yaml` 的 `exclude.routines`：

```yaml
exclude:
  routines:
    - pattern: "*.sp_migrate_sqlserver_*"
      reason: "SQL Server 遷移歷史搬移工具"
      review_by: "2026-09-30"
      owner: "lin"
```

drift detector 會跳過比對；exporter 不會匯入。`review_by` 過期後會自動失效（強迫定期 review）。

### Q: 我要加 table / view / migration 怎麼辦？

Phase 1 不支援。請等未來擴充。
