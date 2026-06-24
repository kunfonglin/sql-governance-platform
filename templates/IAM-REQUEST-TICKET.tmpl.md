<!--
  IAM/WIF 權限申請單範本 — 新專案 onboarding 用
  從 platform/templates/ 複製後，把 __佔位__ 換成實值，貼給 IT / GCP 管理員。
  依據：docs/iam-wif-setup.md（B 方案三 role、禁用 SA key file）
-->
主旨：申請 sql-governance-__PROJECT_NAME__ 的 CI/CD 部署權限（WIF，無 SA key）

【GCP】
- prod project_id：__PROJECT_PROD__
- test project_id：__PROJECT_TEST__
- BQ region：__REGION__

- 需建 Service Account（建在 prod 專案）：
    __PROJECT_NAME__-deploy-sa@__PROJECT_PROD__.iam.gserviceaccount.com

- 給該 SA 三個 role，且 test + prod 兩邊都給（B 方案，不含改 IAM）：
    roles/bigquery.dataEditor      # 部署 SP/FN/View；讀 INFORMATION_SCHEMA
    roles/bigquery.jobUser         # 跑 query / dry-run
    roles/bigquery.resourceAdmin   # drift 查 JOBS_BY_PROJECT（找誰改了 prod）
  ※ 不要給 roles/bigquery.admin（含改 IAM，外洩 blast radius 過大）

- Workload Identity：
    建 pool: github-pool + OIDC provider: github-provider
    issuer: https://token.actions.githubusercontent.com
    attribute condition: assertion.repository_owner == '__PROJECT_OWNER__'

- repo binding（限定只有這個 repo 能 impersonate 該 SA）：
    repo = __PROJECT_OWNER__/__PROJECT_REPO__
    role = roles/iam.workloadIdentityUser

【需要啟用的 API（prod 專案）】
  iam.googleapis.com  iamcredentials.googleapis.com  sts.googleapis.com
  cloudresourcemanager.googleapis.com  bigquery.googleapis.com

【請回傳給我兩個值（設 GitHub Secrets 用）】
- WIF_PROVIDER = projects/<PROJECT_NUMBER>/locations/global/workloadIdentityPools/github-pool/providers/github-provider
- DEPLOY_SA    = __PROJECT_NAME__-deploy-sa@__PROJECT_PROD__.iam.gserviceaccount.com

【執行腳本】管理員可直接套 docs/iam-wif-setup.md §5 的 gcloud 腳本（已參數化）。
