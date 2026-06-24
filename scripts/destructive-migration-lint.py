#!/usr/bin/env python3
"""
destructive-migration-lint.py — 擋「破壞型 DDL 自動 CD」

為什麼需要：
  table/view 進部署範圍後，安全類變更（CREATE TABLE IF NOT EXISTS /
  CREATE OR REPLACE VIEW / ALTER ADD COLUMN）可放心自動 CD —— 它們冪等、不銷毀資料。
  但破壞型變更（DROP TABLE/COLUMN、TRUNCATE、型別收窄、RENAME、UPDATE/DELETE backfill）
  一旦自動跑下去，資料就回不來了（roll-forward 救不回，只能靠 time-travel/快照）。
  → 這支 lint 把破壞型擋在自動部署之外：必須在檔頭明確標
    `-- destructive: <原因>` 且 `-- snapshot: <快照表或 time-travel 證據>` 才放行。

用法:
  python destructive-migration-lint.py <migrations_root>
  # 通常 migrations_root = "migrations"

行為:
  掃 migrations_root 下所有 *.sql：
    - 偵測破壞型關鍵字（去掉 -- 註解後的程式碼部分）
    - 若命中且檔頭前 10 行「沒有」同時出現 -- destructive: 與 -- snapshot: → 違規
  有任何違規 → 列出 → exit 1；否則 exit 0。

設計對齊 project-id-lint.py：同樣掛在 deploy 前置 gate（reusable-deploy.yml），
即使直接 push（沒走 PR）也擋得住，不靠 branch protection。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Windows console UTF-8（避免 cp950 在 ✓ ⚠ 當掉）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]

# (regex, 人類可讀標籤)
DESTRUCTIVE_PATTERNS: list[tuple[str, str]] = [
    (r"\bDROP\s+TABLE\b", "DROP TABLE"),
    (r"\bDROP\s+VIEW\b", "DROP VIEW"),
    (r"\bDROP\s+COLUMN\b", "DROP COLUMN"),
    (r"\bDROP\s+SCHEMA\b", "DROP SCHEMA"),
    (r"\bTRUNCATE\b", "TRUNCATE"),
    (r"\bALTER\s+COLUMN\b[\s\S]*?\bSET\s+DATA\s+TYPE\b", "型別變更 (SET DATA TYPE)"),
    (r"\bRENAME\s+(?:TO|COLUMN)\b", "RENAME"),
    (r"(?im)^\s*UPDATE\s+", "UPDATE backfill"),
    (r"(?im)^\s*DELETE\s+FROM\b", "DELETE"),
]

MARKER_DESTRUCTIVE = "-- destructive:"
MARKER_SNAPSHOT = "-- snapshot:"
HEAD_LINES = 10


def strip_comments(text: str) -> str:
    """去掉每行 -- 之後的註解，只留程式碼（破壞型關鍵字只在程式碼裡才算）。"""
    return "\n".join(line.split("--", 1)[0] for line in text.splitlines())


def is_marked(text: str) -> bool:
    """檔頭前 HEAD_LINES 行是否同時標了 destructive + snapshot。"""
    head = "\n".join(text.splitlines()[:HEAD_LINES])
    return (MARKER_DESTRUCTIVE in head) and (MARKER_SNAPSHOT in head)


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "migrations")
    if not root.exists():
        print(f"destructive-migration lint: 無 {root}/ 目錄，略過")
        return 0

    violations: list[tuple[str, str]] = []
    for f in sorted(root.rglob("*.sql")):
        text = f.read_text(encoding="utf-8")
        marked = is_marked(text)
        code = strip_comments(text)
        for pat, label in DESTRUCTIVE_PATTERNS:
            if re.search(pat, code, re.IGNORECASE) and not marked:
                violations.append((str(f), label))

    if violations:
        print("destructive-migration lint FAILED — 破壞型 DDL 需檔頭標記 + 快照證據才放行：")
        for fp, label in violations:
            print(f"   {fp}  [{label}]  缺 `{MARKER_DESTRUCTIVE}` / `{MARKER_SNAPSHOT}`")
        print()
        print("安全類（CREATE TABLE IF NOT EXISTS / CREATE OR REPLACE VIEW / ALTER ADD COLUMN）不受影響。")
        print("破壞型放行步驟：")
        print("  1. 先做快照：CREATE TABLE dataset.bak_xxx AS SELECT * FROM dataset.xxx（或確認 time-travel 時窗）")
        print("  2. migration 檔頭標：")
        print(f"       {MARKER_DESTRUCTIVE} <為什麼必須破壞>")
        print(f"       {MARKER_SNAPSHOT} <快照表名 / time-travel 證據>")
        print("  3. 仍建議走 prod approval gate 由人覆核。")
        return 1

    n = len(list(root.rglob("*.sql")))
    print(f"destructive-migration lint passed（檢查了 {n} 個 migration 檔）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
