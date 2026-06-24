# WIF 設定實戰筆記（給沒做過 WIF 的人）

> 本文檔記錄 Phase 1 sandbox（`tapirus-test-384312` + `praxis-works-367201`）的 WIF 設定全過程。
> 包含每步在做什麼、為什麼這樣做、踩坑紀錄。新手第一次做請依此文檔走。
> 完整參考規格見 [`iam-wif-setup.md`](iam-wif-setup.md)。

---

## 目錄

1. [先搞清楚 WIF 是什麼](#1-先搞清楚-wif-是什麼)
2. [整體 5 個步驟總覽](#2-整體-5-個步驟總覽)
3. [Step 1: 啟用 5 個 API](#step-1-啟用-5-個-api)
4. [Step 2: 建 Service Account](#step-2-建-service-account)
5. [Step 3: 給 SA 三個 Role（B 方案）](#step-3-給-sa-三個-roleb-方案)
6. [Step 4: 建 Workload Identity Pool + Provider](#step-4-建-workload-identity-pool--provider)
7. [Step 5: 把 repo 綁到 SA](#step-5-把-repo-綁到-sa)（待補）
8. [踩坑紀錄](#踩坑紀錄)
9. [Phase 1 sandbox 實際使用值](#phase-1-sandbox-實際使用值)

---

## 1. 先搞清楚 WIF 是什麼

**Workload Identity Federation** = 讓 GitHub Actions 能用「臨時 token」操作 GCP，不用 JSON key file。

想像一下兩家公司互信：

```
GitHub（外部公司）── 我這個員工是誰，有 OIDC 識別證
                    ↓ 把識別證給 GCP 看
GCP（你家）  ─────── 啊，是 GitHub 的識別證 ✓ 我認識
                    我發一張 1 小時的「訪客證」給你
                    用這張可以做指定範圍的事
```

**對比 JSON key file**：永久通行證、外洩 = 災難。
**WIF**：每次重新換臨時證、外洩影響有限。

---

## 2. 整體 5 個步驟總覽

| Step | 在做什麼 | 範圍 |
|------|----------|------|
| 1 | 啟用 5 個 API | 開啟 GCP 端會用到的功能 |
| 2 | 建 Service Account | 建一個「機器人帳號」當代理 |
| 3 | 給 SA 三個 BigQuery role | 限定機器人能做什麼（最小權限）|
| 4 | 建 Workload Identity Pool + Provider | 教 GCP 怎麼信任 GitHub |
| 5 | 把 repo 綁到 SA | 限定哪個 GitHub repo 可以扮演機器人 |

每步都在「縮小可能性範圍」：
- Step 1：開啟功能
- Step 2：誰
- Step 3：能做什麼
- Step 4：信任誰簽的 token
- Step 5：哪個 repo 能用

---

## Step 1: 啟用 5 個 API

### 為什麼要啟用

GCP 大部分 API 預設關閉，要用前要先開。沒開的 API 你呼叫會報「API not enabled」。

### 5 個 API 的角色

| API | 角色 | 何時被觸發 |
|-----|------|-----------|
| **IAM API** | 管「誰能做什麼」的結構面 | 建 SA、設 SA 的 IAM policy |
| **IAM Service Account Credentials API** | 產生「臨時 token」給 SA | runtime：每次 GitHub Actions 跑要 token |
| **Security Token Service (STS) API** | 換 token 的入口 | runtime：GitHub OIDC token → GCP federated token |
| **Cloud Resource Manager API** | 管 project / folder 結構 | 設 project 層級 IAM policy |
| **BigQuery API** | BQ 操作本身 | runtime：跑 query / deploy SP |

兩兩配對：
- **STS + IAM Credentials** = WIF runtime 的兩個齒輪
- **IAM + Resource Manager** = 設定階段的兩個齒輪
- **BigQuery** = 最終目的地

### UI 步驟

```
左上漢堡選單 → APIs & Services → Library
搜尋 → ENABLE
```

依序啟用 5 個（已 ENABLED 的跳過）：
1. `IAM API`
2. `IAM Service Account Credentials`
3. `Security Token Service`
4. `Cloud Resource Manager`
5. `BigQuery API`

### 驗證

`APIs & Services → Enabled APIs & services` 搜尋 5 個名字都查得到。

---

## Step 2: 建 Service Account

### 為什麼要 SA 不用人類帳號

| 問題 | 用人類帳號 | 用 SA |
|------|-----------|-------|
| 認證 | 要 OAuth + 瀏覽器互動 | 純機器認證 |
| Audit log | 全寫你名字 | 寫 SA email，CI 一眼可辨 |
| 撤銷 | 你離職全部 CI 死 | SA 跟人脫鉤 |
| 範圍 | 你個人權限可能涵蓋多 project | SA 只給最小必要 |

### UI 步驟

到**對應 prod project**（本 sandbox 是 `tapirus-test-384312`），不要在 test project 建。

```
IAM & Admin → Service Accounts → + CREATE SERVICE ACCOUNT
```

| 欄位 | 本 sandbox 用值 | 命名規則 |
|------|----------------|----------|
| Service account name | `pilot-deploy-sa` | `{邏輯專案}-deploy-sa` |
| Service account ID | （自動帶入） | 通常等於 name |
| Description | `Deploy SA for sql-governance-pilot` | 寫清楚用途 |

中間「Grant this service account access to project」**跳過**。
最後「Grant users access」也**跳過**。

### 結果

```
Email: pilot-deploy-sa@tapirus-test-384312.iam.gserviceaccount.com
Status: ✓ Enabled
```

⚠️ **重點**：SA 只在 prod project 建一個，**不要在每個 project 都建一個**。一個 SA 可以跨多 project 授權。

---

## Step 3: 給 SA 三個 Role（B 方案）

### B 方案是什麼

詳見 [`iam-wif-setup.md` §2](iam-wif-setup.md)。簡單說 = 三個 role 的最小組合，**不含 IAM 修改權限**。

| Role | 為什麼需要 |
|------|-----------|
| BigQuery Data Editor | 部署 SP / function |
| BigQuery Job User | 跑 query / dry_run |
| BigQuery Resource Admin | drift detector 查別人的 job（找出誰改了 prod） |

### 為什麼**不**用 BigQuery Admin

`bigquery.admin` 含改 IAM 的權限 → SA 自己能授權自己更多 → 外洩 = 災難。
B 方案三個 role 加起來不含 IAM → 即使外洩，也無法擴權。

### UI 步驟（兩個 project 都做）

#### 3.1 對 prod project（tapirus-test-384312）

確認左上角專案 = `tapirus-test-384312`。

```
IAM & Admin → IAM → + GRANT ACCESS
```

| 欄位 | 值 |
|------|-----|
| New principals | `pilot-deploy-sa@tapirus-test-384312.iam.gserviceaccount.com` |
| Roles | `BigQuery Data Editor` + `BigQuery Job User` + `BigQuery Resource Admin` |

按 **SAVE**。

#### 3.2 對 test project（praxis-works-367201）

切換左上角專案 → `praxis-works-367201`。

```
IAM & Admin → IAM → + GRANT ACCESS
```

⚠️ **principal 還是同一個 SA email**（住在 prod，跨專案授權）：
- New principals: `pilot-deploy-sa@tapirus-test-384312.iam.gserviceaccount.com`
- Roles: 同樣三個

### 驗證

兩個 project 的 IAM 頁面搜尋 `pilot-deploy-sa` 都該看到 3 個 role。

---

## Step 4: 建 Workload Identity Pool + Provider

### Pool / Provider 概念

```
GCP 公司大門
  │
  ├── Workload Identity Pool（合作夥伴名單資料夾）
  │     │
  │     ├── Provider 1: GitHub  ← 「我認 GitHub 發的識別證」
  │     ├── Provider 2: GitLab  ← 「我也認 GitLab」
  │     └── Provider 3: AWS
```

| 概念 | 角色 |
|------|------|
| **Pool** | 「我接受外部識別」的命名容器，1 個 GCP project 通常只開 1 個 |
| **Provider** | 教 GCP「這個 issuer 長怎樣，token 怎麼驗」 |
| **Attribute mapping** | 翻譯規則：把外部 token 欄位翻成 GCP 認得的欄位 |
| **Attribute condition** | 過濾規則：只接受符合條件的外部 token（**安全關鍵**）|

### UI 步驟

確認左上角專案 = `tapirus-test-384312`。

```
IAM & Admin → Workload Identity Federation → CREATE POOL
```

#### 4.1 Pool 設定（第一頁）

| 欄位 | 值 |
|------|-----|
| Name | `GitHub Actions Pool` |
| Pool ID | `github-pool` |
| Description | `Pool for GitHub Actions OIDC federation` |
| Enabled pool | ✅ |

按 **CONTINUE**。

#### 4.2 Provider 設定（第二頁）

| 欄位 | 值 | 注意 |
|------|-----|------|
| Select a provider | **OpenID Connect (OIDC)** | GitHub 用 OIDC |
| Provider name | `GitHub OIDC` | 顯示用 |
| Provider ID | `github-provider` | 不能改 |
| Issuer URL | `https://token.actions.githubusercontent.com` | **一字不差貼** |
| Audiences | （留空 = default） | 預設即可 |

按 **CONTINUE**。

#### 4.3 Provider attributes（第三頁）

**Attribute mapping**（按 + ADD MAPPING 加 3 行）：

| Google attribute | OIDC claim |
|------------------|-----------|
| `google.subject` | `assertion.sub` |
| `attribute.repository` | `assertion.repository` |
| `attribute.repository_owner` | `assertion.repository_owner` |

**Attribute conditions**（⚠️ **必填**，否則重大安全漏洞）：

```
assertion.repository_owner == 'kunfonglin'
```

按 **SAVE**。

### 驗證

`Workload Identity Federation` 列表頁應看到：
```
github-pool         Status: ✓ Enabled
└── github-provider Status: ✓ Active
```

點 `github-pool` → Provider tab → `github-provider` 看 detail，確認：
- Issuer URL 對
- Attribute mappings 三行對
- Attribute conditions 對

---

## Step 5: 把 repo 綁到 SA

### 為什麼還要這一步

Step 4 設了「Pool 守門員只接受 kunfonglin 名下 repo」。但這只是第一層放行。**沒設 Step 5 = token 進來後無法 impersonate 任何 SA = 認證失敗**。

Step 5 在每個 SA 上設「哪個 repo 能扮演我」：
- pilot-deploy-sa：只允許 `sql-governance-pilot`
- marketing-deploy-sa（未來）：只允許 `sql-governance-marketing`
- ……

→ 即使有人在 `kunfonglin` 名下開惡意 repo，他也 impersonate 不了 pilot-deploy-sa。

### 先準備兩件事

#### A. 拿 Project Number

到 GCP Console 首頁（左上 Google Cloud logo）。
右側 Project info card → 看「**專案編號**」是純數字（例：`987654321012`）。

⚠️ Project Number ≠ Project ID。
- Project ID：`tapirus-test-384312`（含字母）
- Project Number：純數字

#### B. 組好 principalSet URL

template：
```
principalSet://iam.googleapis.com/projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/attribute.repository/kunfonglin/sql-governance-pilot
```

把 `PROJECT_NUMBER` 換成 A 取得的數字。**整串包含 `principalSet://` 開頭**。

### UI 操作（中文介面）

```
IAM 與管理員 → 服務帳戶 → 點 pilot-deploy-sa@tapirus-test-384312...
```

進入 SA 詳情頁，上方 tab：

```
詳細資料 / 權限 / 金鑰 / 指標 / 紀錄
```

點 **「權限」** tab。

⚠️ **關鍵**：「權限」tab 進去後有兩個子區塊，**不要點錯**：

| 子區塊 | 用途 | Step 5 該用？ |
|--------|------|---------------|
| **管理存取權** | 給這個 SA 「在哪個 project 上」的 role（Step 3 用過） | ❌ |
| **具備存取權的主體** | 設「誰可以扮演這個 SA」 | ✅ Step 5 |

點 **「具備存取權的主體」** → 右上角「**+ 授予存取權**」開側邊面板：

| 欄位 | 值 |
|------|-----|
| **新增主體** | 整串 `principalSet://iam.googleapis.com/projects/{PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-pool/attribute.repository/kunfonglin/sql-governance-pilot`（先換掉 PROJECT_NUMBER）|
| **角色** | `Workload Identity 使用者`（搜尋 "Workload Identity"） |

按 **儲存**。

### 驗證

「具備存取權的主體」tab 應看到一行：
```
principalSet://...github-pool/attribute.repository/kunfonglin/sql-governance-pilot
角色：Workload Identity 使用者
```

### 印出 GitHub Secrets 要用的兩個值

| Secret 名稱 | 值 |
|-------------|-----|
| `WIF_PROVIDER` | `projects/PROJECT_NUMBER/locations/global/workloadIdentityPools/github-pool/providers/github-provider`（換 PROJECT_NUMBER） |
| `DEPLOY_SA` | `pilot-deploy-sa@tapirus-test-384312.iam.gserviceaccount.com` |

⚠️ 注意 `WIF_PROVIDER` 結尾是 `.../providers/github-provider`，**不是** `principalSet://...`。
兩串很像但用途不同：
- `principalSet://...` = Step 5 設定 SA binding 用
- `projects/.../providers/github-provider` = GitHub Actions 認證時告訴 GCP「我用哪個 provider」

---

## 踩坑紀錄

### 坑 1：在每個 project 都建一個 SA（不該）

**症狀**：以為「test project 給權限要在 test 建 SA」，結果建了 2 個 SA：
- `pilot-deploy-sa@tapirus-test-384312...`
- `pilot-deploy-sa@praxis-works-367201...`

**正解**：只在 prod project 建 1 個 SA，跨專案授權。1 SA / 邏輯專案 = 9 SA 管 9 邏輯專案；2 SA / 邏輯專案 = 18 個要管，無謂複雜。

**修復**：刪掉 test project 那個 SA，重做 Step 3。

### 坑 2：建 SA 時順便給 admin

**症狀**：UI 上「Grant this service account access」那頁順手選 `bigquery.admin`。

**正解**：那頁刻意跳過，到 Step 3 才在 IAM 頁面手動給 B 方案 3 個 role。
- 確保未來文件 / 腳本可以拆解「建 SA」與「給權限」兩動作
- 避免管理員一不小心給過大權限

### 坑 3：Attribute condition 漏設

**症狀**：跳過 condition 那欄按 SAVE，全世界 GitHub repo 都過得了第一道驗證。

**正解**：condition 必填，至少限定 `repository_owner`。

### 坑 4：Provider ID 想之後改

**症狀**：填 Provider ID 後想改名 → 改不了，只能刪重建。

**正解**：填之前想清楚命名規則，慣用 `github-provider`。

### 坑 5：Step 5 中文介面點錯子 tab

**症狀**：進「權限」tab，看到「**管理存取權**」按鈕只能加 role，找不到「新增主體」入口。

**正解**：「權限」tab 內有兩個子區塊：
- 管理存取權：Step 3 用（給 SA 角色）
- **具備存取權的主體**：Step 5 用（設誰能扮演 SA）→ 點這個才有「+ 授予存取權」

### 坑 6：Step 5 把 PROJECT_NUMBER 直接貼新增主體欄位

**症狀**：以為「新增主體」就是貼純數字，貼進去 GCP 報錯。

**正解**：「新增主體」貼**整串 `principalSet://...` URL**，PROJECT_NUMBER 是 URL 中間的一段，不是獨立貼。

---

## Phase 1 sandbox 實際使用值

整理一份備查（之後跨 9 個邏輯專案 onboarding 可對照）：

| 項目 | 值 |
|------|-----|
| Prod GCP project | `tapirus-test-384312` |
| Test GCP project | `praxis-works-367201` |
| GitHub owner | `kunfonglin` |
| GitHub pilot repo | `kunfonglin/sql-governance-pilot` |
| Service account | `pilot-deploy-sa@tapirus-test-384312.iam.gserviceaccount.com` |
| WIF Pool ID | `github-pool` |
| WIF Provider ID | `github-provider` |
| Issuer URL | `https://token.actions.githubusercontent.com` |
| Attribute condition | `assertion.repository_owner == 'kunfonglin'` |
| BQ region | `US` |

之後 step 5 / GitHub Secrets 設定還會用到的兩個值：
```
WIF_PROVIDER = projects/{PROJECT_NUMBER}/locations/global/workloadIdentityPools/github-pool/providers/github-provider
DEPLOY_SA    = pilot-deploy-sa@tapirus-test-384312.iam.gserviceaccount.com
```

`{PROJECT_NUMBER}` 可在 GCP Console 首頁 Project Info card 看到，是純數字。
