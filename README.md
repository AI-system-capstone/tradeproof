# TradeProof (AI-system-capstone)

외환·무역 심사 AI의 **모델·제재 리스트 갱신을 안전하게 운영하는 체계**입니다.
갱신 파일을 반입할 때 검증하고 → 과거 거래로 미리 돌려 보고 → 담당자가 승인하면 → 반영하고 → 반영 후 감시하다가 이상하면 되돌립니다.
실행 환경은 은행 폐쇄망을 가정한 로컬 쿠버네티스(k3s)입니다.

- 요구사항·성공 기준: [docs/requirements.md](docs/requirements.md) (조원 「프로젝트 요구사항 최종」 기준)
- **모듈 간 연동 약속: [docs/interfaces.md](docs/interfaces.md)** ← 연동을 바꾸면 반드시 여기부터

## 구현 단계

| 단계 | 기간 | 범위 | 시연 시나리오 |
|---|---|---|---|
| **1차** | ~ 11/17 | **리스트 갱신만** (모델은 최초 번들 그대로) | S1 변조·손상 리스트 차단, S2 정상 리스트 영향 확인·승인·반려, S3 리스트 반영·자동 복구 |
| 2차 | 11/18 ~ 11/22 (기능 동결) | 모델 갱신 + 재현 | S4 모델 canary, S5 회귀 모델 차단, S6 과거 판정 재현 |
| 마무리 | 11/23 ~ 11/26 | 리허설·보고서 | — |

1차에서도 2차를 위해 **처음부터 지킬 설계**:
번들은 모델+리스트 묶음 / 거래마다 처리한 `bundle_id` 기록 / 옛 번들 파일 삭제 금지 / 검증 처리 API는 `bundle_id`를 받는 범용 형태 / 게이트 결과에 자동 배포·승인 요청·차단 3가지 자리 / 번들 교체는 Rollout + `BUNDLE_ID` 환경변수.

## 폴더와 담당

| 폴더 | 요구사항 모듈 | 담당 | 포트 |
|---|---|---|---|
| [`common/`](common/) | 공통 계약·코드 | 전원 (초안 A) | — |
| [`dataset/`](dataset/) | M7 공통 데이터(내용), M9 데이터 생성 | **미정** | — |
| [`a_screening/`](a_screening/) | M6 샘플 심사 서비스, M8 운영 화면 | A 지윤아 | 8000 |
| [`b_registry/`](b_registry/) | M1 반입 검증, 번들 레지스트리·상태 전이 | B 박시우 | 8100 |
| [`c_replay/`](c_replay/) | M2 릴리스 관리(게이트·승인), M3 검증 | C 강민성 | 8200 |
| [`d_platform/`](d_platform/) | M4 배포·서빙, M5 모니터링, 인프라 | D 최은호 | 8300 |
| [`deploy/`](deploy/) | 배포할 번들 ID (표의 "배포 레포") | C가 쓰고 D가 적용 | — |
| [`scripts/`](scripts/) | 통합 실행·시연 초기화 | D | — |

## 협업 규칙 (요약)

1. **자기 폴더만 고친다.** 다른 폴더가 필요하면 담당자에게 이슈/단톡방으로 요청.
2. **연동(API·데이터·코드 import·K8s)이 생기거나 바뀌면 즉시**
   ① `docs/interfaces.md`에 한 줄 추가 → ② 단톡방 공유 → ③ PR에 `contract-change` 라벨.
3. 브랜치는 `<모듈>/<주제>` (예: `d/compose-seaweedfs`, `data/generator-v1`), `main`에 직접 push 금지.
4. PR 제목은 `[A] …` `[B] …` `[C] …` `[D] …` `[DATA] …`, 리뷰 1명 + CI 통과 후 병합.
5. 필드 이름·코드값·상태값은 문자열로 쓰지 말고 `common/contracts.py`에서 가져온다.
6. 비밀값(.env), 생성 데이터(`data/`), `.venv`는 올리지 않는다.

## 빠른 실행 (개발용 docker compose)

```bash
cp .env.example .env              # 값은 개발용 그대로 써도 됨
docker compose up -d postgres minio minio-init
docker compose ps                 # postgres healthy, minio running 확인
```

모듈 서비스는 각 담당자가 Dockerfile을 완성하면 `docker-compose.yml`의 주석을 풀어 추가합니다.
시연 환경(k3s)은 D가 관리합니다. 팀원은 Dockerfile까지만 책임집니다.

## 실행 환경 공통 규약

Python 3.13, FastAPI, psycopg3, pytest. 의존성은 `==`로 버전 고정, 베이스 이미지 태그 고정.
모든 서비스는 `GET /healthz`(준비 상태, 실패 503), `GET /livez`, `GET /metrics`(이름 `tradeproof_<모듈>_<대상>`)를 가진다.
에러 응답은 `{"code","message","target_id"}`, 시간은 UTC ISO8601(`+00:00`), 로그는 한 줄 JSON + `X-Request-ID`, 행위자 헤더는 `X-Actor`.
감사 기록 기능은 범위에서 제외(11/9 결정) — 반입·승인·배포 이력은 각 모듈 테이블에 남긴다.
