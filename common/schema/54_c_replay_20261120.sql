-- 소유: C — validations 에 진행 상황 컬럼 추가 (M8 운영 화면의 진행 막대용)
-- 30_c_replay.sql 은 11/12 이후 고치지 않는다. 이미 만든 DB 에는 이 파일만 적용하면 된다.
-- 여러 번 적용해도 결과가 같다 (IF NOT EXISTS).
--
-- progress: 검증 Job 이 running 동안 주기적으로 적는다. 다른 모듈은 쓰지 않는다.
--   {"stage": "golden" | "replay", "done": 37, "total": 200, "updated_at": "2026-11-20T09:00:42.000+00:00"}
--   golden 의 total = 골든셋 건수 × 번들 2개 (운영·후보 번들로 각각 처리하므로)
--   replay 의 total = 과거 거래 건수
--   queued 이거나 처리를 시작하기 전이면 NULL
ALTER TABLE validations ADD COLUMN IF NOT EXISTS progress JSONB;
