# CHANGELOG — sql-governance-platform

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
