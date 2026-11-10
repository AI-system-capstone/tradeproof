# common — 공통 계약·공통 코드

모든 모듈이 import합니다. **변경은 `contract-change` 라벨 + 영향 모듈 담당자 리뷰 + 단톡방 공유.**
값은 이름을 바꾸지 말고 **추가**를 우선합니다. 의존성은 표준 라이브러리 + psycopg + minio만.

> A가 기존에 구현·테스트한 `common/`을 이 폴더로 옮겨 오세요. 아래 표는 최종 목표 목록입니다.

| 파일 | 담는 것 | 작성 | 상태 |
|---|---|---|---|
| `contracts.py` | 필드 이름, 분류 3종, 번들·반입·처리 상태값, 에러 코드, ID 형식, 지표 이름, **팀 상수**(게이트·롤백 기준), `blob_key()` | A (변경은 전원 리뷰) | A 기존 코드 |
| `errors.py` | 공통 에러 응답 | A | A 기존 코드 |
| `logging.py` | 한 줄 JSON 로그, `X-Request-ID`, `X-Actor` | A | A 기존 코드 |
| `db.py` | `DATABASE_URL` 연결 | A | A 기존 코드 |
| `blob.py` | SeaweedFS(`minio:9000`) 접근, 버킷 이름 | A (호환 확인 D) | A 기존 코드 |
| `clients.py` | 다른 모듈 API 호출 (연결 2초·응답 5초, GET만 재시도) | A | A 기존 코드 |
| `ids.py` | `new_id(prefix)` — TX·IMP·RP·AP·DP | D | 신규 (11/12) |
| `bundles.py` | `change_kind(stable, candidate) -> "model" \| "list"`, 번들 구성 헬퍼 | D | 신규 (11/12) |
| `metrics.py` | 지표 이름 규칙, `bundle_id` 라벨 헬퍼 | D | 신규 (11/12) |
| `schema/` | 테이블 정의 (파일별 소유: 00 B · 10 A · 15 데이터 · 20 B · 30 C · 40 D) | 각 소유 모듈 | 뼈대 |
| `tests/` | 계약 대조 테스트 (schema ↔ contracts) | A | A 기존 코드 |

`audit_client.py`는 11/9 결정(감사 기록 제외)으로 만들지 않습니다.
