"""1단계: 대상 종목 → corpCode.xml에서 corp_code 찾기 → stock 테이블.

python -m src.s01_load_stocks                       # data/pilot_stocks.txt (파일럿)
python -m src.s01_load_stocks --index KOSPI200=data/kospi200.csv --index KOSDAQ150=data/kosdaq150.csv

--index 파일은 KRX 정보데이터시스템 '지수구성종목' CSV를 그대로 쓴다 (cp949/utf-8 모두 가능).
'종목코드' 또는 'stock_code' 열만 있으면 된다.
"""
from __future__ import annotations

import argparse
import csv
import io
import xml.etree.ElementTree as ET
from pathlib import Path

from .common import DATA, dart_zip, db, decode

CODE_COLUMNS = ("종목코드", "단축코드", "stock_code", "code")


def load_corp_codes() -> list[dict]:
    """상장사만. 결과는 data/corp_codes.csv에 캐시."""
    cache = DATA / "corp_codes.csv"
    if cache.exists():
        with cache.open(encoding="utf-8") as f:
            return list(csv.DictReader(f))

    z = dart_zip("corpCode")
    root = ET.fromstring(z.read(z.namelist()[0]))
    rows = []
    for el in root.iter("list"):
        sc = (el.findtext("stock_code") or "").strip()
        if not sc:
            continue
        rows.append({
            "corp_code": el.findtext("corp_code").strip(),
            "corp_name": el.findtext("corp_name").strip(),
            "stock_code": sc,
        })
    with cache.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["corp_code", "corp_name", "stock_code"])
        w.writeheader()
        w.writerows(rows)
    print(f"corpCode: 상장사 {len(rows)}개 캐시 → {cache}")
    return rows


def read_index_file(path: Path) -> list[str]:
    """지수 구성종목 CSV → 6자리 종목코드 목록. Excel에서 앞자리 0이 빠진 경우도 복구."""
    text = decode(path.read_bytes()).lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    col = next((c for c in CODE_COLUMNS if c in (reader.fieldnames or [])), None)
    if not col:
        raise SystemExit(f"{path}: 종목코드 열을 찾지 못함. 열 목록: {reader.fieldnames}")
    codes = []
    for row in reader:
        c = (row[col] or "").strip().strip("'\"")
        if not c:
            continue
        codes.append(c.zfill(6) if c.isdigit() else c.upper())
    return codes


def read_pilot() -> list[str]:
    return [
        line.strip()
        for line in (DATA / "pilot_stocks.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", action="append", default=[], metavar="NAME=CSV",
                    help="지수 구성종목 파일. 여러 번 지정 가능 (예: KOSPI200=data/kospi200.csv)")
    args = ap.parse_args()

    # (입력값, 지수명) 목록
    wanted: list[tuple[str, str | None]] = []
    if args.index:
        for spec in args.index:
            name, _, path = spec.partition("=")
            if not path:
                raise SystemExit(f"--index 형식은 NAME=CSV 입니다: {spec}")
            codes = read_index_file(Path(path))
            print(f"{name}: {len(codes)}종목 ({path})")
            wanted += [(c, name) for c in codes]
    else:
        wanted = [(w, None) for w in read_pilot()]

    corps = load_corp_codes()
    by_name = {}
    for c in corps:
        by_name.setdefault(c["corp_name"], []).append(c)
    by_code = {c["stock_code"]: c for c in corps}

    found: dict[str, tuple[dict, str | None]] = {}
    missing = []
    for w, index_name in wanted:
        if w in by_code:
            c = by_code[w]
        elif w in by_name and len(by_name[w]) == 1:
            c = by_name[w][0]
        elif w in by_name:
            print(f"[중복] {w}: {[c['stock_code'] for c in by_name[w]]} → 종목코드로 적어 주세요")
            missing.append(w)
            continue
        else:
            hints = [c["corp_name"] for c in corps if w in c["corp_name"] or c["corp_name"] in w][:5]
            print(f"[찾지 못함] {w}  후보: {hints}")
            missing.append(w)
            continue
        if c["stock_code"] in found and found[c["stock_code"]][1] != index_name:
            print(f"[두 지수에 중복] {c['corp_name']} ({c['stock_code']})")
        found[c["stock_code"]] = (c, index_name)

    with db() as conn, conn.cursor() as cur:
        if args.index:
            # 지수 편입 정보는 매번 새로 매긴다 (편출 종목은 NULL로 남음)
            cur.execute("UPDATE stock SET index_name = NULL")
        for c, index_name in found.values():
            cur.execute(
                """INSERT INTO stock (stock_code, corp_code, name, index_name) VALUES (%s, %s, %s, %s)
                   ON CONFLICT (stock_code) DO UPDATE
                   SET corp_code = EXCLUDED.corp_code, name = EXCLUDED.name,
                       index_name = COALESCE(EXCLUDED.index_name, stock.index_name), updated_at = now()""",
                (c["stock_code"], c["corp_code"], c["corp_name"], index_name),
            )
    print(f"적재 {len(found)}개, 실패 {len(missing)}개")


if __name__ == "__main__":
    main()
