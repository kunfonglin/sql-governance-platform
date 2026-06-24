# IAM + WIF 設定文件（給管理員 / 新 project onboarding 用）

> 每個 sql-governance-{project} repo 都需要在對應的 GCP project 設這套，一次性。
> 套這份的人需要：對應 GCP project 的 **Owner** 或 **IAM Admin** 權限。

---

## 1. 一張表看完要做什麼

| 項目 | 數量 / 細節 |
|---|---|
| 啟用 API | 5 個 |
| 建 Service Account | 1 個（命名規則：`{project_name}-deploy-sa`）|
| SA 給予的 GCP role | **3 個**（見下表） |
| 套用範圍 | test project + prod project（兩邊都給）|
| Workload Identity Pool | 1 個（`github-pool`，全 org 共用） |
| OIDC Provider | 1 個（`github-provider`） |
| Repo binding | 1 個 / repo（限定哪個 GitHub repo 可 impersonate SA） |

---

## 2. SA 需要的 IAM Roles（B 方案 — 平衡安全與功能）

| Role 顯示名稱 | Role ID | 為什麼需要 | 是 admin? |
|---|---|---|---|
| BigQuery Data Editor | `roles/bigquery.dataEditor` | 部署 SP / function（CREATE OR REPLACE）;讀 INFORMATION_SCHEMA | ❌ |
| BigQuery Job User | `roles/bigquery.jobUser` | 跑 query / dry_run | ❌ |
| BigQuery Resource Admin | `roles/bigquery.resourceAdmin` | drift detector 查 `INFORMATION_SCHEMA.JOBS_BY_PROJECT`（看其他 user 的 job 找出「誰改了 prod」） | ❌（不能改 IAM） |

### 為什麼不用 `roles/bigquery.admin`

`bigquery.admin` 包含**改 IAM 的權限**，這代表 SA 自己能授權自己更多權限 / 給別人權限 → 一旦 SA 認證憑證外洩，攻擊者可全面接管 BQ。
B 方案三個 role 的聯集**不含 IAM 修改**，外洩時 blast radius 受限。

### 為什麼需要 `resourceAdmin`

drift detector 要查 `INFORMATION_SCHEMA.JOBS_BY_PROJECT` 才能看到「人類使用者」對 prod 做的變更（例：誰手動 `DROP TABLE`）。這需要 `bigquery.jobs.listAll` 權限，只有 `resourceAdmin` 或 `admin` 才有。

如果不在意「找出是誰改的」，只要「知道 git ↔ prod 不一致」，可以省略此 role，只給前兩個。

---

## 3. WIF 機制示意

```
GitHub Actions runner（GitHub 機房內）
       │
       │ 1. 跟 GitHub OIDC service 拿 JWT
       │    內含: repo = kunfonglin/sql-governance-pilot
       │
       ▼
Google STS                   ← Workload Identity Pool 在這驗 JWT
       │
       │ 2. 簽章 OK → 發短期 GCP credential
       │    可 impersonate: pilot-deploy-sa@...
       │
       ▼
GCP API（BigQuery）          ← SA 用 B 方案的 3 個 role 操作
```

關鍵：**沒有任何 JSON key file**。GitHub 與 GCP 互相信任，每次 workflow 跑時換臨時 token。Token 只在 workflow 執行期間有效（通常 1 小時）。

---

## 4. 操作步驟（UI 版）

### 4.1 啟用 API

到 **APIs & Services → Library** 搜尋並啟用以下 5 個：

- IAM API
- IAM Service Account Credentials API
- Security Token Service API
- Cloud Resource Manager API
- BigQuery API（通常已啟用）

### 4.2 建 Service Account

**IAM & Admin → Service Accounts → CREATE SERVICE ACCOUNT**

| 欄位 | 值 |
|---|---|
| Name | `{project_name}-deploy-sa`（例：`pilot-deploy-sa`、`marketing-deploy-sa`）|
| Description | `Deploy SA for sql-governance-{project_name}` |
| Grant access | **跳過此步**（後續手動給）|

### 4.3 給 SA 三個 role

**IAM & Admin → IAM → GRANT ACCESS**

