# B — 패키지 검증·번들 Registry

포트 **8100**. 모델/제재리스트 ZIP을 받아 검증하고 번들을 관리합니다. 반입 검증과 등록은 HTTP 요청 안에서 완료합니다.

## 구조·흐름

| 경로                  | 역할                                                       |
| --------------------- | ---------------------------------------------------------- |
| `api/`                | 외부 HTTP 인터페이스                                       |
| `verify/`             | 서명·해시·내용 검사, 번들 상태 전이, 자동 처리·Replay 요청 |
| `signer/`             | 공급자용 키 생성·서명·변조 도구                            |
| `db.py`, `storage.py` | DB 및 SeaweedFS/S3 저장 (`common.blob` 재사용)             |
| `tests/`              | 검증·API·복구 테스트와 임시 테스트 자산 생성기              |

**업로드 → 검증 → AI 모델 또는 제재 리스트 등록 → 후보 번들(candidate) → C 추가 검증(validated/validation_failed) → 승인(approved)/반려(rejected) → D 카나리(canary) → 활성화(active)/취소(cancelled).**
팀 Registry 규약에 따라 검증 통과 후 번들을 자동 생성합니다. 기존 `/approve`는 생성된 번들 ID를 돌려주는 멱등 호환 API입니다.

최초 모델·리스트도 각각 업로드해야 합니다. 둘이 준비되면 `common.contracts.BASELINE_BUNDLE`부터 번호를 부여합니다. 이후에는 활성 번들의 변경 대상만 교체합니다. 초기 번들을 활성화하기 전 새 버전은 거부합니다.

## HTTP 인터페이스

| 메서드·경로                      | 입력                         | 정상 응답                                                   |
| -------------------------------- | ---------------------------- | ----------------------------------------------------------- |
| `POST /imports`                  | multipart `package`: ZIP 1개 | 검증·등록 완료 시 201 `{import_id, bundle_id}`, 검증 실패 422 / 등록 정책 충돌 409                                           |
| `GET /imports/{id}`              | —                            | 200 `{status: APPROVED\|REJECTED, reason, bundle_id, filename}` |
| `POST /imports/{id}/approve`     | 본문 없음                    | 200 `{bundle_id}` (첫 모델·리스트 준비 전 409)              |
| `GET /bundles`                   | —                            | 번들 목록                                                   |
| `GET /bundles/active`            | —                            | 200 `{active: 번들 또는 null, canary: 번들 또는 null}`                                 |
| `GET /bundles/{id}`              | —                            | 모델·리스트 ID·원본 ZIP URI·상태·생성 시각     |
| `POST /bundles/{id}/transitions` | `{to, actor, reason}`        | 200 `{from, to, changed_at}`                                |
| `POST /bundles/{id}/deployments` | `{actor, reason}` | D 접수 확인 시 202 `{bundle_id, status, deployment_id}` / 재조합 시 200 `{bundle_id, status: cancelled, replacement_bundle_id}` |
| `GET /healthz`                  | —                            | 모듈 준비 상태 확인 (DB/파일 저장소/신뢰 키 장애 시 503) |
| `GET /metrics`                   | —                            | Prometheus 지표                                             |

B는 하나의 컨테이너에서 API와 Replay 요청 워커를 실행하는 모듈입니다. DB·저장소의 수명주기는 상위 관리 프로세스가 담당하며, `/healthz`는 B가 해당 의존성을 사용할 준비가 되었는지만 확인합니다. 별도 `/livez`는 제공하지 않습니다. FastAPI 기본 문서 경로 `/docs`(Swagger UI), `/redoc`(ReDoc), `/openapi.json`(API 명세)을 제공합니다. 정적 명세는 [b_registry.json](docs/openapi/b_registry.json)에 보관하며 `python -m b_registry.cli openapi`로 생성합니다.

