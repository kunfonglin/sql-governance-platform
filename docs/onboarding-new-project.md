# 新專案 Onboarding（Phase 1, SP-only）

> 從 0 到第一次 prod deploy 約 30–60 分鐘（不含基線匯入時間）。

## 0. 需要先準備的資訊

- [ ] 邏輯專案名（例：`marketing` / `finance`）
- [ ] GCP project_id（test / prod 各一）
- [ ] BQ region（如 `asia-east1`）
- [ ] Project owner GitHub handle
- [ ] Approver 名單（至少 1 人能 approve prod environment）
- [ ] TG bot token + chat_id（可共用 platform 的，也可獨立）

---

## 1. 在 GCP 建 SA + WIF

```bash
PROJECT_NAME=marketing                     # 邏輯名
PROJECT_PROD=my-${PROJECT_NAME}-prod       # GCP project id

# 建 deploy SA
gcloud iam service-accounts create ${PROJECT_NAME}-deploy-sa \
  --project=${PROJECT_PROD} \
  --display-name="SQL Governance deploy SA for ${PROJECT_NAME}"

# 給 BQ admin
gcloud projects add-iam-policy-binding ${PROJECT_PROD} \
  --member=serviceAccount:${PROJECT_NAME}-deploy-sa@${PROJECT_PROD}.iam.gserviceaccount.com \
  --role=roles/bigquery.admin

# 同樣對 test project 設 SA（可同 SA 共用，或另開一個）
```

WIF 設定（platform 通常已建好 pool；只要為新 repo 加 attribute condition）：

```bash
gcloud iam service-accounts add-iam-policy-binding \
  ${PROJECT_NAME}-deploy-sa@${PROJECT_PROD}.iam.gserviceaccount.com \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/attribute.repository/OWNER/sql-governance-${PROJECT_NAME}"
```

---

## 2. 建 Audit Log Sink

```
Cloud Logging → Log Router → Create Sink
  Name: governance-audit-sink
  Destination: BigQuery dataset
  Dataset: governance_audit (建在 ${PROJECT_PROD})
  Filter:
    resource.type="bigquery_resource"
    AND (
      protoPayload.methodName=~"google.cloud.bigquery.v2.RoutineService.(Update|Insert|Delete)Routine"
      OR protoPayload.methodName="jobservice.jobcompleted"
    )
  保留: 90 天
```

---

## 3. 建 GitHub repo

```bash
# 從 platform 的 templates 複製 + 客製
gh repo create OWNER/sql-governance-${PROJECT_NAME} --private
cd $(gh repo clone OWNER/sql-governance-${PROJECT_NAME})

# 複製樣板（手動或寫 onboarding 腳本）
cp -r ../sql-governance-platform/templates/governance.yaml.tmpl config/.governance.yaml
cp ../sql-governance-platform/templates/README.md.tmpl       README.md
cp ../sql-governance-platform/templates/PR_TEMPLATE.md.tmpl  .github/PULL_REQUEST_TEMPLATE.md
cp ../sql-governance-platform/templates/CODEOWNERS.tmpl      .github/CODEOWNERS

# 把佔位字串換掉
sed -i "s/__PROJECT_NAME__/${PROJECT_NAME}/g" config/.governance.yaml README.md .github/CODEOWNERS
sed -i "s/__PROJECT_TEST__/my-${PROJECT_NAME}-test/g" config/.governance.yaml
sed -i "s/__PROJECT_PROD__/my-${PROJECT_NAME}-prod/g" config/.governance.yaml
sed -i "s|__PLATFORM_OWNER__|OWNER|g" config/.governance.yaml README.md
sed -i "s/__OWNER__/@owner-handle/g" README.md .github/CODEOWNERS
sed -i "s/__PLATFORM_TEAM__/@platform-team/g" .github/CODEOWNERS
sed -i "s/__PLATFORM_REF__/v1.0/g" README.md
```

---

## 4. 建 4 個 thin wrapper workflows

複製 `phase1/pilot/.github/workflows/*` 樣板（這 4 個檔都很短，每個 < 30 行）。

關鍵：把 `uses:` 路徑改成正確的 platform repo 與 ref：

```yaml
uses: OWNER/sql-governance-platform/.github/workflows/reusable-deploy.yml@v1.0
```

---

## 5. 設 GitHub Environment + Secrets

```
Settings → Environments → New environment
  Name: production
  Required reviewers: <approver>
  Deployment branches: main only

Settings → Environments → New environment
  Name: test
  (no protection)

Settings → Secrets and variables → Actions
  Repository secrets:
    WIF_PROVIDER       (= projects/.../providers/github)
    DEPLOY_SA          (= ${PROJECT_NAME}-deploy-sa@...iam.gserviceaccount.com)
    TG_BOT_TOKEN       (optional)
    TG_CHAT_ID         (optional)

  Repository variables:
    PROJECT_TEST       (= my-${PROJECT_NAME}-test)
    PROJECT_PROD       (= my-${PROJECT_NAME}-prod)
    REGION             (= asia-east1)
```

---

## 6. 跑基線匯入

```bash
# 在 platform repo 跑
cd sql-governance-platform
python scripts/exporter.py \
  --project my-marketing-prod \
  --region asia-east1 \
  --output ../sql-governance-marketing/bigquery \
  --config ../sql-governance-marketing/config/.governance.yaml

cd ../sql-governance-marketing
git add .
git commit -m "chore: initial baseline import"
git push origin main
```

人工 review 匯出結果，把不該納入的 SP 加進 `exclude.routines`，重跑 exporter。

---

## 7. 端到端驗證

1. 開 feature branch，改一個 SP
2. PR feature → development → 觀察 pr-validate 跑綠
3. Merge to development → 觀察 deploy-test 部署到 test
4. PR development → main → reviewer approve
5. Merge to main → environment approval gate 暫停 → approver approve → deploy-prod 跑完
6. 看 `audit/deploys/...-manifest.json` 是否寫入
7. （可選）手動在 prod 改一個 SP，等隔天 03:00 看 drift 報告 / TG 是否抓到

---

## 8. 完成

- [ ] 全 prod routines 在 git
- [ ] PR → dev → prod approval → deploy 全流程通過 1 次
- [ ] Drift detector 能抓到至少 1 筆人工製造變更
- [ ] Audit log 流入 governance_audit
- [ ] README + CODEOWNERS 已客製化
- [ ] Approver 名單已通知

---

## 升級 Platform 版本

當 platform 發布新版（例如 v1.1）：

```yaml
# config/.governance.yaml
platform:
  ref: v1.1                   # ← 改這裡
```

```yaml
# .github/workflows/*.yml
uses: OWNER/sql-governance-platform/.github/workflows/reusable-deploy.yml@v1.1
```

開 PR → 走標準流程驗證。Major 版（v1 → v2）通常含 breaking change，請先讀 CHANGELOG。
