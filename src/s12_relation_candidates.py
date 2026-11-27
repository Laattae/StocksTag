"""태그 정제 5단계: 상·하위 관계 후보 → data/relation_candidates.csv → (승인) data/tag_relations.csv → DB tag_relation

python -m src.s12_relation_candidates           # 후보 만들기
python -m src.s12_relation_candidates --apply   # approve=1 행을 tag_relations.csv에 넣고 DB tag_relation 동기화

관계 kind:
  is_a       child는 parent의 한 종류/일부 (AI 반도체 ⊂ 반도체, 생명보험 ⊂ 보험, 경주 ⊂ 경북, 기아 ⊂ 현대차그룹)
  made_from  child는 parent를 원료로 만든다 (탄산리튬 ← 리튬, 황산니켈 ← 니켈). 공급망 관계라 tag_ancestor로 이어 가지 않는다
뉴스 매칭에서 하위 개념 기사가 상위 태그 종목까지 끌어올지는 이 단계에서 정하지 않는다. 관계만 저장한다.

1) suffix 규칙 (product, application, raw_material, exposure): 한국어 복합명사는 끝말이 중심어라
   짧은 값이 긴 값의 "끝"에 붙으면 하위 후보 (AI 반도체 ⊂ 반도체). 앞에 붙는 것(반도체 장비)은 하위가 아니다.
   - 나열형 값(및, /, ·, 쉼표)은 빼고, 영문 상위는 단어 경계에서만 (OLED ⊄ LED), STOP 목록은 뺀다
   - 한 하위에 상위가 여럿이면 가장 가까운(긴) 것만 남긴다 (장기손해보험 ⊂ 손해보험, 보험은 그 위로 따라감)
2) data/relation_seeds.csv: 지역, 그룹-계열사, 규칙이 못 잡는 것 (사람이 쓴 사전)

suggest는 규칙의 추천(1/0)이고 approve는 사람이 정한다. 다시 만들 때 approve는 (axis, child, parent) 기준으로 이어받는다.
"""
from __future__ import annotations

import argparse
import csv
import re

from .common import DATA, db, load_stoplist, norm_key, stopped, sync_relation_table

OUT = DATA / "relation_candidates.csv"
SEEDS = DATA / "relation_seeds.csv"
REL = DATA / "tag_relations.csv"
FIELDS = ["axis", "child", "child_stocks", "parent", "parent_stocks", "kind", "rule", "suggest", "note", "approve"]
SUFFIX_AXES = ("product", "application", "raw_material", "exposure")
LIST_SEP = re.compile(r"\s및\s|/|,|·|ㆍ|&")
# 글자는 끝에 붙지만 하위가 아닌 것 (parent, child). child가 None이면 그 parent는 suffix 규칙에서 통째로 뺀다
STOP = {
    ("리스", "매트리스"), ("리스", "스테인리스"), ("에틸렌", "폴리에틸렌"), ("프로필렌", "폴리프로필렌"),
    ("프로필렌", "산화프로필렌"), ("물가", "국제 곡물가"), ("엔진", "게임엔진"), ("음료", "식음료"),
    ("우주", "항공우주"), ("항공", "우주항공"), ("전자", "전기전자"), ("메모리 반도체", "비메모리 반도체"),
    ("소세포폐암", "비소세포폐암"), ("재생에너지", "신재생에너지"), ("전기", None),
    ("LED 소재", "OLED 소재"), ("수소", "과산화수소"), ("메모리", "비메모리"), ("크림", "아이스크림"),
    ("수액", "자작나무 수액"), ("재보험", "화재보험"), ("연정광", "아연정광"), ("플랫폼", "해양플랫폼"),
    ("축전지", "비축전지"), ("스틱", None), ("서류", None),
    # 나열형인데 구분자가 없는 것 (예금+적금, 영화+드라마, 농산물+수산물, 유선+무선)
    ("적금", "예적금"), ("드라마", "영화드라마"), ("수산물", "농수산물"), ("무선통신서비스", "유무선 통신서비스"),
}
# 영문 경계 규칙에 걸리지만 실제로 하위인 것 (parent, child)
FORCE = {("PCB", "FPCB"), ("PE", "HDPE"), ("PE", "LDPE"), ("RPG", "MMORPG"), ("TV", "IPTV")}
# 원소 → 화합물: "종류"(is_a)가 아니라 "원료로 만든다"(made_from). 같은 광산·원료를 나눠 써서 시세가 같이 움직인다.
# 값은 suggest. 황화리튬(전고체용)은 시장이 작고 리튬 시세보다 전고체 상용화에 민감해 추천하지 않는다
COMPOUND = {("니켈", "황산니켈"): "1", ("리튬", "수산화리튬"): "1", ("리튬", "탄산리튬"): "1",
            ("아연", "산화아연"): "1", ("리튬", "황화리튬"): "0"}


def is_latin(s: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9 .+\-]+", s))


