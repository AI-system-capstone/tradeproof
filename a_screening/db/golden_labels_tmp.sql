-- 임시: golden_labels 는 데이터 담당 소유(common/schema/15_dataset.sql). 그쪽이 확정되면 이 파일은 지운다.
-- build_seed.py --load 가 적재 전에 실행한다.
CREATE TABLE IF NOT EXISTS golden_labels (
    tx_id             TEXT PRIMARY KEY REFERENCES transactions(tx_id) ON DELETE CASCADE,
    case_type         TEXT NOT NULL,                -- clean / exact_match / fuzzy_match / new_listing / sanctioned_vessel
    matched_entry_id  TEXT,
    expected          JSONB NOT NULL                -- {"list-v14": "PASS_CANDIDATE", "list-v15": "HIGH_RISK_HOLD"}
);
