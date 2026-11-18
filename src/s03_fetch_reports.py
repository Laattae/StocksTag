"""3단계: 최근 사업보고서 원문 → "II. 사업의 내용" 섹션 텍스트 저장.

python -m src.s03_fetch_reports [--force]   # --force: 이미 자른 것도 다시 자르기

원문 전체는 data/reports/full/{rcept_no}.txt에 캐시한다. --force로 다시 돌려도
원문을 다시 내려받지 않고 캐시에서 섹션만 다시 자른다.
"""
from __future__ import annotations

import datetime as dt
import sys

from .common import REPORTS, SECTION_START, cut_business_section, dart_json, dart_xml_to_text, dart_zip, db, report_rel, target_stocks

MIN_SECTION_CHARS = 3000  # 이보다 짧으면 섹션 자르기 실패를 의심
FULL = REPORTS / "full"


def annual_reports(corp_code: str) -> list[dict]:
    """최근 1년 반 사업보고서, 최신순."""
    end = dt.date.today()
    bgn = end - dt.timedelta(days=550)
    j = dart_json(
        "list", corp_code=corp_code, bgn_de=bgn.strftime("%Y%m%d"), end_de=end.strftime("%Y%m%d"),
        pblntf_detail_ty="A001", last_reprt_at="N", page_count=100,  # 정정 이력 전체
    )
    if j.get("status") != "000":
        print(f"  list: {j.get('status')} {j.get('message')}")
        return []
    # [첨부정정]/[첨부추가]는 첨부서류만 바뀐 공시라 본문 문서가 없다 (document API status 014)
    reports = [r for r in j.get("list", [])
               if "사업보고서" in r.get("report_nm", "") and not r["report_nm"].lstrip().startswith("[첨부")]
    return sorted(reports, key=lambda r: r["rcept_no"], reverse=True)


def fetch_text(rcept_no: str) -> str:
    cache = FULL / f"{rcept_no}.txt"
    if cache.exists():
        return cache.read_text(encoding="utf-8")
    z = dart_zip("document", rcept_no=rcept_no)
    # zip 안에서 "II. 사업의 내용" 제목이 있는 파일이 본문. 정정 공시는 감사보고서만 들어 있기도 하다.
    names = sorted(z.namelist(), key=lambda n: z.getinfo(n).file_size, reverse=True)
    for n in names:
        text = dart_xml_to_text(z.read(n))
        if SECTION_START.search(text):
            cache.write_text(text, encoding="utf-8")
            return text
    raise RuntimeError(f"본문 없음 (zip 안 파일: {names})")


def main():
    force = "--force" in sys.argv
    FULL.mkdir(parents=True, exist_ok=True)
    failed, short = [], []
    with db() as conn, conn.cursor() as cur:
        for code, corp_code, name in target_stocks(cur):
            reports = annual_reports(corp_code)
            if not reports:
                print(f"[보고서 없음] {name}")
                failed.append((name, "보고서 없음"))
                continue

            # 최신 보고서 원문이 없으면(status 014 등) 그다음 보고서로
            rep, full, errors = None, None, []
            for r in reports:
                path = REPORTS / f"{code}_{r['rcept_no']}.txt"
                if path.exists() and not force:
                    rep = r
                    break
                try:
                    full = fetch_text(r["rcept_no"])
                    rep = r
                    break
                except Exception as e:
                    errors.append(f"{r['rcept_no']}: {str(e)[:150]}")
            if rep is None:
                print(f"[원문 실패] {name}: {errors}")
                failed.append((name, "; ".join(errors)))
                continue
            rcept_no = rep["rcept_no"]
            path = REPORTS / f"{code}_{rcept_no}.txt"
            if full is None:
                # 섹션 파일이 이미 있음 (저장소에서 받은 경우 포함): 다시 자르지 않고 DB 기록만 맞춘다
                section = path.read_text(encoding="utf-8")
            else:
                section = cut_business_section(full)
                path.write_text(section, encoding="utf-8")
            if len(section) < MIN_SECTION_CHARS:
                print(f"[섹션 짧음 {len(section)}자] {name} {rcept_no} → full/{rcept_no}.txt 확인 필요")
                short.append(name)

            # 이 종목의 report_source는 지금 쓰는 보고서 하나만 남긴다
            cur.execute("DELETE FROM report_source WHERE stock_code = %s AND rcept_no <> %s", (code, rcept_no))
            cur.execute(
                """INSERT INTO report_source (stock_code, rcept_no, report_nm, section_chars, text_path)
                   VALUES (%s, %s, %s, %s, %s)
                   ON CONFLICT (stock_code, rcept_no) DO UPDATE
                   SET section_chars = EXCLUDED.section_chars, text_path = EXCLUDED.text_path, fetched_at = now()""",
                (code, rcept_no, rep["report_nm"], len(section), report_rel(path)),
            )
            conn.commit()
            note = f" (최신 아님: {errors})" if errors else ""
            print(f"{name}: {rep['report_nm'].strip()} {rcept_no}, 섹션 {len(section):,}자{note}")

    print(f"\n실패 {len(failed)}개, 섹션 짧음 {len(short)}개")
    for n, why in failed:
        print(f"  실패: {n} — {why}")
    for n in short:
        print(f"  짧음: {n}")


if __name__ == "__main__":
    main()
