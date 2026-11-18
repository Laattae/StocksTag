# stock-tagging — 종목 속성 태깅 파일럿

뉴스는 아직 다루지 않는다. 이 단계의 목표는 **종목마다 공시 근거가 있는 속성 태그와 alias를 PostgreSQL에 쌓고, 그 품질을 숫자로 확인하는 것**이다.

## 무엇을 만드나

| 축(axis) | 근거 | 방식 | source |
|---|---|---|---|
| `industry_code` | OpenDART 기업개황 `induty_code` | 그대로 저장 | dart |
| `region` (본사) | 기업개황 `adres` | 주소 앞부분 → 시도 + 권역(호남 등) | dart |
| `product` | 사업보고서 "II. 사업의 내용" | LLM 추출 + 근거 문장. 부문명이 아닌 품목명 (MLCC, FC-BGA, 초전도 선재) | llm |
| `application` | 같은 섹션 (전방 산업·응용처) | LLM (데이터센터, 전기차, AI 서버) | llm |
| `customer` | 같은 섹션 (이름이 나온 주요 매출처) | LLM | llm |
| `raw_material` | 같은 섹션 (원재료) | LLM | llm |
| `region` (사업장) | 같은 섹션 (생산설비) | LLM | llm |
| `exposure` | 같은 섹션 (위험관리, 환율 등) | LLM + 방향(+1/-1) | llm |
| alias | 기업개황 이름·영문명 + 수동 사전 | 규칙 + `data/alias_manual.csv` | auto / manual |

테마(HBM 관련주 등)와 태그 간 관계(MLCC ↔ 데이터센터)는 다음 단계다.

