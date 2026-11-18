"""공통: 설정, DB 연결, OpenDART 호출, 텍스트 정규화, 태그 사전."""
from __future__ import annotations

import csv
import html
import io
import os
import re
import time
import zipfile
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
REPORTS = DATA / "reports"
LLM_OUT = DATA / "llm"


BACKSLASH = chr(92)  # Windows 경로 구분자


def report_rel(path) -> str:
    """DB·JSON에 저장하는 섹션 파일 경로. PC마다 달라지지 않게 프로젝트 기준 상대 경로로."""
    return "data/reports/" + str(path).replace(BACKSLASH, "/").rsplit("/", 1)[-1]


def report_path(text_path: str) -> Path:
    """저장된 경로(예전 절대 경로 포함) → 이 PC의 실제 파일."""
    return REPORTS / text_path.replace(BACKSLASH, "/").rsplit("/", 1)[-1]

DART_BASE = "https://opendart.fss.or.kr/api"
DART_SLEEP = float(os.getenv("DART_SLEEP", "0.3"))

LLM_AXES = ("product", "application", "customer", "raw_material", "region", "exposure")
ALL_AXES = ("industry_code",) + LLM_AXES


# ---------------------------------------------------------------- DB
def db():
    import psycopg

    url = os.getenv("DATABASE_URL")
    if not url:
        raise SystemExit("DATABASE_URL이 .env에 없습니다")
    return psycopg.connect(url)


def upsert_tag(cur, axis: str, value: str) -> int:
    cur.execute(
        """INSERT INTO tag (axis, value) VALUES (%s, %s)
           ON CONFLICT (axis, value) DO UPDATE SET value = EXCLUDED.value
           RETURNING tag_id""",
        (axis, value),
    )
    return cur.fetchone()[0]


def target_stocks(cur) -> list[tuple[str, str, str]]:
    """(stock_code, corp_code, name) 목록."""
    cur.execute("SELECT stock_code, corp_code, name FROM stock ORDER BY stock_code")
    return cur.fetchall()


# ---------------------------------------------------------------- OpenDART
def _key() -> str:
    k = os.getenv("DART_API_KEY")
    if not k:
        raise SystemExit("DART_API_KEY가 .env에 없습니다")
    return k


def dart_json(endpoint: str, **params) -> dict:
    params["crtfc_key"] = _key()
    r = requests.get(f"{DART_BASE}/{endpoint}.json", params=params, timeout=30)
    r.raise_for_status()
    time.sleep(DART_SLEEP)
    return r.json()


def dart_zip(endpoint: str, **params) -> zipfile.ZipFile:
    """corpCode.xml, document.xml 처럼 zip으로 오는 응답. 오류면 XML/JSON 메시지가 온다."""
    params["crtfc_key"] = _key()
    r = requests.get(f"{DART_BASE}/{endpoint}.xml", params=params, timeout=60)
    r.raise_for_status()
    time.sleep(DART_SLEEP)
    if not r.content.startswith(b"PK"):
        raise RuntimeError(f"{endpoint} zip 아님: {r.content[:300].decode('utf-8', 'replace')}")
    return zipfile.ZipFile(io.BytesIO(r.content))


