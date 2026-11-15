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
- [x] k3s + ingress-nginx + Argo Rollouts: `helm/a-screening` 차트(더미 이미지), `BUNDLE_ID` 주입, canary 10→50→100 수동 승격·abort·undo 확인 (DEP-03·04·05). 데이터 계층(`k8s/data/`)도 올림
- [ ] 차트에 A의 실제 이미지 2개(API·워커) 넣기 — A의 `BUNDLE_ID` 변경이 `main`에 들어온 뒤
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
| `POST /deployments` `{"bundle_id": "B-8"}` | 202 + 배포 정보. 승인되지 않은 번들 409, 진행 중인 배포가 있으면 409(`target_id` = 그 배포), 없는 번들 404, 이미 운영 중인 번들 409 |
| `GET /deployments/{id}` | `status`, `weight`(후보 번들 비율 %), `step_index`, `steps`, `analysis` |
| `POST /deployments/{id}/rollback` | 200. 진행 중이면 중단하고 되돌림, 가장 최근에 반영된 배포면 직전 번들로 되돌림. 그 외 409. 행위자는 `X-Actor` 헤더 또는 본문 `{"actor", "reason"}`(운영 화면 방식) |
| `GET /deployments` | 최근 배포 목록 |
| `GET /healthz` `/livez` `/metrics` | 지표 이름은 `tradeproof_platform_*` |

`status`: `pending` → `progressing` → `succeeded` 또는 `rolled_back`(`rollback_reason`: `manual` / `analysis_failed`) 또는 `failed`.
현재 stable 번들 = `succeeded` 중 가장 최근 배포의 `bundle_id` (이력이 없으면 `BUNDLE_ID`).

**B 번들 상태 반영:** 100% 반영이 끝나면 B에 `active`, 롤백(수동·자동)이면 `rolled_back` 전이를 보낸다(`POST /bundles/{id}/transitions`, actor `d-platform`). 결과는 `analysis.registry_sync`에 남는다 — `ok` / `pending`(B에 연결 안 됨, 다음 조회 때 다시 시도) / `rejected`(B가 거부). 트래픽 상태(`status`)는 B와 무관하게 바로 확정한다.

"직전 번들"은 두 곳에 따로 기록되고 쓰임이 다르다. D의 `deployments.previous_bundle_id`는 배포를 시작할 때의 stable로, **트래픽을 어느 번들로 되돌릴지**에 쓴다. B의 `bundles.previous_active_id`는 번들이 `active`가 될 때 B가 스스로 적는 값으로, `rolled_back` 전이를 받으면 B가 그 번들을 다시 `active`로 **직접 복원**한다. 그래서 D는 롤백 때 후보 번들에 `rolled_back` 하나만 보내고, 직전 번들에 대한 전이는 보내지 않는다. 배포가 한 번에 하나뿐이라 두 값은 같은 번들을 가리킨다.

**운영 화면(C)에서 직접 호출:** `CORS_ALLOW_ORIGINS`에 적은 출처만 브라우저에서 부를 수 있다(compose 기본 `http://localhost:8200`). 응답의 비율 필드 이름은 `weight`다.

지금은 **가짜 드라이버**다: 실제 트래픽은 바뀌지 않고, `FAKE_STEP_SECONDS`마다 10→50→100으로 올라간 뒤 `succeeded`가 된다. `FAKE_FAIL_BUNDLES`에 든 번들은 50% 단계 분석이 실패해 자동 롤백된다. 승인 여부도 `FAKE_APPROVED_BUNDLES` 목록으로 흉내 낸다.

확정 전(TODO):
- B(M2, 11/13 담당 변경으로 배포 요청은 C가 아니라 B가 보낸다): `POST /deployments` 본문에 `bundle_id` 말고 더 보낼 것(`approval_id` 등)
- B: 배포 요청을 받은 D가 승인 여부를 B에 되묻는다(`GET /bundles/{id}`). B는 번들을 `approved`로 바꾼 **뒤에** 요청을 보내야 하고, 그 요청을 처리하는 동안 B가 같은 번들을 잠그고 있으면 서로 기다리게 된다
- 운영 화면(C): compose에서는 CORS 허용으로 직접 부른다. k3s에서는 ingress 경로로 같은 주소 아래에 묶을지(예: `/api/deployments` → d-platform) 정해야 한다
- 운영 화면(C): 진행 상황은 `GET /deployments/{id}`를 주기적으로 조회(polling)하는 방식이다. 밀어 주는 방식(SSE 등)은 없다
- B: 승인 확인(`status == "approved"`)과 전이(`active`, `rolled_back`)는 B의 PR #8 형식에 맞췄다. 가짜 B 서버 테스트만 했고 **실제 B와 붙여 보지는 않았다**
- B·C: 검증 Job을 누가 어떤 인자로 만드는지 — B는 Job에 `BUNDLE_ID` 환경변수를 넣고, C의 진입점은 `--validation-id`를 받는다(둘이 아직 안 맞음)
- 상태값·ID 접두사를 `common/contracts.py`·`common/ids.py`로 옮기기

