-- 소유: A — transactions, screening_results, screening_jobs
-- 쓰기는 소유 모듈만, 읽기는 누구나. 다른 모듈이 읽는 컬럼은 계약(docs/interfaces.md)이다.
-- 11/12까지는 이 파일을 직접 고치고 DB를 초기화한다. 이후에는 ALTER 파일을 새로 추가한다.
-- 파일 이름 순서대로 적용된다 (docker-compose: /docker-entrypoint-initdb.d).
--
-- bundle_id 는 B 의 bundles 를 가리키지만 FK 는 걸지 않는다 (00_common.sql 확정 전, 적용 순서와 무관하게 동작하도록).
-- 컬럼 이름·CHECK 값은 common/contracts.py 와 같아야 한다 (a_screening/db/check_contracts.py 로 확인).

-- 거래 레코드: 원문 서류 위치 + 추출 필드 + 접수 시각
CREATE TABLE IF NOT EXISTS transactions (
    tx_id              TEXT PRIMARY KEY,            -- 예: TX-0001, TX-20261108-4B307AA5
    received_at        TIMESTAMPTZ NOT NULL,
    doc_invoice_uri    TEXT,                        -- 원문 저장 위치 (contracts.blob_key 규칙)
    doc_bl_uri         TEXT,
    invoice_no         TEXT,
    invoice_date       DATE,
    bl_no              TEXT,
    seller_name        TEXT,
    seller_country     TEXT,
    buyer_name         TEXT,
    buyer_country      TEXT,
    vessel_name        TEXT,
    port_of_loading    TEXT,
    port_of_discharge  TEXT,
    goods_description  TEXT,
    currency           TEXT,
    amount             NUMERIC(18,2),
    extracted_fields   JSONB NOT NULL,              -- 추출 결과 전체 (OCR이면 bbox·confidence 포함)
    extraction_source  TEXT NOT NULL,               -- 'ground_truth', 'ocr:model-v3', 'fields', 'pending'
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 판정 결과: 어떤 번들로 어떤 분류가 나왔는지 + 근거 + 담당자 최종 판단
CREATE TABLE IF NOT EXISTS screening_results (
    result_id          BIGSERIAL PRIMARY KEY,
    tx_id              TEXT NOT NULL REFERENCES transactions(tx_id) ON DELETE CASCADE,
    bundle_id          TEXT NOT NULL,               -- 처리한 번들 (DAT-01)
    classification     TEXT NOT NULL CHECK (classification IN ('PASS_CANDIDATE','MANUAL_REVIEW','HIGH_RISK_HOLD')),
    reasons            JSONB NOT NULL DEFAULT '[]'::jsonb,   -- [{rule, field, value, entry_id, matched_name, score, classification}]
    is_shadow          BOOLEAN NOT NULL DEFAULT false,       -- 섀도 판정 (실제 판정에 안 씀)
    screened_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewer           TEXT,
    reviewer_decision  TEXT CHECK (reviewer_decision IN ('APPROVE','HOLD','REJECT')),
    reviewer_note      TEXT,
    corrected_fields   JSONB,                       -- 담당자가 고친 필드만 {field: value}
    reviewed_at        TIMESTAMPTZ,
    UNIQUE (tx_id, bundle_id)
);
CREATE INDEX IF NOT EXISTS idx_screening_bundle ON screening_results(bundle_id, classification);

-- 심사 처리 큐 (A 내부): 접수 → 처리 중 → 완료/실패. 번들은 접수 시점 고정. 워커가 FOR UPDATE SKIP LOCKED 로 꺼냄
CREATE TABLE IF NOT EXISTS screening_jobs (
    tx_id             TEXT PRIMARY KEY REFERENCES transactions(tx_id) ON DELETE CASCADE,
    bundle_id         TEXT NOT NULL,
    shadow_bundle_id  TEXT,                         -- 섀도 판정 번들 (없으면 NULL)
    status            TEXT NOT NULL CHECK (status IN ('queued','processing','completed','failed')),
    input             JSONB NOT NULL,               -- {source, sample_tx_id, fields}
    conflicts         JSONB NOT NULL DEFAULT '[]'::jsonb,   -- Invoice/B-L 불일치 (OCR 결과)
    error             JSONB,                        -- 공통 에러 형식 {code, message, target_id}
    attempts          INT NOT NULL DEFAULT 0,
    worker_id         TEXT,
    submitted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at        TIMESTAMPTZ,
    completed_at      TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON screening_jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_queue ON screening_jobs(submitted_at) WHERE status = 'queued';
