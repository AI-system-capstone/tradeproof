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
- 기존 `components` 기반 DB 및 간소화 전 컬럼을 가진 DB는 `common/schema/50_b_registry_20261114.sql`과 `51_b_registry_20261115.sql`, `52_b_registry_20261116.sql`, `53_b_registry_20261117.sql` 순서로 적용 후 사용합니다. 실행 시 check-db는 읽기 전용이며 init-db는 제거했습니다.
- 오류·리스트 필드·기준 버전은 `common.contracts`, 파일 입출력은 `common.blob`을 재사용합니다. 다른 모듈 구현을 직접 import하지 않습니다.

## 담당별 남은 연결

| 담당 | 필요한 연결 |
|---|---|
| A | Registry 조회 연결은 구현됨. C가 B 번들을 직접 조회한 경우에도 A OCR이 모델 ZIP 위치를 찾는 경로 확인. 기존 MOCK_NPZ/CSV 테스트 ZIP은 A 런타임용이 아님 |
| C | C 검증 Job·콜백 구현됨. 이미지에 A registry.py 포함 여부와 ZIP 위치 연결 확인. B가 전달한 VALIDATION_ID로 실행하고 VALIDATION_CALLBACK_URL에 완료/실패 결과 POST. B 200까지 동일 결과 재전송. POST /validations는 B에서 호출하지 않음 |
| D | B 서비스·SQL·공개 키 배포, Replay Job create/get RBAC, 배포 접수 응답(B가 canary로 변경), 완료 active, 중단 cancelled 전이. 이전 stable 복원은 해당 superseded 번들을 active로 전이 |
| 모델 제공자 | 정상 개선 모델/설정 원본 제공. 설정 호환 검사 통과가 성능 개선을 증명하지는 않음 |

B는 반입·재조합으로 생긴 candidate를 자동으로 검증합니다. Kubernetes/Docker 실행에 VALIDATION_ID, BUNDLE_ID, BASELINE_BUNDLE_ID, REQUESTED_BY, VALIDATION_CALLBACK_URL을 전달합니다. B는 C 결과를 폴링하지 않으며, C가 결과 수신 API에 완료/실패를 통보합니다. 운영 화면은 B의 검증 결과를 조회합니다.

C가 통보한 결과를 B가 검증하여 정확도 90% 이상·Replay 전체 처리이면 validated로 바꾸고, 승인 API에서 승인 기록과 D 배포 요청까지 진행합니다. 반려는 rejected입니다. 최초 번들은 검증 없이 배포 승인을 받습니다. C 실행 실패는 게이트 불합격과 구분하며, 진행 중 Job이 남아 있으면 중복 재검증을 막습니다.

## 공통 규칙 반영

- 기존 `common/schema/00_common.sql`, `20_b_registry.sql`은 주석만 있는 기준 파일이며 동결 상태로 유지합니다.
- 별도 `50_b_registry_20261114.sql`에서 빈 B 기준 구조 보완 및 기존 테이블 ALTER/데이터 이관을 수행합니다. 상세 절차는 [MIGRATIONS.md](MIGRATIONS.md).
- B의 상태 전이·반입 상태·지표 계약은 `common/contracts.py`에 추가하고 `b_registry/contracts.py`는 호환 import만 제공합니다. 기존 A 상수는 변경하지 않습니다.
- API 변경과 기존 공통 규칙의 예외는 `docs/interfaces.md`에 기록합니다. PR은 `contract-change` 라벨 및 A/C/D 리뷰가 필요합니다. 단톡방 공유는 담당자가 수행합니다.
- 기존 사용자 결정대로 B는 livez를 복원하지 않습니다. 번들 이력은 기존 Bundle의 history JSON 필드로 기록합니다. 일반 규칙의 예외이며 팀 전체 합의가 완료됐다는 뜻은 아닙니다.

반입 시 ZIP·서명·해시·내용 검증을 먼저 통과해야 DB와 파일 저장소에 저장합니다. 검증 실패는 422이고 target_id에 거부 기록의 import_id를 제공합니다. 리스트 변경률·버전 충돌·등록 정책도 저장 전에 검사하며 등록 완료 후 201을 반환합니다. 번들은 candidate로 생성하고 C가 추가 검증합니다.

번들에 base_bundle_id(생성 기준 운영 번들)와 history(상태 변경 목록)를 추가합니다. B 배포 요청 시 기준이 현재 active와 다르면 원래 반입 자산과 현재 active의 나머지 자산을 묶은 새 candidate를 만들고 기존 후보를 cancelled로 전이합니다. 200 응답의 replacement_bundle_id로 새 후보를 조회하고 골든셋·Replay 검증·승인을 다시 진행합니다. 기준 정보가 없는 경우는 409입니다. 상태 변경 actor/reason은 history에 시각과 함께 보존하며 추가 사용자·이력 테이블은 만들지 않습니다. history와 base_bundle_id는 번들 조회 응답에도 포함합니다.

