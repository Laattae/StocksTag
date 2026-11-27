-- 태그 정제(뉴스 적용 전) 지표. python -m src.s09_refine_report 로 실행
-- 단계마다 같은 쿼리로 수치를 비교한다. 정규화 = 소문자 + 공백·하이픈·가운뎃점 제거 (common.norm_key와 같음)

-- name: [요약] 전체 규모
SELECT (SELECT COUNT(*) FROM stock) AS stocks,
       (SELECT COUNT(*) FROM tag) AS tags,
       (SELECT COUNT(*) FROM stock_tag) AS links,
       (SELECT COUNT(*) FROM stock_tag WHERE source = 'llm') AS llm_links,
       (SELECT COUNT(*) FROM tag_synonym) AS synonyms;

-- name: [요약] 축별 태그·연결 수
SELECT t.axis, COUNT(DISTINCT t.tag_id) AS tags, COUNT(st.tag_id) AS links
FROM tag t LEFT JOIN stock_tag st USING (tag_id)
GROUP BY t.axis ORDER BY t.axis;

-- name: [1] 정규화하면 같아지는 표기 쌍 (같은 축)
SELECT a.axis, a.value AS tag_a, b.value AS tag_b
FROM tag a JOIN tag b ON a.axis = b.axis AND a.tag_id < b.tag_id
 AND regexp_replace(lower(a.value), '[\s\-·ㆍ]+', '', 'g') = regexp_replace(lower(b.value), '[\s\-·ㆍ]+', '', 'g')
ORDER BY 1, 2;

-- name: [2] 고객사: 상장사 alias로 같은 종목을 가리키는데 표기가 여러 개
WITH alias_n AS (
  SELECT DISTINCT regexp_replace(lower(alias), '[\s\-·ㆍ]+', '', 'g') AS k, stock_code FROM stock_alias
  UNION
  SELECT DISTINCT regexp_replace(lower(name), '[\s\-·ㆍ]+', '', 'g'), stock_code FROM stock
  UNION
  SELECT DISTINCT regexp_replace(lower(stock_name), '[\s\-·ㆍ]+', '', 'g'), stock_code FROM stock WHERE stock_name IS NOT NULL
), cust AS (
  SELECT t.tag_id, t.value, a.stock_code AS ref_code,
         (SELECT COUNT(*) FROM stock_tag st WHERE st.tag_id = t.tag_id) AS n
  FROM tag t JOIN alias_n a ON a.k = regexp_replace(lower(t.value), '[\s\-·ㆍ]+', '', 'g')
  WHERE t.axis = 'customer'
)
SELECT s.stock_name AS ref_stock, string_agg(c.value || ' ' || c.n, ' · ' ORDER BY c.n DESC) AS variants
FROM cust c JOIN stock s ON s.stock_code = c.ref_code
GROUP BY s.stock_name HAVING COUNT(DISTINCT c.tag_id) > 1
ORDER BY 1;

-- name: [2] 고객사 값 중 상장사 alias와 정확히 맞는 것 / 안 맞는 것
WITH alias_n AS (
  SELECT regexp_replace(lower(alias), '[\s\-·ㆍ]+', '', 'g') AS k FROM stock_alias
  UNION SELECT regexp_replace(lower(name), '[\s\-·ㆍ]+', '', 'g') FROM stock
  UNION SELECT regexp_replace(lower(stock_name), '[\s\-·ㆍ]+', '', 'g') FROM stock WHERE stock_name IS NOT NULL
)
SELECT COUNT(*) FILTER (WHERE m) AS matched_tags, COUNT(*) FILTER (WHERE NOT m) AS unmatched_tags,
       SUM(n) FILTER (WHERE m) AS matched_links, SUM(n) FILTER (WHERE NOT m) AS unmatched_links
FROM (SELECT t.tag_id, EXISTS (SELECT 1 FROM alias_n a WHERE a.k = regexp_replace(lower(t.value), '[\s\-·ㆍ]+', '', 'g')) AS m,
             (SELECT COUNT(*) FROM stock_tag st WHERE st.tag_id = t.tag_id) AS n
      FROM tag t WHERE t.axis = 'customer') x;

-- name: [3] 두 개 이상 축에 있는 값 (축 조합별)
WITH v AS (
  SELECT regexp_replace(lower(value), '[\s\-·ㆍ]+', '', 'g') AS k, axis FROM tag
  WHERE axis <> 'industry_code' AND EXISTS (SELECT 1 FROM stock_tag st WHERE st.tag_id = tag.tag_id)
)
SELECT a.axis AS axis_a, b.axis AS axis_b, COUNT(*) AS shared_values
FROM v a JOIN v b ON a.k = b.k AND a.axis < b.axis
GROUP BY 1, 2 ORDER BY 3 DESC;

