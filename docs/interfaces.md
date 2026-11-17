# 모듈 간 연동 (Interfaces)

모듈 사이의 **모든 약속**(API, 공유 코드, 공유 데이터, 쿠버네티스 연동)을 이 파일 한 곳에 모읍니다.

**연동이 새로 생기거나 바뀌면 바로:**
1. 아래 표에 한 줄 추가하거나 수정하고, 맨 아래 **변경 기록**에 날짜·작성자·내용을 남깁니다.
2. 단톡방에 공유합니다.
3. PR에 `contract-change` 라벨을 달고, 영향받는 모듈 담당자를 리뷰어로 지정합니다.

API 상세(요청·응답 예시)는 각 모듈의 `docs/openapi/<모듈>.json`과 README에 두고, 여기에는 "누가 누구에게 무엇을"만 적습니다.
**11/12~13 계획 변경([결정 0005](decisions/0005-model-path-first.md), [결정 0006](decisions/0006-m8-owner-c-and-exclude-s6.md)):** ① 모델(OCR) 갱신 경로(S4) → ② 모델 변형 시나리오(S1·S5) → ③ 제재 리스트 갱신(S2·S3) 순서로 만든다. 담당은 **B(M1·M2), C(M3·M8), A(M6), D(M4·M5)**로 정리하며, S6(과거 판정 재현)은 제외한다. 그래서 **"단계" 칸의 1차/2차는 더 이상 순서를 뜻하지 않는다** — 리스트 갱신에만 쓰는 줄은 내용에 "(이어서)"라고 적었고, 나머지는 모두 지금 필요하다. (칸 자체는 다른 PR과 충돌을 피하려고 남겨 두고 나중에 정리한다)

## 1. API

| 연결 | 방향 | 내용 | 단계 |
|---|---|---|---|
| 공통 에러 형식 | 모든 모듈 | 실패 응답은 `{code, message, target_id}`. code는 `VALIDATION_ERROR`(422), `NOT_FOUND`(404), `CONFLICT`(409), `SERVICE_UNAVAILABLE`(503), `INTERNAL_ERROR`(500) 중 하나 | 1차 |
| 번들 구성 조회 | B → A·C·D | `GET /bundles/{id}`로 `model_id`, `list_id`, 원본 ZIP URI를 제공한다. 모델/리스트의 중첩 값은 ID와 uri만 포함하며 해시·개별 파일 목록·규칙 버전·Replay 상태는 반환하지 않는다. 생성 기준 base_bundle_id와 상태 변경 history를 포함한다 | 1차 |
| 활성·카나리 조회 | B → A·C·D | `GET /bundles/active`는 200 `{active, canary}`. 각 값은 번들 객체 또는 null이며 동시에 존재할 수 있다 | 1차 |
| 상태 전이 | C·D → B | `POST /bundles/{id}/transitions`에 `{to, actor, reason}` 요청, `{from, to, changed_at}` 반환. 직접 검증·승인·canary 전이는 409이며 B의 M2 API를 사용한다. D는 완료 active·중단 cancelled를 통보한다 | 1차 |
| 지표 이름 | 각 모듈 → D | 각 모듈은 `contracts.py`에 정한 이름(`tradeproof_<모듈>_<대상>`, `bundle_id` 라벨 포함)으로 지표를 내보내고, D는 이 이름으로 canary 단계별 분석과 자동 롤백 판단을 한다 | 1차 |
| 반입 | 운영 화면(C) → B | `POST /imports` (multipart ZIP: manifest.json + 파일 + signature.sig) → 모든 반입 검증·등록 완료 시 201 `{import_id, bundle_id}`, 검증 실패 422 / 등록 정책 충돌 409(거부 메타데이터만 기록, 파일 저장 없음). `GET /imports/{id}`는 `{status, reason, bundle_id, filename}`를 반환하며 성공 APPROVED·reason=null, 거부 REJECTED·사유 기록. 오류 응답 target_id로 반입 기록 조회 가능, 번들 미생성 시 bundle_id=null | 1차 |
| 검증 결과 | C(M3) → B(M2) · 운영 화면(C) | C가 POST /bundles/{id}/validation-results/{validation_id}로 세 ID·status(completed/failed)·golden·replay·error 통보. B가 결과 저장·게이트 판정 후 200 반환, ID/확정 결과 충돌 409·잘못된 결과 422. 운영 화면은 B 결과 조회, 거래 diff는 기존 C 조회 사용. 내부 통보 API 접근 제한은 D 네트워크 정책으로 적용 | 1차 |
| 게이트·승인 | 운영 화면(C) ↔ B(M2) | 반입 후 자동 검증. GET /bundles/{id}/validation으로 결과·게이트 조회. POST /approvals 승인 시 D 배포 요청까지 진행(202 카나리 접수, 기준 변경 시 200 재조합 후보·자동 재검증), 반려 시 200 상태 전이. 정확도 90% 이상·Replay 전체 처리 기준. 최초 번들은 검증 생략 후 배포 승인 | 1차 |
| 배포 | 운영 화면(C) → B(M2) → D | B `POST /bundles/{id}/deployments`에 `{actor, reason}`. 기준 변경 시 재조합 후 200 replacement_bundle_id 반환·재검증 필요. 일치하면 B가 D `POST /deployments`에 `{bundle_id}`와 X-Actor를 보내고 접수 확인 후 canary로 변경, 202 deployment_id 반환 | 1차 |
| 배포 상태·롤백 | D → B · 운영 화면(C) | `GET /deployments/{id}`, `POST /deployments/{id}/rollback` | 1차 |
| 운영 화면 중계 | 운영 화면(C) → C → B·D | 운영 화면은 C 서버 한 곳만 부르고, C의 `/api/b/…`, `/api/d/…`가 정해 둔 경로만 B·D에 그대로 전달한다(B·D의 CORS 불필요). C에 환경변수 `B_URL`·`D_URL` 필요 | 1차 |

