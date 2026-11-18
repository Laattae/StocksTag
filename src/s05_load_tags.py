"""5단계: LLM 결과 검증 → 정규화 → stock_tag 적재.

- evidence가 원문에 실제로 들어 있지 않으면 버린다 (data/rejected.csv)
- 동의어 사전으로 값 통일 (적층세라믹콘덴서 → MLCC)
- 띄어쓰기·대소문자·하이픈만 다른 표기는 가장 많이 쓰인 표기로 (재활 치료 → 재활치료)
- 같은 태그가 여러 번 나오면 weight 최댓값
- 축 안에서 weight 순위(axis_rank) 저장. 자르지는 않는다 (상한은 stock_tag_topk 뷰로 본다)

python -m src.s05_load_tags [--run 0]
"""
from __future__ import annotations

import argparse
import csv
import json

from .common import DATA, LLM_AXES, LLM_OUT, canonical_value, db, load_synonyms, norm_for_match, normalize_tag_shape, report_path, spelling_map, sync_synonym_table, upsert_tag


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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=int, default=0)
    args = ap.parse_args()

    synonyms = load_synonyms()
    spelling = spelling_map(synonyms)
    print(f"표기 정리 규칙 {len(spelling)}개 (띄어쓰기·대소문자·하이픈만 다른 값)")
    rejected = []
    total = kept = 0

    with db() as conn, conn.cursor() as cur:
        sync_synonym_table(cur, synonyms)
        for f in sorted(LLM_OUT.glob(f"*.run{args.run}.json")):
            code = f.name.split(".")[0]
            data = json.loads(f.read_text(encoding="utf-8"))
            source_norm = norm_for_match(report_path(data["text_path"]).read_text(encoding="utf-8"))
            rcept_no = data["text_path"].rsplit("_", 1)[-1].replace(".txt", "")

            best: dict[tuple[str, str], dict] = {}
            for t in data["tags"]:
                total += 1
                reason = validate(t, source_norm)
                if reason:
                    rejected.append({"stock_code": code, "axis": t.get("axis"), "value": t.get("value"),
                                     "reason": reason, "evidence": t.get("evidence")})
                    continue
                axis = t["axis"]
                value = canonical_value(axis, t["value"], synonyms, spelling)
                try:
                    w = max(0.0, min(1.0, float(t.get("weight") or 0.5)))
                except (TypeError, ValueError):
                    w = 0.5
                d = t.get("direction") if axis == "exposure" else None
                d = d if d in (1, -1) else None
                key = (axis, value)
                if key not in best or w > best[key]["weight"]:
                    best[key] = {"weight": w, "direction": d, "evidence": t["evidence"].strip()}

            cur.execute("DELETE FROM stock_tag WHERE stock_code = %s AND source = 'llm'", (code,))
            ranked = {}
            for (axis, value), t in sorted(best.items(), key=lambda kv: -kv[1]["weight"]):
                ranked[axis] = ranked.get(axis, 0) + 1
                tag_id = upsert_tag(cur, axis, value)
                cur.execute(
                    """INSERT INTO stock_tag (stock_code, tag_id, direction, weight, axis_rank,
                                              source, confidence, evidence, rcept_no)
                       VALUES (%s, %s, %s, %s, %s, 'llm', NULL, %s, %s)
                       ON CONFLICT (stock_code, tag_id) DO NOTHING""",  # dart 태그가 이미 있으면 그쪽 유지
                    (code, tag_id, t["direction"], t["weight"], ranked[axis], t["evidence"], rcept_no),
                )
                kept += 1
            print(f"{code}: 적재 {len(best)}개")

        # 어느 종목에도 안 쓰이고 동의어 사전에도 없는 태그 (표기 정리로 밀려난 값 등)는 사전에서 지운다
        cur.execute("""DELETE FROM tag t WHERE NOT EXISTS (SELECT 1 FROM stock_tag st WHERE st.tag_id = t.tag_id)
                         AND NOT EXISTS (SELECT 1 FROM tag_synonym ts WHERE ts.tag_id = t.tag_id)""")
        print(f"안 쓰는 태그 {cur.rowcount}개 정리")

    with (DATA / "rejected.csv").open("w", encoding="utf-8", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=["stock_code", "axis", "value", "reason", "evidence"])
        w.writeheader()
        w.writerows(rejected)
    rate = len(rejected) / total if total else 0
    print(f"\n전체 {total}개 중 거부 {len(rejected)}개 ({rate:.0%}), 적재 {kept}개 → data/rejected.csv")


if __name__ == "__main__":
    main()
