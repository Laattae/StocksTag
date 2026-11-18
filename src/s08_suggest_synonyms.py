"""8단계(보조): 표기가 쪼개진 태그 → 동의어 후보 CSV.

python -m src.s08_suggest_synonyms           # 후보 만들기 → data/synonym_candidates.csv
python -m src.s08_suggest_synonyms --apply   # approve 열이 1인 행만 data/tag_synonyms.csv에 추가

1) 후보 묶음: 같은 축에서 짧은 값(기준값)을 포함하는 다른 값들, 또는 꼬리말
   (산업, 사업, 업, 시장, 분야, 치료 등)만 다른 값들
2) LLM 판정: 묶음 안에서 "같은 것"만 고르고 대표 표기를 정한다. 묶음 밖의 새 표기는 만들지 못한다
3) 사람이 approve 열에 1을 적은 행만 --apply로 tag_synonyms.csv에 들어간다 (자동 반영 없음)

LLM 응답은 data/llm_synonyms/에 캐시되어, 다시 실행해도 같은 결과가 나온다.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import defaultdict

from .common import DATA, LLM_AXES, LLM_OUT, canonical_value, load_synonyms, norm_key, normalize_tag_shape, spelling_map
from .s04_extract_tags import DailyQuotaExhausted, MODEL, call_llm, make_client, parse_json

OUT = DATA / "synonym_candidates.csv"
SYN = DATA / "tag_synonyms.csv"
CACHE = DATA / "llm_synonyms"
SUFFIX = re.compile(r"(산업|사업|업종|업|시장|분야|부문|제품|용|치료제|치료|질환|병)$")
AXES = [a for a in LLM_AXES if a != "region"]  # region은 시/도·국가로 이미 정해진 표기
GROUP_MAX = 12
BATCH = 30

JUDGE = """너는 종목 태그 사전을 정리하는 도구다. 축: {axis}

각 묶음은 같은 축의 태그 값들이다 (괄호 안은 그 태그가 붙은 종목 수).
묶음마다 "완전히 같은 대상을 다르게 쓴 것"만 골라 대표 표기 하나로 모은다.

## 같은 것으로 본다
- 꼬리말만 다름: 건설 = 건설업 = 건설 산업, 로봇 = 로봇 산업
- 질환명 표기: 당뇨 = 당뇨병, 알츠하이머 = 알츠하이머병. application 축에서는 "위암 치료" = 위암 (질환명이 대표)
- 영문/한글, 약어/풀어쓴 말: Inductor = 인덕터, ESS = 에너지저장장치

## 다른 것으로 본다 (합치지 않음)
- 범위가 다름: 암 ≠ 폐암, 발전 ≠ 원자력 발전, 가전 ≠ 생활가전, 반도체 ≠ 반도체 장비
- 글자만 겹침: 전자 ≠ 전자상거래, 의약 ≠ 바이오의약품
- product 축에서 품목과 그 품목의 치료제: 파킨슨병 치료제는 product로 두고 파킨슨병과 합치지 않는다

## 규칙
- canonical과 synonyms는 반드시 묶음 안에 있는 값을 글자 그대로 쓴다. 새 표기를 만들지 않는다.
- canonical은 짧고 일반적인 표기 (꼬리말 없는 쪽, 국문 우선. 업계에서 약어가 표준이면 약어: MLCC, ESS).
- 확신이 없으면 합치지 않는다. 합칠 것이 없는 묶음은 출력하지 않는다.

## 출력
JSON 하나만: {{"merges": [{{"group": 1, "canonical": "건설", "synonyms": ["건설업", "건설 산업"]}}]}}

