# B DB 변경 적용

공통 규칙은 2026-11-12 이후 기존 기준 SQL 편집·DB 초기화 대신 별도 변경 파일을 추가하는 것이다. `common/schema/00_common.sql`과 `20_b_registry.sql`은 현재 주석만 있다. 따라서 테이블이 없는 경우에는 기존 DB에 B 테이블을 추가해야 하며, 이는 데이터베이스 재생성이 아니다.

## 파일과 역할

- 유일한 적용 SQL: `common/schema/50_b_registry_20261114.sql`
- ORM: `b_registry/db.py` — 매핑과 읽기 전용 `check_schema`, 운영 DDL 없음
- 적용 명령: `python -m b_registry.cli migrate-db` — 기존 PostgreSQL 연결만 허용
- 검사 명령: `python -m b_registry.cli check-db` — DDL/데이터 변경 없음
- `init-db` 및 B 전용 중복 생성 SQL은 제거했다. `create_all`은 임시 단위 테스트에서만 쓴다.

## 적용 순서

1. D가 기존 PostgreSQL DB를 백업한다. 제거되는 상세 컬럼이 필요한 경우 백업에서 확인한다.
2. B API·워커와 B 테이블을 변경하는 외부 작업을 정지한다.
3. 기존 DB의 `DATABASE_URL`로 `migrate-db`를 실행한다. 또는 psql의 `ON_ERROR_STOP=1`로 SQL 파일을 실행한다.
4. `check-db`로 매핑을 확인하고 B를 시작한다. 모델·리스트를 다시 반입하거나 볼륨을 초기화할 필요가 없다.

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

하나의 트랜잭션과 PostgreSQL advisory lock으로 적용하며 `b_registry_schema_migrations`에 버전을 기록한다. 재실행은 이미 적용한 버전을 건너뛴다. 이후 변경은 이 파일을 편집하지 않고 새 번호의 마이그레이션을 추가한다. 자동 down migration은 제공하지 않으며 복구는 백업 및 별도 전진 수정으로 수행한다.

## 검증

기본 pytest는 외부 PostgreSQL 없이 단위 테스트를 수행한다. 마이그레이션 통합 테스트는 기존 테스트 DB의 `B_REGISTRY_TEST_DATABASE_URL`을 설정하면 실행한다. 각 테스트는 무작위 이름의 독립 스키마를 만들고 끝나면 그 스키마만 제거한다. 운영 DB 주소를 사용하지 않는다.

공통 변경이므로 A/C/D 리뷰와 `contract-change` 라벨이 필요하다. 과거 사용자 요청으로 제거한 B의 livez/OpenAPI/번들 이력은 복원하지 않으며 `docs/interfaces.md`에 공통 규약 예외로 기록했다.
