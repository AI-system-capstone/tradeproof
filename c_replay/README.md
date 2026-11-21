# c_replay — M3 검증(골든셋·Replay) + M8 운영 화면

- **담당:** C 강민성
- **요구사항 모듈:** M3 검증, M8 운영 화면 *(M2 릴리스 관리는 B 담당)*
- **포트:** 8200
- **요구사항 ID:** VAL-01~03, UI-01~02 *(M2 `REL-01~03`은 B 담당, S6 `VAL-04·UI-03`은 [결정 0006](../docs/decisions/0006-m8-owner-c-and-exclude-s6.md)으로 제외)*

## 할 일 (결정 0005·0006 반영: 모델 갱신 경로 우선, M3·M8 담당)

- [x] 골든셋 검증 (`VAL-01`): 후보·운영 번들의 정확도·미탐·오탐을 나란히 + 차이 계산
- [x] Replay (`VAL-02·03`): A의 OCR·판정 코드를 import해 과거 거래 전체를 재처리, 건수 일치, 달라진 거래(diff)와 이전·후보 값 추출
- [x] 검증 결과·지표·영향 거래(diff) 저장 및 조회 API: M8 운영 화면과 B(M2)의 게이트 판정이 읽음
- [x] `job/`: 검증 Job 진입점. K8s Job과 일반 프로세스 둘 다로 실행 가능
- [x] `job/job-template.json`: B가 검증 Job을 만들 때 쓰는 K8s Job 양식
- [ ] `ui/` (M8 관리자 운영 화면): 반입 업로드·결과/거부 사유 확인(`UI-01`, B 연동), 검증 결과·diff·게이트·승인/반려(`UI-02`, C 검증 데이터 + B 승인 API 연동), 배포 상태 확인·롤백(D 연동) — *A의 담당자 거래 검토 화면(`a_screening/ui/review.html`)과는 별도*
- [ ] 이어서(3순서): 제재 리스트 갱신 시 후보 리스트 버전별 골든셋 검증·영향 확인(S2) 대응

## 검증 흐름

B가 정한 검증 Job 계약을 따른다 (`b_registry/verify/replay.py`, `docs/interfaces.md` 4절).

1. B가 검증할 번들마다 검증 ID를 만들고, `job/job-template.json`에 환경변수 5개를 넣어 K8s Job을 만든다.
   - `VALIDATION_ID`, `BUNDLE_ID`, `BASELINE_BUNDLE_ID`, `REQUESTED_BY`, `VALIDATION_CALLBACK_URL`
2. 검증 Job이 B의 `GET /bundles/{id}`로 두 번들의 `model_id`·`list_id`를 읽고, B가 준 검증 ID로 검증 행을 만들어 골든셋 검증·Replay를 한다.
   - B 주소는 Job 양식의 `B_URL`. 규칙 버전은 번들에 없고 A가 rules-v3로 고정한다.
3. 결과를 `completed` 또는 `failed`로 저장한다.
   - 시간 제한에 걸리면(SIGTERM) `failed`로 저장한다.
4. `VALIDATION_CALLBACK_URL`로 결과 전체를 보낸다: `{validation_id, bundle_id, baseline_bundle_id, status, golden, replay, error}`.
   - B가 200을 줄 때까지 간격을 늘려 가며 다시 보낸다(최대 10분). 409·422면 멈춘다.
   - 끝내 못 보내면 종료 코드 1로 끝나서 Job이 실패로 남는다.
5. B가 받은 결과로 게이트 판정(통과·차단)을 한다. 달라진 거래 목록은 B에 보내지 않고 C의 `/diffs`로 조회한다.

- 검증 등록 API는 없다. 검증 행은 검증 Job만 만든다.
- 새 Job이 시작되면 같은 번들의 끝나지 않은 이전 검증은 `failed`로 정리한다(B는 번들마다 Job을 하나만 돌린다).
- 같은 검증 ID가 이미 끝났으면(Job이 다시 실행됨) 다시 계산하지 않고 그 결과를 다시 보낸다.

## 제공하는 것 / 받는 것

