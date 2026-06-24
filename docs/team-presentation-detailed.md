# 內部報告大綱（詳細版，僅供 lin 本人參考）

> 這份是「**所有細節版**」，留給未來自己回看 / 寫 FAQ / 補簡報。
> 對組員實際報告用精簡版，見聊天紀錄或 `team-presentation-condensed.md`。

---

## 🎯 開場 3 分鐘：為什麼要做這件事

> 「我們現在 prod BQ 是怎麼維護的？」
> - 工程師打開 BQ Console 直接改 SP
> - 改完沒人知道、沒紀錄、沒人 review
> - 出事要 rollback 也找不到上一版長什麼樣
> - 兩個工程師同時改 → 互相覆蓋

**Git + CI/CD 解決的問題**：

| 痛點 | Git 怎麼解 |
|---|---|
| 沒紀錄 | 每次改動都有 commit、看得到誰改了什麼、為什麼改 |
| 沒 review | PR 機制強制至少 1 人 review 才能上線 |
| 沒回滾 | 可以一鍵回到任一歷史版本 |
| 互相覆蓋 | Git 自動偵測衝突、merge 階段強制處理 |
| 沒測試 | 自動先在 test 環境跑、過了才能上 prod |

---

# Section 1：上 Git 作法與架構

## 1.1 一張圖看完整體

```
┌──────────────────────────────────────────────────────────────────┐
│                      開發者本地電腦                                │
│   1. 開檔修改 SP                                                  │
│   2. git push                                                     │
└────────────────────────┬─────────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────────────┐
│            GitHub Repo (sql-governance-{邏輯專案})                │
│   • 存所有 SP 的 .sql 檔                                          │
│   • 自動跑驗證 + 部署的「機器人」(workflow)                       │
│   • 每次改動都留紀錄                                              │
└────────────────────────┬─────────────────────────────────────────┘
                         │ (CI 機器人用「臨時通行證」找 GCP)
                         ▼
              ┌──────────────────────┐
              │   GCP 兩個 Project    │
              │  ┌────────────────┐  │
              │  │  test project  │  │  ← 先在這驗證
              │  └────────────────┘  │
              │  ┌────────────────┐  │
              │  │  prod project  │  │  ← 驗證 OK 才上這
              │  └────────────────┘  │
              └──────────────────────┘
```

## 1.2 prod / test 兩個 GCP project 的關係

```
1 邏輯專案 (例: marketing)
  │
  ├── marketing-test  (GCP project)  ← 開發者改完先丟這
  │     dev 工程師都可隨意操作
  │
  └── marketing-prod  (GCP project)  ← 正式上線後才碰這
        嚴格限定走 CI 才能改
```

**重點**：**git repo 同一份程式碼**，**會自動部署到兩個地方**。差別在「先 test、再 prod」、不是兩份維護。

## 1.3 給管理員的權限申請清單

```
① 建 1 支 Service Account
   名稱: {project}-deploy-sa@{prod}.iam.gserviceaccount

② 給這支 SA 三個 BQ 權限（不給 admin！）
   • bigquery.dataEditor    → 能 CREATE / REPLACE SP
   • bigquery.jobUser       → 能跑 query
   • bigquery.resourceAdmin → 能讀 INFORMATION_SCHEMA
   範圍: test + prod 兩個 GCP project 都要授

③ 建 WIF Pool（全 org 9 個邏輯專案共用 1 個）
   名稱: github-pool

④ 建 OIDC Provider（一樣 9 個共用 1 個）
   ⚠ 關鍵安全條件:
     attribute.repository_owner == '我們 org 名稱'

⑤ 把 SA 綁定到指定 repo（一個 SA = 一個 repo）
   ⚠ 鎖定條件:
     attribute.repository = '<owner>/<repo-name>'
```

各元件對應關係：

| 元件 | 數量 | 範圍 | 誰建 |
|---|---|---|---|
| WIF Pool | 1 個全 org 共用 | GCP 組織級 | 管理員 |
| OIDC Provider | 1 個全 org 共用 | GCP 組織級 | 管理員 |
| Service Account | 9 支（每邏輯專案 1 支） | 各 prod project 內 | 管理員 |
| SA → repo binding | 9 條（每 repo 1 條） | 各 SA 設定內 | 管理員 |
| GitHub Secrets | 4 個/repo × 9 = 36 個 | 各 repo 內 | 開發者 |

## 1.4 GitHub 端要設定的東西

```
Secrets (機密，4 個)
  WIF_PROVIDER     ← 管理員給的
  DEPLOY_SA        ← 管理員給的
  TG_BOT_TOKEN     ← 共用 Telegram bot
  TG_CHAT_ID       ← 共用 Telegram chat

Variables (普通變數，3 個)
  PROJECT_TEST     = marketing-test
  PROJECT_PROD     = marketing-prod
  REGION           = US（或 asia-east1）

Environments (環境，2 個)
  test         ← 自動，無需 approve
  production   ← ⚠ 必設 "Required reviewers"
```

## 1.5 部署後使用的樣子