def suffix_candidates(tags: list[tuple[str, str, int]]):
    by_axis: dict[str, list[tuple[str, int]]] = {}
    for axis, value, n in tags:
        if axis in SUFFIX_AXES:
            by_axis.setdefault(axis, []).append((value, n))
    for axis, vals in by_axis.items():
        for child, cn in vals:
            ck = norm_key(child)
            parents = []
            for parent, pn in vals:
                pk = norm_key(parent)
                if len(pk) < 2 or len(ck) <= len(pk) or not ck.endswith(pk):
                    continue
                if (parent, None) in STOP or (parent, child) in STOP:
                    continue
                parents.append((parent, pn))
            # 가장 가까운 상위만: 다른 후보 상위의 끝말인 상위는 뺀다
            keep = [(p, pn) for p, pn in parents
                    if not any(q != p and norm_key(q).endswith(norm_key(p)) for q, _ in parents)]
            for parent, pn in keep:
                suggest, note = "1", ""
                if LIST_SEP.search(child):
                    suggest, note = "0", "나열형 값"
                elif is_latin(parent):
                    before = child[: len(child) - len(parent)] if child.lower().endswith(parent.lower()) else ""
                    if not before or re.search(r"[A-Za-z0-9]$", before):
                        suggest, note = "0", "영문 약어가 단어 중간에 걸림"
                if (parent, child) in FORCE:
                    suggest, note = "1", ""
                kind = "is_a"
                if (parent, child) in COMPOUND:
                    kind, suggest = "made_from", COMPOUND[(parent, child)]
                    note = "원소 → 화합물" + ("" if suggest == "1" else ". 연관 약함 (전고체 상용화에 더 민감)")
                yield {"axis": axis, "child": child, "child_stocks": cn, "parent": parent, "parent_stocks": pn,
                       "kind": kind, "rule": "suffix", "suggest": suggest, "note": note, "approve": ""}


def apply():
    approved = []
    with OUT.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["approve"].strip() == "1":
                approved.append((r["axis"], r["child"], r["parent"], r["rule"], r["kind"] or "is_a"))
    old = []
    if REL.exists():
        with REL.open(encoding="utf-8") as f:
            old = [(r["axis"], r["child"], r["parent"], r["source"], r.get("kind") or "is_a") for r in csv.DictReader(f)]
    stoplist = load_stoplist()
    rows, seen, dropped = [], set(), 0
    for axis, child, parent, src, kind in old + approved:
        k = (axis, norm_key(child), norm_key(parent))
        if stopped(axis, child, stoplist) or stopped(axis, parent, stoplist):
            dropped += 1  # data/tag_stoplist.csv 값이 들어간 관계는 남기지 않는다
            continue
        if k not in seen:
            seen.add(k)
            rows.append([axis, child, parent, kind, src])
    with REL.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["axis", "child", "parent", "kind", "source"])
        w.writerows(sorted(rows))
    print(f"tag_relations.csv: {len(old)}행 → {len(rows)}행 (stoplist 값이 들어가 뺀 것 {dropped}개)")
    with db() as conn, conn.cursor() as cur:
        sync_relation_table(cur)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    if args.apply:
        apply()
        return

    with db() as conn, conn.cursor() as cur:
        cur.execute("""SELECT t.axis, t.value, COUNT(st.tag_id) FROM tag t JOIN stock_tag st USING (tag_id)
                       WHERE t.axis <> 'industry_code' GROUP BY t.tag_id""")
        tags = cur.fetchall()
    used = {(a, v): n for a, v, n in tags}

    rows = {}
    for r in suffix_candidates(tags):
        rows[(r["axis"], r["child"], r["parent"])] = r
    with SEEDS.open(encoding="utf-8") as f:
        for s in csv.DictReader(f):
            axis, child, parent = s["axis"].strip(), s["child"].strip(), s["parent"].strip()
            missing = [v for v in (child, parent) if (axis, v) not in used]
            note = (f"지금 태그에 없음: {', '.join(missing)} (DB에는 둘 다 있을 때만 들어감). " if missing else "") + s.get("note", "")
            rows[(axis, child, parent)] = {"axis": axis, "child": child, "child_stocks": used.get((axis, child), 0),
                                           "parent": parent, "parent_stocks": used.get((axis, parent), 0),
                                           "kind": (s.get("kind") or "is_a").strip(), "rule": "seed", "suggest": "1", "note": note.strip(), "approve": ""}

    if OUT.exists():
        with OUT.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                k = (r["axis"], r["child"], r["parent"])
                if k in rows:
                    rows[k]["approve"] = r.get("approve", "")

    out = sorted(rows.values(), key=lambda r: (r["rule"], r["axis"], -int(r["parent_stocks"]), r["parent"], r["child"]))
    with OUT.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(out)
    stat = {}
    for r in out:
        k = (r["rule"], r["axis"], r["suggest"])
        stat[k] = stat.get(k, 0) + 1
    print(f"후보 {len(out)}행 → {OUT}")
    for k in sorted(stat):
        print("  ", k, stat[k])


if __name__ == "__main__":
    main()
