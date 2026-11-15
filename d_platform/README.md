# d_platform — 배포·감시 + 인프라

- **담당:** D 최은호
- **요구사항 모듈:** M4 배포·서빙, M5 모니터링, 인프라(compose·k3s), M7 저장소(스키마 적용·버킷)
- **포트:** 8300
- **요구사항 ID:** DEP-01~05, MON-01·02, UI-04(P1), TST-07~09

## 폴더

| 폴더 | 내용 |
|---|---|
| `api/` | 배포 API: `POST /deployments`, `GET /deployments/{id}`, `POST /deployments/{id}/rollback` |
| `helm/` | 모듈 차트·values. `deploy/`의 번들 ID를 A의 `BUNDLE_ID`로 주입 |
| `k8s/` | Argo Rollouts Rollout·AnalysisTemplate, Replay Job 템플릿(C와 협의), RBAC, NetworkPolicy |
| `monitoring/` | Prometheus 규칙, Grafana 대시보드 |
| `compose/` | docker-compose 보조 설정 (SeaweedFS S3 계정) |
| `tools/` | M9: 심사 트래픽(TST-07), 이상 상황 입력(TST-08), 시나리오 실행(TST-09) |

## 할 일 (11/12 변경: 모델 갱신 경로 먼저 — [결정 0005](../docs/decisions/0005-model-path-first.md))

모델 경로(S4)를 먼저 끝까지 만든다. 자동 롤백(MON-02)은 canary에 먼저 적용한다.

- [x] docker-compose: postgres + SeaweedFS(`minio:9000`) + 버킷 생성, minio 파이썬 클라이언트 smoke test (`tests/test_blob_smoke.py`)
- [ ] compose에 A 서비스 2개(`a-screening` API, `a-ocr-worker`) 연결 — A 코드가 `BUNDLE_ID`를 읽는 변경이 `main`에 들어온 뒤. 그때까지 A의 번들 목록은 B-7~B-10이라 `BUNDLE_ID=B-1`로는 뜨지 않는다
- [x] 배포 API: 미승인 409, 진행 중 배포 409, `deployments` 기록 (가짜 드라이버)
- [ ] k3s + ingress-nginx + Argo Rollouts: `helm/a-screening` 차트, `deploy/values-dev.yaml` → `BUNDLE_ID` 주입, canary 10→50→100 승격·abort·undo (DEP-03·04·05)
- [ ] TODO(A와 확정): 워커 canary — A 제안은 "워커는 하나, 번들은 접수 때 거래마다 고정(`screening_jobs.bundle_id`), canary는 API Rollout만 나눈다". 워커 지표는 A의 API `/metrics`가 DB에서 `bundle_id`별로 집계(`tradeproof_screening_failed_total`, `tradeproof_screening_processing_seconds_avg`)
- [ ] Prometheus 수집(MON-01), AnalysisTemplate로 단계별 분석 → 기준 초과 시 자동 abort 후 undo(MON-02), 롤백 알림 → B 전이 API
- [ ] 배포 API 실제 드라이버(Argo Rollouts 조작), 감지 시간·전환 시간 측정
- [ ] 트래픽·이상 입력 도구 (TST-07·08)
- [ ] 검증 Job(골든셋 검증 + Replay) 템플릿(Indexed Job)·RBAC (C·B와 협의)
- [ ] `scripts/reset_demo.sh`: DB·버킷 초기화 → 최초 번들 반입(B) → 과거 거래·골든셋 적재(데이터)
- [ ] Grafana 감시 화면 (UI-04, P1), 시나리오 실행 스크립트 (TST-09), NetworkPolicy

이어서 (모델 경로 S4·S6 → 모델 변형 S1·S5가 끝난 뒤, 3순서):

- [ ] 리스트 번들 전면 반영(`steps=[100]`) (DEP-02), 리스트 반영 후 이상 감지 기준·자동 복구 (S3)

## 제공하는 것 / 받는 것