```
早上 9:00  工程師 A 改一支 SP，push 到 development
早上 9:01  GitHub 自動驗證 SQL 沒爆 → 30 秒回報
早上 9:03  工程師 A 開 PR 到 main
早上 9:05  工程師 B Review、approve
早上 9:06  Merge → 自動部署到 test BQ
早上 9:07  工程師 A 在 test BQ 驗結果 OK
早上 9:08  到 GitHub Actions 點「approve prod」
早上 9:09  自動部署到 prod、TG 通知 ✅
```

開發者**實際 click 4 次**：push、開 PR、merge、approve production。

## 1.6 單 Repo vs 多 Repo 設計

### A：1 邏輯專案 1 repo（推薦）

```
                  ┌──────────────────────┐
                  │ sql-governance-      │
                  │ platform             │  ← 共用「機器人邏輯」
                  └──────────┬───────────┘
                             │ 引用
       ┌─────────────────────┼─────────────────────┐
       ▼                     ▼                     ▼
  ┌─────────┐           ┌─────────┐           ┌─────────┐
  │ pilot   │           │marketing│  ...      │ finance │
  └─────────┘           └─────────┘           └─────────┘
```

### B：所有專案共用 1 個大 repo

```
                  ┌──────────────────────┐
                  │ sql-governance-mono  │
                  │  ├ pilot/...         │
                  │  ├ marketing/...     │
                  │  └ ... (9 個資料夾)  │
                  └──────────────────────┘
```

### 比較表

| 比較項 | 🟢 A: 多 repo | 🟡 B: mono repo |
|---|---|---|
| 安全 blast radius | 1 repo 被入侵 = 1 個 GCP 專案 | 1 repo 被入侵 = 9 個全爆 |
| 權限切分 | 自然：repo 即邊界 | 要靠 CODEOWNERS 補 |
| 新增專案 | 從 template 複製新 repo | 加一個資料夾 |
| 跨專案搜尋 | 9 個 repo 各搜 | 1 個 repo 全搜 |
| CI 速度 | 改 A 專案不影響 B 的 PR | 要 path filter 不然 9 個全跑 |
| PR review 視野 | 只看本專案 | 跨專案脈絡負擔 |
| 未來移交給各組 | 直接交 repo | 要 fork 抽離 |

**建議**：採用 A（多 repo）。

---

# Section 2：部署流程

## 2.1 開發者要做的事

```
   你的電腦                GitHub                 BQ
   ─────────              ───────                ───
   ① 改 SP    ────push────→  自動驗證
                              ↓
                            ② Review PR
                              ↓
                            merge to development ──→ test BQ ✓
                              ↓
                            ③ 開 PR → main
                              ↓
                            ④ Approve production
                              ↓
                            merge to main ──────→ prod BQ ✓
                              ↓
                            TG 通知
```

## 2.2 情境 A：修改既有 SP

開發者步驟：
1. 用 BQ Studio 或本地 IDE 打開 `bigquery/{schema}/routines/sp_x.sql`
2. 改完 commit + push
3. 開 PR、找同事 approve
4. Merge 後等 CI 跑、自動推 test → prod
5. 最後到 GitHub Actions 點 production approval

## 2.3 情境 B：新增 SP

跟「修改」**完全一樣**，步驟 1 變成新增檔案。

## 2.4 情境 C：刪除 SP（用 migration）

```
[必須同一個 PR 內做兩件事]

1. 在 migrations/ 新增檔案：
   檔名：YYYY-MM-DD-HHMM-drop-sp-foo.sql
   內容：DROP PROCEDURE IF EXISTS `<schema>.sp_foo`;

2. 同時刪除 bigquery/<schema>/routines/sp_foo.sql

3. Commit 兩個變更（必須同一個 commit）
4. Push、開 PR、Review、Merge
5. PR development → main → Approve → 部 prod
```

❗ 注意：
- 不能只寫 migration、保留 .sql 檔 → 下次部署會把 SP「復活」
- 不能改 migration 檔名 → 系統會誤認是新的 migration、重複執行
- 同一個邏輯刪除 = 一個 migration 檔 + 對應 .sql 檔刪除 = 一個 commit

---

# Section 3：議題討論

## 議題 1：選 A 還是 B 的 repo 設計？
> 提案：A（多 repo），安全是主因

## 議題 2：誰負責 baseline 匯入？
> 9 個邏輯專案、每個約 30-60 分鐘人工

## 議題 3：Hotfix 流程的容忍度
> 提案：原則上不允許跳 test，例外情境要 reviewer 簽切結書

## 議題 4：何時排 Rollback 演練
> 提案：等第一批 baseline 匯入完成後 1 週內

## 議題 5：何時上 branch protection ruleset
> 提案：第 2 個 dev 加入 / Phase 1 上線後 1 個月

## 議題 6：baseline 是否匯入 table DDL
> 提案：先抓但不入 git，Phase 2 再決定

## 議題 7：TG 通知頻道分還是合
> 提案：先共用，量大再切

## 議題 8：Phase 2 啟動時機
> 提案：Phase 1 全 9 專案上線後 6 週評估
