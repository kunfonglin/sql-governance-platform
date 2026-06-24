# Baseline 匯入 SOP（手動跑 exporter.py / 9 專案輪流匯入）

> 把既有 prod BigQuery 的 routines + tables/views 抓下來變 git baseline。
> 每個邏輯專案做 1 次，是 onboarding 流程的「重資料量」步驟。

---

## 0. 前置確認

- [ ] Cloud SDK 已裝（`gcloud --version` 印得出版本）
- [ ] Poetry 已裝（`poetry --version` 印得出版本）
- [ ] 在 `phase1/platform/` 目錄底下執行（venv 跟 deps 已透過 `poetry install` 裝好）
- [ ] 對該 project 有 **`roles/bigquery.metadataViewer` + `roles/bigquery.jobUser`** 以上權限（個人帳號或 SA 皆可）

---

## 1. 認證方式（兩擇一）

### 方式 A：個人 OAuth 登入（適合一次性 / 偶發匯入）

```powershell
# 1. 登入（會跳瀏覽器）
gcloud auth login <你的 email>

# 2. 切換 active account 到剛登入那個
gcloud config set account <你的 email>

# 3. 驗證：應該回 1
$verifyQuery = "SELECT 1 AS ok"
& "C:\Users\<user>\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin\bq.cmd" `
  query --project_id=<target-project> --use_legacy_sql=false --format=json $verifyQuery
```

### 方式 B：SA Key File（適合 9 專案批次）

```powershell
# 1. 一次性：管理員給你 lineage-baseline-sa.json (路徑假設 D:\sa-keys\lineage-baseline-sa.json)

# 2. 啟用 SA
gcloud auth activate-service-account `
  --key-file="D:\sa-keys\lineage-baseline-sa.json"

# 3. 驗證
& "C:\Users\<user>\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin\bq.cmd" `
  query --project_id=<target-project> --use_legacy_sql=false --format=json "SELECT 1 AS ok"
```

> **SA key 保管原則**：放在本機加密磁碟、不要 push 進 git、不要貼 Slack。用完工作收尾把 `gcloud auth revoke` 清掉。

---

## 2. 看一下目前在哪個帳號

每次切換之前 / 跑 baseline 之前**先確認**：

```powershell
# 看所有登入過的帳號
gcloud auth list

# 預期輸出，'*' 號標的是 active：
#                 Credentialed Accounts
# ACTIVE  ACCOUNT
# *       bertramlinigs@gmail.com
#         kunfonglin@i17game.net
#         lineage-baseline-sa@some-project.iam.gserviceaccount.com
```

跑錯帳號的常見後果：權限不夠 → bq 報 `Access Denied` / `404 not found`，浪費時間 debug。

---

## 3. 跑 baseline 匯入

每個邏輯專案做一次：

```powershell
# 變數設定（依專案修改）
$LOGICAL_PROJECT  = "marketing"                     # 邏輯專案名（給 output path 用）
$GCP_PROD_PROJECT = "my-marketing-prod"             # 該邏輯專案的 prod GCP project id
$REGION           = "asia-east1"                    # BQ region
$OUTPUT_ROOT      = "D:\baseline\$LOGICAL_PROJECT"  # 匯入結果存放位置
$GOV_CONFIG       = "D:\Claude\BQ_Governance\phase1\platform\templates\governance.yaml.tmpl"

# 切換到該專案有權限的帳號
gcloud config set account <該專案有權限的帳號 email>

# 跑 exporter（dry-run 先看會抓什麼）
cd D:\Claude\BQ_Governance\phase1\platform
poetry run python scripts\exporter.py `
  --project $GCP_PROD_PROJECT `
  --region $REGION `
  --output "$OUTPUT_ROOT\bigquery" `
  --config $GOV_CONFIG `
  --include-tables `
  --dry-run

# dry-run 數字看起來合理（沒有把不該抓的抓進來）→ 拿掉 --dry-run 真正跑
poetry run python scripts\exporter.py `
  --project $GCP_PROD_PROJECT `
  --region $REGION `
  --output "$OUTPUT_ROOT\bigquery" `
  --config $GOV_CONFIG `
  --include-tables
```

---

## 4. 匯入後驗收（必做）

```powershell
# 看資料夾結構
tree /F "$OUTPUT_ROOT\bigquery" | Select-Object -First 30
```

