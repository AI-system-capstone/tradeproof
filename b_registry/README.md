# b_registry — 반입 검증 + 번들 레지스트리

- **담당:** B 박시우
- **요구사항 모듈:** M1 반입 검증, (M2 중) 후보 번들 생성·상태 저장
- **포트:** 8100
- **요구사항 ID:** ING-01~03, DAT-02, TST-03·04(서명·변조 처리)

## 1차 (~11/17, 리스트 S1~S3)

- [ ] `POST /imports`: 서명 → 해시 → 형식 검사, 실패 시 등록 거부 + 단계별 사유를 `imports`에 기록 (ING-01·03)
- [ ] 최초 기준 번들도 `POST /imports`로만 등록 (ING-02) — `scripts/reset_demo.sh`에서 호출
- [ ] 통과 시 운영 번들에서 바뀐 구성(리스트)만 교체한 후보 번들 생성, 번들 ID `B-<순번>`
- [ ] `GET /bundles/{id}`, `GET /bundles/active`, `POST /bundles/{id}/transitions` (허용표 밖 전이는 409, `superseded` 포함)
- [ ] 검증 통과 전이 시 C의 Replay Job 시작 (interfaces.md 4절)
- [ ] `signer/`: 서명 키 생성·서명·검증 CLI, 변조(서명 후 내용 변경)·손상(파일 훼손) 리스트 만들기 (TST-03·04)
- [ ] 모델·리스트 파일 삭제 금지 (DAT-02)

## 2차 (S4~S6)

- [ ] 모델 패키지 반입·서명 (TST-05·06)

## 제공하는 것 / 받는 것

| 제공 | 받는 쪽 | 받는 것 | 주는 쪽 |
|---|---|---|---|
| 번들 구성 조회 | A·C·D | 상태 전이 요청 | C·D |
| 상태 전이 API | C·D | 롤백 알림 (Argo Rollouts) | D |
| Replay Job 시작 | C | 시연용 리스트 원본 | 데이터 |
| 반입 결과·거부 사유 | 운영 화면(A) | 배포 이력 (`deployments` 읽기) | D |

## 실행·테스트

```bash
pytest b_registry/tests
```