저장 전에 ZIP·서명·manifest 해시·내용·리스트 변경률·버전 충돌·초기 등록 정책을 모두 검사합니다. 검증·등록 정책 실패 시 파일과 자산·번들을 저장하지 않고 Import에 REJECTED, 파일명, 사유, 완료 시각만 기록합니다. 오류 응답의 target_id는 이 import_id이며 GET /imports/{id}로 조회할 수 있습니다. 성공한 요청만 파일·DB 등록을 수행한 뒤 201을 반환합니다. 등록 도중 저장소/DB 오류가 나면 DB 트랜잭션은 롤백됩니다. 파일 저장 후 DB 커밋 실패 시 검증된 ZIP이 저장소에 남을 수 있으며 재요청은 같은 내용 키를 사용합니다.

조회 응답은 `status`, `reason`, `bundle_id`, `filename`이며 쿼리 옵션은 없습니다. 성공은 APPROVED·reason=null, 거부는 REJECTED·거부 사유입니다. filename은 경로·제어문자를 제거한 최대 255자의 표시용 파일명입니다. 최초 모델 또는 리스트만 등록된 경우 `bundle_id`는 null이며, 상대 자산 등록 후 연결됩니다. `completed_at`은 반입 성공 또는 거부 결정 시각이며 추가 성능 검증·배포 완료 시각이 아닙니다. 기존 반려 이력의 `REJECTED`와 사유는 조회 호환성을 위해 보존합니다.

처리 대기 202 응답은 없습니다. 동일 ZIP 재전송은 같은 import ID를 반환합니다. `GET /bundles/active`는 active와 canary를 함께 반환하며, 각각 없으면 null입니다. 둘 다 없어도 200입니다.
오류는 공통 `{code, message, target_id}`. `X-Request-ID`를 응답·JSON 로그에 전달하며, 전이의 `X-Actor`를 보내면 본문의 `actor`와 같아야 합니다. 전이 API는 C·D가 호출하는 내부 서비스 인터페이스입니다.

허용 전이:
- `candidate → validated | validation_failed`: C의 추가 검증 결과. 반입 검증 합격과 구분합니다.
- `validated → approved | rejected`: 검증 후 배포 승인/반려.
- `approved → canary | cancelled`: 카나리 시작/배포 전 취소.
- `canary → active | cancelled`: 전체 승격/카나리 중단.
- `superseded → active`: 카나리가 없을 때 현재 활성 번들의 직전 stable 복원만 허용.

active와 canary는 각각 최대 하나입니다. 카나리 중단 시 기존 active를 유지합니다. 승격 시 이전 active는 superseded로 바뀝니다. 이전 stable 복원 시 교체되는 현재 번들은 cancelled가 됩니다. B는 rolled_back 상태를 사용하지 않습니다. 상태 변경은 D의 실제 배포 결과 통보이며 B가 트래픽을 직접 전환하지 않습니다.

## 실행·테스트

### DB 구조

| 테이블 | 역할 | 주요 참조 |
|---|---|---|
| `ai_models` | AI 모델 ID·원본 ZIP 위치·등록 시각 | PK `model_id` |
| `sanctions_lists` | 제재 리스트 ID·원본 ZIP 위치·등록 시각 | PK `list_id` |
| `bundles` | 심사에 사용할 모델·리스트 묶음 | `model_id` → ai_models, `list_id` → sanctions_lists |
| `imports` | ZIP 반입·검증 이력 | nullable `model_id` 또는 `list_id`, `bundle_id` |
| `registry_lock` | 동시 등록·상태 변경의 직렬화 | 고정 행 `id=1` |

모델과 리스트는 독립 테이블이며 같은 ID 문자열을 각각 사용할 수 있습니다. 한 반입 기록이 모델과 리스트를 동시에 참조하는 것은 DB 제약으로 금지합니다. 실제 파일은 원본 ZIP 하나로 저장합니다. 개별 payload 파일은 별도로 저장하지 않습니다.

