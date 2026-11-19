-- 종목 태깅 파일럿 스키마
-- de-pjt와 같은 DB를 쓸 경우 테이블 이름 충돌을 먼저 확인할 것

CREATE TABLE IF NOT EXISTS stock (
  stock_code   VARCHAR(6) PRIMARY KEY,
  corp_code    VARCHAR(8) NOT NULL UNIQUE,  -- OpenDART 고유번호
  name         TEXT NOT NULL,               -- DART 정식 회사명
  stock_name   TEXT,                        -- 종목명(약식)
  name_eng     TEXT,
  market       TEXT,                        -- KOSPI / KOSDAQ / KONEX / ETC
  induty_code  TEXT,                        -- 표준산업분류 코드
  address      TEXT,
  index_name   TEXT,                        -- KOSPI200 / KOSDAQ150 / NULL (s01 --index로 채움)
  updated_at   TIMESTAMPTZ DEFAULT now()
);
-- 이미 만든 DB에 컬럼 추가
ALTER TABLE stock ADD COLUMN IF NOT EXISTS index_name TEXT;

-- 수집한 사업보고서 (어느 문서에서 태그가 나왔는지 추적)
CREATE TABLE IF NOT EXISTS report_source (
  stock_code    VARCHAR(6) REFERENCES stock ON DELETE CASCADE,
  rcept_no      VARCHAR(14) NOT NULL,
  report_nm     TEXT,
  section_chars INT,                        -- 잘라낸 "사업의 내용" 길이
  text_path     TEXT,
  fetched_at    TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (stock_code, rcept_no)
);

-- 태그 사전: 값은 여기에 한 번만
CREATE TABLE IF NOT EXISTS tag (
  tag_id  SERIAL PRIMARY KEY,
  axis    TEXT NOT NULL,     -- industry_code / region / product / application / customer / raw_material / exposure
  value   TEXT NOT NULL,
  UNIQUE (axis, value)
);

-- 태그 동의어 (적층세라믹콘덴서 -> MLCC). 나중에 뉴스 매칭에도 쓴다
CREATE TABLE IF NOT EXISTS tag_synonym (
  synonym  TEXT PRIMARY KEY,
  tag_id   INT REFERENCES tag ON DELETE CASCADE
);

-- 종목-태그 연결
CREATE TABLE IF NOT EXISTS stock_tag (
  stock_code  VARCHAR(6) REFERENCES stock ON DELETE CASCADE,
  tag_id      INT REFERENCES tag ON DELETE CASCADE,
  direction   SMALLINT,          -- exposure 전용: +1 변수 상승 시 유리 / -1 불리 / NULL
  weight      REAL,              -- 0~1, 회사 사업에서의 중요도
  axis_rank   INT,               -- 같은 종목·같은 축 안에서 weight 순위 (1부터)
  source      TEXT NOT NULL,     -- dart / llm / manual
  confidence  REAL,
  evidence    TEXT,              -- 근거 원문 구절
  rcept_no    VARCHAR(14),       -- 근거 문서
  created_at  TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (stock_code, tag_id)
);
-- llm: LLM이 준 근거 구절 / repaired: LLM 근거가 원문에 없어 프로그램이 원문에서 값이 든 문장을 찾은 것
ALTER TABLE stock_tag ADD COLUMN IF NOT EXISTS evidence_source TEXT;

-- alias (뉴스 본문에서 종목 찾기용)
CREATE TABLE IF NOT EXISTS stock_alias (
  alias       TEXT NOT NULL,
  stock_code  VARCHAR(6) REFERENCES stock ON DELETE CASCADE,
  source      TEXT NOT NULL,     -- auto / manual
  PRIMARY KEY (alias, stock_code)
);

-- 종목·축별 상위 k개만 보는 뷰 (k 바꾸려면 숫자만 수정해서 다시 실행)
CREATE OR REPLACE VIEW stock_tag_topk AS
SELECT st.*, t.axis, t.value
FROM stock_tag st JOIN tag t USING (tag_id)
WHERE st.axis_rank IS NULL OR st.axis_rank <= 5;

CREATE INDEX IF NOT EXISTS idx_stock_tag_tag ON stock_tag(tag_id);
CREATE INDEX IF NOT EXISTS idx_stock_alias_code ON stock_alias(stock_code);
