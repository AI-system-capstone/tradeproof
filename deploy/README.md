# deploy — 배포할 번들 ID (팀 연동 표의 "배포 레포")

- **쓰는 사람·적용하는 사람:** D (배포 API, `d_platform/api/drivers.py`). 2026-11-15 팀 합의로 B에서 D로 바꿨다. B(M2 릴리스 관리)는 검증·승인이 끝난 번들을 `POST /deployments`로 요청만 하고, 이 파일은 고치지 않는다.
- 파일 하나에 환경 하나. `stable`은 현재 운영 번들, `candidate`는 canary 진행 중인 후보(없으면 `""`).
- 배포 API가 배포 시작·100% 반영·롤백 때마다 이 파일을 고치고 Helm으로 적용한다. 손으로 고치지 않는다. 저장소에 커밋된 값은 최초 상태(`common/contracts.py`의 `BASELINE_BUNDLE`)이고, 배포 이력은 D의 `deployments` 표에 남는다.
- Argo CD는 쓰지 않습니다(자원 제약).

```bash
helm upgrade --install a-screening d_platform/helm/a-screening -f deploy/values-dev.yaml -n tradeproof
```
