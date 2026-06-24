# BQ 治理 / SQL 版控工具市場比較

> 這份是「**我們在地圖上的位置**」整理。為什麼自建、市面上有什麼選項、各自適合什麼場景。
> 給 architect review / 簡報補充用、給未來成員理解設計脈絡。

---

## TL;DR — 一張定位圖

```
                  state-based ─────────────── migration-based
                  (git = 完整目標狀態)        (git = 變更步驟序列)
                       │
                       │
            ┌──────────┼──────────┐
            │          │          │
        Dataform     我們      Flyway
        dbt              ↑      Liquibase
                    state-based 為主
                    + migration ledger 補 DROP/ALTER
                    + 自寫 drift detector
                    + 為 BQ Stored Procedure 量身打造
```

**一句話定位**：
> **「我們 ≈ 為 BQ Stored Procedure 量身打造的 mini-Dataform，加上 Flyway 風格的 ledger 補不擅長的 DROP/ALTER，加上自己寫的 drift detector」**

---

## 兩種部署模型的根本差異

| 維度 | State-based | Migration-based |
|---|---|---|
| Git 裡放什麼 | 「目標狀態」的完整定義（每支 SP 完整 DDL） | 「變更」的順序序列（add column / drop SP 等） |
| 部署邏輯 | 跑 CREATE OR REPLACE 全部、讓 BQ 變成 git 的狀態 | 跑沒跑過的 migration 序列 |
| 閱讀友善度 | 高（看一支 SP 直接知道現在長什麼樣） | 中（要追 migration 序列才知道目前狀態） |
| 對 DROP / RENAME 友善度 | 低（state 無法表達動作） | 高（migration 天生就是動作） |
| 對 CREATE OR REPLACE 友善度 | 高 | 中（每改一次寫一個 migration 累） |
| 自癒（prod 手動改會被覆蓋） | 是 | 否 |
| 重跑安全 | 是（冪等） | 視 migration 寫得冪等與否 |

---

## 完整工具比較表

| 工具 | 模型 | 適合場景 | 對 BQ SP 友善 | 跟我們的相似度 |
|---|---|---|---|---|
| **我們 (sql-governance-platform)** | State + Migration hybrid | BQ SP/UDF 為主、團隊小 | 🟢 100%（為此設計） | — |
| **Dataform** | State + DAG | BQ 為主、SQLX pipeline | 🔴 SP 是二等公民（Issue #1151 closed not planned） | **85%** |
| **dbt** | State + DAG（hash 算改動） | 跨資料庫 model、analytics | 🔴 設計給 SELECT model | **70%** |
| **Flyway** | Migration-only | 傳統 RDBMS schema 變更 | 🟡 BQ plugin 是 community 品質、不穩 | **30%**（migration ledger 借鑑） |
| **Liquibase** | Migration-only（YAML/XML/SQL） | 跨資料庫、企業合規 | 🟡 同 Flyway | **30%** |
| **Sqitch** | Migration + 依賴圖 | 依賴複雜的 schema | 🟡 BQ 支援有限 | **25%** |
| **Atlas** | State + Plan/Apply（Terraform 風格） | schema 版控（像 Terraform of DB） | 🟡 BQ adapter 新 | **50%** |
| **Schemachange** | Migration-only | Snowflake 專屬 | ❌ Snowflake only | N/A |
| **Terraform + BQ provider** | State + Plan/Apply | 基礎設施（dataset 建立）非 SP | ❌ 不適用 SP code | **40%**（CI 思路像） |
| **Alembic** | Migration（Python 生態） | SQLAlchemy + Python 專案 | ❌ 不適用 BQ | N/A |

---

## 三大 state-based 工具直接對比（我們 vs Dataform vs dbt）

