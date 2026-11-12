-- 시드 적재 확인용 쿼리:  psql "$DSN" -f db/verify.sql
\echo '== 테이블별 건수 (기대: transactions 100, screening_results 100, golden_labels 100)'
SELECT 'transactions' AS tbl, count(*) FROM transactions
UNION ALL SELECT 'screening_results', count(*) FROM screening_results
UNION ALL SELECT 'golden_labels', count(*) FROM golden_labels;

\echo '== B-7 판정 vs 정답 (기대: 불일치 0건)'
SELECT g.case_type, s.classification, g.expected->>'list-v14' AS expected_v14, count(*),
       sum(CASE WHEN s.classification <> g.expected->>'list-v14' THEN 1 ELSE 0 END) AS mismatches
FROM screening_results s JOIN golden_labels g USING (tx_id)
WHERE s.bundle_id = 'B-7'
GROUP BY 1, 2, 3 ORDER BY 1;

\echo '== v15에서 판정이 바뀔 거래 (시연 장면 2, 기대: 5건)'
SELECT t.tx_id, t.seller_name, t.buyer_name, g.expected->>'list-v14' AS v14, g.expected->>'list-v15' AS v15
FROM transactions t JOIN golden_labels g USING (tx_id)
WHERE g.expected->>'list-v14' <> g.expected->>'list-v15'
ORDER BY t.tx_id;

\echo '== 고위험 보류 근거 예시'
SELECT tx_id, r->>'rule' AS rule, r->>'value' AS value, r->>'matched_name' AS matched, r->>'entry_id' AS entry_id
FROM screening_results, jsonb_array_elements(reasons) r
WHERE classification = 'HIGH_RISK_HOLD'
ORDER BY tx_id LIMIT 5;