## 시연 환경 (k3s)

k3s(`--disable traefik`) + ingress-nginx + Argo Rollouts가 설치돼 있어야 한다. 네임스페이스는 `tradeproof`.
Argo Rollouts는 `kubectl apply --server-side`로 설치한다(일반 apply는 `rollouts`·`analysisruns` CRD가 너무 커서 실패).

```bash
scripts/k3s_data_up.sh        # Postgres + SeaweedFS(minio:9000) + 버킷 + Secret tradeproof-env
helm upgrade --install a-screening d_platform/helm/a-screening -n tradeproof -f deploy/values-dev.yaml
curl -H 'Host: tradeproof.local' localhost      # 더미 이미지는 응답 본문이 BUNDLE_ID
```

**번들 교체 = Rollout의 `BUNDLE_ID` 환경변수 변경.** 파드 템플릿이 바뀌면 Argo Rollouts가 canary(10 → 50 → 100)를 시작한다.

- Helm은 **설치할 때만** `BUNDLE_ID`를 정한다(`bundle.stable`). 그 뒤에는 배포 API가 Rollout을 직접 고치고, 차트를 다시 적용해도 떠 있는 값을 유지한다(`bundle.keepLive`). 그래서 `deploy/values-dev.yaml`은 설치·시연 초기화 때의 기준 번들을 뜻한다.
- 롤백 순서: **① abort**(트래픽을 stable로) → **② `BUNDLE_ID`를 직전 번들로 되돌림**(Rollout이 `Healthy`로). 100% 반영이 끝난 뒤의 롤백은 ②만 하면 되고, `rollbackWindow` 덕분에 단계 없이 바로 전환된다.
- 주의: 최근 3개 버전 안에 있는 번들로 바꾸면 "되돌리기"로 보고 canary 단계를 건너뛴다. 새 번들은 해당 없음.

손으로 해 볼 때 (배포 API의 실제 드라이버가 하게 될 일):

```bash
set_bundle() { kubectl -n tradeproof patch rollout a-screening --type=json -p "[
  {\"op\":\"replace\",\"path\":\"/spec/template/spec/containers/0/env/0/value\",\"value\":\"$1\"},
  {\"op\":\"replace\",\"path\":\"/spec/template/metadata/labels/tradeproof~1bundle-id\",\"value\":\"$1\"}]"; }

set_bundle B-8                                                   # canary 시작, 10% 에서 멈춤
kubectl argo rollouts get rollout a-screening -n tradeproof
kubectl argo rollouts promote a-screening -n tradeproof          # → 50%, 한 번 더 → 100%
kubectl argo rollouts abort a-screening -n tradeproof            # 롤백 ①
set_bundle B-7                                                   # 롤백 ② (직전 번들)
```

2026-11-15 측정 (더미 이미지):

| 확인 | 결과 |
|---|---|
| 10% / 50% / 100% 단계의 후보 응답 비율 | 9.3% (37/400) / 51% (205/400) / 100% (300/300) |
| 차트를 다시 적용했을 때 | `BUNDLE_ID` 유지, canary 시작 안 됨 |
| canary 중 롤백 (abort → 직전 번들) | 비율 0까지 0.53초, `Healthy`까지 1.55초 |
| 100% 반영 뒤 롤백 (직전 번들로 변경만) | 단계 없이 즉시 전환, 그 뒤 후보 응답 0건 |
| abort 후 후보의 마지막 응답 (50% 단계, 3회) | 0.63초 / 5.96초 / 2.26초 — **한 번은 목표 5초 초과.** 측정 도구를 만들어 감시 단계에서 다시 잰다 |

## 실행·테스트

```bash
docker compose up -d postgres minio minio-init
pip install -r d_platform/requirements.txt
pytest d_platform/tests                      # 저장소·DB가 없으면 해당 테스트는 skip

docker compose up -d --build d-platform
curl -s -X POST localhost:8300/deployments -H 'X-Actor: me' -H 'Content-Type: application/json' -d '{"bundle_id":"B-8"}'
```
