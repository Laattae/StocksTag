"""2단계: 기업개황(company.json) → 시장, 업종코드, 주소 + 본사 지역 태그.

python -m src.s02_company_info [--show]   # --show: 첫 응답을 그대로 출력 (필드명 확인용)
"""
from __future__ import annotations

import json
import sys

from .common import dart_json, db, parse_region, target_stocks, upsert_tag

MARKET = {"Y": "KOSPI", "K": "KOSDAQ", "N": "KONEX", "E": "ETC"}


def main():
    show = "--show" in sys.argv
    with db() as conn, conn.cursor() as cur:
        stocks = target_stocks(cur)
        for i, (code, corp_code, name) in enumerate(stocks):
            j = dart_json("company", corp_code=corp_code)
            if show and i == 0:
                print(json.dumps(j, ensure_ascii=False, indent=2))
            if j.get("status") != "000":
                print(f"[실패] {name}: {j.get('status')} {j.get('message')}")
                continue

            address = j.get("adres") or ""
            cur.execute(
                """UPDATE stock SET stock_name=%s, name_eng=%s, market=%s, induty_code=%s,
                          address=%s, updated_at=now() WHERE stock_code=%s""",
                (j.get("stock_name"), j.get("corp_name_eng"), MARKET.get(j.get("corp_cls")),
                 j.get("induty_code"), address, code),
            )

            # 이 단계가 만드는 dart 태그만 지우고 다시 넣는다
            cur.execute(
                """DELETE FROM stock_tag st USING tag t
                   WHERE st.tag_id = t.tag_id AND st.stock_code = %s AND st.source = 'dart'
                     AND t.axis IN ('industry_code', 'region')""",
                (code,),
            )
            rows = []
            if j.get("induty_code"):
                rows.append(("industry_code", j["induty_code"], f"induty_code={j['induty_code']}"))
            region = parse_region(address)
            if region:
                sido, area = region
                rows += [("region", sido, address), ("region", area, address)]
            else:
                print(f"[주소 파싱 실패] {name}: {address!r}")

            for axis, value, ev in rows:
                tag_id = upsert_tag(cur, axis, value)
                cur.execute(
                    """INSERT INTO stock_tag (stock_code, tag_id, weight, source, confidence, evidence)
                       VALUES (%s, %s, 1.0, 'dart', 1.0, %s)
                       ON CONFLICT (stock_code, tag_id) DO UPDATE
                       SET source='dart', weight=1.0, confidence=1.0, evidence=EXCLUDED.evidence""",
                    (code, tag_id, f"본사 주소: {ev}" if axis == "region" else ev),
                )
            print(f"{name}: 업종 {j.get('induty_code')}, 지역 {region}")


if __name__ == "__main__":
    main()
