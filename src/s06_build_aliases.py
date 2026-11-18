"""6단계: alias 사전.

자동: 정식명, 종목명(약식), 영문명, '(주)'·'주식회사' 제거형
수동: data/alias_manual.csv
위험한 alias(짧음, 충돌, 다른 종목 이름에 포함)는 지우지 않고 s07_report에서 보여준다.

python -m src.s06_build_aliases
"""
from __future__ import annotations

import csv
import re

from .common import DATA, db


def variants(*names: str | None) -> set[str]:
    out = set()
    for n in names:
        if not n:
            continue
        n = n.strip()
        out.add(n)
        stripped = re.sub(r"\(주\)|㈜|주식회사|\bCo\.?,?\s*Ltd\.?|\bCorp(oration)?\.?|\bInc\.?", "", n, flags=re.I)
        stripped = re.sub(r"\s+", " ", stripped).strip(" ,.")
        if stripped:
            out.add(stripped)
    return {v for v in out if len(v) >= 2}


def main():
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT stock_code, name, stock_name, name_eng FROM stock")
        stocks = cur.fetchall()
        by_name = {}
        for code, name, short, _ in stocks:
            by_name[name] = code
            if short:
                by_name[short] = code

        cur.execute("DELETE FROM stock_alias WHERE source IN ('auto', 'manual')")
        n_auto = n_manual = 0
        for code, name, short, eng in stocks:
            for a in variants(name, short, eng):
                cur.execute("INSERT INTO stock_alias VALUES (%s, %s, 'auto') ON CONFLICT DO NOTHING", (a, code))
                n_auto += 1

        path = DATA / "alias_manual.csv"
        if path.exists():
            with path.open(encoding="utf-8") as f:
                for row in csv.DictReader(f):
                    key = row["name_or_code"].strip()
                    code = key if key.isdigit() else by_name.get(key)
                    if not code:
                        print(f"[수동 alias 무시] 대상 종목 아님: {key}")
                        continue
                    cur.execute(
                        """INSERT INTO stock_alias VALUES (%s, %s, 'manual')
                           ON CONFLICT (alias, stock_code) DO UPDATE SET source = 'manual'""",
                        (row["alias"].strip(), code),
                    )
                    n_manual += 1
    print(f"자동 {n_auto}개, 수동 {n_manual}개")


if __name__ == "__main__":
    main()
