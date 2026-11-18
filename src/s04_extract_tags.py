"""4단계: 사업의 내용 섹션 → LLM 태그 추출 (근거 구절 포함).

python -m src.s04_extract_tags [--runs 2] [--only 001820]
python -m src.s04_extract_tags --ping          # 키·모델 확인
python -m src.s04_extract_tags --list-models   # (Gemini 등) 쓸 수 있는 모델 이름
결과: data/llm/{stock_code}.run{n}.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time

from .common import LLM_OUT, db, report_path, report_rel

PROVIDER = os.getenv("LLM_PROVIDER", "anthropic").strip().lower()
MODEL = os.getenv("LLM_MODEL", "claude-sonnet-5-5")
MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "8000"))  # thinking 모델은 생각 토큰도 여기서 소모
LLM_SLEEP = float(os.getenv("LLM_SLEEP", "0"))          # 호출 간격(초). 무료 할당량이면 분당 제한에 맞춰 설정
MAX_CHARS = int(os.getenv("LLM_MAX_CHARS", "40000"))
MAX_PER_AXIS = int(os.getenv("TAG_MAX_PER_AXIS", "5"))
MAX_PRODUCT = int(os.getenv("TAG_MAX_PRODUCT", "8"))   # 세부 품목은 다른 축보다 많이
# 프롬프트를 고치면 올린다. 결과 JSON에 기록되어, 어떤 종목이 어느 버전으로 뽑혔는지 추적 가능
PROMPT_VERSION = 2

PROMPT = """너는 한국 상장사 사업보고서의 "사업의 내용" 섹션에서 종목 속성 태그를 뽑는 도구다.

회사: {name}

## 축 정의
- product: 회사가 만들거나 파는 구체적인 품목·서비스. 사업부문명("전자부품 부문", "소재사업")이 아니라 그 안의 품목명을 쓴다 (예: MLCC, HBM, FC-BGA, 초전도 선재, 양극재, 바이오시밀러). 원문이 부문명만 쓰고 품목을 밝히지 않으면 부문명을 써도 된다. 브랜드·모델명(참이슬, 갤럭시 S25)이 아니라 일반 품목명(소주, 스마트폰)을 쓴다. 부문 약칭("DX 부문", "DS 부문")이나 자회사·계열사 이름(Harman, SDC)은 product가 아니다. 매출 비중이 큰 핵심 품목을 빠뜨리지 않는다.
- application: 제품이 쓰이는 전방 산업·응용처·최종 수요처 (예: 데이터센터, 전기차, AI 서버, 스마트폰, 조선, 전력망). 회사 이름은 여기 쓰지 않는다.
- customer: 원문에 이름이 나온 주요 매출처·고객사 (예: 삼성전자, Apple, 현대자동차). 산업명은 application에 쓴다.
- raw_material: 주요 원재료·부품 (예: 니켈, 웨이퍼)
- region: 생산설비·사업장이 있는 곳. 국내는 시·군이 아니라 시/도 약칭 하나로 쓴다 (서울, 부산, 대구, 인천, 광주, 대전, 울산, 세종, 경기, 강원, 충북, 충남, 전북, 전남, 경북, 경남, 제주. 예: 이천→경기, 마산→경남). 해외는 국가명 (미국, 중국, 베트남). 본사 주소는 제외.
- exposure: 원문이 실적 변동 요인으로 직접 언급한 외부 변수 (예: 환율, 유가, 금리, 원자재 가격, 특정 산업 수요)

## 규칙
1. 원문에 근거가 있는 것만 뽑는다. 상식이나 배경지식으로 추가하지 않는다.
2. evidence는 원문에서 그대로 복사한 연속 구절(10~150자)이다. 바꿔 쓰거나 요약하지 않는다. 검증 프로그램이 evidence가 원문에 글자 그대로 있는지 확인하고, 없으면 태그를 버린다.
   - 가능하면 표보다 본문 문장에서 근거를 찾는다.
   - 표에서 가져올 때는 셀 하나 안의 구절만 쓴다. 여러 셀·여러 행을 이어 붙이거나 표 제목과 셀을 합치지 않는다.
3. product는 최대 {kp}개, 나머지 축은 최대 {k}개. 중요한 순서로.
4. value는 짧은 명사(2~20자). 회사 자신의 이름은 쓰지 않는다.
5. weight는 0~1. 이 회사 사업에서의 비중·중요도. 원문에 매출 비중이 있으면 반영한다.
6. direction은 exposure에만 쓴다. 그 변수가 오를 때 회사에 유리하다고 원문에서 읽히면 1, 불리하면 -1, 판단할 수 없으면 null. 다른 축은 null.
7. 해당하는 내용이 없는 축은 비워 둔다.

## 출력
JSON 하나만 출력한다. 설명 문장 금지.
{{"tags": [{{"axis": "product", "value": "MLCC", "weight": 0.9, "direction": null, "evidence": "원문 구절"}}]}}

