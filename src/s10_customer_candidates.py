"""태그 정제 3단계: 고객사(customer) 표기 통일 후보 → data/customer_candidates.csv

python -m src.s10_customer_candidates           # 후보 만들기
python -m src.s10_customer_candidates --apply   # approve 열이 1인 행만 data/tag_synonyms.csv에 추가 (s08과 같은 방식)

1) listed: 고객사 값이 상장 종목을 가리키면 대표 = 그 종목의 stock_name.
   법인 접미사((주), ㈜, 주식회사, Co., Ltd., Inc. …)를 떼고, 엘지·에스케이 같은 음차를 영문으로 바꾼 뒤
   stock_alias / stock.name / stock.stock_name과 맞춘다. 여러 종목에 맞으면 후보에서 뺀다.
2) data/customer_groups.csv: 해외·비상장사, 옛 사명, 그룹, 상장사 대표 표기(display) (사람이 쓴 사전). 대표는 뉴스에서 쓰는 한글 표기,
   영문은 동의어. 뉴스 표기가 영문 약칭인 곳(GSK, ZF, TSMC 등)만 영문 대표.
   variant가 지금 태그로 없더라도 동의어로는 넣는다 (뉴스 매칭용).

후보 CSV는 다시 만들 때 기존 approve 값을 (canonical, synonym) 기준으로 이어받는다.
반영 뒤에는 python -m src.s05_load_tags 로 다시 적재한다.
"""
from __future__ import annotations

import argparse
import csv
import re

from .common import DATA, db, load_synonyms, norm_key
from .s08_suggest_synonyms import apply

OUT = DATA / "customer_candidates.csv"
GROUPS = DATA / "customer_groups.csv"
FIELDS = ["axis", "canonical", "synonym", "canon_stocks", "syn_stocks", "method", "ref_stock_code", "note", "approve"]

LEGAL = re.compile(
    r"^\(주\)|^㈜|\(주\)$|㈜$|^주식회사\s*|\s*주식회사$|\s+사$"
    r"|[,.\s]*(co\.?,?\s*ltd\.?|ltd\.?|inc\.?|corp\.?|corporation|company|llc|pte\.?|ag|s\.a\.?)\s*$",
    re.IGNORECASE,
)
TRANSLIT = [("엘지", "lg"), ("에스케이", "sk"), ("지에스", "gs"), ("씨제이", "cj"), ("에이치디", "hd"), ("케이티", "kt")]


def company_key(s: str) -> str:
    s = (s or "").strip()
    for _ in range(3):  # "X Co., Ltd." 처럼 접미사가 겹친 경우
        s2 = LEGAL.sub("", s).strip()
        if s2 == s:
            break
        s = s2
    k = norm_key(s)
    for ko, en in TRANSLIT:
        if k.startswith(ko):
            k = en + k[len(ko):]
    return re.sub(r"[.,&()]", "", k)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    if args.apply:
        apply(OUT)
        return

    synonyms = load_synonyms()
    with db() as conn, conn.cursor() as cur:
        cur.execute("""SELECT t.value, COUNT(st.tag_id) FROM tag t JOIN stock_tag st USING (tag_id)
                       WHERE t.axis = 'customer' GROUP BY t.value""")
        used = dict(cur.fetchall())
        cur.execute("SELECT stock_code, name, stock_name FROM stock")
        stocks = {c: (n, sn or n) for c, n, sn in cur.fetchall()}
        cur.execute("SELECT alias, stock_code FROM stock_alias")
        names = cur.fetchall() + [(n, c) for c, (n, _) in stocks.items()] + [(sn, c) for c, (_, sn) in stocks.items()]

    index: dict[str, set[str]] = {}
    for alias, code in names:
        index.setdefault(company_key(alias), set()).add(code)

    rows: dict[tuple[str, str], dict] = {}

    def add(canon, syn, method, ref="", note=""):
        if norm_key(canon) == norm_key(syn):
            return
        prev = synonyms.get(("customer", norm_key(syn)))
        if prev and norm_key(prev) != norm_key(syn):
            if norm_key(prev) == norm_key(canon):
                return  # 이미 사전에 있음
            note = f"사전에 이미 {prev}(으)로 있음 → 승인하려면 tag_synonyms.csv 해당 행을 직접 고칠 것. " + note
        rows[(canon, syn)] = {"axis": "customer", "canonical": canon, "synonym": syn,
                              "canon_stocks": used.get(canon, 0), "syn_stocks": used.get(syn, 0),
                              "method": method, "ref_stock_code": ref, "note": note.strip(), "approve": ""}

    with GROUPS.open(encoding="utf-8") as f:
        groups = list(csv.DictReader(f))
    # kind=display: 상장사 대표 표기를 stock_name 대신 이것으로 (케이티 → KT)
    display = {norm_key(r["variant"]): r["canonical"].strip() for r in groups if r["kind"].strip() == "display"}

    # 1) 상장사
    for value in sorted(used):
        codes = index.get(company_key(value), set())
        if len(codes) == 1:
            code = next(iter(codes))
            name = stocks[code][1]
            add(display.get(norm_key(name), name), value, "listed", code)
        elif len(codes) > 1:
            print(f"  [건너뜀] {value}: 여러 종목 {sorted(codes)}")

    # 2) 사람이 쓴 사전
    for r in groups:
        canon, var = r["canonical"].strip(), r["variant"].strip()
        codes = index.get(company_key(canon), set())
        ref = next(iter(codes)) if len(codes) == 1 else ""
        note = r.get("note", "").strip()
        if var not in used:
            note = ("지금 태그에는 없음(뉴스 매칭용 동의어). " + note).strip()
        add(canon, var, r["kind"].strip(), ref, note)

    # 이전 승인 값 이어받기
    if OUT.exists():
        with OUT.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                k = (r["canonical"], r["synonym"])
                if k in rows:
                    rows[k]["approve"] = r.get("approve", "")

    out = sorted(rows.values(), key=lambda r: (r["method"], r["canonical"], r["synonym"]))
    with OUT.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(out)
    by = {}
    for r in out:
        by[r["method"]] = by.get(r["method"], 0) + 1
    print(f"후보 {len(out)}행 {by} → {OUT}")


if __name__ == "__main__":
    main()