## 준비

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # DART_API_KEY, DATABASE_URL, LLM_* (Gemini 등 OpenAI 호환 또는 Anthropic) 채우기
psql "$DATABASE_URL" -f sql/01_schema.sql
```

- OpenDART 인증키: opendart.fss.or.kr 에서 발급 (일 호출 한도는 개발가이드에서 확인)
- de-pjt와 같은 DB를 써도 되지만, 테이블 이름이 겹치지 않는지 먼저 확인할 것 (`stock`, `tag` 등)

## 작업 순서

각 단계는 다시 돌려도 결과가 덮어써지도록(멱등) 만들어 두었다.

| 단계 | 명령 | 하는 일 | 확인할 것 |
|---|---|---|---|
| 1 | `python -m src.s01_load_stocks` | `data/pilot_stocks.txt`의 종목명/코드 → corpCode.xml로 corp_code 찾아 `stock` 적재 | "찾지 못함" 목록이 비었는지. 이름이 DART 정식명과 다르면 txt를 고친다. 지수 전체는 `--index KOSPI200=data/KOSPI200.csv --index KOSDAQ150=data/KOSDAQ150.csv` (KRX 정보데이터시스템 지수구성종목 CSV, 기준일을 파일명에 남길 것) |
| 2 | `python -m src.s02_company_info` | 기업개황 → 시장, 업종코드, 주소, 본사 지역 태그 | 주소 파싱 실패 경고가 없는지 |
| 3 | `python -m src.s03_fetch_reports` | 최근 사업보고서 원문 → "사업의 내용" 섹션 텍스트를 `data/reports/`에 저장 | 섹션 길이 경고. 파일 1~2개를 직접 열어서 섹션이 제대로 잘렸는지 눈으로 확인 |
| 4 | `python -m src.s04_extract_tags --runs 2` | LLM 태그 추출 → `data/llm/{code}.run{n}.json` | 원본 JSON 1~2개를 열어 본다 |
| 5 | `python -m src.s05_load_tags` | 근거 문장이 원문에 실제로 있는지 검증 → 동의어 정규화 → `stock_tag` 적재 | 거부된 태그 수, `data/rejected.csv` |
| 6 | `python -m src.s06_build_aliases` | 자동 alias + 수동 사전 → `stock_alias` | — |
| 7 | `python -m src.s07_report` | 품질 점검 쿼리 출력, 실행 간 일관성, 검수용 CSV 생성 | 아래 통과 기준 |
| 7b | `python -m src.s08_suggest_synonyms` → (사람) `data/synonym_candidates.csv`의 `approve`에 1 → `--apply` → s05 재실행 | 표기가 쪼개진 태그를 LLM이 묶어 동의어 후보 제시. 사람이 승인한 것만 `tag_synonyms.csv`에 추가 | — |
| 8 | (사람) `data/review_sample.csv`의 `correct` 열에 1/0 기입 후 `python -m src.s07_report --score` | 축별 정확도 | — |

처음에는 **종목 10개**로만 돌리고, 통과 기준을 본 뒤 범위를 늘린다.

## 통과 기준 (파일럿)

- 대상 종목 전부가 `industry_code`와 본사 `region`을 가진다
- `data/rejected.csv`의 거부율(근거가 원문에 없는 태그)을 기록한다. 높으면 프롬프트를 고친다
- alias 충돌 0건, 2글자 이하 alias는 전부 사람이 확인
- 두 번 실행한 태그의 겹침(Jaccard)을 축별로 기록한다. 낮으면 프롬프트가 불안정하다는 뜻
- 검수 샘플의 축별 정확도를 기록한다 (목표치는 첫 결과를 보고 정한다)
- 상식 점검: 삼화콘덴서에 `product=MLCC`가 붙었는가

## 태그 상한(k)을 정하는 법

`stock_tag`에는 검증된 태그를 **전부** 넣고 `axis_rank`(축 안에서 weight 순위)를 같이 저장한다. 상한은 저장할 때 자르지 않고 `stock_tag_topk` 뷰(기본 k=5)로 본다. 이 단계에서는 `s07_report`의 분포(종목당·축별 태그 수, 너무 흔한 태그)를 보고, 최종 k는 뉴스 매칭을 붙인 뒤 정확도로 정한다.

## 알려진 한계

- 사업보고서 원문 XML 구조는 회사·연도마다 조금씩 다르다. 섹션 자르기는 제목 정규식이라 실패할 수 있으니 3단계에서 꼭 눈으로 본다.
- `induty_code`는 표준산업분류(KSIC) 코드다. 업종 이름이 필요하면 통계청 KSIC 표를 따로 넣어야 한다.
- 지배구조(그룹·계열사)는 "사업의 내용" 밖에 있어서 이번 범위에서 뺐다.
- OpenDART 엔드포인트·필드명은 초안 작성자가 아는 범위로 썼다. 첫 실행 때 응답을 출력해서 개발가이드와 맞는지 확인한다.

## 현재 상태 (2026-11-18)

| 항목 | 값 |
|---|---|
| 대상 | KOSPI200 201 + KOSDAQ150 150 = 351종목 (사업보고서 없음 1: 한화머시너리앤서비스홀딩스) |
| LLM | gemini-3.5-flash-lite 무료 할당량. run0 350, run1 350 (run0 중 281종목은 프롬프트 v1, 나머지 v2) |
| 적재 | `stock_tag` LLM 태그 6,666개, 거부율 5% |
| 일관성 (Jaccard, run0 vs run1) | product 0.55, application 0.42, customer 0.52, raw_material 0.64, region 0.63, exposure 0.60 |
| 동의어 | `tag_synonyms.csv` 254행 (사람이 승인한 것만) + 띄어쓰기·대소문자만 다른 표기는 s05가 자동 정리 |

## 남은 작업 (태깅)

1. 2회차 JSON 파싱 실패 종목 다시 뽑기: 신성델타테크, 와이지엔터테인먼트, 효성중공업 (`--only 코드 --force`, run1)
2. 프롬프트 v3: application 축은 질환·산업명만 (꼬리말 "치료", "산업" 금지), 의약품은 질환명을 application에도. 그 뒤 v1로 뽑힌 281종목 run0을 v3로 다시 뽑아 버전 통일
3. 사람 검수: `data/review_sample.csv` 90건에 1/0 → `s07_report --score`. 특히 "근거 구절은 원문에 있지만 태그를 뒷받침하지 않는" 경우(예: 금리 태그에 밀가루 작황 문장)가 얼마나 되는지
4. 글자가 겹치지 않는 동의어(금리/이자율, Inductor/인덕터, ESS/에너지저장장치) 후보 찾기
5. 헷갈리는 alias 표시: 일반 단어(대상, 동서, 테스, 하이브, 고영, 태성, 후성, 풍산), 영어 단어(Solid, NC, SL), 그룹명(SK, LG, 한화, GS, LS, CJ, 두산 …)

## 다른 PC에서 쓰기 (공유)

저장소에 들어 있는 것: 코드, 스키마, 입력 사전(`data/*.csv`), 사업보고서 섹션(`data/reports/*.txt`), LLM 결과(`data/llm/`). 원문 전체 캐시(`data/reports/full/`)와 `.env`는 올리지 않는다.

DB는 둘 중 하나로 만든다.

```bash
# A. 덤프 복원 (가장 빠름). stocktag.dump는 Releases에서 받는다:
#    https://github.com/Laattae/StocksTag/releases/tag/v0.1-tags
docker run -d --name stocktag-pg -e POSTGRES_USER=stocktag -e POSTGRES_PASSWORD=... -p 5432:5432 postgres:16
docker exec -i stocktag-pg pg_restore -U stocktag -d stocktag --no-owner < stocktag.dump

# B. 저장소 파일로 다시 쌓기 (LLM 호출 없음. DART 키만 필요)
psql "$DATABASE_URL" -f sql/01_schema.sql
python -m src.s01_load_stocks --index KOSPI200=data/KOSPI200.csv --index KOSDAQ150=data/KOSDAQ150.csv
python -m src.s02_company_info
python -m src.s03_fetch_reports      # 섹션 파일이 있으면 다시 받지 않고 DB만 채운다
python -m src.s05_load_tags && python -m src.s06_build_aliases
```

덤프 만들기: `docker exec stocktag-pg pg_dump -U stocktag -d stocktag --no-owner -Fc > stocktag.dump` → 새 Release에 첨부 (`gh release create <태그> stocktag.dump`). 최신 덤프: [v0.1-tags](https://github.com/Laattae/StocksTag/releases/tag/v0.1-tags)

## 다음 단계: 급등락 → 뉴스 연결 설계

서비스 흐름: 급등락 감지 → 후보 뉴스 모으기 → 연관성 점수 → 1위 푸시. 판별할 것은 두 가지다.

| 문제 | 질문 | 예 |
|---|---|---|
| ① 이름 판별 | 이 단어가 그 회사인가? | "적용 대상"의 대상, "하이브리드"의 하이브 |
| ② 역할 판별 | 그 회사가 이 기사의 주인공인가? | "현대차·기아 등에 납품하는 A사" 기사의 기아 |

**수집 시 (모든 기사, 규칙 기반)**: 기사마다 종목 언급을 찾아 `news_mention`에 신호를 저장한다.
- 이름 판별: 가장 긴 이름 먼저 매칭(LS전선 안의 LS는 따로 세지 않음). 일반 단어·영어 alias는 `(주)`/`㈜`가 붙거나 정식명·종목코드가 함께 나올 때만 인정
- 역할 신호: 제목 등장, 리드 문장 등장, 언급 횟수, 조사("가/는" 주어, "에" 거래 상대, "등"·"·" 나열), 기사 속 회사 수
- 태그 연결: 기사 단어 → `tag`/`tag_synonym` → `stock_tag`. 흔한 태그(환율 307종목, 경기·서울)는 가중치를 낮춘다 (IDF)

**급등락 시 (후보 몇 개만, LLM)**
1. 감지 시각 전 몇 시간 안의 기사 중 직접 언급 + 태그(품목·고객사·테마) 언급 기사를 모은다
2. 신호 점수로 상위 5개 정도
3. LLM에 "이 기사가 X의 +7%를 설명하는가, X의 역할(주인공/거래 상대/경쟁사/나열), 근거 문장"을 물어 다시 순위
4. 1위 점수가 기준 미달이면 엉뚱한 뉴스 대신 "관련 뉴스 없음 / 같은 태그 종목 동반 상승" 알림

점수 예: 직접 언급(역할별 가중) + 태그 일치 × `weight` × IDF + exposure 방향 일치(+1 태그·변수 상승·주가 상승) 가점. 여러 종목이 같이 움직이면 공통 태그로 테마 뉴스를 찾는다. 알림에는 `stock_tag.evidence`를 이유로 붙인다.

**검증**: 과거 급등락 50~100건의 정답 기사를 사람이 정하고, 규칙만/LLM 재순위 각각의 precision@1을 잰다.
