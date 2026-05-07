-- setup-ledger.sql
-- Phase 1 v1.1 前置設定：在每個 GCP project 建 governance_audit dataset + migrations_applied ledger 表
--
-- 用法（每個 GCP project 各跑一次）:
--   bq query --project_id=<PROJECT> --use_legacy_sql=false --location=US < setup-ledger.sql
--
-- 你會用兩個 project 各跑一次：
--   bq query --project_id=praxis-works-367201 --use_legacy_sql=false --location=US < setup-ledger.sql
--   bq query --project_id=tapirus-test-384312 --use_legacy_sql=false --location=US < setup-ledger.sql

CREATE SCHEMA IF NOT EXISTS `governance_audit`
OPTIONS (
  description = 'SQL governance: migrations ledger and (future) audit log sink',
  location = 'US'
);

CREATE TABLE IF NOT EXISTS `governance_audit.migrations_applied` (
  migration_id   STRING NOT NULL,    -- 檔名（不含 .sql），例: 2026-05-07-1400-drop-sp-hello-world
  applied_at     TIMESTAMP NOT NULL,
  applied_by     STRING NOT NULL,    -- CI service account email
  git_sha        STRING,             -- 對應 commit SHA
  status         STRING NOT NULL,    -- 'applied' | 'failed'
  duration_ms    INT64,
  error_message  STRING,
  -- 可選擴充
  pr_url         STRING,
  manifest_id    STRING
)
PARTITION BY DATE(applied_at)
CLUSTER BY migration_id
OPTIONS (
  description = 'Phase 1 v1.1 simplified migration ledger. See platform/docs/two-phase-deploy-and-migration-spec.md §3.4'
);
