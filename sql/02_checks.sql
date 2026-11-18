-- name: 종목 수와 기본 속성 보유율
SELECT COUNT(*) AS stocks,
       COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM stock_tag st JOIN tag t USING (tag_id)
                                      WHERE st.stock_code = s.stock_code AND t.axis = 'industry_code')) AS has_industry,
       COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM stock_tag st JOIN tag t USING (tag_id)
                                      WHERE st.stock_code = s.stock_code AND t.axis = 'region' AND st.source = 'dart')) AS has_hq_region,
       COUNT(*) FILTER (WHERE EXISTS (SELECT 1 FROM report_source r WHERE r.stock_code = s.stock_code)) AS has_report
FROM stock s;

-- name: 종목별 축별 태그 수
SELECT s.name, t.axis, COUNT(*) AS n
FROM stock_tag st JOIN tag t USING (tag_id) JOIN stock s USING (stock_code)
GROUP BY s.name, t.axis
ORDER BY s.name, t.axis;

-- name: 축별 종목당 태그 수 (평균, 최대)
SELECT axis, ROUND(AVG(n)::numeric, 1) AS avg_per_stock, MAX(n) AS max_per_stock
FROM (SELECT st.stock_code, t.axis, COUNT(*) AS n
      FROM stock_tag st JOIN tag t USING (tag_id) GROUP BY 1, 2) x
GROUP BY axis ORDER BY axis;

-- name: 태그가 거의 없는 종목 (LLM 태그 2개 이하, 섹션 파싱 실패 의심)
SELECT s.stock_code, s.name, COUNT(st.tag_id) FILTER (WHERE st.source = 'llm') AS llm_tags
FROM stock s LEFT JOIN stock_tag st USING (stock_code)
GROUP BY s.stock_code, s.name
HAVING COUNT(st.tag_id) FILTER (WHERE st.source = 'llm') <= 2
ORDER BY llm_tags;

-- name: 여러 종목이 공유하는 태그 (흔할수록 변별력이 낮음)
SELECT t.axis, t.value, COUNT(*) AS stocks
FROM stock_tag st JOIN tag t USING (tag_id)
WHERE t.axis <> 'industry_code'
GROUP BY t.axis, t.value
HAVING COUNT(*) > 1
ORDER BY stocks DESC, t.axis
LIMIT 30;

-- name: 비슷한 표기로 쪼개진 태그 후보 (같은 축, 공백 제거 후 한쪽이 다른 쪽을 포함)
SELECT a.axis, a.value AS tag_a, b.value AS tag_b
FROM tag a JOIN tag b ON a.axis = b.axis AND a.tag_id < b.tag_id
WHERE a.axis NOT IN ('industry_code')
  AND (replace(lower(a.value), ' ', '') LIKE '%' || replace(lower(b.value), ' ', '') || '%'
    OR replace(lower(b.value), ' ', '') LIKE '%' || replace(lower(a.value), ' ', '') || '%')
ORDER BY a.axis
LIMIT 50;

-- name: alias 충돌 (한 alias가 여러 종목)
SELECT alias, array_agg(stock_code ORDER BY stock_code) AS stocks
FROM stock_alias
GROUP BY alias HAVING COUNT(DISTINCT stock_code) > 1;

-- name: alias가 다른 종목 이름에 포함됨 (부분 일치 오탐 위험)
SELECT a.alias, a.stock_code, s.stock_code AS other_code, s.name AS other_name
FROM stock_alias a JOIN stock s
  ON s.stock_code <> a.stock_code
 AND (s.name LIKE '%' || a.alias || '%' OR s.stock_name LIKE '%' || a.alias || '%');

-- name: 2글자 이하 alias (사람이 확인)
SELECT alias, stock_code, source FROM stock_alias WHERE char_length(alias) <= 2 ORDER BY alias;

-- name: exposure 태그와 방향
SELECT s.name, t.value, st.direction, st.weight, left(st.evidence, 60) AS evidence
FROM stock_tag st JOIN tag t USING (tag_id) JOIN stock s USING (stock_code)
WHERE t.axis = 'exposure'
ORDER BY s.name, st.axis_rank;