## 2. 코드 (import)

| 연결 | 방향 | 내용 | 단계 |
|---|---|---|---|
| `contracts.py` | 공통 → 전체 | 모든 모듈은 필드 이름·코드값·상태값·팀 상수를 이 파일에서 가져다 쓴다 | 1차 |
| 판정 코드·OCR 코드 | A → C | C는 A의 처리 경로(`from a_screening.pipeline import screen_documents, bundle_spec` — 서류 폴더 → OCR → 필드 → 판정, 워커와 같은 함수)를 그대로 가져다 써서 실제 심사와 같은 결과를 낸다. A는 이 코드를 **C가 import할 수 있는 형태**로 유지한다. 규칙 버전은 번들에 없고 A가 `rules-v3`로 고정한다(`contracts.RULES_FIXED`, 번들 구성에 `rules_version`을 빼고 넘기면 이 값) | 1차 |

## 3. 데이터

| 연결 | 방향 | 내용 | 단계 |
|---|---|---|---|
| 과거 거래·판정 결과 | A → C | C는 A의 거래·판정 테이블을 **읽기 전용**으로 읽는다 | 1차 |
| 원문 서류 | A → C | C는 A가 SeaweedFS(`minio:9000`)에 저장한 원문을 `contracts.blob_key()` 경로 규칙으로 찾는다 | 1차 |
| 골든셋 | 데이터 → C | C는 `golden_labels` 테이블과 `manifest.json`(`dataset/spec/manifest.schema.json`)을 읽는다. 골든셋은 **리스트 버전별**. 모델 경로를 만드는 동안은 최초 리스트 기준 하나만 있다 | 1차 |
| 합성 서류·정답 라벨 | 데이터 → A·C | 노이즈 섞인 합성 송장·B/L과 정답 라벨(`dataset/spec/label.schema.json`) | 1차 |
| 과거 거래 적재 | 데이터 → A·D | `dataset/seed/`가 합성 서류를 최초 번들로 A의 평시 경로에 넣어 과거 거래를 만든다. D의 `scripts/reset_demo.sh`가 호출 | 1차 |
| 시연용 리스트 원본 | 데이터 → B | (이어서, 3순서) 정상(추가)·변조·손상 리스트 원본. 서명·변조 처리는 B의 `signer/`. 그 전까지 리스트는 최초 번들의 한 버전을 그대로 쓴다 | 1차 |
| 시연용 모델 파일 | **TODO(제공자 미정)** → B | **정상 개선 모델 원본 1개**(S4)부터. 서명은 B의 `signer/`. 성능 저하 모델(S5)과 변조·손상본(S1)은 그다음(2순서) | 1차 |
| B 스키마 적용 | B → D | 기존 DB에 `common/schema/50_b_registry_20261114.sql`, `51_b_registry_20261115.sql`, `52_b_registry_20261116.sql`, `53_b_registry_20261117.sql`을 순서대로 적용한다. 모델·리스트는 `ai_models`·`sanctions_lists`, 번들은 `model_id`·`list_id`로 참조한다 | 1차 |
| 배포 이력 | D → B·C | B·C는 D의 `deployments` 테이블을 읽어 현재 stable/canary를 판단할 수 있다 | 1차 |

