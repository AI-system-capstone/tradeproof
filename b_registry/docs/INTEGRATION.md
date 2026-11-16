# B 연동 사항

## 제공 API·데이터

- `GET /imports/{id}`는 조회 옵션 없이 `{status, reason, bundle_id, filename}`를 반환합니다. 성공은 APPROVED·reason=null, 거부는 REJECTED·거부 사유입니다. 거부 기록에는 파일을 저장하지 않습니다. 번들이 없으면 bundle_id는 null입니다. 기존 반려 이력은 보존합니다.
- `POST /imports`는 모든 반입 검증·등록 완료 후 201 `{import_id, bundle_id}`를 반환합니다. 검증·등록 정책 실패는 Import 메타데이터만 기록하고 오류 응답 target_id로 조회합니다.
- `GET /bundles/active`는 200 `{active, canary}`를 반환하며 없는 항목은 null입니다.
- 주소: `http://b-registry:8100`. API 경로와 응답: [README](../README.md#http-인터페이스).
- B는 단일 컨테이너 모듈로 `/healthz`와 `/metrics`를 제공합니다. `/livez`는 제공하지 않습니다. `/docs`, `/redoc`, `/openapi.json`과 [정적 OpenAPI 명세](openapi/b_registry.json)를 제공합니다.
- 테스트 반입 파일은 Git에서 제외하며 `python -m b_registry.tests.generate_fixtures`로 로컬 생성합니다. 자동 테스트는 임시 디렉터리에 별도로 생성합니다.
- `GET /bundles/{id}`의 `model`은 `{model_id, uri}`, `list`는 `{list_id, uri}`입니다. URI는 원본 ZIP입니다. 개별 파일 메타데이터는 ZIP 안의 manifest에서 읽습니다. DB/API의 `files`, `sha256`, 중복 버전 필드는 제거했습니다.
- S3 URI는 `s3://tradeproof-registry/registry/<sha256>`. A/C가 같은 SeaweedFS에서 읽고 해시를 확인합니다. 로컬 `file://registry/...`는 B의 BLOB_DIR 기준 키이므로 단독 개발용입니다.
- 최초 모델·리스트는 HTTP 업로드로 등록합니다. 최초 ID는 `common.contracts.BASELINE_BUNDLE` (현재 B-7), 이후 번호 증가.
- DB는 `ai_models`와 `sanctions_lists`로 분리했습니다. 번들·반입 기록의 `model_id`, `list_id`가 각각을 참조합니다. 번들 API도 `model_id`, `list_id`을 사용하며 중첩 `model.model_id`, `list.list_id`를 반환합니다. 기존 `*_component_id`/`component_id`를 사용하는 호출자는 변경이 필요합니다.
- 기존 `components` 기반 DB 및 간소화 전 컬럼을 가진 DB는 `common/schema/50_b_registry_20261114.sql`과 `51_b_registry_20261115.sql`, `52_b_registry_20261116.sql` 순서로 적용 후 사용합니다. 실행 시 check-db는 읽기 전용이며 init-db는 제거했습니다.
- 오류·리스트 필드·기준 버전은 `common.contracts`, 파일 입출력은 `common.blob`을 재사용합니다. 다른 모듈 구현을 직접 import하지 않습니다.

## 담당별 남은 연결

| 담당 | 필요한 연결 |
|---|---|
| A | 정적 BUNDLES/MODELS 대신 Registry 조회·모델 설정/리스트 파일 사용. 기존 MOCK_NPZ/CSV 테스트 ZIP은 A 런타임용이 아님 |
| C | 실제 Replay Job JSON 제공. BUNDLE_ID로 번들 조회, 추가 검증 후 validated/validation_failed, 이후 승인/반려 approved/rejected 전이 요청 |
| D | B 서비스·SQL·공개 키 배포, Replay Job create/get RBAC, 배포 접수 응답(B가 canary로 변경), 완료 active, 중단 cancelled 전이. 이전 stable 복원은 해당 superseded 번들을 active로 전이 |
| 모델 제공자 | 정상 개선 모델/설정 원본 제공. 설정 호환 검사 통과가 성능 개선을 증명하지는 않음 |

B가 C의 템플릿에 `metadata.name=registry-replay-<bundle-id 소문자>`, `tradeproof-bundle-id` 라벨과 각 컨테이너의 `BUNDLE_ID` 환경변수를 설정합니다. `REPLAY_NAMESPACE`가 없으면 서비스 계정 namespace를 씁니다. 클러스터 내 인증서·토큰으로 Kubernetes API를 호출합니다.

Job 이름과 Kubernetes 응답으로 중복 생성을 막고, 장애 후 재시도합니다. DB에 Replay 상태나 사유를 저장하지 않으며 실패 사유는 로그에 남깁니다. 템플릿 미설정 시 실행하지 않습니다. 완료·성능 성공 판정은 C의 책임입니다. candidate 번들의 Job을 삭제하면 재생성될 수 있으므로 추가 검증 결과 반영 전까지 완료 Job을 유지합니다. 로컬 compose에는 Kubernetes Job 실행기가 없으므로 C의 로컬 실행 연결은 아직 제공하지 않습니다.

## 공통 규칙 반영

- 기존 `common/schema/00_common.sql`, `20_b_registry.sql`은 주석만 있는 기준 파일이며 동결 상태로 유지합니다.
- 별도 `50_b_registry_20261114.sql`에서 빈 B 기준 구조 보완 및 기존 테이블 ALTER/데이터 이관을 수행합니다. 상세 절차는 [MIGRATIONS.md](MIGRATIONS.md).
- B의 상태 전이·반입 상태·지표 계약은 `common/contracts.py`에 추가하고 `b_registry/contracts.py`는 호환 import만 제공합니다. 기존 A 상수는 변경하지 않습니다.
- API 변경과 기존 공통 규칙의 예외는 `docs/interfaces.md`에 기록합니다. PR은 `contract-change` 라벨 및 A/C/D 리뷰가 필요합니다. 단톡방 공유는 담당자가 수행합니다.
- 기존 사용자 결정대로 B는 livez를 복원하지 않습니다. 번들 이력은 기존 Bundle의 history JSON 필드로 기록합니다. 일반 규칙의 예외이며 팀 전체 합의가 완료됐다는 뜻은 아닙니다.

반입 시 ZIP·서명·해시·내용 검증을 먼저 통과해야 DB와 파일 저장소에 저장합니다. 검증 실패는 422이고 target_id에 거부 기록의 import_id를 제공합니다. 리스트 변경률·버전 충돌·등록 정책도 저장 전에 검사하며 등록 완료 후 201을 반환합니다. 번들은 candidate로 생성하고 C가 추가 검증합니다.

번들에 base_bundle_id(생성 기준 운영 번들)와 history(상태 변경 목록)를 추가합니다. B 배포 요청 시 기준이 현재 active와 다르면 원래 반입 자산과 현재 active의 나머지 자산을 묶은 새 candidate를 만들고 기존 후보를 cancelled로 전이합니다. 200 응답의 replacement_bundle_id로 새 후보를 조회하고 면밀 검증·승인을 다시 진행합니다. 기준 정보가 없는 경우는 409입니다. 상태 변경 actor/reason은 history에 시각과 함께 보존하며 추가 사용자·이력 테이블은 만들지 않습니다. history와 base_bundle_id는 번들 조회 응답에도 포함합니다.

- 배포 요청 진입점은 B의 `POST /bundles/{id}/deployments` (`actor`, `reason`)입니다. B가 재조합 필요 여부를 판단하고 기준이 맞으면 D `POST /deployments` (`bundle_id`, `X-Actor`)를 호출합니다. 접수 확인 후 B가 canary로 전이합니다.
- 응답 202는 D deployment_id 포함, 응답 200은 새 replacement_bundle_id를 재검증해야 한다는 의미입니다. transitions의 직접 canary 요청은 409입니다.
- DEPLOYMENT_URL로 D 주소를 지정합니다. 접수 결과가 불명확하면 503이며 재호출은 D 조회만 수행합니다. D 구 rolled_back 통보는 cancelled 또는 이전 stable의 active 전이로 연동 변경이 필요합니다.
