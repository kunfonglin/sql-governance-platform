# sql-governance-platform

> 共用 SQL Governance 工具與 CI/CD 模組。供 N 個 `sql-governance-*` project repo 引用。

**範圍：** Phase 1, SP-only。
**版本：** v1.0
**對應設計：** [`bq-sql-governance-phase1-mvp.md`](../../docs/BQ_governance/bq-sql-governance-phase1-mvp.md)

---

## 結構

```
pyproject.toml                ← Poetry 依賴管理
poetry.lock                   ← 鎖死版本（commit 進 git）
.python-version               ← 指定 Python 3.11
.gitignore

.github/
  workflows/                  ← 給 project repo 引用的 reusable workflows
    reusable-pr-validate.yml
    reusable-deploy.yml
    reusable-nightly-drift.yml
  actions/                    ← composite actions
    auth-gcp/
    deploy-routines/
    dry-run-routines/
    write-manifest/
    notify-tg/
scripts/                      ← 共用 Python / shell 工具
  exporter.py
  drift-detector.py
  lineage-extract.py
  apply-routines.sh
  notify-tg.sh
  README-lineage.md
templates/                    ← 新專案 onboarding 樣板
  governance.yaml.tmpl
  README.md.tmpl
  PR_TEMPLATE.md.tmpl
  CODEOWNERS.tmpl
docs/
  sql-rules.md                ← 4 條 SQL 規範
  onboarding-new-project.md   ← 新 project repo 上線流程
  dev-setup.md                ← 平台維護者開發環境（Poetry / gcloud）
CHANGELOG.md
```

---

## 平台維護者開發

依賴用 Poetry 管理。完整步驟見 [`docs/dev-setup.md`](docs/dev-setup.md)。

```bash
cd phase1/platform
poetry install                          # 一次安裝
gcloud auth application-default login   # GCP 認證

poetry run python scripts/lineage-extract.py from-jobs \
  --project tapirus-test-384312 --region US --days 30 --db ./lineage.db
```

---

## Project repo 怎麼用

```yaml
# project repo 的 .github/workflows/deploy-prod.yml（thin wrapper）
name: deploy-prod
on:
  push:
    branches: [main]
jobs:
  deploy:
    uses: OWNER/sql-governance-platform/.github/workflows/reusable-deploy.yml@v1.0
    with:
      project_id: ${{ vars.PROJECT_PROD }}
      environment: production
      logical_project: marketing
      platform_repo: OWNER/sql-governance-platform
      platform_ref: v1.0
    secrets:
      WIF_PROVIDER: ${{ secrets.WIF_PROVIDER }}
      DEPLOY_SA: ${{ secrets.DEPLOY_SA }}
      TG_BOT_TOKEN: ${{ secrets.TG_BOT_TOKEN }}
      TG_CHAT_ID: ${{ secrets.TG_CHAT_ID }}
```

完整 onboarding：see [docs/onboarding-new-project.md](docs/onboarding-new-project.md).

---

## 版本管理

走 git tag。Project repo `uses: ...@v1.0` 鎖版本，平台升級時各 project 自選何時切：

| Tag | 變更 |
|-----|------|
| v1.0 | Phase 1 初版 — SP-only |
| v1.1+ | 修 bug / 補規格（不破壞既有 project） |
| v2.0+ | Breaking change（如加 tables 支援會影響 governance.yaml schema） |

完整變更見 [CHANGELOG.md](CHANGELOG.md).

---

## 引用權限

Platform 與 project repo 建議放同一個 GitHub org 下，全 private，引用零障礙。
跨 org 引用見 GitHub 官方文件 (Organization → Settings → Actions → Allow actions from selected repositories)。