| 欄位 | 值 |
|---|---|
| Principal | `{project_name}-deploy-sa@{prod_project_id}.iam.gserviceaccount.com` |
| Roles | `BigQuery Data Editor`、`BigQuery Job User`、`BigQuery Resource Admin`（一次選 3 個） |

**對 prod 跟 test 兩個 project 都要做這步**（切換 project 後重複）。

### 4.4 建 Workload Identity Pool（每 GCP project 只需一次，全部 repo 共用）

**IAM & Admin → Workload Identity Federation → CREATE POOL**

Pool 設定：
| 欄位 | 值 |
|---|---|
| Name / Pool ID | `github-pool` |
| Display name | `GitHub Actions Pool` |

Provider 設定：
| 欄位 | 值 |
|---|---|
| Provider type | OpenID Connect (OIDC) |
| Provider name / ID | `github-provider` |
| Issuer URL | `https://token.actions.githubusercontent.com` |
| Attribute mapping | `google.subject = assertion.sub`<br>`attribute.repository = assertion.repository`<br>`attribute.repository_owner = assertion.repository_owner` |
| Attribute condition | `assertion.repository_owner == '{github_org_or_username}'` |

> **Attribute condition 很重要** — 限定只有指定 GitHub org / 個人帳號的 repo 可以用，避免別人猜到你的 SA 名稱就能 impersonate。

### 4.5 把特定 repo 綁到 SA

**IAM & Admin → Service Accounts → {sa-name} → PERMISSIONS tab → GRANT ACCESS**

| 欄位 | 值 |
|---|---|
| Principal | `principalSet://iam.googleapis.com/projects/{PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-pool/attribute.repository/{github_owner}/{repo_name}` |
| Role | `Workload Identity User` |

> `{PROJECT_NUMBER}` 在 GCP Console 首頁的 Project Info card 看；不是 project ID 是純數字。
> 這一步要**對每個新 project repo 重複**（每個 repo 一行 binding）。

---

## 5. 操作步驟（gcloud 版）

```bash
# === 變數 ===
PROJECT_PROD=tapirus-test-384312               # 改成你的 prod project
PROJECT_TEST=praxis-works-367201               # 改成你的 test project
SA_NAME=pilot-deploy-sa                        # 改成 {project_name}-deploy-sa
GH_OWNER=kunfonglin                            # 改成 GitHub org 或個人 handle
GH_REPO=sql-governance-pilot                   # 改成 project repo 名稱

SA="${SA_NAME}@${PROJECT_PROD}.iam.gserviceaccount.com"

# === 1. 啟用 API ===
gcloud services enable \
  iam.googleapis.com iamcredentials.googleapis.com sts.googleapis.com \
  cloudresourcemanager.googleapis.com bigquery.googleapis.com \
  --project=${PROJECT_PROD}

# === 2. 建 SA ===
gcloud iam service-accounts create ${SA_NAME} --project=${PROJECT_PROD} \
  --display-name="Deploy SA for sql-governance-${GH_REPO#sql-governance-}"

# === 3. 給 SA 三個 role（兩個 project 都給）===
for PROJ in ${PROJECT_PROD} ${PROJECT_TEST}; do
  for ROLE in roles/bigquery.dataEditor roles/bigquery.jobUser roles/bigquery.resourceAdmin; do
    gcloud projects add-iam-policy-binding ${PROJ} \
      --member="serviceAccount:${SA}" --role="${ROLE}"
  done
done

# === 4. WIF Pool（每 GCP project 只需一次）===
gcloud iam workload-identity-pools create github-pool \
  --project=${PROJECT_PROD} --location=global --display-name="GitHub Actions Pool"

# === 5. OIDC Provider ===
gcloud iam workload-identity-pools providers create-oidc github-provider \
  --project=${PROJECT_PROD} --location=global --workload-identity-pool=github-pool \
  --display-name="GitHub OIDC" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository,attribute.repository_owner=assertion.repository_owner" \
  --attribute-condition="assertion.repository_owner == '${GH_OWNER}'"

# === 6. Bind SA to specific repo ===
PROJECT_NUMBER=$(gcloud projects describe ${PROJECT_PROD} --format='value(projectNumber)')

gcloud iam service-accounts add-iam-policy-binding ${SA} --project=${PROJECT_PROD} \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-pool/attribute.repository/${GH_OWNER}/${GH_REPO}"

# === 7. 印出 GitHub Secrets 要用的兩個值 ===
echo "WIF_PROVIDER = projects/${PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-pool/providers/github-provider"
echo "DEPLOY_SA    = ${SA}"
```

