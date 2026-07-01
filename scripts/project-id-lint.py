#!/usr/bin/env python3
"""
project-id-lint.py — 擋「SQL 裡硬寫自家環境 project id」

為什麼需要：
  規範是「自己環境的表不寫 project id（用 dataset.table），靠 bq --project_id 導到對的環境」。
  若有人硬寫了自家 project id（例如 prod 的 id），那份 script 部到測試機時仍會指向原專案的表
  → 跨環境污染資料（測試機排程動到正式資料）。
  drift 抓不到（normalize 會把自家 id 剝掉）、dry-run 也抓不到（語法合法）→ 只有這個 lint 擋得住。

用法:
  python project-id-lint.py <bigquery_root> [<forbidden_id> ...]
  # forbidden_id 通常傳 PROJECT_PROD 與 PROJECT_TEST

行為:
  掃 root 下所有 *.sql 的「程式碼部分」（去掉 -- 註解），
  出現任何 forbidden_id 字串 → 列出位置 → exit 1。
  跨專案引用（指向「別的」專案）不受影響——那不是自家 id。
"""
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: project-id-lint.py <bigquery_root> [forbidden_id ...]", file=sys.stderr)
        return 2
    root = Path(sys.argv[1])
    if not root.exists():
        print(f"project-id lint: 無 {root}/ 目錄，略過")
        return 0
    forbidden = [x for x in sys.argv[2:] if x.strip()]
    if not forbidden:
        print("project-id lint: 未提供 forbidden project id，略過")
        return 0

    violations: list[tuple[str, int, str, str]] = []
    for f in sorted(root.rglob("*.sql")):
        for lineno, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("--", 1)[0]  # 去掉行內註解，只看程式碼
            for pid in forbidden:
                if pid in code:
                    violations.append((str(f), lineno, pid, line.strip()))

    if violations:
        print("project-id lint FAILED — 自家環境 project id 不該硬寫進 SQL（請改用 dataset.table）：")
        for fp, ln, pid, text in violations:
            print(f"   {fp}:{ln}  [{pid}]  {text}")
        print("\n原因：硬寫自家 id 會讓同一份 script 部到別的環境時仍指向原專案 -> 跨環境污染資料。")
        return 1

    print(f"project-id lint passed（檢查了 {len(forbidden)} 個禁用 id）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