모델·리스트 테이블은 각각 `model_id` 또는 `list_id`, `artifact_uri`, `created_at` 세 컬럼만 가집니다. ID가 버전 식별자이므로 중복 `logical_version`을 제거했습니다. DB의 `sha256`, `files`, `imports.package_hash`, `imports.steps`, `registry_lock.revision`도 제거했습니다.

해시·전자서명 검증은 유지합니다. 파일별 해시는 ZIP 내부 manifest를 사용하고, 저장소는 기존 내용 기반 키(`registry/<sha256>`)를 유지하므로 동일 ZIP 중복 처리와 저장 후 변경 확인에 별도 DB 해시 컬럼이 필요하지 않습니다. 검증 단계별 결과는 검증 함수의 일시적인 결과이며 DB에 보관하지 않습니다.

반입은 성공과 거부 메타데이터를 남기며 거부된 행의 artifact_uri와 자산·번들 참조는 null입니다. 번들의 `previous_active_id`는 카나리 기준 stable 및 이전 stable 복원에 사용합니다. 번들에는 생성 당시 운영 번들 `base_bundle_id`와 상태 변경 `history`를 추가합니다. B 배포 요청 시 base_bundle_id와 현재 active ID가 다르면 반입한 자산을 유지하고 나머지를 현재 active에서 가져온 새 candidate를 생성합니다. 기존 후보는 cancelled로 바꾸고 200 응답의 replacement_bundle_id로 새 ID를 반환합니다. 새 후보는 면밀 검증·승인을 다시 받아야 합니다. 기존 번들의 구성과 Import 연결은 변경하지 않습니다. 기준 정보가 없어 변경 자산을 식별할 수 없으면 409입니다. history는 `{from, to, actor, reason, changed_at}` 목록이며 자동 교체·복원되는 양쪽 번들 모두 같은 트랜잭션으로 기록합니다. actor는 호출자가 전달한 식별자이며 사용자 인증을 대신하지 않습니다. 규칙 버전·Replay 제출 상태는 저장하지 않습니다. 이번 변경은 B에만 적용하며 A의 규칙 처리는 별도 작업으로 유지합니다.

`WorkflowLock`은 `registry_lock`에 매핑된 내부 잠금 모델입니다. 단일 컨테이너에서도 여러 HTTP 등록·상태 변경 요청이 동시에 실행될 수 있으므로 유지합니다. 트랜잭션마다 같은 행을 UPDATE하여 충돌을 막으며, 별도 관리 프로세스가 아닙니다.

번들 API는 `model_id`, `list_id`를 반환합니다. `rules_version`, `replay` 응답은 제거했으며 `base_bundle_id`, `history`는 조회 응답에 포함합니다. 중첩 `model`은 `model_id`, 중첩 `list`는 `list_id`를 사용합니다. 기존 `*_component_id`와 중첩 `component_id`는 제거했습니다. 중첩 `model`/`list`는 ID와 ZIP `uri`만 반환하며 `version`, `sha256`, `files`는 없습니다. 최상위 중복 `model_version`, `list_version`, `is_active`도 제거했습니다. A·C는 ZIP을 받아 manifest에 따라 파일을 읽어야 합니다.

**DB 적용:** 기존 공통 SQL(00/20)은 동결된 기준 파일이며 수정하지 않습니다. 변경은 `common/schema/50_b_registry_20261114.sql`, `51_b_registry_20261115.sql`, `52_b_registry_20261116.sql` 순서로 적용합니다. ORM은 테이블 매핑만 담당하며 `create_all()`은 일회용 단위 테스트에서만 사용합니다. 운영 적용 절차는 [MIGRATIONS.md](docs/MIGRATIONS.md)를 따릅니다.

저장소 루트에서 실행합니다 (Python 3.13, PowerShell).