| 維度 | 我們 | Dataform | dbt |
|---|---|---|---|
| **開發 IDE** | BQ Studio Repository 或本地 IDE | Dataform Web UI / VS Code | dbt Cloud / VS Code |
| **程式語言** | 純 SQL | SQLX（SQL + JavaScript 模板） | SQL + Jinja |
| **Stored Procedure 友善度** | 🟢 為此打造 | 🔴 差（SP 二等公民） | 🔴 差（不在設計目標） |
| **Hash-based 增量部署** | ⬜ Phase 1.5 待做 | 🟢 內建 | 🟢 內建 |
| **Migration（DROP/ALTER）機制** | 🟢 有 ledger 表 | 🟡 弱 | 🟡 弱 |
| **依賴 DAG / 拓樸序部署** | ⬜ lineage 工具雛形已有 | 🟢 內建 | 🟢 內建 |
| **Drift detection** | 🟢 自寫 | 🟡 弱 | 🟡 弱 |
| **Vendor lock-in** | 無 | 中（Google 工具） | 低（OSS） |
| **跨資料庫** | BQ-only | BQ-only | 高（多種） |
| **Approval gate 整合 GitHub** | 🟢 用 GitHub Environment | 🟡 自家流程、不易與 GitHub PR review 結合 | 🟡 同 |
| **學習曲線** | 低（純 SQL + 4 個 button） | 中（要學 SQLX） | 高（要學 dbt 模型 / Jinja） |
| **適合團隊規模** | 小到中（10 人內） | 中到大（資料工程團隊） | 大（dbt 社群成熟） |

---

## 為什麼我們沒直接用 Dataform

評估細節見 `docs/BQ_governance/02-dataform-core-concepts.md` 跟 `03-stored-procedures-on-dataform.md`，重點：

| 問題 | 說明 |
|---|---|
| **SP 是二等公民** | Dataform Issue #1151 「支援 SP 為 first-class」被 closed not planned，未來路線圖明確不會做 |
| **動態 SQL 不支援** | `EXECUTE IMMEDIATE` 等 SP 常用功能 Dataform 解析不了 |
| **Approval gate 弱** | Dataform 的 approval 在 Google 自家流程內、不易跟 GitHub PR review / Environment 結合 |
| **Vendor lock-in** | 設計依賴 Google 持續投資；歷史上類似工具被 sunset 過（如舊版 Dataflow Templates 等） |
| **過度設計** | Dataform 給的是 SQLX / DAG / 增量 model 等全套，對「SP-only 版控」場景大材小用 |

選**自建 mini-tool**的好處：
- 程式碼 < 2000 行、5 個檔
- 完全自己 own、隨時可改
- 對 BQ SP 友善
- 學習成本低（純 SQL + git + GitHub workflow）

---

## 為什麼我們沒選 Flyway / Liquibase

雖然 ledger 機制借鑑自它們，但**整套 migration-based** 對 SP 場景太重：

| 問題 | 說明 |
|---|---|
| **每改一次 SP body 寫一個 migration** | 對「改 SP 內邏輯」這類高頻情境太囉嗦 |
| **看不到 SP 當前長什麼樣** | 要追完所有 migration 才能拼湊出當前 SP body |
| **沒有 self-healing** | prod 被手動改不會被修正、需要另寫 drift |
| **BQ adapter 品質一般** | Flyway 的 BQ plugin 是 community contribution、不是官方支援、踩雷率高 |

我們**抽出 migration 機制的 ledger 概念**（用來追 DROP / RENAME），核心仍用 state-based。**最好的兩半都拿到**。

---

## 何時應該考慮換工具

如果你以後遇到下面任一情境，可能要重新評估：

| 情境 | 建議重新看的工具 |
|---|---|
| 變得不只管 BQ、也要管 Postgres / MySQL / Snowflake | dbt / Liquibase / Atlas |
| SP 數爆增到 5000+、自建工具維護成本變高 | Dataform（接受 SP 友善度不足） |
| 公司要強制走「跨資料庫合規工具」 | Liquibase（企業常見） |
| Pure analytics 用、不再用 SP、改用 dbt-style models | dbt |
| 要管 dataset / IAM / schema 等基礎設施 | Terraform + BQ provider |

短期內（300-600 SP、BQ-only、SP 為主）**我們自建路線是最佳解**。

---

## 已知未來優化方向

| 項目 | 狀態 | 對應 memory |
|---|---|---|
| Hash-based incremental deploy | ⬜ Phase 1.5 提案中 | `phase1_5_hash_based_deploy.md` |
| 拓樸序部署（依 SP→SP 依賴排序） | ⬜ Phase 2 評估 | lineage 工具已有依賴圖、可接 |
| Pre-deploy drift gate（部署前先比對 prod） | ⬜ 未決 | `cicd_phase1_status.md` 未決議題 |
| Audit log sink | ⬜ Phase 2 | 等 GitHub 通公司網段 |
| Table / View 部署（不只 SP） | ⬜ Phase 2 | exporter 已能匯出 DDL、目前 `.gitignore` 過濾 |

---

## 這份文件的更新原則

當市面上工具有重大變化（例：Dataform 突然支援 SP first-class）或我們設計有大改（例：真的接 dbt）時更新。**不需要每月維護**。
