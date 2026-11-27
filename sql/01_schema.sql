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
-- 같은 단어가 축마다 다른 태그를 가리킬 수 있어 (axis, synonym)이 키다 (에너지저장장치 → product ESS / application ESS)
-- axis는 tag_id가 가리키는 tag.axis와 같다 (sync_synonym_table이 채움)
CREATE TABLE IF NOT EXISTS tag_synonym (
  axis     TEXT NOT NULL,
  synonym  TEXT NOT NULL,
  tag_id   INT REFERENCES tag ON DELETE CASCADE,
  PRIMARY KEY (axis, synonym)
);
-- 이미 만든 DB: PK가 synonym 하나였던 것을 (axis, synonym)으로
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM information_schema.columns
                 WHERE table_name = 'tag_synonym' AND column_name = 'axis') THEN
    ALTER TABLE tag_synonym ADD COLUMN axis TEXT;
    UPDATE tag_synonym s SET axis = t.axis FROM tag t WHERE t.tag_id = s.tag_id;
    DELETE FROM tag_synonym WHERE axis IS NULL;
    ALTER TABLE tag_synonym ALTER COLUMN axis SET NOT NULL;
    ALTER TABLE tag_synonym DROP CONSTRAINT IF EXISTS tag_synonym_pkey;
    ALTER TABLE tag_synonym ADD PRIMARY KEY (axis, synonym);
  END IF;
END $$;
CREATE INDEX IF NOT EXISTS idx_tag_synonym_synonym ON tag_synonym(synonym);

-- 태그 관계. 원본은 data/tag_relations.csv (s12_relation_candidates --apply, s05가 동기화)
--   is_a      child는 parent의 한 종류/일부 (AI 반도체 ⊂ 반도체, 경주 ⊂ 경북, 기아 ⊂ 현대차그룹)
--   made_from child는 parent를 원료로 만든다 (탄산리튬 ← 리튬). 공급망 관계
-- 바로 위 상위만 저장하고, 더 위(is_a만)는 tag_ancestor 뷰로 따라간다
CREATE TABLE IF NOT EXISTS tag_relation (
  child_tag_id   INT REFERENCES tag ON DELETE CASCADE,
  parent_tag_id  INT REFERENCES tag ON DELETE CASCADE,
  kind           TEXT NOT NULL DEFAULT 'is_a',
  source         TEXT,              -- suffix(규칙 후보를 사람이 승인) / seed(사람이 씀)
  PRIMARY KEY (child_tag_id, parent_tag_id),
  CHECK (child_tag_id <> parent_tag_id)
);
ALTER TABLE tag_relation ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'is_a';

