# B DB 변경 적용

공통 규칙은 2026-11-12 이후 기존 기준 SQL 편집·DB 초기화 대신 별도 변경 파일을 추가하는 것이다. `common/schema/00_common.sql`과 `20_b_registry.sql`은 현재 주석만 있다. 따라서 테이블이 없는 경우에는 기존 DB에 B 테이블을 추가해야 하며, 이는 데이터베이스 재생성이 아니다.

## 파일과 역할

- 적용 SQL: `common/schema/50_b_registry_20261114.sql` → `51_b_registry_20261115.sql` → `52_b_registry_20261116.sql` → `53_b_registry_20261117.sql` 순서
- ORM: `b_registry/db.py` — 매핑과 읽기 전용 `check_schema`, 운영 DDL 없음
- 적용 명령: `python -m b_registry.cli migrate-db` — 기존 PostgreSQL 연결만 허용
- 검사 명령: `python -m b_registry.cli check-db` — DDL/데이터 변경 없음
- `init-db` 및 B 전용 중복 생성 SQL은 제거했다. `create_all`은 임시 단위 테스트에서만 쓴다.

## 적용 순서

1. D가 기존 PostgreSQL DB를 백업한다. 제거되는 상세 컬럼이 필요한 경우 백업에서 확인한다.
2. B API·워커와 B 테이블을 변경하는 외부 작업을 정지한다.
3. 기존 DB의 `DATABASE_URL`로 `migrate-db`를 실행한다. 또는 psql의 `ON_ERROR_STOP=1`로 네 SQL 파일을 순서대로 실행한다.
4. `check-db`로 매핑을 확인하고 B를 시작한다. 완료된 모델·리스트의 재반입이나 볼륨 초기화는 필요 없다. 구 RECEIVED 반입은 아래 절차대로 ZIP을 재전송한다.

```powershell
python -m b_registry.cli migrate-db
python -m b_registry.cli check-db
```

공통 SQL 디렉터리는 Docker PostgreSQL의 최초 볼륨 초기화 시에만 실행된다. 이미 실행 중인 DB에는 새 파일을 추가하는 것만으로 적용되지 않는다. Compose의 B 시작 명령은 check-db만 수행한다.

## 지원 경로와 보존 범위

- B 테이블이 없는 공통 기준 DB: 누락된 B 테이블 추가.
- 구 components 방식: 모델·리스트 행 복사, 번들 참조 컬럼 이름 변경, 반입 참조 이관, FK 변경.
- 분리 테이블 방식: 제거 요청된 중복 버전·해시·files·단계·규칙·이력·Replay 컬럼 ALTER.
- 현재 최소 스키마: 버전 기록과 제약 정합성 적용.
- 모델/리스트/번들/반입 ID, 원본 ZIP URI, 처리 결과·시각, 롤백 참조를 보존한다. 외부 파일은 수정하지 않는다.
- 구 components는 이관 검토용으로 남겨 두며 새 코드가 사용하지 않는다. 테이블 삭제·CASCADE 및 A/C/D 데이터 변경은 하지 않는다.
- 제거되는 컬럼은 의도적으로 보존하지 않는다. 적용 전 백업이 필요하다.
- 알 수 없는 구조나 ID 충돌·참조 무결성 위반은 실패하여 전체 트랜잭션을 되돌린다. 운영 DB에서 임의 복구를 시도하지 않는다.

각 파일은 별도 트랜잭션과 PostgreSQL advisory lock으로 적용하며 `b_registry_schema_migrations`에 버전을 기록한다. 재실행은 이미 적용한 버전을 건너뛴다. 이후 변경은 이 파일을 편집하지 않고 새 번호의 마이그레이션을 추가한다. 자동 down migration은 제공하지 않으며 복구는 백업 및 별도 전진 수정으로 수행한다.

## 검증

기본 pytest는 외부 PostgreSQL 없이 단위 테스트를 수행한다. 마이그레이션 통합 테스트는 기존 테스트 DB의 `B_REGISTRY_TEST_DATABASE_URL`을 설정하면 실행한다. 각 테스트는 무작위 이름의 독립 스키마를 만들고 끝나면 그 스키마만 제거한다. 운영 DB 주소를 사용하지 않는다.

공통 변경이므로 A/C/D 리뷰와 `contract-change` 라벨이 필요하다. B의 livez는 복원하지 않으며 `docs/interfaces.md`에 공통 규약 예외로 기록했다.

## 2026-11-15 반입·카나리 변경

- 기존 50 SQL은 수정하지 않는다. 51 SQL이 canary 단일 실행 인덱스를 추가한다.
- 구 validated는 반입 검증만 끝난 상태이므로 candidate로 바꿔 C의 추가 검증을 받는다. 기존 approved/active는 유지한다.
- rolled_back은 cancelled로 변경하며 번들 ID와 previous_active_id 참조를 보존한다.
- 구 RECEIVED는 자동 승인하지 않고 재전송 안내 사유와 함께 REJECTED로 종료한다. 원본 ZIP을 POST /imports로 다시 보내면 전체 검증 후 같은 import ID를 등록 완료 상태로 갱신한다. 이미 반려된 기록은 보존한다.
- 기존 Replay Job이 남아 있으면 C가 결과를 새 candidate 계약으로 반영해야 한다. Job 이름은 동일하므로 자동으로 두 번째 검증 Job을 만들지 않는다.
- API 시작은 마이그레이션을 수행하지 않는다. check-db는 active/canary 유일 인덱스도 검사한다.

## 2026-11-16 기준 번들·거부 기록·상태 이력

52 SQL은 기존 테이블만 ALTER합니다. bundles에 base_bundle_id(nullable FK), history(JSON, 기본 [])를 추가하고 imports에 filename(nullable)을 추가하며 artifact_uri를 nullable로 바꿉니다. 새 모델·테이블은 없습니다.

진행 중 canary가 있으면 적용을 거부합니다. 먼저 배포를 완료하거나 취소하고 B를 정지한 뒤 적용합니다. 기존 후보의 생성 기준은 추정하지 않으므로 base_bundle_id=null이며 현재 active가 있으면 카나리 진입이 차단됩니다. 기존 후보를 그대로 승격하지 말고 현재 운영 번들 기준으로 새 후보를 구성·검증해야 합니다. 기존 history는 []로 시작하며 기록되지 않았던 행위자·사유는 복구할 수 없습니다. 기존 Import ID·URI·상태·사유는 보존합니다.

## 2026-11-17 M2 검증 상태

53 SQL은 bundles에 validation_id(VARCHAR 80, nullable)와 validation(JSON, nullable)만 추가합니다. 새 테이블을 만들거나 C/D 테이블을 변경하지 않습니다. 기존 번들·반입·이력 데이터는 유지합니다. 50→51→52→53 순서로 적용하며 CLI migrate-db도 이 순서를 사용합니다.

기존 approved/validated 번들은 검증 결과가 없으면 M2 배포가 차단됩니다. 현재 운영 중인 번들은 유지하며, 기존 후보를 새 기준으로 재등록하거나 검증 상태를 확인한 별도 전진 마이그레이션으로 정리해야 합니다. 검증 데이터 없는 번들을 임의로 합격 처리하지 않습니다. 최초 배포 예외는 운영·배포 이력이 전혀 없는 경우만 적용됩니다.
