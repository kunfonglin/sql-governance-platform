# Platform 開發環境設定

> 給平台維護者（你）開發 / 測試 platform 工具用。
> Project repo 端不需要這份 — 它們用 GitHub Actions 跑工具，不直接動 Python。

---

## 一次性安裝

### 1. Python 3.11

確認 Python 版本：

```bash
python --version
# Python 3.11.x
```

如果沒 3.11，建議用 [pyenv](https://github.com/pyenv/pyenv-win)（Windows）或 [pyenv](https://github.com/pyenv/pyenv)（Mac/Linux）裝。

### 2. Poetry

```bash
# 跨平台官方安裝
curl -sSL https://install.python-poetry.org | python3 -

# 或 Windows PowerShell
(Invoke-WebRequest -Uri https://install.python-poetry.org -UseBasicParsing).Content | py -

# 驗證
poetry --version
```

### 3. gcloud CLI

```bash
gcloud --version
```

接下來認證有兩種方式，挑一個：

#### 方式 1: ADC（個人 OAuth 帳號）

```bash
gcloud auth application-default login
gcloud config set project tapirus-test-384312
```

適合：純你個人開發、用個人帳號權限即可。

#### 方式 2: Service Account JSON Key（推薦給平台維護者）

```bash
# 一次性產 key（如果還沒）
gcloud iam service-accounts keys create \
  ~/secrets/sql-governance-key.json \
  --iam-account=pilot-deploy-sa@tapirus-test-384312.iam.gserviceaccount.com

# 啟用該 SA
gcloud auth activate-service-account \
  --key-file=~/secrets/sql-governance-key.json

gcloud config set project tapirus-test-384312
```

或用 env var（不改變 gcloud session，只影響當前 shell）：

```bash
export GOOGLE_APPLICATION_CREDENTIALS="$HOME/secrets/sql-governance-key.json"
gcloud auth application-default set-quota-project tapirus-test-384312
```

適合：以 SA 身份測試「真實 CI 行為」、需要切換多個 SA 身份。

⚠ **安全提醒**：
- JSON key 絕不入 git（`.gitignore` 已涵蓋常見命名）
- 放 user home 底下，Mac/Linux 設 `chmod 600`
- CI/CD 仍用 WIF，不要把 key 放 GitHub Secrets
- 每 90 天輪換 key

#### 驗證（任一方式設好後）

```bash
gcloud auth list                          # 看當前活躍帳號
bq ls --project_id=tapirus-test-384312    # 跑得起來就成功
```

---

## 安裝專案依賴

```bash
cd D:/Claude/BQ_Governance/phase1/platform

# 建 venv + 裝所有依賴（含 dev tools）
poetry install

# 確認進得了 venv
poetry shell
python --version
exit
```

`poetry install` 會：
- 建一個本地 `.venv/`（已被 .gitignore 排除）
- 裝 `pyyaml`、`sqlglot`
- 裝 dev tools：`pytest`、`pytest-cov`、`ruff`
- 產生 `poetry.lock`（**請 commit 進 git**，鎖死所有依賴版本）

---

## 跑工具

所有 Python 工具用 `poetry run python` 開頭：

```bash
# Lineage 工具
poetry run python scripts/lineage-extract.py from-jobs \
  --project tapirus-test-384312 \
  --region US \
  --days 30 \
  --db ./lineage.db

# Drift detector
poetry run python scripts/drift-detector.py \
  --project tapirus-test-384312 \
  --region US \
  --git-root ../pilot/bigquery \
  --config ../pilot/config/.governance.yaml \
  --output ../pilot/audit

# Exporter
poetry run python scripts/exporter.py \
  --project tapirus-test-384312 \
  --region US \
  --output ../pilot/bigquery \
  --config ../pilot/config/.governance.yaml \
  --dry-run
```

或進 venv 後直接跑：

```bash
poetry shell
python scripts/lineage-extract.py from-jobs ...
exit
```

---

## 開發工作流

### 寫 / 改 code

```bash
# Lint
poetry run ruff check scripts/

# Auto-fix
poetry run ruff check --fix scripts/

# 跑測試（之後寫了再用）
poetry run pytest
```

### 加新依賴

```bash
# 加 runtime 依賴
poetry add some-package

# 加 dev 依賴（lint / test 用，不會打進 production）
poetry add --group dev some-package
```

### 升級依賴

```bash
poetry update                  # 在 ^ 範圍內更新
poetry update sqlglot          # 只更新某一個
poetry show --outdated         # 看哪些有新版本
```

---

## CI / GitHub Actions 怎麼跑

> 注意：CI 端目前**不依賴 Poetry**，用 `pip install pyyaml sqlglot` 直接裝。
> 理由：CI 環境是一次性的、不需要鎖定 venv；Poetry 反而增加冷啟動時間。

如果之後要 CI 也用 Poetry：

```yaml
# .github/workflows/reusable-nightly-drift.yml 加一段
- uses: snok/install-poetry@v1
  with:
    version: 1.8.0
- run: poetry install --no-root --only main
- run: poetry run python .platform/scripts/drift-detector.py ...
```

但 phase1 不急著做。

---

## Troubleshooting

### Poetry 找不到 Python 3.11

```bash
poetry env use 3.11
# 或指定完整路徑
poetry env use C:/Users/.../python.exe
```

### `poetry install` 卡住 / 太慢

設定鏡像（中國 / 公司內網用）：

```bash
poetry source add --priority=primary tsinghua https://pypi.tuna.tsinghua.edu.cn/simple/
```

### `bq` CLI 找不到

```bash
gcloud components install bq
gcloud components update
```

### `gcloud auth application-default login` 之後還是 401

```bash
gcloud config set project tapirus-test-384312
gcloud auth application-default set-quota-project tapirus-test-384312
```

---

## 路徑慣例（避免之後混亂）

| 類型 | 路徑 |
|---|---|
| Platform 工具開發位置 | `D:/Claude/BQ_Governance/phase1/platform/` |
| 你 sandbox 連結的 git repo | 之後推上 GitHub 才有；本地用 `phase1/pilot/` 當代理 |
| Lineage DB 暫存 | 自己決定，建議放 `phase1/platform/lineage.db`（已 gitignore）|
| Audit / drift 輸出 | `phase1/pilot/audit/`（pilot repo 內） |

跑工具時用 `--db`、`--git-root`、`--output` 等參數明確指向。
