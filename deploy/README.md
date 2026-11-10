# deploy — 배포할 번들 ID (팀 연동 표의 "배포 레포")

- **쓰는 사람:** C (승인된 번들 ID 기록) · **적용하는 사람:** D (`helm upgrade`)
- 파일 하나에 환경 하나. 번들이 바뀌면 이 파일의 값만 바꾸는 커밋이 곧 "배포 기록"이 됩니다.
- Argo CD는 쓰지 않습니다(자원 제약). D가 `d_platform/helm/` 차트에 이 values를 넣어 적용합니다.

```bash
helm upgrade --install a-screening d_platform/helm/a-screening -f deploy/values-dev.yaml -n tradeproof
```
