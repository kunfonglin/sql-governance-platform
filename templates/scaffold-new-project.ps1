<#
.SYNOPSIS
  一鍵 scaffold 一個新的 sql-governance-{logical} 專案 repo（薄 wrapper）。

.DESCRIPTION
  從 platform/templates/ 產出新 project repo 的所有檔案：
    - .github/workflows/ 四個 thin wrapper（deploy-test/deploy-prod/pr-validate/nightly-drift）
    - config/.governance.yaml
    - README.md / .github/CODEOWNERS / .github/PULL_REQUEST_TEMPLATE.md
    - 目錄骨架 bigquery/ migrations/ audit/deploys/
  並（可選）建 GitHub repo + 設 Repo Variables + 建 production 環境。

  注意：
    - 本腳本只負責「開發入口 + repo 骨架」。CI/CD 權限（WIF/SA）走 IAM 申請單
      （見 templates/IAM-REQUEST-TICKET.tmpl.md），拿回 WIF_PROVIDER/DEPLOY_SA 後再設 secrets。
    - table/view 進部署（Track B）需 platform 端 destructive-migration-lint + deploy-views 上線，
      在那之前 .governance.yaml 的 drift scope 維持 routines。

.EXAMPLE
  ./scaffold-new-project.ps1 -Logical marketing -ProdId my-mkt-prod -TestId my-mkt-test `
      -Region asia-east1 -Org IGS-ARCADE-DIVISION-RD5-WebBackend -Owner '@lin' `
      -OutDir D:\Claude\BQ_Governance\phase1\marketing -CreateRepo
#>
param(
  [Parameter(Mandatory)][string]$Logical,
  [Parameter(Mandatory)][string]$ProdId,
  [Parameter(Mandatory)][string]$TestId,
  [Parameter(Mandatory)][string]$Region,
  [Parameter(Mandatory)][string]$Org,                       # 專案 repo 所在 GitHub org（公司 org）
  [string]$Repo = '',                                       # repo 名；空=預設 sql-governance-{Logical}
  [string]$Owner = '@owner-handle',                         # CODEOWNERS 預設 reviewer
  [string]$PlatformTeam = '@platform-team',
  [string]$PlatformOwner = 'IGS-ARCADE-DIVISION-RD5-WebBackend',   # 平台 repo 擁有者（已搬 org；個人 sandbox 才傳 kunfonglin）
  [string]$PlatformRef = 'v1.1',
  [Parameter(Mandatory)][string]$OutDir,                    # 本機輸出資料夾
  [switch]$CreateRepo                                       # 加了才真的去 gh 建 repo + 設 vars
)

$ErrorActionPreference = 'Stop'
$Tpl = $PSScriptRoot                                        # templates/ 目錄
if (-not $Repo) { $Repo = "sql-governance-$Logical" }

function Expand-Placeholders([string]$text) {
  $text `
    -replace '__PROJECT_NAME__', $Logical `
    -replace '__PROJECT_TEST__', $TestId `
    -replace '__PROJECT_PROD__', $ProdId `
    -replace '__REGION__', $Region `
    -replace '__PROJECT_OWNER__', $Org `
    -replace '__PROJECT_REPO__', $Repo `
    -replace '__PLATFORM_OWNER__', $PlatformOwner `
    -replace '__PLATFORM_TEAM__', $PlatformTeam `
    -replace '__PLATFORM_REF__', $PlatformRef `
    -replace '__OWNER__', $Owner
}

function Write-FromTemplate([string]$src, [string]$dst) {
  $content = Expand-Placeholders (Get-Content -Raw -Encoding UTF8 $src)
  New-Item -ItemType Directory -Force (Split-Path $dst) | Out-Null
  # 寫 UTF-8 無 BOM
  [IO.File]::WriteAllText($dst, $content, (New-Object Text.UTF8Encoding($false)))
  Write-Host "  wrote $dst"
}

Write-Host "Scaffolding $Repo → $OutDir"
New-Item -ItemType Directory -Force `
  "$OutDir/bigquery", "$OutDir/migrations", "$OutDir/audit/deploys", `
  "$OutDir/config", "$OutDir/.github/workflows" | Out-Null

# 4 個 workflow
foreach ($w in 'deploy-test', 'deploy-prod', 'pr-validate', 'nightly-drift') {
  Write-FromTemplate "$Tpl/workflows/$w.yml.tmpl" "$OutDir/.github/workflows/$w.yml"
}
# config + repo meta
Write-FromTemplate "$Tpl/governance.yaml.tmpl" "$OutDir/config/.governance.yaml"
Write-FromTemplate "$Tpl/README.md.tmpl"        "$OutDir/README.md"
Write-FromTemplate "$Tpl/CODEOWNERS.tmpl"       "$OutDir/.github/CODEOWNERS"
Write-FromTemplate "$Tpl/PR_TEMPLATE.md.tmpl"   "$OutDir/.github/PULL_REQUEST_TEMPLATE.md"
# IAM 申請單（填好實值，直接可送 IT）
Write-FromTemplate "$Tpl/IAM-REQUEST-TICKET.tmpl.md" "$OutDir/IAM-REQUEST-TICKET.md"

Write-Host "`n本機檔案完成。"

if ($CreateRepo) {
  Write-Host "`n建立 GitHub repo + 設定 variables ..."
  gh repo create "$Org/$Repo" --private --source $OutDir --remote origin --push
  gh variable set PROJECT_TEST --repo "$Org/$Repo" --body $TestId
  gh variable set PROJECT_PROD --repo "$Org/$Repo" --body $ProdId
  gh variable set REGION       --repo "$Org/$Repo" --body $Region
  gh api -X PUT "repos/$Org/$Repo/environments/production" | Out-Null
  Write-Host "→ 到 Settings > Environments > production 加 Required reviewers（核准人）"
  Write-Host "→ Secrets（WIF_PROVIDER/DEPLOY_SA）等 IAM 申請單回來再 gh secret set"
} else {
  Write-Host "（未加 -CreateRepo：只產本機檔案，未碰 GitHub）"
}

Write-Host "`n下一步："
Write-Host "  1. 送 $OutDir/IAM-REQUEST-TICKET.md 給 IT，拿回 WIF_PROVIDER / DEPLOY_SA"
Write-Host "  2. gh secret set WIF_PROVIDER / DEPLOY_SA / TG_BOT_TOKEN / TG_CHAT_ID"
Write-Host "  3. 跑 baseline 匯入：見 docs/baseline-import-sop.md（exporter.py --include-tables）"