-- 뉴스 키워드 매칭에서 뺄 태그 (너무 흔함: 환율 91%, 수도권 75% …). 원본은 data/tag_match_exclude.csv
-- 태그 자체는 남긴다 (exposure 방향 등 다른 용도로 쓸 수 있음). s05·s13이 동기화
CREATE TABLE IF NOT EXISTS tag_match_exclude (
  tag_id  INT PRIMARY KEY REFERENCES tag ON DELETE CASCADE,
  reason  TEXT
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
-- stock_tag에 컬럼이 늘면 st.* 순서가 바뀌어 CREATE OR REPLACE가 실패하므로 지우고 만든다
DROP VIEW IF EXISTS stock_tag_topk;
CREATE VIEW stock_tag_topk AS
SELECT st.*, t.axis, t.value
FROM stock_tag st JOIN tag t USING (tag_id)
WHERE st.axis_rank IS NULL OR st.axis_rank <= 5;

-- 태그 값 비교 키: 소문자 + 공백·하이픈·가운뎃점 제거 (src/common.py norm_key와 같아야 한다)
CREATE OR REPLACE FUNCTION tag_key(v TEXT) RETURNS TEXT
LANGUAGE sql IMMUTABLE AS $$ SELECT regexp_replace(lower(v), '[\s\-·ㆍ]+', '', 'g') $$;

-- 종목 × 개념: 같은 값이 여러 축에 붙어도(데이터센터 application+product) 한 번만.
-- 뉴스 매칭 점수는 이 뷰 단위로 센다 (축별 점수를 그냥 더하면 같은 개념이 두 번 계산됨)
-- weight는 축 중 최댓값, axes/tag_ids로 어느 축에서 왔는지 남긴다. industry_code는 코드라 뺀다
-- region은 다른 축과 글자가 같아도 뜻이 달라서(exposure 경기 = 경기 흐름, region 경기 = 경기도) 따로 묶는다
DROP VIEW IF EXISTS stock_tag_concept;
CREATE VIEW stock_tag_concept AS
SELECT st.stock_code,
       CASE WHEN t.axis = 'region' THEN 'region:' ELSE '' END || tag_key(t.value) AS concept_key,
       (array_agg(t.value ORDER BY st.weight DESC NULLS LAST, t.axis))[1] AS value,
       array_agg(t.axis ORDER BY st.weight DESC NULLS LAST, t.axis)        AS axes,
       array_agg(st.tag_id ORDER BY st.weight DESC NULLS LAST, t.axis)     AS tag_ids,
       MAX(st.weight)                                  AS weight,
       MAX(st.confidence)                              AS confidence,
       COUNT(*)                                        AS n_axes
FROM stock_tag st JOIN tag t USING (tag_id)
WHERE t.axis <> 'industry_code'
GROUP BY 1, 2;

-- 태그별 흔한 정도와 IDF. idf = ln(전체 종목 수 / 이 태그가 붙은 종목 수). 뉴스 매칭 점수에 곱한다
-- match_excluded: tag_match_exclude에 있으면 키워드 매칭에서 뺀다
DROP VIEW IF EXISTS tag_weight;
CREATE VIEW tag_weight AS
WITH n AS (SELECT COUNT(*)::numeric AS total FROM stock),
     df AS (SELECT tag_id, COUNT(*) AS stocks FROM stock_tag GROUP BY tag_id)
SELECT t.tag_id, t.axis, t.value, df.stocks,
       ROUND(100 * df.stocks / n.total, 1)    AS pct,
       ROUND(ln(n.total / df.stocks), 3)      AS idf,
       (x.tag_id IS NOT NULL)                 AS match_excluded,
       x.reason                               AS exclude_reason
FROM tag t JOIN df USING (tag_id) CROSS JOIN n
LEFT JOIN tag_match_exclude x USING (tag_id);

-- 태그 → 모든 상위 (is_a만, depth 1 = 바로 위). 순환이 있어도 멈추도록 path로 막는다
-- made_from은 이어 가지 않는다 (탄산리튬 ← 리튬 ← ? 처럼 공급망을 따라 올라가면 뜻이 흐려짐)
DROP VIEW IF EXISTS tag_ancestor;
CREATE VIEW tag_ancestor AS
WITH RECURSIVE a(tag_id, ancestor_id, depth, path) AS (
  SELECT child_tag_id, parent_tag_id, 1, ARRAY[child_tag_id, parent_tag_id] FROM tag_relation WHERE kind = 'is_a'
  UNION ALL
  SELECT a.tag_id, r.parent_tag_id, a.depth + 1, a.path || r.parent_tag_id
  FROM a JOIN tag_relation r ON r.child_tag_id = a.ancestor_id AND r.kind = 'is_a'
  WHERE NOT r.parent_tag_id = ANY(a.path) AND a.depth < 10
)
SELECT tag_id, ancestor_id, MIN(depth) AS depth FROM a GROUP BY tag_id, ancestor_id;

CREATE INDEX IF NOT EXISTS idx_stock_tag_tag ON stock_tag(tag_id);
CREATE INDEX IF NOT EXISTS idx_tag_relation_parent ON tag_relation(parent_tag_id);
CREATE INDEX IF NOT EXISTS idx_stock_alias_code ON stock_alias(stock_code);
