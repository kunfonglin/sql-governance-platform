# CHANGELOG — sql-governance-platform

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
