# a_screening — 샘플 심사 서비스 + 운영 화면

- **담당:** A 지윤아
- **요구사항 모듈:** M6 샘플 심사 서비스, M8 운영 화면
- **포트:** 8000
- **요구사항 ID:** SVC-01~04, DAT-01, DEP-01(서비스 쪽), MON-01(지표 노출), UI-01~03

> 기존에 구현한 코드(`api/`, `screening/`, `ocr/`, `docs/`, `sanctions/`, `db/`, `tools/`)를 이 폴더로 옮겨 오세요.
> 이 README와 `.gitkeep`은 덮어써도 됩니다.

## 1차 (~11/17, 리스트 S1~S3)

- [ ] `BUNDLE_ID` 환경변수로 번들을 받아 처리하고, 거래마다 `bundle_id`·`rules_version` 기록 (DAT-01, DEP-01)
- [ ] 같은 번들·같은 서류 → 항상 같은 추출값·판정 (SVC-04): 난수 시드·스레드 고정, 3회 반복 테스트
- [ ] C가 import할 수 있게 판정 코드(`screening/match.py`) 정리 (interfaces.md 2절)
- [ ] 지표에 `bundle_id` 라벨: 처리 지연, 오류 건수, 분류별 건수 (MON-01)
- [ ] 운영 화면: 반입 결과(UI-01), 검증·승인(UI-02) — 데이터는 B·C API

## 2차 (S4~S6)

- [ ] OCR 추출 코드도 C가 import할 수 있게 정리, 번들별 워커(canary 중 두 모델)
- [ ] 개선·저하 모델 변형을 설정으로 교체 (TST-05·06, 데이터 담당과 함께)
- [ ] 재현 화면 (UI-03)

## 제공하는 것 / 받는 것

| 제공 | 받는 쪽 | 받는 것 | 주는 쪽 |
|---|---|---|---|
| 판정·OCR 코드 (import) | C | 번들 구성 (`GET /bundles/{id}`) | B |
| 거래·판정 테이블 (읽기) | C | `BUNDLE_ID` 환경변수 | D (deploy/ 값) |
| 원문 서류 (SeaweedFS) | C | 합성 서류·과거 거래 적재 | 데이터 |
| 번들별 지표 | D | 게이트 결과·승인 API | C |

## 실행·테스트

```bash
docker compose up -d postgres minio minio-init
pytest a_screening/tests
```