```powershell
py -3.13 -m venv b_registry/.venv
b_registry/.venv/Scripts/Activate.ps1
pip install -r b_registry/requirements.txt
# 수동 테스트용 샘플 생성 (출력 폴더가 이미 있으면 덮어쓰지 않음)
python -m b_registry.tests.generate_fixtures
$env:TRUST_KEY="b_registry/tests/fixtures/trusted.pub"  # 로컬 생성한 테스트 공개 키
# 기존 PostgreSQL에 마이그레이션 적용 후 읽기 전용 검사
python -m b_registry.cli migrate-db
python -m b_registry.cli check-db
uvicorn b_registry.api.main:app --host 0.0.0.0 --port 8100
```

```powershell
# pytest는 임시 디렉터리에 샘플을 생성하므로 로컬 fixtures가 없어도 실행 가능
pytest -q b_registry/tests
curl.exe -F "package=@b_registry/tests/fixtures/packages/model-v3.zip" http://localhost:8100/imports
curl.exe -F "package=@b_registry/tests/fixtures/packages/list-v14.zip" http://localhost:8100/imports
```

`tests/fixtures/`의 ZIP·원본 CSV·테스트 공개 키와 `trust/`의 로컬 신뢰 키는 Git에 올리지 않습니다. 생성 코드만 공유하며 기존 로컬 샘플은 보존합니다. Compose 실행 시 `REGISTRY_TRUST_KEY`로 공개 키 경로를 지정하거나 위 명령으로 기본 테스트 샘플을 먼저 생성하세요.

미설정 시 SQLite 연결 기본값은 남아 있지만 자동 생성하지 않습니다. 공유 환경 실행과 마이그레이션에는 기존 PostgreSQL의 DATABASE_URL이 필요합니다. SQLite 스키마 생성은 임시 단위 테스트 전용입니다. 팀 환경에서는 `DATABASE_URL=postgresql://...`, `BLOB_ENDPOINT`, `BLOB_ACCESS_KEY`, `BLOB_SECRET_KEY`, `BLOB_SECURE`를 설정하면 PostgreSQL·SeaweedFS를 사용합니다. 버킷 기본값은 `tradeproof-registry` (`REGISTRY_BLOB_BUCKET`으로 변경).

루트 compose를 수정하지 않고 B를 추가하는 설정은 `compose.yml`입니다:
`docker compose --env-file .env.example -f docker-compose.yml -f b_registry/compose.yml up --build b-registry`.
공개 키는 `TRUST_KEY`에 지정·마운트합니다. Compose는 시작 시 `check-db`만 실행하며 마이그레이션을 자동 적용하지 않습니다. 기존 DB는 별도로 백업하고 B를 정지한 뒤 D가 마이그레이션을 적용합니다. DB/볼륨 재생성은 하지 않습니다.

## 검증·패키지

ZIP: `manifest.json + signature.sig + payload/*`. 기존 `manifest.sig`도 허용하지만 서명 파일은 하나만 있어야 합니다.
검증은 **안전한 ZIP → Ed25519 서명 → manifest·파일 해시 → 내용·호환성** 순서입니다. 제재리스트는 활성 리스트 대비 (추가+삭제+수정)/기준 건수가 **20% 초과면 REJECTED**이며 사유에 변경률을 반환합니다.

| 종류                     | 내용 형식                                                           |
| ------------------------ | ------------------------------------------------------------------- |
| 모델 `TESSERACT_CONFIG`  | `payload/model_config.json`: A의 engine/lang/config/preprocess 형식 |
| 모델 `MOCK_NPZ`          | 기존 테스트용 설정 + `weights.npz` (실제 OCR 모델 아님)             |
| 리스트 `TRADEPROOF_JSON` | `payload/sanctions.json`: 공통 `ENTRY_FIELDS` 및 버전·수집일·건수   |
| 리스트 `SYNTHETIC_TEST`  | 기존 합성 CSV 테스트 형식                                           |

설정 패키지는 엔진 바이너리를 포함하지 않습니다. B는 형식·무결성을 검증하며 **OCR 품질/개선 여부는 C의 Replay에서 판단**합니다. 기존 NPZ/CSV ZIP은 독립 검증 테스트용이며 A의 실제 입력으로 사용하지 않습니다.

