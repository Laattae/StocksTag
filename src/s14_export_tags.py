"""최종 태그 내보내기 → data/export/stock_tag.csv (DB 없이 레포만 받아도 태그를 볼 수 있게)

python -m src.s14_export_tags

종목·축·순위 순으로 정렬해서 쓴다 (다시 실행해도 같은 파일이 나오도록 created_at 같은 시각 값은 넣지 않는다).
idf, match_excluded는 tag_weight 뷰 값 (뉴스 매칭 단계 참고용).
"""
from __future__ import annotations

import csv

from .common import DATA, db

OUT = DATA / "export" / "stock_tag.csv"
QUERY = """
SELECT st.stock_code, s.stock_name, t.axis, t.value, st.axis_rank, st.weight, st.direction, st.confidence,
       st.source, st.evidence_source, w.idf, w.match_excluded, st.rcept_no, st.evidence
FROM stock_tag st JOIN stock s USING (stock_code) JOIN tag t USING (tag_id) JOIN tag_weight w USING (tag_id)
ORDER BY st.stock_code, t.axis, st.axis_rank NULLS FIRST, t.value
"""


def main():
    OUT.parent.mkdir(exist_ok=True)
    with db() as conn, conn.cursor() as cur:
        cur.execute(QUERY)
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
    with OUT.open("w", encoding="utf-8-sig", newline="") as f:  # 엑셀에서 한글 깨짐 방지
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(rows)
    print(f"{len(rows)}행 → {OUT}")


if __name__ == "__main__":
    main()
