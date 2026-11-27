"""태그 정제 지표 (뉴스 적용 전). 단계마다 같은 지표로 비교한다.

python -m src.s09_refine_report                 # 화면 출력
python -m src.s09_refine_report --save step0    # data/refine/step0.txt 에도 저장

sql/03_refine_checks.sql의 쿼리 + data/tag_synonyms.csv와 DB tag_synonym의 정합성.
"""
from __future__ import annotations

import argparse
import csv
import io
import re
from collections import defaultdict
from contextlib import redirect_stdout

from .common import DATA, ROOT, db

OUT = DATA / "refine"


def run_queries(cur):
    sql = (ROOT / "sql" / "03_refine_checks.sql").read_text(encoding="utf-8")
    for block in re.split(r"(?m)^-- name:", sql)[1:]:
        title, _, query = block.partition("\n")
        print(f"\n### {title.strip()}")
        cur.execute(query.strip().rstrip(";"))
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
        print(f"({len(rows)}행)")
        print(" | ".join(cols))
        for r in rows[:150]:
            print(" | ".join("" if v is None else str(v) for v in r))


def synonym_consistency(cur):
    print("\n### [동의어] CSV ↔ DB 정합성")
    rows = list(csv.DictReader((DATA / "tag_synonyms.csv").open(encoding="utf-8")))
    csv_keys = {(r["axis"].strip(), r["synonym"].strip()) for r in rows}
    by_word = defaultdict(set)
    for axis, syn in csv_keys:
        by_word[syn].add(axis)
    multi = {w: a for w, a in by_word.items() if len(a) > 1}
    print(f"CSV 행 {len(rows)}, 같은 단어를 여러 축에 등록 {len(multi)}개: "
          + ", ".join(f"{w}({'/'.join(sorted(a))})" for w, a in sorted(multi.items())))

    cur.execute("SELECT t.axis, s.synonym, t.value FROM tag_synonym s JOIN tag t USING (tag_id)")
    db_rows = cur.fetchall()
    db_keys = {(a, s) for a, s, _ in db_rows}
    stale = sorted(db_keys - csv_keys)
    missing = sorted(csv_keys - db_keys)
    print(f"DB 행 {len(db_rows)}, CSV에 없는데 DB에 남은 것 {len(stale)}개, CSV에 있는데 DB에 없는 것 {len(missing)}개")
    for a, s in stale:
        print(f"  DB만: {a} | {s}")
    for a, s in missing:
        print(f"  CSV만: {a} | {s}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", help="data/refine/<이름>.txt 로 저장")
    args = ap.parse_args()
    buf = io.StringIO()
    with db() as conn, conn.cursor() as cur, redirect_stdout(buf):
        run_queries(cur)
        synonym_consistency(cur)
    text = buf.getvalue()
    print(text)
    if args.save:
        OUT.mkdir(exist_ok=True)
        (OUT / f"{args.save}.txt").write_text(text, encoding="utf-8")
        print(f"→ {OUT / (args.save + '.txt')}")


if __name__ == "__main__":
    main()