## 원문 ({part})
<source>
{text}
</source>"""


def chunks(text: str, size: int, overlap: int = 1000) -> list[str]:
    if len(text) <= size:
        return [text]
    out, i = [], 0
    while i < len(text):
        out.append(text[i: i + size])
        i += size - overlap
    return out


def parse_json(s: str) -> dict:
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        raise ValueError("JSON 없음")
    return json.loads(m.group(0))


def make_client():
    """LLM_PROVIDER=anthropic(기본) 또는 openai(OpenAI 호환: Gemini, OpenAI, Ollama 등)."""
    if PROVIDER == "anthropic":
        import anthropic
        return anthropic.Anthropic()
    from openai import OpenAI
    key = os.getenv("LLM_API_KEY")
    if not key:
        raise SystemExit("LLM_API_KEY가 .env에 없습니다")
    return OpenAI(base_url=os.getenv("LLM_BASE_URL") or None, api_key=key)


def _call_once(client, prompt: str) -> str:
    if PROVIDER == "anthropic":
        msg = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    r = client.chat.completions.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},  # 키 누락 같은 JSON 깨짐 방지
    )
    return r.choices[0].message.content or ""


class DailyQuotaExhausted(Exception):
    """하루 무료 할당량 소진. 기다려도 소용없으니 멈추고 내일 이어서 돌린다."""


def call_llm(client, prompt: str, retries: int = 5) -> str:
    """무료 할당량의 분당 제한(429)에 걸리면 기다렸다가 다시 시도."""
    for attempt in range(retries):
        try:
            out = _call_once(client, prompt)
            time.sleep(LLM_SLEEP)
            return out
        except Exception as e:  # SDK마다 예외 클래스가 달라서 상태코드로 판단
            status = getattr(e, "status_code", None)
            if status == 429 and "perday" in str(e).replace(" ", "").lower():
                raise DailyQuotaExhausted(str(e)[:400]) from e
            if status in (429, 500, 503) and attempt < retries - 1:
                wait = 30 * (attempt + 1)
                print(f"  [{status}] {wait}초 후 재시도 ({attempt + 1}/{retries - 1})")
                time.sleep(wait)
                continue
            raise


def extract(client, name: str, text: str) -> dict:
    parts = chunks(text, MAX_CHARS)
    tags, raws = [], []
    for i, part in enumerate(parts, 1):
        prompt = PROMPT.format(name=name, k=MAX_PER_AXIS, kp=MAX_PRODUCT, part=f"{i}/{len(parts)}", text=part)
        raw = call_llm(client, prompt)
        raws.append(raw)
        try:
            tags += parse_json(raw).get("tags", [])
        except (ValueError, json.JSONDecodeError) as e:
            print(f"  [파싱 실패 part {i}] {e}")
    return {"model": MODEL, "prompt_version": PROMPT_VERSION, "parts": len(parts), "tags": tags, "raw": raws}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1, help="일관성 확인용 반복 횟수")
    ap.add_argument("--only", help="종목코드 하나만")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--list-models", action="store_true", help="사용 가능한 모델 이름 출력 (openai 방식만)")
    ap.add_argument("--ping", action="store_true", help="키·모델 확인용 짧은 호출 한 번")
    args = ap.parse_args()

    client = make_client()
    if args.list_models:
        for m in client.models.list():
            print(m.id)
        return
    if args.ping:
        print(f"{PROVIDER} / {MODEL}: {call_llm(client, '한 단어로 답해: 대한민국의 수도는?')!r}")
        return
    LLM_OUT.mkdir(parents=True, exist_ok=True)

    with db() as conn, conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT ON (r.stock_code) r.stock_code, s.name, r.text_path
               FROM report_source r JOIN stock s USING (stock_code)
               ORDER BY r.stock_code, r.rcept_no DESC"""
        )
        rows = cur.fetchall()

    # 전 종목 run0을 먼저 끝내고 run1로 (할당량이 끊겨도 태그 없는 종목이 덜 남게)
    failed = []
    done = skipped = 0
    try:
        for n in range(args.runs):
            for code, name, text_path in rows:
                if args.only and code != args.only:
                    continue
                out = LLM_OUT / f"{code}.run{n}.json"
                if out.exists() and not args.force:
                    skipped += 1
                    continue
                text = report_path(text_path).read_text(encoding="utf-8")
                if len(text) < 500:
                    print(f"[건너뜀] {name}: 섹션 {len(text)}자")
                    continue
                try:
                    result = extract(client, name, text)
                except DailyQuotaExhausted:
                    raise
                except Exception as e:
                    print(f"[실패] {name} run{n}: {type(e).__name__} {str(e)[:200]}")
                    failed.append((name, n))
                    continue
                result["text_path"] = report_rel(text_path)
                out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                done += 1
                print(f"{name} run{n}: 태그 {len(result['tags'])}개 (part {result['parts']})", flush=True)
    except DailyQuotaExhausted as e:
        print(f"\n[하루 할당량 소진] 여기서 멈춤. 내일 같은 명령을 다시 실행하면 이어서 진행.\n  {e}")
    print(f"\n이번 실행 {done}건, 이미 있음 {skipped}건, 실패 {len(failed)}건 {failed[:20]}")


if __name__ == "__main__":
    main()