공급자 CLI:

```bash
python -m b_registry.signer keygen --private-key vendor.key --public-key publisher.pub
python -m b_registry.signer.package_release --type ocr_model --item-id model-v3 --source model-dir --output model.zip --private-key vendor.key
python -m b_registry.cli validate model.zip --trust-key publisher.pub
python -m b_registry.signer tamper model.zip tampered.zip
python -m b_registry.signer corrupt model.zip corrupted.zip
```

리스트 서명은 `--type sanctions_list --source list.json` (또는 CSV). Registry에는 공개 키만 전달합니다.

## 다른 모듈 연결

Replay는 `REPLAY_JOB_TEMPLATE`과 K8s 서비스 계정 권한이 준비되면 `candidate` 번들에 대해 Job 생성을 시도합니다. DB에 제출 상태를 저장하지 않고 결정적인 Job 이름과 Kubernetes 응답으로 중복을 확인합니다. 실패는 로그로 남기고 재시도하며, 템플릿이 없으면 실행하지 않습니다. 번들이 candidate인 동안 Job을 삭제하면 다시 생성될 수 있으므로 완료 Job은 추가 검증 결과 반영 전까지 유지해야 합니다. Replay 검증 결과는 C가 validated/validation_failed로 반영하고, 이후 approved/rejected를 결정합니다. Replay 대기 지표는 제거했습니다.

현재 A의 Registry 조회 연결, C의 실제 Job 템플릿·성능 검증, D의 RBAC·배포 연결은 별도 작업입니다. 공통 계약 반영 목록과 연결 조건은 [INTEGRATION.md](docs/INTEGRATION.md)에 정리했습니다. 연동 계약 변경은 `docs/interfaces.md`에도 기록합니다.

파일 필드가 파싱되기 전의 잘못된 multipart 요청이나 HTTP 전체 크기 제한 거부는 Import를 생성하지 않습니다. 신뢰 키·DB·저장소 장애(503)는 파일 검증 불합격으로 기록하지 않습니다.

## B에서 D로 배포 요청

면밀 검증과 승인을 마쳐 approved가 된 번들에 `POST /bundles/{id}/deployments`로 `{actor, reason}`을 보냅니다. B가 기준 번들을 확인합니다. 기준이 바뀌면 새 candidate를 만들고 기존 후보를 cancelled로 바꾸며 D는 호출하지 않습니다. 반환한 replacement_bundle_id를 다시 검증·승인해야 합니다.

기준이 같으면 B가 `DEPLOYMENT_URL`(기본 `http://d-platform:8300`)의 `POST /deployments`에 `{bundle_id}`와 `X-Actor`를 보냅니다. D는 이때 B에서 approved 상태를 조회할 수 있습니다. D의 202 접수를 확인한 뒤 B가 canary로 전이하고 deployment_id를 반환합니다. `POST /bundles/{id}/transitions`의 직접 canary 요청은 409로 막습니다. D의 완료(active)·중단(cancelled) 통보는 기존 transitions API를 사용합니다.

요청 대기는 기존 history에 deployment_request로 기록합니다. D의 명시적 4xx 거절은 B 409이며 approved를 유지합니다. 타임아웃·5xx·응답 오류는 503이며 결과 미확인 요청으로 남깁니다. 같은 배포 요청을 다시 보내면 POST를 반복하지 않고 D의 최근 배포 목록에서 결과를 찾습니다. 미확인 상태에서는 다른 배포와 상태 전이를 막습니다. 최근 100건에서 결과가 없거나 중복되면 자동 재요청하지 않으므로 D 상태를 확인하는 운영 조치가 필요합니다.

D의 구 rolled_back 콜백은 현재 B 상태 계약과 다르며 D 담당자 연동 작업이 필요합니다. 이번 변경은 D 코드를 수정하지 않습니다. 검증 결과 수집·게이트 판정 자체는 이번 배포 요청 API와 별도입니다.