-- name: [3] 두 개 이상 축에 있는 값 (전체 개수)
SELECT COUNT(*) AS values_in_2plus_axes FROM (
  SELECT regexp_replace(lower(value), '[\s\-·ㆍ]+', '', 'g') AS k FROM tag
  WHERE axis <> 'industry_code' AND EXISTS (SELECT 1 FROM stock_tag st WHERE st.tag_id = tag.tag_id)
  GROUP BY 1 HAVING COUNT(DISTINCT axis) > 1) x;

-- name: [3] 같은 종목에 같은 값이 두 축 이상으로 붙은 건수 (stock_tag_concept에서 한 번만 셈. region은 따로)
SELECT COUNT(*) FILTER (WHERE n_axes > 1) AS stock_value_dups, COUNT(*) AS concept_rows,
       (SELECT COUNT(*) FROM stock_tag st JOIN tag t USING (tag_id) WHERE t.axis <> 'industry_code') AS tag_rows
FROM stock_tag_concept;

-- name: [3] 동의어가 축 간에 어긋남 (A축 동의어로 등록된 단어가 B축에 별도 태그로 쓰임)
SELECT t.axis AS syn_axis, s.synonym, t.value AS syn_canonical, o.axis AS other_axis, o.value AS other_tag,
       (SELECT COUNT(*) FROM stock_tag st WHERE st.tag_id = o.tag_id) AS other_stocks
FROM tag_synonym s JOIN tag t USING (tag_id)
JOIN tag o ON o.axis <> t.axis
 AND regexp_replace(lower(o.value), '[\s\-·ㆍ]+', '', 'g') = regexp_replace(lower(s.synonym), '[\s\-·ㆍ]+', '', 'g')
WHERE EXISTS (SELECT 1 FROM stock_tag st WHERE st.tag_id = o.tag_id)
ORDER BY other_stocks DESC;

-- name: [4] 지역 태그 전체 (표기 확인용)
SELECT t.value, COUNT(st.tag_id) AS stocks,
       COUNT(*) FILTER (WHERE st.source = 'dart') AS dart, COUNT(*) FILTER (WHERE st.source = 'llm') AS llm
FROM tag t JOIN stock_tag st USING (tag_id) WHERE t.axis = 'region'
GROUP BY t.value ORDER BY t.value;

-- name: [5] 상·하위 후보 수 (같은 축, 짧은 값이 긴 값의 앞이나 끝에 붙음, 둘 다 사용 중)
WITH v AS (
  SELECT t.tag_id, t.axis, t.value, regexp_replace(lower(t.value), '[\s\-·ㆍ]+', '', 'g') AS k,
         (SELECT COUNT(*) FROM stock_tag st WHERE st.tag_id = t.tag_id) AS n
  FROM tag t WHERE t.axis IN ('product', 'application', 'raw_material', 'exposure', 'customer')
)
SELECT c.axis, COUNT(*) AS pairs, COUNT(*) FILTER (WHERE p.n >= 3 AND c.n >= 2) AS pairs_parent3_child2
FROM v c JOIN v p ON c.axis = p.axis AND c.tag_id <> p.tag_id
 AND char_length(p.k) >= 2 AND char_length(c.k) > char_length(p.k)
 AND (c.k LIKE p.k || '%' OR c.k LIKE '%' || p.k)
WHERE c.n >= 1 AND p.n >= 1
GROUP BY c.axis ORDER BY c.axis;

-- name: [6] 흔한 태그 (종목 15% 이상, industry_code 제외) + 매칭 제외 여부
SELECT axis, value, stocks, pct, idf, match_excluded FROM tag_weight
WHERE axis <> 'industry_code' AND pct >= 15
ORDER BY stocks DESC;

-- name: [6] 키워드 매칭 대상 연결 수 (제외 태그·industry_code 빼고)
SELECT COUNT(*) FILTER (WHERE NOT w.match_excluded) AS matchable_links,
       COUNT(*) FILTER (WHERE w.match_excluded) AS excluded_links
FROM stock_tag st JOIN tag_weight w USING (tag_id) WHERE w.axis <> 'industry_code';

-- name: [5] 태그 관계 수 (축·종류별)
SELECT c.axis, r.kind, COUNT(*) AS relations, COUNT(DISTINCT r.parent_tag_id) AS parents
FROM tag_relation r JOIN tag c ON c.tag_id = r.child_tag_id
GROUP BY 1, 2 ORDER BY 1, 2;

-- name: [5] 하위가 많은 상위 태그 (is_a, 모든 깊이) 상위 20
SELECT p.axis, p.value, COUNT(*) AS descendants,
       (SELECT COUNT(*) FROM stock_tag st WHERE st.tag_id = p.tag_id) AS own_stocks
FROM tag_ancestor a JOIN tag p ON p.tag_id = a.ancestor_id
GROUP BY p.tag_id ORDER BY descendants DESC LIMIT 20;