## 4. 쿠버네티스

| 연결 | 방향 | 내용 | 단계 |
|---|---|---|---|
| 번들 ID | D → A | D가 배포 레포(`deploy/`)에 적은 번들 ID가 Helm 설정을 거쳐 A의 환경변수 `BUNDLE_ID`로 들어간다 (적용은 D) | 1차 |
| 검증 Job 시작 | B → C | 후보 등록 후 자동 Job 생성. VALIDATION_ID·BUNDLE_ID·BASELINE_BUNDLE_ID·REQUESTED_BY·VALIDATION_CALLBACK_URL 전달. C POST /validations 및 결과 폴링 미사용. C는 완료/실패 시 콜백 URL에 POST하고 B 200까지 동일 결과 재전송(최대 10분, 409·422면 중단). C는 Job 양식의 `B_URL`로 두 번들의 `model_id`·`list_id`를 읽는다 | 1차 |
| 배포 결과 알림 | D → B | B가 D 접수 확인 후 canary로 변경하며 D는 승격 active·중단 cancelled를 통보한다. 승격 후 복원은 직전 superseded 번들을 active로 전이하며 현재 번들은 cancelled로 변경한다 | 1차 |
| A 서비스 구성 | A → D | A는 같은 Dockerfile의 빌드 대상 2개로 뜬다(compose·k3s 공통). `a-screening`(`target: screening-api`, 포트 8000, 지표 `GET :8000/metrics`) + `a-ocr-worker`(`target: ocr-worker`, 포트 없음). 둘 다 DB·`BLOB_*`·`BUNDLE_ID`를 같이 받는다. **TODO(A·D): 워커에 canary를 적용하는 방식과 워커 지표 수집 경로** | 1차 |

## 5. 메모: 최종 요구사항 문서와 다르게 정한 부분

- **C가 A의 코드를 직접 실행한다.** 최종본은 "검증도 M6(심사 서비스)의 평시 경로를 거친다"이고, 이 표는 "같은 코드를 C 안에서 실행"이다. 결과는 같아야 하므로 **SVC-04(같은 입력 → 같은 결과)를 A·C 양쪽 테스트로 확인**한다. 규약의 import 예외가 판정 코드에서 OCR 코드까지 넓어진다.
- **M3 검증을 K8s Job으로 실행한다.** docker compose에는 Job이 없으므로 C의 `job/` 진입점은 **일반 프로세스로도 실행 가능**하게 만든다. B가 Job을 만들 권한(RBAC)은 D가 부여한다.
- **반영·롤백 결과는 B(번들 상태)에도 남는다.** 롤백 시 B의 상태가 갱신되지 않으면 번들 상태, "갱신은 한 번에 하나" 잠금, 다음 후보의 기준 번들이 모두 틀어진다.

## 변경 기록

