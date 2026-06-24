# 給 GCP 管理員的簡報：WIF + SA 申請

> 給管理員 5 分鐘看完的速覽。詳細逐步 UI 操作見 [`iam-wif-setup.md`](iam-wif-setup.md)。

---

## 我們在請求什麼

對每個邏輯專案（共 9 個），在它的 **prod + test GCP project** 各做一次：

1. 建 1 個 Service Account（`{project-name}-deploy-sa`）
2. 給這支 SA 3 個 BigQuery role（**不含 admin**，最小權限原則）
3. 全 org 共用 1 個 Workload Identity Pool + 1 個 OIDC Provider
4. 把 SA 綁定到指定 GitHub repo（一個 SA = 一個 repo，attribute condition 強制）

---

## 為什麼這樣做：JSON key file vs WIF

| | JSON Key File（傳統） | Workload Identity Federation（我們選這個） |
|---|---|---|
| Key 在哪 | 平面檔案，可能存在筆電 / Slack / git | 沒有 key，token 動態換 |
| 外洩風險 | 高（永久有效、難追蹤副本） | 低（token 1 小時過期） |
| 輪換負擔 | 每 90 天人工換 | 不用 |
| 撤銷 | 要找出所有副本確保刪光 | 刪 binding 立即斷線 |
| 公司 SecOps 偏好 | 通常**禁用** | 通常**推薦** |

---

## 安全性風險評估

### 🟢 完全安全（無 attack surface）

| 項目 | 為什麼安全 |
|---|---|
| 建立 Service Account 本身 | 只是建一個身份，沒給任何權限 |
| 建立 WIF Pool | 純命名空間，不授權任何東西 |
| 建立 OIDC Provider | 只是註冊「GitHub OIDC 是合法 token 來源」 |
| 啟用 5 個 API | 開啟功能不等於授權，BQ API 通常本來就開 |

### 🟡 需要審視（一般風險）

| 項目 | 風險 | 緩解 |
|---|---|---|
| 給 SA `bigquery.dataEditor` | SA 可改/建/刪 BQ 內所有 dataset 內容 | 範圍只在這個 GCP project 內，且 SA 不能授權自己更多權限 |
| 給 SA `bigquery.jobUser` | SA 能跑 query（消耗 slot / 算錢） | 同上，project 內限定 |
| 給 SA `bigquery.resourceAdmin` | SA 能查 `INFORMATION_SCHEMA.JOBS_BY_PROJECT`（看其他 user 的 job 紀錄） | 只能 READ，無法改 IAM；對 drift detector 必要 |
| Token 在 GitHub Actions 環境內被用 | 理論上某段 workflow 邏輯外洩、攻擊者拿到 token | Token 限 1 小時、限定 scope；workflow 改動走 PR review |

### 🔴 真正要慎重的點

| 項目 | 風險 | 必做的緩解 |
|---|---|---|
| **WIF Provider 的 attribute condition 寫太寬** | 若條件設成「任何 GitHub repo」都能借這 SA，**全 GitHub 用戶都能 impersonate** | **必設** `assertion.repository_owner == '<我們公司 org name>'` |
| **SA → repo 的 binding 寫太寬** | 若 binding 沒指定特定 repo，任何 org 內 repo 都能借這 SA | **必設** binding 指定 `attribute.repository=<owner>/<repo>` 精確到 repo |
| **同一 SA 被多 repo 共用** | 若 marketing repo 借 pilot SA → 兩專案隔離破壞 | **每邏輯專案獨立 SA**，1 對 1 binding |

簡單來說：**唯一真正危險的地方是 attribute condition 跟 binding 的精確度**。其他都是常規最小權限授權，跟你給人類分析師 BQ Editor 風險等級相同。

---

## 為什麼不直接給 `bigquery.admin`

`bigquery.admin` 包含 **改 IAM 的權限**。SA 拿到後可以：
- 給自己加更多 role
- 給「外部攻擊者帳號」授權
- 修改其他 SA 的權限

我們的三個 role 聯集（dataEditor + jobUser + resourceAdmin）**不含 IAM 修改**，外洩時攻擊者最多只能改 BQ 資料、不能擴權。Blast radius 收斂在 BQ 資料層。

---

## 完整撤銷流程（如果之後不想用了）

任何時候可以**單一指令終止 CI/CD**：

```bash
# 撤銷某個 repo 的 access (CI 立即不能 deploy)
gcloud iam service-accounts remove-iam-policy-binding {sa-email} \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://.../attribute.repository/{owner}/{repo}"

# 或完全刪 SA (整個 CI/CD 立刻斷線)
gcloud iam service-accounts delete {sa-email}
```

刪除立即生效，**無遺留資產要追蹤**（這是 WIF 比 key file 好的核心優勢）。

---

## 給管理員的決策 checklist

- [ ] 確認「不給 admin role」的最小權限方案合理
- [ ] 確認 attribute condition 限制到 org level 合理
- [ ] 確認每邏輯專案獨立 SA + 獨立 binding 合理
- [ ] 同意走 WIF（不發 JSON key file）
- [ ] 同意一次建 1 套設定（Pool + Provider 全 org 共用）+ 9 對 SA（每邏輯專案 prod / test 各授）

同意之後 → 走 [`iam-wif-setup.md`](iam-wif-setup.md) §4（UI 版步驟）執行。