| 제공 | 받는 쪽 | 받는 것 | 주는 쪽 |
|---|---|---|---|
| M8 운영 화면 (`UI-01·UI-02`) | 운영자 | 판정·OCR 코드 (`import`) | A |
| 검증 결과·지표·영향 거래(diff) 조회 API | B(M2 게이트) · M8(C) | 거래·판정 테이블 · 원문 서류 (읽기) | A |
| 검증 결과 전송 (`VALIDATION_CALLBACK_URL`) | B | 골든셋 · `manifest.json` | 데이터 |
| 검증 Job 양식 (`job/job-template.json`) · 이미지 | B · D | 검증 Job 시작 (K8s Job 생성, 환경변수 5개) | B |
| | | 검증 결과 수신 API (`POST /bundles/{id}/validation-results/{validation_id}`) | B |
| | | Job 생성 권한(RBAC) · 이미지 배포 · `data/` 볼륨 | D |
| | | 배포 상태 조회 · 롤백 | D |

## 실행·테스트

```bash
pytest c_replay/tests
```

검증 Job을 일반 프로세스로 실행하는 예:

```bash
BUNDLE_ID=B-9 DATABASE_URL=postgresql://... python -m c_replay.job
```

- 필수는 `BUNDLE_ID`, `DATABASE_URL` 둘이다.
- 손으로 돌릴 때 `VALIDATION_ID`가 없으면 C가 만들고, `BASELINE_BUNDLE_ID`가 없으면 `contracts.BASELINE_BUNDLE`(B-7)을 쓴다.
- `VALIDATION_CALLBACK_URL`이 없으면 결과를 보내지 않고 C의 DB에만 저장한다.
- `B_URL`이 없으면 번들 구성을 `contracts.BUNDLES`(B-7~B-10)에서 읽는다.
- 종료 코드: 0 = completed로 저장했고 B가 받음(또는 보낼 곳 없음), 1 = 그 밖의 경우, 2 = 필수 환경변수 없음.

운영 화면(`GET /admin`)을 띄우는 예:

```bash
B_URL=http://localhost:8100 D_URL=http://localhost:8300 uvicorn c_replay.api.main:app --port 8200
```

- 화면은 C 서버 한 곳만 부른다. B·D는 C의 `/api/b/…`, `/api/d/…` 중계를 거친다. 그래서 B·D에 브라우저 호출 허용(CORS)이 필요 없다.
- 중계는 화면이 쓰는 경로만 넘긴다(명세 `docs/openapi/c_replay.json`의 `/api/{service}/{path}`).
- 검증은 B가 만든 검증 ID 하나로 따라간다: 게이트·지표는 B의 `GET /bundles/{id}/validation`, 달라진 거래는 같은 ID로 C의 `/diffs`.
- 화면 맨 위에 현재 운영 번들(active)과 카나리 번들(canary, 일부 트래픽에만 배포 중)을 보여 준다.
  - B의 `GET /bundles/active`를 읽는다. 화면을 열 때, 승인·롤백 뒤, 새로고침 버튼을 누를 때 다시 읽는다.
- 검증이 queued·running이면 C의 `GET /validations/{id}`로 경과 시간과 진행 막대(골든셋·Replay 처리 건수)를 보여 준다.
  - 서버는 5초마다 다시 조회한다. 경과 시간은 브라우저가 1초마다 세고, 막대는 새 값까지 5초 동안 천천히 늘어난다.
  - 단계는 두 줄(① 골든셋, ② Replay)로 보여 준다. 최근 처리 속도로 "이 단계 약 N초 남음"을 10초 단위로 보여 준다.
  - 다시 조회해도 단계가 같으면 숫자·막대만 고친다. 화면 읽기 프로그램은 단계가 바뀔 때만 읽어 준다.
  - 처리 건수는 검증 Job이 `validations.progress`에 적는다. 처음·마지막 건과 그 사이 2초마다 한 번 적는다.
  - 이미 만든 DB에는 `common/schema/54_c_replay_20261120.sql`을 적용해야 `progress` 컬럼이 생긴다.
- 서버 없이 화면만 볼 때: `c_replay/ui/admin.html?mock=1` (진행 중 화면은 `?mock=running`)

이미지 빌드 (저장소 루트에서):

```bash
docker build -f c_replay/Dockerfile --target api -t tradeproof/c-replay:dev .
```

```bash
docker build -f c_replay/Dockerfile --target job -t tradeproof/c-replay-job:dev .
```

- 검증 Job 이미지는 실행할 때 `data/sanctions`(제재 리스트)와 `data/docs`(과거 거래 서류)를 `/app/data`에 붙여야 한다.