| 날짜 | 작성자 | 변경 |
|---|---|---|
| 2026-11-10 | D 최은호 | 최초 작성 (팀 연동 표 12줄 + 데이터 연동 + 1차 API) |
| 2026-11-12 | D 최은호 | 계획 변경(결정 0005): 모델 갱신 경로를 먼저, 리스트 갱신은 이어서. "단계" 칸 설명, "시연용 모델 파일" 추가, 리스트 전용 줄에 "(이어서)" 표시, 지표 용도 문구, A 서비스 구성(서비스 2개·포트·지표 경로) 추가 |
| 2026-11-12 | A 지윤아 | `common/contracts.py`·`common/blob.py` 추가, A 테이블 정의(`common/schema/10_a_screening.sql`: transactions·screening_results·screening_jobs), 판정 코드 import 경로 `a_screening.screening.match` |
| 2026-11-12 | A 지윤아 | C가 쓸 처리 경로 `a_screening.pipeline.screen_documents(tx_dir, bundle)` (워커와 같은 함수, SVC-04 테스트), A 접수 번들 환경변수 `BUNDLE_ID` |
| 2026-11-13 | C 강민성 | [결정 0006](decisions/0006-m8-owner-c-and-exclude-s6.md): 모듈 담당 재정비(B: M1·M2 / C: M3·M8 / A: M6), S6 제외, "Replay Job"을 M3 전체(골든셋 검증·Replay)를 포괄하는 "검증 Job" 표현으로 정비 |
| 2026-11-14 | B 박시우 | 번들 조회를 모델·리스트 ID와 원본 ZIP URI 기준으로 변경. 반입 검증 실패는 422, 반입 조회는 쿼리 옵션 없이 `{status, reason, bundle_id}` 반환. D 적용용 B 스키마 마이그레이션 추가 |
| 2026-11-14 | B 박시우 | `common/contracts.py`에 반입 상태 `IMPORT_*`(A), 번들 상태·전이 `REGISTRY_*`(C·D), 지표 이름 `METRIC_REGISTRY_*`(D) 추가. 기존 A 정의 유지 |
| 2026-11-15 | B 박시우 | 반입은 전체 검증·등록 후 201 반환. 번들 candidate 이후 C 추가 검증·승인, D canary/active/cancelled로 분리하고 rolled_back 제거. 활성 조회를 {active, canary}로 변경. 51 마이그레이션 추가 |
| 2026-11-16 | B 박시우 | 병합 충돌을 해소하고 C 운영 화면·B 게이트 담당과 최신 반입 API를 통합. B의 canary/active/cancelled 계약을 유지하며 D 구현 연동은 별도 작업으로 남김 |
| 2026-11-16 | B 박시우 | 카나리 시작 시 생성 기준 운영 번들 불일치 409. 거부 파일은 저장하지 않고 Import 파일명·사유 기록 및 오류 target_id로 조회. 번들 조회에 base_bundle_id·history 추가, 52 ALTER 마이그레이션 추가 |
| 2026-11-16 | B 박시우 | 기준 변경 시 카나리 요청을 단순 409 대신 현재 운영 자산과 재조합한 새 candidate 생성으로 처리. 응답 replacement_bundle_id 제공, 기존 후보 cancelled 및 새 후보 재검증·승인 필요 |
| 2026-11-16 | A 지윤아 | 판정 규칙 버전을 번들과 무관하게 `rules-v3`로 고정(`contracts.RULES_FIXED`). A API 명세서 `docs/openapi/a_screening.json` 추가 |
| 2026-11-17 | A 지윤아 | A의 번들 필드 이름을 B 레지스트리와 맞춤: `model_version` → `model_id`, `list_version` → `list_id` (값은 그대로, 예: `model-v3`, `list-v14`). A API 응답·`contracts.BUNDLES`·OCR 결과 파일에 적용. 제재 리스트 파일 안의 `list_version` 키는 B 반입 검사가 보므로 그대로 |
| 2026-11-17 | B 박시우 | 재조합을 B 배포 요청 단계로 이동. B가 D POST /deployments를 호출하고 접수 확인 후 canary로 전이. 직접 canary 전이 차단, 결과 미확인 시 재호출은 D 조회로 확인 |
| 2026-11-17 | B 박시우 | M2 후보 등록 후 자동 검증 Job 실행·C 완료 콜백·90% 게이트·배포 승인 API 추가. C 결과 폴링과 운영 화면 검증 요청 제거. 승인 시 D 배포 요청, 최초 번들은 검증 생략. 콜백 URL 환경변수·결과 통보 계약과 53 ALTER SQL 추가(C/D 연동 확인 필요) |
| 2026-11-17 | A 지윤아 | `contracts.BUNDLES` 에서 `rules_version` 제거 (B 번들과 같은 모양: `model_id`, `list_id`). 규칙은 A가 `contracts.RULES_FIXED`(rules-v3)로 고정. A 응답의 `rules_version` 은 그대로(어떤 규칙으로 판정했는지 표시) |
| 2026-11-17 | C 강민성 | 검증 Job 구현 반영(4절): B가 준 검증 ID로 저장, 콜백 URL로 결과 재전송, Job 양식의 `B_URL`로 번들 구성(`model_id`·`list_id`) 조회 |
| 2026-11-17 | C 강민성 | 1절에 운영 화면 중계(C → B·D) 추가 |
