"""태그 정제 4단계: 축 간 동의어 어긋남 → data/cross_axis_candidates.csv

python -m src.s11_cross_axis_candidates           # 후보 만들기
python -m src.s11_cross_axis_candidates --apply   # approve 열이 1인 행만 data/tag_synonyms.csv에 추가 (s08과 같은 방식)

A축에 "syn → canon" 동의어가 있는데 B축에는 syn이 별도 태그로 쓰이는 경우,
B축에도 같은 동의어 "syn → canon"을 넣자는 후보를 만든다 (예: 배터리가 product에선 2차전지의 동의어인데 application에선 별도 태그).
뜻이 축마다 다를 수 있으므로(product 외환 = 은행 외국환 업무, exposure 외환 = 환율) 사람이 approve를 정한다.
후보 CSV는 다시 만들 때 기존 approve 값을 (axis, synonym) 기준으로 이어받는다.
"""
from __future__ import annotations

import argparse
import csv

from .common import DATA, db
from .s08_suggest_synonyms import apply

OUT = DATA / "cross_axis_candidates.csv"
FIELDS = ["axis", "canonical", "synonym", "canon_stocks", "syn_stocks", "from_axis", "note", "approve"]

QUERY = """
WITH used AS (
  SELECT t.tag_id, t.axis, t.value, tag_key(t.value) AS k, COUNT(st.tag_id) AS n
  FROM tag t JOIN stock_tag st USING (tag_id) GROUP BY t.tag_id
)
SELECT o.axis, t.value AS canonical, o.value AS synonym,
       COALESCE((SELECT n FROM used c WHERE c.axis = o.axis AND c.k = tag_key(t.value)), 0) AS canon_stocks,
       o.n AS syn_stocks, t.axis AS from_axis
FROM tag_synonym s JOIN tag t USING (tag_id)
JOIN used o ON o.axis <> t.axis AND o.k = tag_key(s.synonym)
WHERE tag_key(s.synonym) <> tag_key(t.value) AND o.axis <> 'industry_code'
ORDER BY o.n DESC, o.axis, o.value
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    if args.apply:
        apply(OUT)
        return

    with db() as conn, conn.cursor() as cur:
        cur.execute(QUERY)
        cols = [d.name for d in cur.description]
        rows = {}
        for r in cur.fetchall():
            d = dict(zip(cols, r), note="", approve="")
            rows.setdefault((d["axis"], d["synonym"]), d)  # 같은 (축, 단어)가 여러 대표로 걸리면 첫 번째만

    if OUT.exists():
        with OUT.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                k = (r["axis"], r["synonym"])
                if k in rows:
                    rows[k]["approve"], rows[k]["note"] = r.get("approve", ""), r.get("note", "")

    with OUT.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows.values())
    print(f"후보 {len(rows)}행 → {OUT}")


if __name__ == "__main__":
    main()