# ---------------------------------------------------------------- 텍스트
def decode(raw: bytes) -> str:
    for enc in ("utf-8", "cp949", "euc-kr"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def dart_xml_to_text(raw: bytes) -> str:
    """DART 공시 XML → 줄 단위 텍스트. 표 셀은 ' | '로 구분."""
    s = decode(raw)
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</tr>|</title>|</table>", "\n", s)
    s = re.sub(r"(?i)</t[dheu]>", " | ", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t 　]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


# 제목은 한 줄을 통째로 차지한다. "Ⅱ. 사업의 내용을 참조하시기 바랍니다" 같은 본문 속 참조 문구는 제외.
SECTION_START = re.compile(r"(?m)^[ \t]*(?:II|Ⅱ)\s*\.\s*사업의\s*내용[ \t]*$")
SECTION_END = re.compile(r"(?m)^[ \t]*(?:III|Ⅲ)\s*\.\s*재무에\s*관한\s*사항[ \t]*$")


def cut_business_section(text: str) -> str:
    """'II. 사업의 내용' ~ 'III. 재무에 관한 사항' 사이. 목차 때문에 여러 번 나오면 가장 긴 구간."""
    best = ""
    for m in SECTION_START.finditer(text):
        end = SECTION_END.search(text, m.end())
        chunk = text[m.start(): end.start() if end else len(text)]
        if len(chunk) > len(best):
            best = chunk
    return best.strip()


def norm_for_match(s: str) -> str:
    """근거 문장 검증용: 공백, 표 구분자, 일부 문장부호 제거 + 소문자."""
    s = html.unescape(s or "").lower()
    return re.sub(r"[\s|·ㆍ,.\-()\[\]\"'“”‘’]+", "", s)


def norm_key(s: str) -> str:
    """태그 값/동의어 비교용."""
    return re.sub(r"[\s\-·ㆍ]+", "", (s or "").lower())


def normalize_tag_shape(t: dict) -> dict:
    """LLM 출력 모양 정리 (내용은 바꾸지 않음).
    - axis 앞뒤 공백/대소문자
    - {"application": "건설"}처럼 축 이름을 키로 쓴 경우 → axis/value로 옮김
    evidence 검증은 이 뒤에 그대로 한다."""
    axis = str(t.get("axis") or "").strip().lower()
    value = t.get("value")
    if not str(value or "").strip():
        keyed = [k for k in LLM_AXES if isinstance(t.get(k), str) and t[k].strip()]
        if axis in keyed:
            value = t[axis]
        elif not axis and len(keyed) == 1:
            axis, value = keyed[0], t[keyed[0]]
    t["axis"], t["value"] = axis, value
    return t


# ---------------------------------------------------------------- 태그 사전
def load_synonyms() -> dict[tuple[str, str], str]:
    """(axis, norm_key(synonym)) -> canonical."""
    out: dict[tuple[str, str], str] = {}
    path = DATA / "tag_synonyms.csv"
    if not path.exists():
        return out
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            axis, canon, syn = row["axis"].strip(), row["canonical"].strip(), row["synonym"].strip()
            out[(axis, norm_key(syn))] = canon
            out[(axis, norm_key(canon))] = canon
    return out


def canonical(axis: str, value: str, synonyms: dict) -> str:
    v = re.sub(r"\s+", " ", value).strip()
    return synonyms.get((axis, norm_key(v)), v)


def spelling_map(synonyms: dict) -> dict[tuple[str, str], str]:
    """띄어쓰기·대소문자·하이픈·가운뎃점만 다른 표기 → 대표 표기 하나.

    LLM 결과 전체(run0, run1 모두)에서 (axis, norm_key)가 같은 값끼리 묶고,
    가장 많은 종목에 쓰인 표기를 대표로 한다 (동률이면 글자순). 뜻은 바꾸지 않는 표기 정리라
    tag_synonyms.csv에 넣지 않고 매번 계산한다. 동의어 사전이 먼저 적용된다."""
    import json
    from collections import Counter, defaultdict

    stocks: dict[tuple[str, str], set[str]] = defaultdict(set)
    for f in LLM_OUT.glob("*.run*.json"):
        code = f.name.split(".")[0]
        for t in json.loads(f.read_text(encoding="utf-8"))["tags"]:
            t = normalize_tag_shape(dict(t))
            v = str(t["value"] or "").strip()
            if t["axis"] and v:
                stocks[(t["axis"], canonical(t["axis"], v, synonyms))].add(code)
    groups: dict[tuple[str, str], Counter] = defaultdict(Counter)
    for (axis, v), codes in stocks.items():
        groups[(axis, norm_key(v))][v] = len(codes)
    return {k: min(c, key=lambda v: (-c[v], v)) for k, c in groups.items() if len(c) > 1}


def canonical_value(axis: str, value: str, synonyms: dict, spelling: dict) -> str:
    """동의어 사전 → 표기 정리 순서로 대표 표기."""
    v = canonical(axis, value, synonyms)
    return spelling.get((axis, norm_key(v)), v)


def sync_synonym_table(cur, synonyms: dict) -> None:
    """tag_synonyms.csv 내용을 DB tag_synonym에도 반영 (뉴스 매칭 단계에서 사용)."""
    path = DATA / "tag_synonyms.csv"
    if not path.exists():
        return
    with path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            tag_id = upsert_tag(cur, row["axis"].strip(), row["canonical"].strip())
            cur.execute(
                """INSERT INTO tag_synonym (synonym, tag_id) VALUES (%s, %s)
                   ON CONFLICT (synonym) DO UPDATE SET tag_id = EXCLUDED.tag_id""",
                (row["synonym"].strip(), tag_id),
            )


# ---------------------------------------------------------------- 지역
SIDO = [
    # (주소 앞부분 패턴, 시도 표준명, 권역)
    (r"^서울", "서울", "수도권"),
    (r"^경기", "경기", "수도권"),
    (r"^인천", "인천", "수도권"),
    (r"^부산", "부산", "영남"),
    (r"^대구", "대구", "영남"),
    (r"^울산", "울산", "영남"),
    (r"^경상?남|^경남", "경남", "영남"),
    (r"^경상?북|^경북", "경북", "영남"),
    (r"^광주", "광주", "호남"),
    (r"^전라?남|^전남", "전남", "호남"),
    (r"^전라?북|^전북", "전북", "호남"),
    (r"^대전", "대전", "충청"),
    (r"^세종", "세종", "충청"),
    (r"^충청?남|^충남", "충남", "충청"),
    (r"^충청?북|^충북", "충북", "충청"),
    (r"^강원", "강원", "강원"),
    (r"^제주", "제주", "제주"),
]


def parse_region(address: str) -> tuple[str, str] | None:
    """주소 → (시도, 권역). 앞의 우편번호·괄호는 무시."""
    a = re.sub(r"^[\s(\[]*\d{3}-?\d{2,3}[)\]]?\s*", "", address or "").strip()
    a = a.replace("특별시", "").replace("광역시", "")
    for pat, sido, area in SIDO:
        if re.match(pat, a):
            return sido, area
    return None