預期：

```
bigquery\
  ├─ {schema1}\
  │  ├─ routines\        ← SP / UDF
  │  ├─ tables\          ← BASE TABLE DDL
  │  └─ views\           ← VIEW DDL (if any)
  ├─ {schema2}\
  │  └─ ...
  └─ ...
```

抽查內容：

```powershell
# 隨機挑 2-3 個 routine 對照 BQ Console 看內容沒丟字
type "$OUTPUT_ROOT\bigquery\<schema>\routines\<sp_name>.sql"
```

確認重點：
- [ ] `-- routine_type: PROCEDURE` 或 `FUNCTION` 註解正確
- [ ] `CREATE OR REPLACE` 開頭、沒丟字
- [ ] 跨專案引用有 `-- cross-project: other-proj.dataset.tbl` 註解
- [ ] 同專案引用 backtick 形式是 `` `dataset.name` ``（沒有 `proj.` 前綴）

---

## 5. 加 .gitignore 過濾 tables/views（Phase 1 不入 git）

在 `$OUTPUT_ROOT` 下建 `.gitignore`：

```gitignore
# Phase 1 SP-only — table/view DDL 抓下來但不入 git
# Phase 2 想開始管 schema 變更時刪掉這兩行
bigquery/**/tables/
bigquery/**/views/
```

效果：

```powershell
cd $OUTPUT_ROOT
git init
git add .
git status                              # tables/ + views/ 不會列出
```

---

## 6. 多 project 批次（半人工）

跑完 1 個專案 → **切帳號** → 跑下一個。注意中間別忘了切：

```powershell
# Project A
gcloud config set account proj-a-baseline-sa@...
poetry run python scripts\exporter.py --project proj-a-prod --output D:\baseline\proj-a\bigquery ...

# Project B
gcloud config set account proj-b-baseline-sa@...
poetry run python scripts\exporter.py --project proj-b-prod --output D:\baseline\proj-b\bigquery ...

# ...
```

> 用 SA key 方式可以省「切 active account」這步：一支 SA 對所有 project 都有讀權限的話，跑完 9 個用同一支認證。但 SA 對 9 個 project 要全部授 metadataViewer + jobUser。**建議跟管理員談時直接申請這支「baseline-importer SA」**。

未來 wrapper script 可以接這個（拿 SA key + project 清單一次跑完），但 phase 1 階段先手動沒問題，每專案約 5-10 分鐘。

---

## 7. 完成後清理（重要 / 資安）

```powershell
# 如果用了 SA key 認證、且工作結束（不需馬上再用）→ 撤銷該 SA 在本機的認證
gcloud auth revoke lineage-baseline-sa@some-project.iam.gserviceaccount.com

# 確認
gcloud auth list           # 該 SA 已不在 active 列表
```

SA key 檔案本身先留著（之後可能會再 baseline），但用完一定 `revoke` 把 active token 清掉，**避免電腦被竊或被當跳板的話 SA 立即可用**。

---

## 8. 常見錯誤對照

| 錯誤訊息 | 通常原因 | 解法 |
|---|---|---|
| `Access Denied: User does not have permission` | active 帳號錯了或權限不夠 | §2 看一下、§1 切對帳號 |
| `404 Not found` on INFORMATION_SCHEMA | region 寫錯（要小寫 + `region-` 前綴） | 在 `--region` 填 `asia-east1` 不是 `region-asia-east1`（exporter 會自動加） |
| `gcloud: command not found` | Cloud SDK 沒裝或沒進 PATH | 裝 / 加 PATH，或者用全路徑 |
| `poetry: command not found` | Poetry 沒裝 | `pip install poetry` 或 https://python-poetry.org/docs/#installation |
| 跑得了但檔案沒寫 | 用了 `--dry-run` | 拿掉 |
| 跑得了但 routines/ 是空的 | governance.yaml 把所有 dataset 都 exclude 了 | 檢查 config 的 `exclude.datasets` |

---

## 9. 接續流程

baseline 匯入完成、`.gitignore` 設好、第一次 commit 之後，後續就走標準 onboarding 流程：

1. 設 GitHub repo 4 個 secrets + 3 個 vars
2. 推 main 開第一輪 CI (`pr-validate` 應該綠勾)
3. 進入正常開發循環

詳見 [`onboarding-new-project.md`](onboarding-new-project.md)。