- 배포 승인 진입점은 B의 `POST /approvals`입니다. 기존 `POST /bundles/{id}/deployments`는 approved 건의 재요청·접수 확인용으로 유지합니다. B가 재조합 필요 여부를 판단하고 기준이 맞으면 D `POST /deployments` (`bundle_id`, `X-Actor`)를 호출합니다. 접수 확인 후 B가 canary로 전이합니다.
- 응답 202는 D deployment_id 포함, 응답 200은 새 replacement_bundle_id를 재검증해야 한다는 의미입니다. transitions의 직접 canary 요청은 409입니다.
- DEPLOYMENT_URL로 D 주소를 지정합니다. 접수 결과가 불명확하면 503이며 재호출은 D 조회만 수행합니다. 현재 D는 cancelled 또는 이전 stable의 active 전이로 통보합니다.

## M2 연동 확인 사항

- C Job 진입점과 콜백 처리는 구현되어 있습니다. 검증 이미지·데이터 마운트 연결은 아래 점검 사항을 확인합니다. B는 C의 코드·테이블을 수정하지 않습니다.
- D의 최초 번들 즉시 적용 및 active/cancelled 통보는 구현되어 있습니다. 한글 담당자 지원은 D 배포 요청 계약 변경이 필요합니다.
- M2 API: POST /bundles/{id}/validation-results/{validation_id}, GET /bundles/{id}/validation, POST /approvals. 상세 바디·오류·설정은 README/OpenAPI를 기준으로 합니다.
- 공통 constants/ALTER SQL 및 본 연동 변경은 contract-change 라벨·A/C/D 리뷰·단톡방 공유 대상입니다. 로컬 문서 갱신과 실제 공유·리뷰 완료를 구분합니다.

## 2026-11-21 시연 복구 연동 점검

- B-1: D는 변경하지 않습니다. 한글 X-Actor는 전송 전에 실패로 기록하여 pending 잠금은 남기지 않지만, 한글 배포 지원 자체는 미완료입니다. D에 JSON actor 수신·requested_by 원문 저장 또는 명시적 헤더 디코딩 계약을 요청합니다. 현재 B→D 요청 바디/헤더는 기존 계약 그대로입니다.
- B-2: 미전송 확정 실패는 재승인 가능. 응답 유실은 pending 유지. 운영자 확인 후 POST /bundles/{id}/deployment-recovery로 해제하며, D의 목록 조회가 완전하지 않거나 연결 실패 시 해제하지 않습니다. D 담당자는 원래 요청의 처리 종료·미접수를 확인해야 합니다.
- B-3: C는 POST /bundles/{id}/validations 중계와 '다시 검증' 버튼을 연결해야 합니다. candidate + validation.failed만 대상, 202 새 실행 / 진행 중·게이트 불합격 409 / Job 상태 확인 불가 503입니다. 기존 자동 검증·결과 콜백은 유지합니다.
- 취소 번들 재승인: B도 409로 차단합니다. C는 cancelled/superseded 승인 버튼을 비활성화하고 오류 응답을 성공 문구로 표시하지 않아야 합니다.
- 이번 수정은 기존 JSON history/validation을 사용합니다. 새 테이블·컬럼·마이그레이션·common 계약 상수 변경은 없습니다.

### B-4 배포 설정 리뷰 상태

현재 main b4c046d에는 문서의 d_platform/k8s/demo/b-registry.yaml이 없습니다. 실제 YAML 리뷰·승인과 k3s 실행 검증은 **미완료**이며, D가 시연 키트 원본을 공유하거나 PR로 제공해야 합니다. 확인한 c_replay/job/job-template.json 기준의 점검 내용은 다음과 같습니다.

| 항목 | 점검 내용 / 필요한 연결 |
|---|---|
| B 서비스 | DATABASE_URL, BLOB_* 및 시연 서명 키에 대응하는 TRUST_KEY 읽기 전용 마운트. 서명 비밀 키는 B에 배포하지 않음 |
| D·콜백 주소 | DEPLOYMENT_URL은 실제 D 서비스 주소, VALIDATION_CALLBACK_BASE_URL은 C Job에서 접근 가능한 B 주소 |
| 템플릿 | VALIDATION_RUNNER=kubernetes, VALIDATION_JOB_TEMPLATE JSON 파일 마운트 및 VALIDATION_NAMESPACE. C 원본 양식과 실행 명령·제한 시간 일치 확인 |
| RBAC | B 서비스 계정에 해당 namespace의 batch/jobs get/create. 현재 B 실행 경로에는 cluster-admin·Job delete 권한 불필요 |
| C 이미지 | 현재 C Dockerfile의 A 파일 COPY에 a_screening/registry.py 누락. C 담당 수정 필요. A의 ZIP 위치 조회 보완도 A 담당 확인 |
| C 환경 | 원본 Job 양식은 B_URL만 명시. A의 REGISTRY_MODE=http 및 REGISTRY_URL도 환경/Secret에서 실제 제공되는지 확인 |
| 데이터 | 원본 Job 양식에 볼륨 없음. data/docs·data/sanctions 실제 데이터 마운트, 골든셋 테이블·DB·BLOB_* 접근 확인 |
| 통합 확인 | 서명 ZIP 반입 → C Job 생성 → 콜백 수신. 실패 뒤 이미지 수정 → 재검증 API → 새 Job/완료까지 실행 확인 필요 |

공유 대상: C(중계·버튼·Job 이미지/템플릿), D(한글 actor 계약·미확정 요청 확인·배포 YAML), A(C Job에서 모델 ZIP 위치 조회). 실제 단톡방 공유·리뷰 요청·contract-change 라벨은 아직 수행하지 않았습니다.
