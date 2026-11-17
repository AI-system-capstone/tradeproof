# a_screening — 샘플 심사 서비스

- **담당:** A 지윤아
- **요구사항 모듈:** M6 샘플 심사 서비스 (M8 운영 화면은 결정 0006으로 C)
- **포트:** 8000
- **요구사항 ID:** SVC-01~04, DAT-01, DEP-01(서비스 쪽), MON-01(지표 노출)

서류(송장·B/L)를 받아 OCR로 필드를 뽑고, 제재 리스트와 대조해 3단계(통과 후보·담당자 검토·고위험 보류)로 분류합니다.
AI는 OCR에만 쓰고, 필드 추출·매칭·분류는 모두 코드입니다. 판정에는 항상 근거(`reasons`)가 남습니다.

## 구성

| 경로 | 내용 |
|---|---|
| `api/main.py` | 심사 API (접수·조회·검토·지표). OCR은 하지 않고 큐에 넣기만 함 |
| `ocr/worker.py` | OCR 워커. `screening_jobs` 큐를 `FOR UPDATE SKIP LOCKED`로 꺼내 OCR → 판정 → 저장. 실패 시 재시도, 3회 넘으면 담당자 검토 |
| `ocr/extract.py` | OCR·필드 추출 (tesseract). `model-v3`(정상)·`model-v4`(일부러 성능 저하) |
| `pipeline.py` | 한 건 처리 경로 `screen_documents(서류 폴더, 번들)`: OCR → 필드 → 판정. **워커와 C(골든셋·Replay)가 같이 씀** |
| `screening/match.py` | 판정 순수 함수 `screen(fields, index, rules_version, ocr)` |
| `store.py` | 저장소 (메모리·Postgres). 테이블 정의는 `common/schema/10_a_screening.sql` |
| `ui/review.html` | 담당자 검토 화면 (`GET /review`, 외부 라이브러리 없음) |
| `sanctions/`, `docs/`, `db/`, `run_all.sh` | 시연 데이터 생성: 제재 리스트 → 합성 서류 100건 → OCR → 평가 → 과거 거래 적재 (데이터 담당이 정해지면 `dataset/`로 이관) |
| `tools/loadgen.py` | 시연용 트래픽 생성기 |

## API

| 메서드·경로 | 하는 일 |
|---|---|
| `POST /screenings` | 서류 접수 (multipart PDF/PNG, 또는 필드 JSON) → 202 + `tx_id`. 번들은 접수 시점에 고정 |
| `GET /screenings`, `GET /screenings/{tx_id}` | 목록·상세 (분류, 근거, 추출 필드, 처리 상태) |
| `GET /screenings/{tx_id}/documents/{doc_type}` | 원문 서류 이미지 (검토 화면용) |
| `POST /screenings/{tx_id}/review` | 담당자 판단·필드 정정. 정정 값으로 재판정해 고위험이면 승인 불가(409) |
| `GET /metrics` | Prometheus 지표 (`bundle_id` 라벨) |
| `GET /healthz`, `GET /livez`, `GET /meta` | 준비 상태·생존·계약 값 |

에러는 공통 형식 `{code, message, target_id}`. 상세는 실행 후 `http://localhost:8000/docs`.

## 실행·테스트

```bash
pip install -r a_screening/requirements.txt          # 호스트 실행 시 tesseract-ocr, poppler(pdftoppm) 필요
a_screening/run_all.sh                               # data/ 생성 (리스트·서류·OCR·평가·DB 적재)
pytest a_screening/tests                             # 메모리 저장소 (data/ 없으면 데이터 테스트는 건너뜀)
TEST_DATABASE_URL=postgresql://tradeproof:tradeproof-dev@localhost:5432/tradeproof pytest a_screening/tests
python a_screening/db/check_contracts.py             # 스키마 ↔ contracts.py 대조

docker build -f a_screening/Dockerfile --target screening-api -t tradeproof/a-screening:dev .
docker build -f a_screening/Dockerfile --target ocr-worker    -t tradeproof/a-ocr-worker:dev .
```

환경변수: `DATABASE_URL`, `BLOB_ENDPOINT`·`BLOB_ACCESS_KEY`·`BLOB_SECRET_KEY`, `BUNDLE_ID`(접수 번들, 예전 이름 `ACTIVE_BUNDLE`), `SHADOW_BUNDLE`.
OCR 결과는 tesseract 버전에 따라 달라서 이미지에서 5.5.0으로 고정합니다 (팀 기준값: model-v3 정확도 94.3%, 미탐 0).

**규칙 버전은 `rules-v3`로 고정**합니다 (`contracts.RULES_FIXED`, 11/16). 규칙은 `screening/match.py` 코드라서 B 번들(모델 + 리스트)에 없고,
C는 `pipeline`을 쓰면 자동으로 같은 규칙을 씁니다. rules-v3 = rules-v2 + OCR 오류 대비 규칙(OCR 혼동 글자 감안 일치 → 보류, 신뢰도 낮음·서류 불일치·누락 → 검토).
기준값(rules-v3): model-v3 미탐 0·하향 0 / model-v4(저하) 미탐 0·**보류→검토 하향 3**. 저하는 하향으로 드러나므로 게이트가 하향도 미탐으로 세야 S5가 막힙니다.

