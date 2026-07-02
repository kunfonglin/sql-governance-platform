# CHANGELOG — sql-governance-platform

## v1.6 (2026-07-01)

### Changed — project-id-lint 擴到 migrations/ + exporter 反斜線修補 + drift 對稱 exclude
- **`reusable-deploy.yml` / `reusable-pr-validate.yml`**：project-id-lint 除了 `bigquery_root`，現在也掃 `migrations/`（migration 硬寫自家 project id 也會被擋）。
- **`scripts/project-id-lint.py`**：加「路徑不存在則略過」防護（掃 migrations/ 缺目錄時不崩）。
- **`scripts/exporter.py`**：修 bq CLI 對含反斜線 DDL（regex）輸出不合法 JSON 導致匯出崩潰的 bug（`_repair_lone_backslashes` 逐字掃描，只在嚴格解析失敗時啟動）。
- **`scripts/drift-detector.py`**：修**單邊 exclude bug**——`exclude.datasets` 原本只套在 live（prod）側、git 側沒濾 → 排除「git 也有追蹤」的 dataset 時，該 dataset 的 git 檔全變假 `not_deployed`（也 orphan/content 不對稱）。改成 **git 側也套 dataset exclude**（routines/views/tables 三處對稱）。

### Breaking changes
- **無**。input/secret 介面不變；lint 多掃一個目錄，行為向後相容。

### Migration guide (v1.5 → v1.6)
1. project repo wrapper：`@v1.5` → `@v1.6`、`platform_ref: v1.6`
2. pilot 不動

### 決策紀錄（2026-07-01）
- **destructive-lint 不接線**：操作者評估效益有限、增開發者困擾；破壞型改靠 time-travel（2–7 天）+ prod approval gate 人工核實 +「誰觸發誰負責」。
- **bq-deploy adapter 退役**：新版同事 `bq-schema-change` skill 直接寫最終格式（剝 id + 冪等 + 分流）進 repo，adapter 的格式正規化已多餘。

## v1.5 (2026-06-25)

### Changed — 部署順序修正（依相依關係）
- **`reusable-deploy.yml` 部署順序 `routines → migrations → views` 改為 `migrations → routines → views`**
  - 根因：`CREATE OR REPLACE PROCEDURE` 預設 `strict_mode` 會驗證 SP body；當 SP 引用「同一 release 新建」的表時，舊順序 SP 先於表部署 → `Not found` 失敗（pr-validate dry-run 與 prod/clean 環境首次部署都會踩到）
  - 新順序符合相依鏈：**表(migrations) → routines(SP/UDF) → views**，SP 在引用的表已存在後才建立，**保留 strict 驗證**（不需 `strict_mode=false`）
  - manifest 仍由 deploy-routines 產出、之後 patch（migrations 的 `applied_json` 為 step output，重排不影響）

### Breaking changes
- **無**。單一 deploy job 內 step 重排，input/secret 介面不變。

### Migration guide (v1.4 → v1.5)
1. project repo wrapper：`@v1.4` → `@v1.5`、`platform_ref: v1.5`
2. **pilot 不動**（仍 `@v1.1`）

### 已知限制（本次不修，列 backlog）
- **pr-validate 仍唯讀、不建表** → 「同一 PR 新增 table + 新 SP 引用該 table」時，SP dry-run 仍會因表尚未建而紅。規避＝先在 test 開發建表（dev-on-test）再發 PR。
- 正式把關強化（含手寫 migration 的 project-id-lint / destructive-lint / migration dry-run）見 self-authored gating P1-P4，另排 backlog。

---

## v1.4 (2026-06-23)

### Added — Views 進部署 + PR 驗證
- **新 script** `apply-views.sh`：對 `bigquery/*/views/*.sql` 跑 `CREATE OR REPLACE VIEW`，two-phase 容錯（沿用 apply-routines 模式，解 view 互相引用）
  - View 是無資料物件 → `CREATE OR REPLACE VIEW` 非破壞，跟 SP 同性質、可全量重佈；**與 table 不同**（table 走 migration，因 `CREATE OR REPLACE` 會掉資料）
- **新 composite action** `deploy-views`、`dry-run-views`
- **`reusable-deploy.yml`**：在 `apply-migrations` 之後新增 **Deploy views** step（順序：routines → migrations → views，確保 view 引用的 table 已由 migration 建好）；manifest 補 `deployed.views`
- **`reusable-pr-validate.yml`**：新增 **Dry-run changed views** step（壞掉的 view SQL 在 merge 前擋下）

