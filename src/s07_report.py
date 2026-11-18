"""7단계: 품질 점검.

python -m src.s07_report            # 점검 쿼리 + 실행 간 일관성 + 검수 샘플 CSV 생성
python -m src.s07_report --score    # data/review_sample.csv의 correct 열(1/0)로 축별 정확도
"""
from __future__ import annotations

import csv
import json
import random
import re
import sys
from collections import defaultdict

from .common import DATA, LLM_OUT, ROOT, canonical_value, db, load_synonyms, normalize_tag_shape, spelling_map

REVIEW = DATA / "review_sample.csv"


def run_checks(cur):
    sql = (ROOT / "sql" / "02_checks.sql").read_text(encoding="utf-8")
    for block in re.split(r"(?m)^-- name:", sql)[1:]:
        title, _, query = block.partition("\n")
        print(f"\n### {title.strip()}")
        cur.execute(query.strip().rstrip(";"))
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()
        print(" | ".join(cols))
        for r in rows[:50]:
            print(" | ".join("" if v is None else str(v) for v in r))
        if not rows:
            print("(없음)")


def consistency():
    """같은 종목의 run0, run1 태그 겹침 (축별 Jaccard)."""
    syn = load_synonyms()
    spell = spelling_map(syn)
    per_axis = defaultdict(list)
    for f0 in LLM_OUT.glob("*.run0.json"):
        f1 = f0.with_name(f0.name.replace(".run0.", ".run1."))
        if not f1.exists():
            continue
        sets = []
        for f in (f0, f1):
            tags = json.loads(f.read_text(encoding="utf-8"))["tags"]
            s = defaultdict(set)
            for t in map(normalize_tag_shape, tags):
                if t["axis"] and str(t["value"] or "").strip():
                    s[t["axis"]].add(canonical_value(t["axis"], str(t["value"]), syn, spell))
            sets.append(s)
        for axis in set(sets[0]) | set(sets[1]):
            a, b = sets[0][axis], sets[1][axis]
            per_axis[axis].append(len(a & b) / len(a | b) if a | b else 1.0)
    print("\n### 실행 간 일관성 (run0 vs run1, 축별 평균 Jaccard)")
    if not per_axis:
        print("(run1 결과 없음: s04를 --runs 2로 돌리세요)")
    for axis, vals in sorted(per_axis.items()):
        print(f"{axis}: {sum(vals) / len(vals):.2f}  (종목 {len(vals)}개)")


def export_review(cur, per_axis: int = 15):
    if REVIEW.exists():
        print(f"\n검수 파일이 이미 있어 덮어쓰지 않음: {REVIEW}")
        return
    cur.execute(
        """SELECT s.name, t.axis, t.value, st.direction, st.weight, st.evidence
           FROM stock_tag st JOIN tag t USING (tag_id) JOIN stock s USING (stock_code)
           WHERE st.source = 'llm'"""
    )
    rows = cur.fetchall()
    random.seed(42)
    by_axis = defaultdict(list)
    for r in rows:
        by_axis[r[1]].append(r)
    sample = [r for axis in by_axis for r in random.sample(by_axis[axis], min(per_axis, len(by_axis[axis])))]
    with REVIEW.open("w", encoding="utf-8-sig", newline="") as f:  # 엑셀에서 한글 깨짐 방지
        w = csv.writer(f)
        w.writerow(["name", "axis", "value", "direction", "weight", "evidence", "correct", "note"])
        for r in sample:
            w.writerow(list(r) + ["", ""])
    print(f"\n검수 샘플 {len(sample)}건 → {REVIEW}  (correct 열에 1/0 기입 후 --score)")


def score():
    stats = defaultdict(lambda: [0, 0])
    with REVIEW.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            c = row["correct"].strip()
            if c not in ("0", "1"):
                continue
            stats[row["axis"]][0] += int(c)
            stats[row["axis"]][1] += 1
    print("### 축별 정확도 (사람 검수)")
    for axis, (ok, n) in sorted(stats.items()):
        print(f"{axis}: {ok}/{n} = {ok / n:.0%}")


def main():
    if "--score" in sys.argv:
        score()
        return
    with db() as conn, conn.cursor() as cur:
        run_checks(cur)
        export_review(cur)
    consistency()
    rej = DATA / "rejected.csv"
    if rej.exists():
        with rej.open(encoding="utf-8") as f:
            reasons = defaultdict(int)
            for r in csv.DictReader(f):
                reasons[r["reason"]] += 1
        print("\n### 거부 사유")
        for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"{k}: {v}")


if __name__ == "__main__":
    main()