**API 명세서:** [`docs/openapi/a_screening.json`](../docs/openapi/a_screening.json). 코드에서 만듭니다: `python -m a_screening.api.export_openapi`
(코드를 바꾸고 명세를 안 고치면 `tests/test_openapi.py`가 실패). 띄운 서버에서는 `/docs`.

## 진행 상황

구현 순서는 결정 0005를 따릅니다. 제재 리스트 갱신을 빼는 것이 아니라 **모델(OCR) 갱신 경로를 먼저** 만듭니다.
A 담당은 결정 0006에 따라 M6(심사 서비스, 담당자 검토 화면 포함)입니다. S6(재현)은 결정 0006에서 제외됐습니다.

1. 정상 모델 하나로 모델 갱신 경로: S4(개선 모델 canary·자동 롤백)
2. 모델 변형 시나리오: S1(변조·손상 모델 차단), S5(저하 모델 게이트 차단)
3. 제재 리스트 갱신: S2·S3 (그동안 리스트는 최초 번들의 한 버전 그대로)

**끝난 것**

- [x] 기존 코드를 팀 레포 구조로 이전 (감사 기록 제외 — 결정 0002)
- [x] C가 import할 수 있는 판정 코드: `from a_screening.screening.match import screen, load_index`
- [x] 거래마다 처리한 `bundle_id` 기록 (canary 중 어느 모델이 처리했는지 구분)
- [x] 접수 시점에 번들 고정: API 파드마다 번들이 달라도(canary) 워커 하나가 두 모델을 모두 처리
- [x] C가 쓸 처리 경로 `a_screening.pipeline.screen_documents` — 워커도 같은 함수를 씀 (모델 Replay는 과거 서류를 새 모델로 다시 OCR)
- [x] 같은 번들·같은 서류 → 같은 결과 (SVC-04): 3회 반복 + 워커 경로 == pipeline 테스트 (tesseract 5.5.0 컨테이너에서도 확인)
- [x] `BUNDLE_ID` 환경변수로 접수 번들 지정 (예전 이름 `ACTIVE_BUNDLE`도 받음)
- [x] 판정 규칙 `rules-v3` 고정 (`contracts.RULES_FIXED`)
- [x] API 명세서 `docs/openapi/a_screening.json` + 명세 == 코드 == 실제 응답 테스트
- [x] 담당자 검토 화면, 원문 업로드(PDF·PNG → SeaweedFS), 재시도·섀도 판정, `/metrics`·`/healthz`·`/livez`

**남은 것 (1순위: S4)**

- [ ] k3s에 A 실제 이미지 올리기: D 차트(`d_platform/helm/a-screening`)는 지금 더미 이미지. A 이미지 2개(`screening-api`, `ocr-worker`)와
  필요한 설정(DB·SeaweedFS 주소, 제재 리스트·모델 설정 위치)을 D에 넘긴다
- [ ] 번들 구성을 B의 `GET /bundles/{id}`에서 가져오기 (B PR #8 병합 뒤). 응답의 `model.uri`·`list.uri`(SeaweedFS `tradeproof-registry`)에서
  ZIP을 받아 `payload/model_config.json`·`payload/sanctions.json`을 쓴다. 지금은 `contracts.BUNDLES`(B-7~B-10)와 `data/`
- [ ] 번들별 지표: 이름을 `tradeproof_<모듈>_<대상>` 규칙으로, 모든 지표에 `bundle_id` 라벨 (D의 canary 비교·자동 롤백 근거, MON-01)

**남은 것 (2순위: S1·S5)**

- [ ] 모델 변형을 설정으로 교체: 기준 / 개선(S4) / 저하(S5, 지금 `model-v4`) — 노이즈 서류는 데이터 담당과 함께 (TST-05·06)

**남은 것 (3순위: S2·S3)**

- [ ] 리스트 버전을 번들 구성에서 받아 판정 (판정 코드는 이미 리스트 버전별로 동작)

**팀과 정할 것**

- S4 자동 롤백을 일으키는 방법: "골든셋은 통과하지만 실제 트래픽에서 나빠지는 모델"을 A가 만들지, 장애를 주입할지
- 워커 canary (interfaces TODO A·D): 번들은 접수 때 거래마다 고정되고 워커는 거래의 번들로 처리하므로,
  API 파드만 `BUNDLE_ID`를 나눠 canary 하면 워커 하나가 두 모델을 다 처리한다. 워커는 `BUNDLE_ID`를 읽지 않는다
- 시연용 정상 개선 모델 파일을 누가 만드는지 (interfaces TODO)

## 제공하는 것 / 받는 것

| 제공 | 받는 쪽 | 받는 것 | 주는 쪽 |
|---|---|---|---|
| 처리 경로 `pipeline.screen_documents` (import) | C | 번들 구성 (`GET /bundles/{id}`) | B |
| 거래·판정 테이블 (읽기) | C | 모델 설정·제재 리스트 ZIP (SeaweedFS `tradeproof-registry`) | B |
| 원문 서류 (SeaweedFS) | C | `BUNDLE_ID` 환경변수 (D가 `deploy/values-dev.yaml` → Helm으로 넣음) | D |
| 번들별 지표 (`/metrics`) | D | 합성 서류·과거 거래 적재 | 데이터 |