| 제공 | 받는 쪽 | 받는 것 | 주는 쪽 |
|---|---|---|---|
| 개발 환경(compose), 시연 환경(k3s) | 전원 | 배포 요청 (`POST /deployments`) | B (M2) |
| `BUNDLE_ID` 주입 | A | 번들 ID (`deploy/`) | B (M2) |
| 롤백 알림 → 상태 전이 | B | 번들별 지표 | A |
| 배포 이력 (`deployments`), 배포 상태 조회·롤백 API | B·운영 화면(C) | 과거 거래 적재 스크립트 | 데이터 |
| 검증 Job 템플릿·권한 | B·C | | |

## 배포 API (포트 8300)

상세는 [`docs/openapi/d_platform.json`](../docs/openapi/d_platform.json). 요청에는 `X-Actor` 헤더가 필요하다.

| 호출 | 결과 |
|---|---|
| `POST /deployments` `{"bundle_id": "B-2"}` | 202 + 배포 정보. 승인되지 않은 번들 409, 진행 중인 배포가 있으면 409(`target_id` = 그 배포), 없는 번들 404, 이미 운영 중인 번들 409 |
| `GET /deployments/{id}` | `status`, `weight`(후보 번들 비율 %), `step_index`, `steps`, `analysis` |
| `POST /deployments/{id}/rollback` | 200. 진행 중이면 중단하고 되돌림, 가장 최근에 반영된 배포면 직전 번들로 되돌림. 그 외 409 |
| `GET /deployments` | 최근 배포 목록 |
| `GET /healthz` `/livez` `/metrics` | 지표 이름은 `tradeproof_platform_*` |

`status`: `pending` → `progressing` → `succeeded` 또는 `rolled_back`(`rollback_reason`: `manual` / `analysis_failed`) 또는 `failed`.
현재 stable 번들 = `succeeded` 중 가장 최근 배포의 `bundle_id` (이력이 없으면 `BUNDLE_ID`).

지금은 **가짜 드라이버**다: 실제 트래픽은 바뀌지 않고, `FAKE_STEP_SECONDS`마다 10→50→100으로 올라간 뒤 `succeeded`가 된다. `FAKE_FAIL_BUNDLES`에 든 번들은 50% 단계 분석이 실패해 자동 롤백된다. 승인 여부도 `FAKE_APPROVED_BUNDLES` 목록으로 흉내 낸다.

확정 전(TODO):
- B(M2, 11/13 담당 변경으로 배포 요청은 C가 아니라 B가 보낸다): `POST /deployments` 본문에 `bundle_id` 말고 더 보낼 것(`approval_id` 등)
- B: 배포 요청을 받은 D가 승인 여부를 B에 되묻는다(`GET /bundles/{id}`). B는 번들을 `approved`로 바꾼 **뒤에** 요청을 보내야 하고, 그 요청을 처리하는 동안 B가 같은 번들을 잠그고 있으면 서로 기다리게 된다
- 운영 화면(C): 브라우저에서 D의 API(8300)를 직접 부르면 주소가 달라 CORS가 걸린다. k3s에서는 ingress 경로로 같은 주소 아래에 묶을 예정(예: `/api/deployments` → d-platform). compose에서 직접 부를지, C의 서버를 거칠지 정해야 한다
- 운영 화면(C): 진행 상황은 `GET /deployments/{id}`를 주기적으로 조회(polling)하는 방식이다. 밀어 주는 방식(SSE 등)은 없다
- B: `GET /bundles/{id}` 응답의 상태 필드 이름·승인 상태값 (지금은 `status == "approved"`로 가정), 반영 완료·롤백을 알릴 전이 값
- 상태값·ID 접두사를 `common/contracts.py`·`common/ids.py`로 옮기기

## 실행·테스트

```bash
docker compose up -d postgres minio minio-init
pip install -r d_platform/requirements.txt
pytest d_platform/tests                      # 저장소·DB가 없으면 해당 테스트는 skip

docker compose up -d --build d-platform
curl -s -X POST localhost:8300/deployments -H 'X-Actor: me' -H 'Content-Type: application/json' -d '{"bundle_id":"B-2"}'
```
