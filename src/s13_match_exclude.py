"""태그 정제 6단계: 흔한 태그 → 뉴스 키워드 매칭 제외 목록 동기화 + 점검.

python -m src.s13_match_exclude                  # data/tag_match_exclude.csv → DB, 결과 출력
python -m src.s13_match_exclude --threshold 30   # 종목 30% 이상인데 제외 목록에 없는 태그도 보여줌 (기본 30)

- 태그는 지우지 않는다. 키워드 매칭에서만 뺀다 (환율·금리는 exposure 방향으로 따로 쓸 수 있음)
- 나머지 태그의 흔한 정도는 tag_weight 뷰의 idf로 매칭 점수에서 낮춘다
- s05도 끝에서 같은 동기화를 한다
"""
from __future__ import annotations

import argparse

from .common import db, sync_match_exclude


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=30.0, help="이 비율(%%) 이상인데 제외 목록에 없는 태그를 보여줌")
    args = ap.parse_args()
    with db() as conn, conn.cursor() as cur:
        sync_match_exclude(cur)
        cur.execute("""SELECT axis, value, stocks, pct, idf, exclude_reason FROM tag_weight
                       WHERE match_excluded ORDER BY stocks DESC""")
        print("\n제외 목록")
        for r in cur.fetchall():
            print("  " + " | ".join(str(v) for v in r))
        cur.execute("""SELECT axis, value, stocks, pct, idf FROM tag_weight
                       WHERE NOT match_excluded AND axis <> 'industry_code' AND pct >= %s ORDER BY stocks DESC""",
                    (args.threshold,))
        rows = cur.fetchall()
        print(f"\n종목 {args.threshold:g}% 이상인데 제외 목록에 없는 태그: {len(rows)}개")
        for r in rows:
            print("  " + " | ".join(str(v) for v in r))


if __name__ == "__main__":
    main()
