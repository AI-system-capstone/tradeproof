# 0001 오브젝트 스토리지는 SeaweedFS를 minio:9000으로
- 날짜: 2026-11-09
- 결정: MinIO 대신 S3 호환 SeaweedFS 사용. 호스트 minio, 포트 9000, BLOB_* 환경변수, common의 minio 파이썬 클라이언트는 그대로
- 이유: MinIO 공식 이미지 pull 불가(11/8 확인). 팀원 코드 변경 없이 교체하기 위함
