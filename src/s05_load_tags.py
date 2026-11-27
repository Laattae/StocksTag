"""5단계: LLM 결과 검증 → 정규화 → stock_tag 적재.

- 여러 회차(run0, run1)를 합친다. 어느 회차에서든 검증을 통과한 태그는 적재하고,
  confidence = 그 태그가 나온 회차 수 / 그 종목의 회차 수
- evidence가 원문에 실제로 들어 있지 않으면:
  product·customer·raw_material 축이고 값이 3글자 이상이면 원문에서 값이 그대로 들어 있는 문장을 찾아
  근거로 쓴다 (evidence_source='repaired'). 찾은 문장도 원문 검증을 다시 통과해야 한다.
  그래도 없으면 버린다 (data/rejected.csv)
- 근거 우선순위: LLM 근거 중 값이 들어 있는 것 > 다시 찾은 근거 > LLM 근거 중 값이 없는 것
- 동의어 사전으로 값 통일 (적층세라믹콘덴서 → MLCC)
- data/tag_stoplist.csv의 값(원가 표의 '부품', 부문 이름 '솔루션' 등)은 근거가 있어도 적재하지 않는다 (rejected.csv에 사유 남김)
- 끝에 data/tag_relations.csv로 tag_relation을 다시 맞춘다
- 띄어쓰기·대소문자·하이픈만 다른 표기는 가장 많이 쓰인 표기로 (재활 치료 → 재활치료)
- 같은 태그가 여러 번 나오면 weight 최댓값
- 축 안에서 weight 순위(axis_rank) 저장. 자르지는 않는다 (상한은 stock_tag_topk 뷰로 본다)

python -m src.s05_load_tags [--runs 0,1]
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import defaultdict

from .common import DATA, LLM_AXES, LLM_OUT, canonical_value, db, load_stoplist, load_synonyms, norm_for_match, stopped, sync_match_exclude, norm_key, normalize_tag_shape, report_path, spelling_map, sync_relation_table, sync_synonym_table, upsert_tag

REPAIR_AXES = ("product", "customer", "raw_material")
REPAIR_MIN_LEN = 3      # norm_key 기준 글자 수. "SK", "LG", "전력"처럼 짧은 값은 아무 문장에서나 찾혀서 제외
SEGMENT_MAX = 250       # 다시 찾은 근거가 이보다 길면 값 주변만 (원문 그대로 잘라냄)


def validate(tag: dict, source_norm: str) -> str | None:
    """문제가 있으면 사유, 없으면 None. (axis 공백, 축 이름을 키로 쓴 모양은 여기서 정리)"""
    normalize_tag_shape(tag)
    if tag["axis"] not in LLM_AXES:
        return f"알 수 없는 axis {tag.get('axis')!r}"
    v = (tag.get("value") or "").strip()
    if not (1 <= len(v) <= 30):
        return "value 길이"
    ev = norm_for_match(tag.get("evidence") or "")
    if len(ev) < 4:
        return "evidence 없음/짧음"
    if ev not in source_norm:
        return "evidence가 원문에 없음"
    return None


# ---------------------------------------------------------------- 근거 다시 찾기
def segments(source: str) -> list[tuple[str, bool]]:
    """원문 → (구절, 본문 문장인가). 표 행은 셀로, 셀·줄은 문장으로 나눈다.
    공시 원문은 "있습니다.지역별로"처럼 문장 사이 띄어쓰기가 빠진 경우가 많아 '다.' 뒤는 항상 끊는다.
    표 안이라도 '다'로 끝나는 문장은 본문 문장으로 친다."""
    out = []
    for line in source.splitlines():
        table = " | " in line
        for cell in line.split(" | ") if table else [line]:
            for s in re.split(r"(?<=[.!?])\s+|(?<=다\.)(?=\S)", cell):
                s = s.strip(" |")
                if s:
                    out.append((s, not table or bool(re.search(r"다\.?$", s))))
    return out


def value_pattern(value: str) -> re.Pattern:
    """띄어쓰기·하이픈 차이는 무시하고 값을 찾는 패턴."""
    chars = [re.escape(c) for c in norm_key(value)]
    return re.compile(r"[\s\-·ㆍ]*".join(chars), re.IGNORECASE)


def bigrams(s: str) -> set[str]:
    s = norm_for_match(s)
    return {s[i:i + 2] for i in range(len(s) - 1)}


def has_value(evidence: str, values: list[str]) -> bool:
    ev = norm_key(evidence)
    return any(norm_key(v) in ev for v in values if v)


def repair(values: list[str], llm_evidence: str, segs: list[tuple[str, bool]]) -> str | None:
    """값이 그대로 들어 있는 원문 구절. 본문 문장 우선, 그다음 LLM 근거와 비슷한 순."""
    pats = [value_pattern(v) for v in dict.fromkeys(values) if len(norm_key(v)) >= REPAIR_MIN_LEN]
    if not pats:
        return None
    target = bigrams(llm_evidence)
    best, best_key = None, None
    for seg, prose in segs:
        m = next((m for p in pats if (m := p.search(seg))), None)
        if not m:
            continue
        if len(seg) > SEGMENT_MAX:
            half = (SEGMENT_MAX - (m.end() - m.start())) // 2
            a = max(0, m.start() - half)
            seg = seg[a: a + SEGMENT_MAX].strip()
        b = bigrams(seg)
        sim = len(b & target) / len(b | target) if target and b else 0.0
        key = (prose, sim, -len(seg))
        if best_key is None or key > best_key:
            best, best_key = seg, key
    return best


# ---------------------------------------------------------------- 적재
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default="0,1", help="합칠 회차 (예: 0,1)")
    args = ap.parse_args()
    runs = [int(r) for r in args.runs.split(",")]

    synonyms = load_synonyms()
    spelling = spelling_map(synonyms)
    stoplist = load_stoplist()
    print(f"표기 정리 규칙 {len(spelling)}개 (띄어쓰기·대소문자·하이픈만 다른 값)")

    files: dict[str, list] = defaultdict(list)
    for n in runs:
        for f in LLM_OUT.glob(f"*.run{n}.json"):
            files[f.name.split(".")[0]].append((n, f))

    rejected = []
    total = kept = repaired = 0
    with db() as conn, conn.cursor() as cur:
        cur.execute("ALTER TABLE stock_tag ADD COLUMN IF NOT EXISTS evidence_source TEXT")
        sync_synonym_table(cur, synonyms)
        for code in sorted(files):
            # key -> {"runs": set, "weight", "direction", "cands": [(우선순위, evidence, source)]}
            best: dict[tuple[str, str], dict] = {}
            n_runs = 0
            rcept_no = None
            for n, f in sorted(files[code]):
                data = json.loads(f.read_text(encoding="utf-8"))
                if not data["tags"]:
                    continue  # 빈 회차(실패)는 분모에서 뺀다
                n_runs += 1
                source = report_path(data["text_path"]).read_text(encoding="utf-8")
                source_norm = norm_for_match(source)
                segs = None
                rcept_no = data["text_path"].rsplit("_", 1)[-1].replace(".txt", "")

                for t in data["tags"]:
                    total += 1
                    reason = validate(t, source_norm)
                    axis = t["axis"]
                    raw_value = str(t.get("value") or "").strip()
                    ev = (t.get("evidence") or "").strip()
                    if reason in ("evidence가 원문에 없음", "evidence 없음/짧음") and axis in REPAIR_AXES:
                        segs = segs if segs is not None else segments(source)
                        value = canonical_value(axis, raw_value, synonyms, spelling)
                        found = repair([raw_value, value], ev, segs)
                        if found and norm_for_match(found) in source_norm:
                            ev, src, reason = found, "repaired", None
                            repaired += 1
                    else:
                        src = "llm"
                    if reason:
                        rejected.append({"stock_code": code, "run": n, "axis": t.get("axis"), "value": t.get("value"),
                                         "reason": reason, "evidence": t.get("evidence")})
                        continue
                    value = canonical_value(axis, raw_value, synonyms, spelling)
                    if stopped(axis, value, stoplist):  # 근거는 있지만 정보가 없는 값 (data/tag_stoplist.csv)
                        rejected.append({"stock_code": code, "run": n, "axis": axis, "value": t.get("value"),
                                         "reason": "포괄적인 값 (tag_stoplist)", "evidence": t.get("evidence")})
                        continue
                    try:
                        w = max(0.0, min(1.0, float(t.get("weight") or 0.5)))
                    except (TypeError, ValueError):
                        w = 0.5
                    d = t.get("direction") if axis == "exposure" else None
                    d = d if d in (1, -1) else None

                    if src == "repaired":
                        prio = 1
                    else:
                        prio = 2 if has_value(ev, [raw_value, value]) else 0
                    e = best.setdefault((axis, value), {"runs": set(), "weight": -1, "direction": None, "cands": []})
                    e["runs"].add(n)
                    if w > e["weight"]:
                        e["weight"], e["direction"] = w, d
                    e["cands"].append((prio, ev, src))

            cur.execute("DELETE FROM stock_tag WHERE stock_code = %s AND source = 'llm'", (code,))
            ranked = {}
            for (axis, value), e in sorted(best.items(), key=lambda kv: (-kv[1]["weight"], kv[0])):
                ranked[axis] = ranked.get(axis, 0) + 1
                _, ev, src = max(e["cands"], key=lambda c: (c[0], -len(c[1])))
                tag_id = upsert_tag(cur, axis, value)
                cur.execute(
                    """INSERT INTO stock_tag (stock_code, tag_id, direction, weight, axis_rank,
                                              source, confidence, evidence, evidence_source, rcept_no)
                       VALUES (%s, %s, %s, %s, %s, 'llm', %s, %s, %s, %s)
                       ON CONFLICT (stock_code, tag_id) DO NOTHING""",  # dart 태그가 이미 있으면 그쪽 유지
                    (code, tag_id, e["direction"], e["weight"], ranked[axis],
                     round(len(e["runs"]) / n_runs, 2), ev, src, rcept_no),
                )
                kept += cur.rowcount
            print(f"{code}: 적재 {len(best)}개 (회차 {n_runs})")

        # 어느 종목에도 안 쓰이고 동의어 사전에도 없는 태그 (표기 정리로 밀려난 값 등)는 사전에서 지운다
        cur.execute("""DELETE FROM tag t WHERE NOT EXISTS (SELECT 1 FROM stock_tag st WHERE st.tag_id = t.tag_id)
                         AND NOT EXISTS (SELECT 1 FROM tag_synonym ts WHERE ts.tag_id = t.tag_id)""")
        print(f"안 쓰는 태그 {cur.rowcount}개 정리")
        sync_relation_table(cur)  # 태그가 지워지면 관계도 CASCADE로 지워지므로 다시 맞춘다
        sync_match_exclude(cur)

    with (DATA / "rejected.csv").open("w", encoding="utf-8", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=["stock_code", "run", "axis", "value", "reason", "evidence"])
        w.writeheader()
        w.writerows(rejected)
    rate = len(rejected) / total if total else 0
    print(f"\n전체 {total}개 중 근거 다시 찾음 {repaired}개, 거부 {len(rejected)}개 ({rate:.0%}), "
          f"적재 {kept}개 → data/rejected.csv")


if __name__ == "__main__":
    main()
