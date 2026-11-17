-- 소유: C — validations, validation_diffs (M3 검증 결과·지표·영향 거래)
-- 쓰기는 소유 모듈만, 읽기는 누구나. 다른 모듈이 읽는 컬럼은 계약(docs/interfaces.md)이다.
-- 11/12까지는 이 파일을 직접 고치고 DB를 초기화한다. 이후에는 ALTER 파일을 새로 추가한다.
-- 파일 이름 순서대로 적용된다 (docker-compose: /docker-entrypoint-initdb.d).
--
-- bundle_id 는 B 의 bundles 를 가리키지만 FK 는 걸지 않는다 (A·D 와 같은 이유: 적용 순서와 무관하게 동작하도록).
-- 컬럼 이름은 docs/openapi/c_replay.json 의 Validation·Diff 와 같다.

-- 검증 1번 = 한 줄: 후보 번들 하나를 운영 번들과 비교 (골든셋 검증 + Replay)
CREATE TABLE IF NOT EXISTS validations (
    validation_id       TEXT PRIMARY KEY,           -- RP-<hex>. B 가 만든 검증 ID (B PR #17). 손으로 실행하면 C 가 만든다
    bundle_id           TEXT NOT NULL,              -- 검증할 후보 번들
    baseline_bundle_id  TEXT NOT NULL,              -- 비교 기준(운영) 번들
    status              TEXT NOT NULL CHECK (status IN ('queued','running','completed','failed')),
    golden              JSONB,                      -- 골든셋 검증 결과 (completed 일 때)
    replay              JSONB,                      -- Replay 요약 (completed 일 때)
    error               JSONB,                      -- 공통 에러 형식 {code, message, target_id} (failed 일 때)
    requested_by        TEXT NOT NULL,              -- 검증을 시작시킨 쪽 (B 가 만든 Job 이면 b_registry)
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at          TIMESTAMPTZ,                -- running 이 된 시각
    finished_at         TIMESTAMPTZ                 -- completed / failed 가 된 시각
);

-- 같은 번들에 진행 중(queued·running)인 검증은 하나만 (두 요청이 동시에 와도 DB 가 막는다)
CREATE UNIQUE INDEX IF NOT EXISTS validations_one_in_progress
    ON validations (bundle_id) WHERE status IN ('queued', 'running');

CREATE INDEX IF NOT EXISTS validations_created_at ON validations (created_at DESC);

-- 달라진 거래 (VAL-03): Replay 에서 운영 번들의 기록과 후보 번들의 결과가 다른 거래
CREATE TABLE IF NOT EXISTS validation_diffs (
    validation_id   TEXT NOT NULL REFERENCES validations(validation_id) ON DELETE CASCADE,
    tx_id           TEXT NOT NULL,                  -- A 의 transactions.tx_id (FK 는 걸지 않는다)
    direction       TEXT NOT NULL CHECK (direction IN ('up','down')),   -- down = 후보 번들에서 위험도가 낮아짐
    before          JSONB NOT NULL,                 -- {bundle_id, classification, reasons} 운영 번들의 기록
    after           JSONB NOT NULL,                 -- {bundle_id, classification, reasons} 후보 번들의 결과
    fields_changed  JSONB NOT NULL DEFAULT '[]'::jsonb,   -- [{field, before, after}]
    PRIMARY KEY (validation_id, tx_id)
);
