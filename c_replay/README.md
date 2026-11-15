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

1. B가 반입 검증을 통과한 번들(`validated`)마다 `job/job-template.json`에 `BUNDLE_ID`를 넣어 K8s Job을 만든다.
2. 검증 Job이 B의 `GET /bundles/active`로 운영 번들을 확인한다.
3. 검증 Job이 검증 행을 만들고 골든셋 검증·Replay를 한다.
4. 결과를 `completed` 또는 `failed`로 저장한다.
   - 시간 제한에 걸리면(SIGTERM) `failed`로 저장한다.
5. B에 완료 알림을 보낸다: `POST /bundles/{bundle_id}/validation-results` `{validation_id, bundle_id, status}`.
   - B가 만들 API이고, 경로는 가정이다.
6. B가 C의 `GET /validations?bundle_id=…`로 결과를 읽고 게이트 판정(승인·반려)을 한다.

- 검증 등록 API는 없다. 검증 행은 검증 Job만 만든다.
- 같은 번들에 진행 중인 검증이 있으면 Job은 아무것도 하지 않고 끝난다(종료 코드 1).

## 제공하는 것 / 받는 것

| 제공 | 받는 쪽 | 받는 것 | 주는 쪽 |
|---|---|---|---|
| M8 운영 화면 (`UI-01·UI-02`) | 운영자 | 판정·OCR 코드 (`import`) | A |
| 검증 결과·지표·영향 거래(diff) 조회 API | B(M2 게이트) · M8(C) | 거래·판정 테이블 · 원문 서류 (읽기) | A |
| 검증 완료 알림 | B | 골든셋 · `manifest.json` | 데이터 |
| 검증 Job 양식 (`job/job-template.json`) · 이미지 | B · D | 검증 Job 시작 (K8s Job 생성) | B |
| | | 운영 번들 조회 (`GET /bundles/active`) · 게이트 판정 요청 API | B |
| | | Job 생성 권한(RBAC) · 이미지 배포 · `data/` 볼륨 | D |
| | | 배포 상태 조회 · 롤백 | D |

## 실행·테스트

```bash
pytest c_replay/tests
```

검증 Job을 일반 프로세스로 실행하는 예:

```bash
BUNDLE_ID=B-9 DATABASE_URL=postgresql://... B_URL=http://localhost:8100 python -m c_replay.job
```

- `B_URL`이 없으면 운영 번들은 `contracts.BASELINE_BUNDLE`(B-7)로 보고, 완료 알림을 보내지 않는다.
- 종료 코드: 0 = completed, 1 = failed 또는 시작 못 함, 2 = 필수 환경변수 없음.

이미지 빌드 (저장소 루트에서):

```bash
docker build -f c_replay/Dockerfile --target api -t tradeproof/c-replay:dev .
```

```bash
docker build -f c_replay/Dockerfile --target job -t tradeproof/c-replay-job:dev .
```

- 검증 Job 이미지는 실행할 때 `data/sanctions`(제재 리스트)와 `data/docs`(과거 거래 서류)를 `/app/data`에 붙여야 한다.
