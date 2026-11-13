-- 소유: D — deployments (배포·롤백 이력). audit_events 는 11/9 결정으로 삭제
-- 쓰기는 소유 모듈만, 읽기는 누구나. 다른 모듈이 읽는 컬럼은 계약(docs/interfaces.md)이다.
-- 11/12까지는 이 파일을 직접 고치고 DB를 초기화한다. 이후에는 ALTER 파일을 새로 추가한다.
-- 파일 이름 순서대로 적용된다 (docker-compose: /docker-entrypoint-initdb.d).

-- 배포 1번 = 한 줄. 현재 stable = status 'succeeded' 중 finished_at 이 가장 늦은 줄의 bundle_id.
-- 진행 중(canary) = status 가 'pending' 또는 'progressing' 인 줄 (동시에 최대 1개).
-- bundle_id 는 B 의 bundles 를 가리키지만 B 정의가 확정되기 전이라 FK 는 걸지 않는다.
CREATE TABLE IF NOT EXISTS deployments (
    deployment_id      text        PRIMARY KEY,                 -- DP-<12자리 hex>
    bundle_id          text        NOT NULL,                    -- 반영하려는 후보 번들
    previous_bundle_id text        NOT NULL,                    -- 배포 시작 시점의 stable (롤백 대상, DEP-05)
    status             text        NOT NULL
        CHECK (status IN ('pending', 'progressing', 'succeeded', 'rolled_back', 'failed')),
    weight             integer     NOT NULL DEFAULT 0 CHECK (weight BETWEEN 0 AND 100),  -- 후보로 가는 트래픽 비율(%)
    step_index         integer     NOT NULL DEFAULT 0,          -- steps 중 몇 번째 단계까지 왔는지 (0부터)
    steps              jsonb       NOT NULL,                    -- canary 단계, 예: [10, 50, 100]
    analysis           jsonb       NOT NULL DEFAULT '{}'::jsonb, -- 단계별 분석 결과 (MON-02 판단 근거)
    rollback_reason    text,                                    -- 'manual' | 'analysis_failed' (rolled_back 일 때)
    requested_by       text        NOT NULL,                    -- X-Actor
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    finished_at        timestamptz                              -- succeeded / rolled_back / failed 가 된 시각
);

-- "갱신은 한 번에 하나": 진행 중인 배포는 동시에 하나만 (두 요청이 동시에 와도 DB 가 막는다)
CREATE UNIQUE INDEX IF NOT EXISTS deployments_one_in_progress
    ON deployments ((true)) WHERE status IN ('pending', 'progressing');

CREATE INDEX IF NOT EXISTS deployments_created_at ON deployments (created_at DESC);