PowerShell 版見 `phase1/platform/docs/onboarding-new-project.md`。

---

## 6. 驗證設定

### 6.1 SA 與 role 確認

```bash
# 看 SA 存在
gcloud iam service-accounts describe ${SA} --project=${PROJECT_PROD}

# 看 prod 給了什麼 role 給這個 SA
gcloud projects get-iam-policy ${PROJECT_PROD} \
  --flatten="bindings[].members" \
  --filter="bindings.members:serviceAccount:${SA}" \
  --format="table(bindings.role)"

# 應該看到 3 行：dataEditor / jobUser / resourceAdmin
```

### 6.2 WIF binding 確認

```bash
gcloud iam service-accounts get-iam-policy ${SA} --project=${PROJECT_PROD}
# 應該看到 workloadIdentityUser binding 指向 sql-governance-pilot
```

### 6.3 從 GitHub Actions 跑通

設好 GitHub Secrets 後，在 pilot repo 開個 PR，觀察 `pr-validate` workflow 是否能成功 auth GCP（不報 401 / 403）。

---

## 7. 撤銷 / 旋轉

### 撤銷某個 repo 的 access

```bash
gcloud iam service-accounts remove-iam-policy-binding ${SA} --project=${PROJECT_PROD} \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://.../attribute.repository/${GH_OWNER}/${GH_REPO}"
```

### 完全撤銷 SA

```bash
gcloud iam service-accounts delete ${SA} --project=${PROJECT_PROD}
```

刪 SA 後，所有 binding 自動失效，CI/CD 馬上停止運作。

---

## 8. 為什麼不用 JSON key file

- **無 key 外洩風險**：WIF 不存 long-lived key，token 1 小時就過期
- **無 key 輪換負擔**：不用每 90 天手動換 key
- **可撤銷**：刪 binding 即斷線，不像 key 要追蹤所有副本
- **GitHub 原生支援**：`google-github-actions/auth@v2` 內建 WIF
- **公司合規常見要求**：「禁止 SA key file」是常見的 SecOps policy

JSON key 仍可用於**本地開發**（個人電腦），但 CI/CD 一律走 WIF。

---

## 9. 常見錯誤

| 錯誤訊息 | 原因 | 解法 |
|---|---|---|
| `Permission 'iam.serviceAccounts.getAccessToken' denied` | repo binding 沒設或 attribute condition 不符 | 確認 §4.5 binding + §4.4 attribute condition `repository_owner` 對 |
| `Permission 'bigquery.jobs.create' denied` | SA 沒給 `jobUser` | 補 §4.3 |
| `Permission 'bigquery.routines.create' denied` | SA 沒給 `dataEditor` | 補 §4.3 |
| `INFORMATION_SCHEMA.JOBS_BY_PROJECT` 查不到別人的 job | 沒給 `resourceAdmin` | 補 §4.3（或接受 drift detector 受限） |
| GitHub Actions log: `400 Bad Request: Service account not found` | `DEPLOY_SA` secret 寫錯 | 重新檢查 GitHub Secrets 內容 |

---

## 10. 提供給未來 admin 的 checklist

```
[ ] 收到 GitHub repo URL: https://github.com/{owner}/{repo}
[ ] 對應 GCP project_id: prod=___, test=___
[ ] 讀過本文件 §2 確認 3 個 role 的合理性
[ ] 跑 §5 gcloud 腳本完成設定
[ ] 跑 §6 驗證
[ ] 把 §5.7 印出的 WIF_PROVIDER + DEPLOY_SA 給開發者設 GitHub Secrets
[ ] 文件存檔（誰申請、何時開、屬於哪個邏輯專案）
```