### Added — Drift 擴充到 views + tables
- **`drift-detector.py`** 新增 `--include-views`：抓 `INFORMATION_SCHEMA.TABLES`（table_type='VIEW'）的 ddl，與 git `views/*.sql` 走同一套 DDL diff
- **`drift-detector.py`** 新增 `--include-tables`：抓 `INFORMATION_SCHEMA.COLUMNS` 比 git `tables/*.sql` 的**欄位名 + 型別 + nullable**（用 sqlglot 解析；**不比整段 DDL / partition / cluster**，避免格式誤報）
  - 抓「有人偷改資料表欄位」：drift 同時涵蓋 prod 多欄 / 缺欄 / 型別變更 / nullable 變更（NULLABLE↔NOT NULL，會影響下游資料流）
  - ARRAY/REPEATED 欄位 BQ 一律報 NOT NULL → 兩邊強制當非 nullable，避免誤報
  - audit-log「誰改的」查詢擴充 statement_type（CREATE/ALTER/DROP VIEW·TABLE）→ table/view drift 也能查到修改人
  - CTAS（無明確欄位）與無法解析的 table 會略過並警告，不中斷 drift run
- **`reusable-nightly-drift.yml`**：新增 `check_views` / `check_tables` input（**預設 false，向後相容**）；`check_tables` 時自動 `pip install sqlglot`
- Drift report 新增 **Object** 欄（routine/view/table），diff preview 涵蓋 table 欄位變更

### Changed
- 3 個 reusable workflow 預設 `platform_ref` → `v1.4`
- `governance.yaml.tmpl` 的 `exclude` 補 `views: []` / `tables: []`；`drift_check.scope` 補 views/tables 註解

### Breaking changes
- **無**。views 部署：沒有 `views/` 目錄則 no-op；drift 兩個新 flag 預設關。既有 routines/migrations 行為完全不變

### Migration guide (v1.3 → v1.4)
1. Project repo 的 wrapper：`@v1.3` → `@v1.4`、`platform_ref: v1.4`
2. （選用）要開 view/table drift：在 `nightly-drift` wrapper 的 `with:` 加 `check_views: true` / `check_tables: true`
3. View 開發：放 `bigquery/{dataset}/views/{view}.sql`，內容 `CREATE OR REPLACE VIEW`（不含 project id，照 routines 同規範）
4. Table 結構快照放 `bigquery/{dataset}/tables/{table}.sql`（drift 比對基準），實際變更走 `migrations/`（不變）
5. **pilot 不動**：仍釘 `@v1.1`

---

## v1.3 (2026-06-18)

### Fixed
- **private 平台 repo 的 checkout 認證**：3 個 reusable workflow 的「Checkout platform repo」步驟改帶 `token: ${{ secrets.PLATFORM_READ_TOKEN || github.token }}`
  - 根因：reusable workflow 跑起來時預設 `GITHUB_TOKEN` 綁 **caller repo**，對 **private 的 `sql-governance-platform`** 沒有 contents:read → `actions/checkout` 回 `Repository not found`（fatal exit 128）
  - 註：org「Actions access」只放行 **`uses:` 引用** reusable workflow / action，**不等於**放行 `actions/checkout` 整個 repo clone — 兩種權限
  - pilot v1.1 不受影響（個人 sandbox 平台 repo 是 public，免 token）

### Added
- 3 個 reusable workflow 新增可選 secret `PLATFORM_READ_TOKEN`（`required: false`）

### Breaking changes
- **無**。`token` 用 `|| github.token` fallback：不傳 `PLATFORM_READ_TOKEN` 則行為與 v1.2 一致（適用 public 平台 repo）

### Migration guide (v1.2 → v1.3)
1. **建 PAT**：fine-grained PAT，僅 `sql-governance-platform` 的 **Contents: Read-only**
2. **設 secret**：在 org（或各 project repo）設 `PLATFORM_READ_TOKEN` = 該 PAT
3. Project repo 的 4 個 wrapper workflow：`@v1.2` → `@v1.3`、`platform_ref: v1.3`、`secrets:` 區塊加 `PLATFORM_READ_TOKEN: ${{ secrets.PLATFORM_READ_TOKEN }}`
4. **pilot 不動**：仍釘 `@v1.1`

---

## v1.2 (2026-06-18)

### Added
- **可選 self-hosted runner 支援**：3 個 reusable workflow（`reusable-pr-validate` / `reusable-deploy` / `reusable-nightly-drift`）新增 `runs_on` input
  - `runs-on: ubuntu-latest` → `runs-on: ${{ inputs.runs_on }}`
  - 預設 `ubuntu-latest`（**向後相容**，不傳就跟 v1.1 行為一致）
  - 專案要走自架 runner 時傳 `runs_on: self-hosted`
  - 動機：公司 org 開了 **IP allow list**，GitHub 託管 runner 動態 IP 被 403 擋下；自架 runner（固定 IP 進白名單）是官方解

