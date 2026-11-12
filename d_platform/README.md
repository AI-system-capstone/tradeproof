# d_platform — 배포·감시 + 인프라

- **담당:** D 최은호
- **요구사항 모듈:** M4 배포·서빙, M5 모니터링, 인프라(compose·k3s), M7 저장소(스키마 적용·버킷)
- **포트:** 8300
- **요구사항 ID:** DEP-01·03·04·05, MON-01·02, UI-04(P1), TST-07~09 (DEP-02는 11/12 제외)

## 폴더

| 폴더 | 내용 |
|---|---|
| `api/` | 배포 API: `POST /deployments`, `GET /deployments/{id}`, `POST /deployments/{id}/rollback` |
| `helm/` | 모듈 차트·values. `deploy/`의 번들 ID를 A의 `BUNDLE_ID`로 주입 |
| `k8s/` | Argo Rollouts Rollout·AnalysisTemplate, Replay Job 템플릿(C와 협의), RBAC, NetworkPolicy |
| `monitoring/` | Prometheus 규칙, Grafana 대시보드 |
| `compose/` | docker-compose 보조 설정 (SeaweedFS S3 계정) |
| `tools/` | M9: 심사 트래픽(TST-07), 이상 상황 입력(TST-08), 시나리오 실행(TST-09) |

## 할 일 (11/12 변경: 모델 갱신 경로만 — [결정 0005](../docs/decisions/0005-model-path-only.md))

리스트 전면 반영(DEP-02)과 리스트용 이상 감지 기준은 범위에서 빠졌다. 자동 롤백(MON-02)은 canary에 적용한다.

- [ ] docker-compose: postgres + SeaweedFS(`minio:9000`) + 버킷 생성, minio 파이썬 클라이언트 smoke test (`tests/test_blob_smoke.py`)
- [ ] compose에 A 서비스 2개(`a-screening` API, `a-ocr-worker`) 연결 — A의 PR 병합 후
- [ ] 배포 API: 미승인 409, 진행 중 배포 409, `deployments` 기록 (처음엔 가짜 드라이버)
- [ ] k3s + ingress-nginx + Argo Rollouts: `helm/a-screening` 차트, `deploy/values-dev.yaml` → `BUNDLE_ID` 주입, canary 10→50→100 승격·abort·undo (DEP-03·04·05)
- [ ] TODO(A와 협의): OCR 워커에 canary를 적용하는 방식(번들별 워커 배치 제안), 워커 지표 수집 경로
- [ ] Prometheus 수집(MON-01), AnalysisTemplate로 단계별 분석 → 기준 초과 시 자동 abort 후 undo(MON-02), 롤백 알림 → B 전이 API
- [ ] 배포 API 실제 드라이버(Argo Rollouts 조작), 감지 시간·전환 시간 측정
- [ ] 트래픽·이상 입력 도구 (TST-07·08)
- [ ] Replay Job 템플릿(Indexed Job)·RBAC (C·B와 협의)
- [ ] `scripts/reset_demo.sh`: DB·버킷 초기화 → 최초 번들 반입(B) → 과거 거래·골든셋 적재(데이터)
- [ ] Grafana 감시 화면 (UI-04, P1), 시나리오 실행 스크립트 (TST-09), NetworkPolicy

## 제공하는 것 / 받는 것

| 제공 | 받는 쪽 | 받는 것 | 주는 쪽 |
|---|---|---|---|
| 개발 환경(compose), 시연 환경(k3s) | 전원 | 배포 요청 | C |
| `BUNDLE_ID` 주입 | A | 번들 ID (`deploy/`) | C |
| 롤백 알림 → 상태 전이 | B | 번들별 지표 | A |
| 배포 이력 (`deployments`) | B·C | 과거 거래 적재 스크립트 | 데이터 |
| Replay Job 템플릿·권한 | B·C | | |

## 실행·테스트

```bash
docker compose up -d postgres minio minio-init
pytest d_platform/tests
```
