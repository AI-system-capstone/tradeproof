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
- [x] 차트에 A의 실제 이미지 2개(API·워커) (A #23), 샘플 서류 볼륨 선택 값(`api.volumes`·`worker.volumes`)
- [x] OCR 워커 자동 확장(HPA, CPU 70%, 1~3개) + Postgres·심사 API CPU 요청 보장 → 360건 처리 647초 → 428초 (11/25)
- [ ] TODO(A와 확정): 워커 canary — A 제안은 "워커는 하나, 번들은 접수 때 거래마다 고정(`screening_jobs.bundle_id`), canary는 API Rollout만 나눈다". 워커 지표는 A의 API `/metrics`가 DB에서 `bundle_id`별로 집계(`tradeproof_screening_failed_total`, `tradeproof_screening_processing_seconds_avg`)
- [x] Prometheus 수집(MON-01, `k8s/monitoring/`), A 차트 AnalysisTemplate(처리 시간 비율·실패율, 11/28 오판율 추가: 후보 10건 판정 + 5건 채점 뒤 판단)을 canary 각 비율의 통과 조건으로(시간으로 올라가지 않음, 11/23) → 기준 초과 시 Argo 중단 → 배포 API가 백그라운드 확인(`RECONCILE_SECONDS`)으로 직전 번들 복원·B에 `cancelled` (MON-02, 11/21 k3d 확인)
- [x] 배포 API 실제 드라이버(`DEPLOY_DRIVER=argo`): `deploy/values-dev.yaml`을 쓰고 Helm으로 canary 시작, 롤백(진행 중·반영 뒤 같은 방식, undo 없음), 밖에서 중단된 배포 감지. k3s에서 확인
- [x] 배포 API를 클러스터 안에서 띄우기: 이미지에 helm·kubectl·Argo Rollouts 플러그인·A 차트(`Dockerfile`), 배포 파일·권한(`k8s/platform/d-platform.yaml`). 시연 키트가 같이 올린다 (11/21)
- [ ] 감지 시간·전환 시간 측정 도구
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
| `POST /deployments` `{"bundle_id": "B-8"}` | 202 + 배포 정보. 승인되지 않은 번들 409, 진행 중인 배포가 있으면 409(`target_id` = 그 배포), 없는 번들 404, 이미 운영 중인 번들 409(최초 번들 예외는 아래) |
| `GET /deployments/{id}` | `status`, `weight`(후보 번들 비율 %), `step_index`, `steps`, `analysis` |
| `POST /deployments/{id}/rollback` | 200. 진행 중이면 중단하고 되돌림, 가장 최근에 반영된 배포면 직전 번들로 되돌림. 그 외 409. 행위자는 `X-Actor` 헤더 또는 본문 `{"actor", "reason"}`(운영 화면 방식) |
| `GET /deployments` | 최근 배포 목록 |
| `GET /healthz` `/livez` `/metrics` | 지표 이름은 `tradeproof_platform_*` |

`status`: `pending` → `progressing` → `succeeded` 또는 `rolled_back`(`rollback_reason`: `manual` / `analysis_failed`) 또는 `failed`.

**최초 번들:** 배포 이력이 없고 요청한 번들이 `BUNDLE_ID`(지금 떠 있는 최초 번들)와 같으면, canary 없이 바로 `succeeded`(`weight` 100, `analysis.initial: true`)로 기록하고 202를 돌려준다. B는 검증 없이 최초 번들을 승인해 배포를 요청하고(B PR #17), 응답의 `succeeded`를 보고 스스로 `active`로 바꾸므로 D는 전이를 보내지 않는다. `argo` 드라이버는 클러스터의 `BUNDLE_ID`가 그 번들인지 확인하고(아니면 409) `deploy/values-dev.yaml`을 `stable: <번들>`로 맞춘다. 되돌릴 직전 번들이 없어 이 배포는 롤백할 수 없다(409).
현재 stable 번들 = `succeeded` 중 가장 최근 배포의 `bundle_id` (이력이 없으면 `BUNDLE_ID`).

**B 번들 상태 반영** (`POST /bundles/{id}/transitions`, actor `d-platform`. B의 `b/registry` 브랜치 2026-11-16 계약 기준):

| 언제 | 어느 번들 | 전이 |
|---|---|---|
| 배포 시작 | 후보 | **D는 보내지 않는다.** B가 D의 `POST /deployments` 202를 받은 뒤 스스로 `approved` → `canary`로 바꾼다(B는 다른 모듈의 `canary` 전이를 409로 막는다). 시작에 실패하면(503) B가 응답·`GET /deployments`로 알고 정리한다 |
| 100% 반영 완료 | 후보 | `canary` → `active` |
| 진행 중 롤백·중단·실패 | 후보 | `canary` → `cancelled` |
| 100% 반영 뒤 롤백 | **직전 번들** | `superseded` → `active` (B가 지금 `active`인 번들을 `cancelled`로 바꾼다) |

결과는 `analysis.registry_sync`(`ok` / `pending`: B에 연결 안 됨, 다음 조회 때 다시 시도 / `rejected`: B가 거부)와 `analysis.registry_change`(마지막으로 보낸 전이)에 남는다. 배포가 끝난 뒤의 트래픽 상태(`status`)는 B와 무관하게 바로 확정한다.

**직전 번들**은 D가 배포를 시작할 때 적는 `deployments.previous_bundle_id`를 쓴다. 롤백 때 트래픽을 되돌릴 번들도, 반영 뒤 롤백 때 B에 `active`로 보내는 번들도 이 값이다. B의 `bundles.previous_active_id`는 B가 `canary` 전이를 받을 때 스스로 적는 값으로, API로는 내보내지 않는다. 배포가 한 번에 하나뿐이라 두 값은 같은 번들을 가리킨다.

**운영 화면(C)에서 직접 호출:** `CORS_ALLOW_ORIGINS`에 적은 출처만 브라우저에서 부를 수 있다(compose 기본 `http://localhost:8200`). 응답의 비율 필드 이름은 `weight`다.

드라이버는 두 가지다(`DEPLOY_DRIVER`).

- `fake`(기본, compose·테스트): 실제 트래픽은 바뀌지 않고, `FAKE_STEP_SECONDS`마다 10→50→100으로 올라간 뒤 `succeeded`가 된다. `FAKE_FAIL_BUNDLES`에 든 번들은 50% 단계 분석이 실패해 자동 롤백된다.
- `argo`(k3s): `helm`·`kubectl`로 Rollout `a-screening`을 조작한다. 아래 "시연 환경" 참고. 클러스터를 조작·조회하지 못하면 503(`SERVICE_UNAVAILABLE`)이고, 시작하지 못한 배포는 `failed`로 남는다(`analysis.message`에 사유).

승인 여부는 `REGISTRY_MODE=fake`일 때 `FAKE_APPROVED_BUNDLES` 목록으로 흉내 낸다.

확정 전(TODO):
- B(M2, 11/13 담당 변경으로 배포 요청은 C가 아니라 B가 보낸다): `POST /deployments` 본문에 `bundle_id` 말고 더 보낼 것(`approval_id` 등)
- B: 배포 요청을 받은 D가 승인 여부를 B에 되묻는다(`GET /bundles/{id}`). B는 번들을 `approved`로 바꾼 **뒤에** 요청을 보내야 하고, 그 요청을 처리하는 동안 B가 같은 번들을 잠그고 있으면 서로 기다리게 된다
- 운영 화면(C): compose에서는 CORS 허용으로 직접 부른다. k3s에서는 ingress 경로로 같은 주소 아래에 묶을지(예: `/api/deployments` → d-platform) 정해야 한다
- 운영 화면(C): 진행 상황은 `GET /deployments/{id}`를 주기적으로 조회(polling)하는 방식이다. 밀어 주는 방식(SSE 등)은 없다
- B: 승인 확인(`status == "approved"`)과 전이(`active`, `cancelled`)는 B의 `b/registry` 브랜치(PR #8, 11/17 커밋 기준) 형식에 맞췄다. B의 연동 테스트(`b_registry/tests/test_deployment.py`, 실제 D 앱을 붙여 돌림)는 통과했지만, 실제 서버끼리 붙여 보지는 않았다
- B: B는 배포 요청을 보낸 뒤 202를 받아 `canary`로 바꾸기 전까지 모든 전이를 409로 막는다. D가 그 사이에 `active`·`cancelled`를 보내면 거부(`rejected`)로 남고 다시 보내지 않는다. 단계가 `CANARY_PAUSE_SECONDS`(기본 30초) 이상이라 실제로는 생기지 않지만, 생기면 B 상태가 `canary`로 남는다
- B: 배포 이력이 없을 때의 최초 stable은 환경변수 `BUNDLE_ID`다. B의 `GET /bundles/active`(`{active, canary}`)에서 읽도록 바꿀지 정해야 한다
- B·C: 검증 Job을 누가 어떤 인자로 만드는지 — B는 Job에 `BUNDLE_ID` 환경변수를 넣고, C의 진입점은 `--validation-id`를 받는다(둘이 아직 안 맞음)
- 상태값·ID 접두사를 `common/contracts.py`·`common/ids.py`로 옮기기

## 시연 환경 (k3s)

**시연 키트 (k3d, 명령 하나로):** [`k8s/demo/README.md`](k8s/demo/README.md) — `scripts/demo_up.sh`가 클러스터·A·B·C·데이터 계층·최초 번들 B-7까지 올린다. 아래는 그 구성의 바탕이 된 k3s 수동 절차와 측정값이다.

k3s(`--disable traefik`) + ingress-nginx + Argo Rollouts가 설치돼 있어야 한다. 네임스페이스는 `tradeproof`.
Argo Rollouts는 `kubectl apply --server-side`로 설치한다(일반 apply는 `rollouts`·`analysisruns` CRD가 너무 커서 실패).

```bash
scripts/k3s_data_up.sh        # Postgres + SeaweedFS(minio:9000) + 버킷 + Secret tradeproof-env
helm upgrade --install a-screening d_platform/helm/a-screening -n tradeproof -f deploy/values-dev.yaml
curl -H 'Host: tradeproof.local' localhost      # 더미 이미지는 응답 본문이 BUNDLE_ID
```

**번들 교체 = `BUNDLE_ID` 환경변수 변경.** 번들 ID는 `deploy/values-dev.yaml`에 적고 Helm으로 적용한다. 이 파일은 **D(배포 API)만 쓴다**(2026-11-15 팀 합의, 그 전에는 B가 쓰기로 했었다). `bundle.candidate`에 새 번들이 있으면 파드 템플릿이 바뀌어 canary(10 → 50 → 100)가 시작된다.

손으로 해 볼 때:

```bash
helm upgrade a-screening d_platform/helm/a-screening -n tradeproof --set bundle.stable=B-7 --set bundle.candidate=B-8
kubectl argo rollouts get rollout a-screening -n tradeproof      # 10% 에서 멈춤
kubectl argo rollouts promote a-screening -n tradeproof          # → 50%, 한 번 더 → 100%
kubectl argo rollouts abort a-screening -n tradeproof            # 롤백 ① 트래픽을 stable 로 (비율 0)
kubectl argo rollouts undo a-screening -n tradeproof             # 롤백 ② spec 을 직전 번들로 → Healthy
```

- abort는 트래픽만 되돌린다(Rollout은 `Degraded`). spec까지 되돌리려면 직전 번들을 다시 적용해야 한다. undo는 그중 한 방법인데, 배포 API는 쓰지 않는다(아래).

### 배포 API로 조작 (`DEPLOY_DRIVER=argo`)

```bash
# WSL, 저장소 최상위에서. helm·kubectl·kubectl-argo-rollouts 가 PATH 에 있고 KUBECONFIG 가 k3s 를 가리켜야 한다
DEPLOY_DRIVER=argo CANARY_PAUSE_SECONDS=30 BUNDLE_ID=B-7 FAKE_APPROVED_BUNDLES=B-8 \
  uvicorn d_platform.api.main:app --port 8300
curl -s -X POST localhost:8300/deployments -H 'X-Actor: me' -H 'Content-Type: application/json' -d '{"bundle_id":"B-8"}'
```

| API | 드라이버가 하는 일 |
|---|---|
| 배포 시작 | 파일에 `stable: <직전>`, `candidate: <새 번들>`을 쓰고 `helm upgrade -f deploy/values-dev.yaml`(+ `canary.pauseSeconds`, `canary.weights`). 단계는 Argo가 `CANARY_PAUSE_SECONDS`마다 올린다(0이면 사람이 promote) |
| 상태 조회 | Rollout의 status를 읽어 `weight`·`step_index`에 옮긴다. `Healthy` + 새 번들이 stable이면 `succeeded`, 파일을 `stable: <새 번들>`, `candidate: ""`로 고친다 |
| 롤백 | (진행 중이면 abort →) 파일을 `stable: <직전>`, `candidate: ""`로 → `helm upgrade` → 직전 번들로 가는 canary가 생겼으면 `promote --full`로 단계를 건너뜀. 진행 중이든 100% 반영 뒤든 같다 |
| 밖에서 중단됨 | Rollout이 중단(abort)·`Degraded`면 `rolled_back`(`analysis_failed`)으로 기록하고 위 롤백을 한다. 감시(AnalysisRun)가 중단시키는 경우도 이 경로로 온다 |

**undo는 쓰지 않는다.** k3s에서 확인한 것(2026-11-15):

- 100% 반영이 끝난 뒤의 undo는 **canary 단계(10 → 50)를 다시 밟는다.** 단계가 수동 승격이면 10%에서 멈춘다.
- undo는 "바로 앞 revision"으로 간다. 정상 순서에서는 직전 번들이었지만, revision 순서가 어긋난 상태에서는 예전에 중단했던 후보(B-31)로 돌아간 적이 있다.
- 직전 번들을 직접 지정하면 어느 번들로 갈지가 revision 순서와 무관하고, 파일·Helm 값·실제 상태가 같이 맞는다.

배포 API로 확인한 결과(2026-11-16, 더미 이미지, 단계당 5초, B는 가짜 레지스트리):

| 확인 | 결과 |
|---|---|
| 배포 B-8 → B-91 | 10% → 50% → `succeeded`(14.1초). 파일은 시작 때 `B-8`/`B-91`, 끝난 뒤 `B-91`/`""` |
| 진행 중 두 번째 배포 | 409 |
| 100% 반영 뒤 롤백 | 응답 200까지 3.5초, 2.9초 뒤 `Healthy`(B-8), 응답 100/100 B-8, 파일 `B-8`/`""` |
| 50% 단계에서 롤백 | 응답 200까지 6.0초, `Healthy`(B-8), 응답 100/100 B-8 |
| `kubectl argo rollouts abort`로 밖에서 중단 | 다음 조회에서 `rolled_back`(`analysis_failed`), 클러스터 B-8, 파일·Helm 값 `stable: B-8`, `candidate: ""` |

알려진 것:

- 롤백 요청이 3.5~6초 걸린다. 대부분 `helm upgrade` 시간이다. 진행 중 롤백은 abort가 첫 명령이라 트래픽은 그보다 먼저 돌아간다(정확한 시간은 측정 도구로 다시 잰다).
- `succeeded` 직후 몇 초 동안 옛 번들 응답이 섞인다(100건 중 2~21건). 옛 파드는 30초(`scaleDownDelaySeconds`) 동안 살아 있고 nginx가 새 대상을 늦게 받는 것으로 보인다. abort 뒤 후보 응답이 남는 것과 같은 현상이다.
- 밖에서 중단된 배포는 누가 조회(`GET /deployments/{id}`·목록·`/metrics`)해야 기록·복원이 일어난다. 운영 화면이 주기적으로 조회하거나 Prometheus가 `/metrics`를 긁으면 된다.
- 배포 API는 `deploy/values-dev.yaml`을 **고치기만 하고 커밋하지 않는다.** 배포 이력은 `deployments` 표에 남는다. k3s 안에서 띄울 때 이 파일을 어디에 둘지(볼륨·ConfigMap)는 정해야 한다.

### 손으로 잰 값

2026-11-15 측정 (더미 이미지):

| 확인 | 결과 |
|---|---|
| 10% / 50% / 100% 단계의 후보 응답 비율 | 9.3% (37/400) / 51% (205/400) / 100% (300/300) |
| abort | 비율 0까지 0.46초·0.82초, 그 뒤 후보 응답 0건(300건 중), Rollout `Degraded` |
| undo | `Healthy`, `BUNDLE_ID`가 직전 번들 |
| abort 후 후보의 마지막 응답 (50% 단계, 3회) | 0.63초 / 5.96초 / 2.26초 — **한 번은 목표 5초 초과.** 측정 도구를 만들어 감시 단계에서 다시 잰다 |

## 실행·테스트

```bash
docker compose up -d postgres minio minio-init
pip install -r d_platform/requirements.txt
pytest d_platform/tests                      # 저장소·DB가 없으면 해당 테스트는 skip

docker compose up -d --build d-platform
curl -s -X POST localhost:8300/deployments -H 'X-Actor: me' -H 'Content-Type: application/json' -d '{"bundle_id":"B-8"}'
```
