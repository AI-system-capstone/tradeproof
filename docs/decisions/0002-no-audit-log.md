# 0002 감사 기록 기능 제외
- 날짜: 2026-11-09
- 결정: 별도 감사 서비스·해시 체인·audit_client·/audit API를 만들지 않음. 이력은 각 모듈 테이블(imports·approvals·deployments)에 남김
- 이유: 구현 범위가 커서 제외. 추적성은 DAT-01(거래별 번들 기록)로 확보