### Changed
- 3 個 reusable workflow 預設 `platform_ref` 統一 → `v1.2`

### Breaking changes
- **無**。純 additive：`runs_on` 不傳則行為不變

### Migration guide (v1.1 → v1.2)
1. Project repo 的 4 個 wrapper workflow `uses:` 改 `@v1.1` → `@v1.2`、`platform_ref: v1.2`
2. 要用自架 runner 的專案：在每個 wrapper 的 `with:` 加 `runs_on: self-hosted`
3. **前置（非平台可控，需各自處理）**：
   - 自架 runner 機器（建議 Linux，對外固定 IP）由 **IT 加進 org IP allow list**
   - runner 裝 `git` + `python3`（`gcloud`/`bq` 由 `setup-gcloud` action 執行時自動裝）
   - runner 跑成服務、保持 Idle 在線（離線則 job 排隊等待）
4. **pilot 不動**：仍釘 `@v1.1`，保留可重現性

---

## v1.1 (2026-05-07)

### Added
- **兩階段部署**：`apply-routines.sh` 改成「失敗自動 retry，無進展才放棄」
  - 開發者**不再需要**對 orchestrator SP 加 `OPTIONS(strict_mode=false)`
  - 一般 1-2 pass 跑完，循環依賴會被偵測報錯
- **Migration 機制（簡化版）**：`apply-migrations.sh` + `migrations_applied` ledger
  - 支援 DROP SP（情境 C 的標準路徑）
  - Ledger 表 schema 見 `docs/two-phase-deploy-and-migration-spec.md` §3.4
  - 簡化版不含 checksum / require_manual_step（v2.0 補）
- **新 composite action**：`apply-migrations`
- **`reusable-deploy.yml` 新增 step**：Apply migrations + 把 manifest 補上 `migrations_applied` 欄位
- **Manifest schema bump v1 → v2**：新增 `deployed.migrations_applied` 與 `deploy_passes` 欄位
- **新文件**：`two-phase-deploy-and-migration-spec.md`

### Changed
- `sql-rules.md` 規則 5 更新：strict_mode=false 只在 EXECUTE IMMEDIATE 才需要
- `sql-rules.md` 新增規則 6：刪除 SP 走 migration
- `reusable-deploy.yml` 預設 `platform_ref` 從 `v1.0` → `v1.1`

### Breaking changes
- **無，但需要前置設定**：使用前必須在 test + prod 兩個 GCP project 各自建好 `governance_audit.migrations_applied` ledger 表（DDL 見 spec §3.4 / onboarding 文件）
- Project repo 的 wrapper workflow 升級時：`uses:` 路徑改 `@v1.0` → `@v1.1`

### Migration guide (v1.0 → v1.1)
1. 在每個 GCP project（test + prod）建 `governance_audit.migrations_applied` ledger 表
2. Project repo 的 4 個 wrapper workflow 改 `@v1.0` → `@v1.1`
3. （可選）拿掉既有 SP 上的 `OPTIONS(strict_mode=false)`，改靠兩階段部署解依賴
4. 開始用 `migrations/` 目錄走 DROP SP 流程

---

## v1.0 (Phase 1 初版)

### Added
- Reusable workflows: `reusable-pr-validate.yml`, `reusable-deploy.yml`, `reusable-nightly-drift.yml`
- Composite actions: `auth-gcp`, `deploy-routines`, `dry-run-routines`, `write-manifest`, `notify-tg`
- Scripts: `exporter.py`, `drift-detector.py`, `apply-routines.sh`, `notify-tg.sh`
- Templates: `governance.yaml.tmpl`, `README.md.tmpl`, `PR_TEMPLATE.md.tmpl`, `CODEOWNERS.tmpl`
- Docs: `sql-rules.md`, `onboarding-new-project.md`, `iam-wif-setup.md`, `wif-walkthrough.md`

### Scope
- Routines (SP / FN) only
- 不含 tables / views / migrations / lineage（為未來擴充）

### Known issues fixed during Phase 1 sandbox testing
- ROUTINES_JSON unbound variable (apply-routines.sh manifest writer)
- Wrapper workflow permissions block missing
- Various Chinese encoding issues (PowerShell vs UTF-8)

---

## (template for future entries)

## v1.x
### Added
### Changed
### Fixed
### Breaking changes (none expected for minor bumps)

## v2.0
### Added
- Tables / views deployment（state-based with two-phase）
- Migration 完整版：checksum / requires_manual_step / dry_run pre-validation
- 開發者 prod admin 收緊
### Breaking changes
- TBD
