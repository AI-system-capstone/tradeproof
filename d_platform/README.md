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

## 1차 (~11/17, 리스트 S1~S3)

- [ ] docker-compose: postgres + SeaweedFS(`minio:9000`) + 버킷 생성, A의 blob 테스트로 호환 확인 (11/10)
- [ ] 배포 API: 미승인 409, 진행 중 배포 409, `deployments` 기록 (11/12)
- [ ] `scripts/reset_demo.sh`: DB·버킷 초기화 → 최초 번들 반입 → 과거 거래 적재 (데이터 담당 seed 호출)
- [ ] k3s + Argo Rollouts: 리스트 번들 전면 반영(`steps=[100]`) (DEP-02)
- [ ] Prometheus 수집(MON-01), 기준 초과 시 직전 번들로 자동 롤백(MON-02, DEP-05), 롤백 알림 → B 전이 API
- [ ] 트래픽·이상 입력 도구 (TST-07·08)

## 2차 (S4~S6)

- [ ] canary 단계·승격·중단 (DEP-03·04), 번들별 워커 배치
- [ ] Grafana 감시 화면 (UI-04), 시나리오 실행 스크립트 (TST-09)

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