## 묶음
{groups}"""


def collect() -> dict[str, dict[str, set[str]]]:
    """axis -> value(기존 동의어 적용 후) -> 종목코드 집합. run0, run1 전부."""
    syn = load_synonyms()
    spell = spelling_map(syn)  # 띄어쓰기·대소문자만 다른 건 s05가 이미 합치므로 후보에서 뺀다
    out: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for f in sorted(LLM_OUT.glob("*.run*.json")):
        code = f.name.split(".")[0]
        for t in map(normalize_tag_shape, json.loads(f.read_text(encoding="utf-8"))["tags"]):
            v = str(t["value"] or "").strip()
            if t["axis"] in AXES and 1 <= len(v) <= 30:
                out[t["axis"]][canonical_value(t["axis"], v, syn, spell)].add(code)
    return out


def groups_for(values: dict[str, set[str]]) -> list[list[str]]:
    keys = {v: norm_key(v) for v in values}
    stem = {v: SUFFIX.sub("", k) or k for v, k in keys.items()}
    groups, seen = [], set()
    for a in sorted(values, key=lambda v: (len(keys[v]), v)):
        if len(keys[a]) < (3 if keys[a].isascii() else 2):  # AN, IP 같은 영문 2글자는 아무 단어에나 들어감
            continue
        members = [b for b in values if b != a and (keys[a] in keys[b] or stem[a] == stem[b])]
        if not members:
            continue
        members.sort(key=lambda b: (-len(values[b]), b))
        g = tuple(sorted([a] + members[:GROUP_MAX - 1]))
        if g not in seen:
            seen.add(g)
            groups.append([a] + members[:GROUP_MAX - 1])
    return groups


def judge(client, axis: str, batch: list[list[str]], counts: dict[str, set[str]]) -> list[dict]:
    text = "\n".join(f"{i}. " + ", ".join(f"{v}({len(counts[v])})" for v in g) for i, g in enumerate(batch, 1))
    prompt = JUDGE.format(axis=axis, groups=text)
    CACHE.mkdir(parents=True, exist_ok=True)
    cache = CACHE / f"{axis}_{hashlib.sha1((MODEL + prompt).encode()).hexdigest()[:16]}.json"
    if cache.exists():
        raw = cache.read_text(encoding="utf-8")
    else:
        raw = call_llm(client, prompt)
        cache.write_text(raw, encoding="utf-8")
    out = []
    for m in parse_json(raw).get("merges", []):
        try:
            g = batch[int(m["group"]) - 1]
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        canon = m.get("canonical")
        if canon not in g:  # 묶음 밖 표기는 버린다
            continue
        for s in m.get("synonyms") or []:
            if s in g and s != canon:
                out.append({"axis": axis, "canonical": canon, "synonym": s})
    return out


def suggest():
    data = collect()
    client = make_client()
    rows, stop = [], False
    for axis in AXES:
        groups = groups_for(data[axis])
        print(f"{axis}: 태그 {len(data[axis])}개, 후보 묶음 {len(groups)}개")
        for i in range(0, len(groups), BATCH):
            try:
                rows += judge(client, axis, groups[i:i + BATCH], data[axis])
            except DailyQuotaExhausted as e:
                print(f"[하루 할당량 소진] {axis} {i}번째 묶음에서 멈춤. 내일 다시 실행하면 캐시 뒤부터 이어감.\n  {e}")
                stop = True
                break
            except Exception as e:
                print(f"  [실패] {axis} 묶음 {i}~: {type(e).__name__} {str(e)[:200]}")
        if stop:
            break

    # 한 synonym이 여러 대표로 가면 사람이 고르도록 표시. 대표가 다른 행의 synonym이면 연결 표시.
    by_syn = defaultdict(set)
    for r in rows:
        by_syn[(r["axis"], r["synonym"])].add(r["canonical"])
    syns = set(by_syn)
    uniq = {(r["axis"], r["canonical"], r["synonym"]): r for r in rows}
    with OUT.open("w", encoding="utf-8-sig", newline="") as f:  # 엑셀에서 열 수 있게
        w = csv.writer(f)
        w.writerow(["axis", "canonical", "synonym", "canon_stocks", "syn_stocks", "note", "approve"])
        for (axis, canon, s) in sorted(uniq):
            notes = []
            if len(by_syn[(axis, s)]) > 1:
                notes.append("대표 후보 여러 개: " + "/".join(sorted(by_syn[(axis, s)])))
            if (axis, canon) in syns:
                notes.append("대표가 다른 행의 synonym")
            w.writerow([axis, canon, s, len(data[axis][canon]), len(data[axis][s]), "; ".join(notes), ""])
    print(f"\n후보 {len(uniq)}행 → {OUT}\napprove 열에 1을 적고 --apply, 그다음 s05를 다시 실행")


def apply():
    """승인 행을 사전에 합친다. 대표가 다른 행의 synonym이면(A→B, B→C) 최종 대표(C)로 이어 준다.
    기존 사전 행도 같이 다시 써서, 사전 전체에 연쇄가 남지 않게 한다."""
    with SYN.open(encoding="utf-8") as f:
        old = [(r["axis"].strip(), r["canonical"].strip(), r["synonym"].strip()) for r in csv.DictReader(f)]
    approved = []
    with OUT.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r["approve"].strip() == "1":
                approved.append((r["axis"].strip(), r["canonical"].strip(), r["synonym"].strip()))

    to: dict[tuple[str, str], str] = {}  # (axis, norm_key(synonym)) -> canonical
    for axis, canon, syn in old + approved:  # 기존 사전이 먼저: 이미 있는 synonym은 그대로 둔다
        k = (axis, norm_key(syn))
        if k in to and norm_key(to[k]) != norm_key(canon):
            print(f"  [건너뜀] {axis} {syn}: 사전에 이미 {to[k]}(으)로 있음 (후보: {canon})")
            continue
        to.setdefault(k, canon)

    def final(axis: str, v: str) -> str:
        seen = set()
        while (axis, norm_key(v)) in to and norm_key(to[(axis, norm_key(v))]) != norm_key(v):
            if v in seen:
                raise SystemExit(f"순환: {axis} {seen}")
            seen.add(v)
            v = to[(axis, norm_key(v))]
        return v

    rows, keys = [], set()
    for axis, canon, syn in old + approved:
        c = final(axis, canon)
        k = (axis, norm_key(syn))
        if k in keys or norm_key(c) == norm_key(syn):
            continue
        keys.add(k)
        rows.append([axis, c, syn])
    with SYN.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["axis", "canonical", "synonym"])
        w.writerows(rows)
    print(f"tag_synonyms.csv: {len(old)}행 → {len(rows)}행. python -m src.s05_load_tags 로 다시 적재하세요.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    apply() if args.apply else suggest()


if __name__ == "__main__":
    main()
