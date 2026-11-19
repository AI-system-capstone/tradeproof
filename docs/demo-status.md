# 시연(S4·S5·S1) 점검 결과: 고친 것과 남은 것

기준: main `b4c046d` (PR #23까지), 2026-11-19. 로컬 k3s(k3d)에서 반입부터 롤백까지 실제로 돌려 보며 정리했습니다.
따라하기: [demo-runbook.md](demo-runbook.md)

## 확인된 결과 (k3s, 실제 canary)

| 단계 | 결과 |
|---|---|
| S1 변조 ZIP | 즉시 거부 `PAYLOAD_HASH_MISMATCH` |
| S1 손상 ZIP | 즉시 거부 `ZIP_INVALID` |
| S5 성능 저하 모델 | 반입 통과 → 검증 75초 → 게이트 차단. 골든셋 정확도 0.89 → 0.23, 미탐 0 → 3, 오탐 11 → 74. canary 없음 |
| S4 개선 모델 | 검증 약 80초 → 게이트 통과. 정확도 0.89 → 0.94, 미탐 0 → 0, 오탐 11 → 6. Replay 전체 처리, 위험도 하향 0건 |
| canary | ingress로 40건씩 요청해 셈: 10% 단계 B-새번들 2~8건, 50% 단계 15~26건, 100%에서 전부 (약 65초) |
| 롤백 | D 롤백 API 응답 0.4초, 약 5~8초 뒤 요청 전부 이전 번들. B 상태도 정리됨 (이전 번들 active, 후보 cancelled) |

검증 Job은 골든셋 100건과 과거 거래 100건을 후보·운영 모델로 각각 OCR해서 전부 처리합니다 (OCR 약 400번, 장당 약 0.17초).

## 키트 검증 (2026-11-19, 맥, 빈 상태에서)

| 단계 | 결과 |
|---|---|
| `scripts/demo_up.sh` (클러스터 없음 → B-7 운영) | 215초, 오류 없음 |
| team-A S1 변조·손상 | 즉시 거부 (`PAYLOAD_HASH_MISMATCH`, `ZIP_INVALID`) |
| team-A S5 → B-8 | 검증 82초, 게이트 차단 (정확도 0.89 → 0.23) |
| team-A S4 → B-9 | 검증 79초, 게이트 통과 (0.89 → 0.94, Replay 100/100, 하향 0) |
| 승인 → canary | 10% 단계 B-9 2~7건/40, 33초 뒤 50% 단계 19건/40 |
| 50%에서 롤백 | 응답 즉시, 4초 뒤 40건 모두 B-7. B-7 active, B-9 cancelled |
| `scripts/demo_reset.sh` | 13초, B-7만 운영. 이미 쓴 team-A ZIP을 다시 올리면 새 번들로 처리됨 |
| `scripts/demo_sign.sh` | 새 키 + 2세트 6초, 12개 파일 모두 기대대로 (S1 거부, 나머지 통과) |

이 검증은 이 PR의 코드(A-1·C-1 수정 포함)로 만든 이미지 그대로 했습니다. 임시 패치 이미지는 쓰지 않았습니다.

## 이 PR에서 고친 것

| 담당 | 파일 | 내용 |
|---|---|---|
| A | `a_screening/registry.py` | 모델·리스트 ZIP 위치를 모르면 B의 `GET /bundles` 목록에서 찾는다. C 검증 Job은 번들 구성을 B에서 직접 읽고 `model_id`만 넘기므로, 이전에는 "model 'model-v3' 의 ZIP 위치를 모름"으로 검증이 실패했다 |
| A | `a_screening/tests/test_registry.py` | 위 경우 테스트 추가 (`test_model_and_list_without_bundle_lookup`) |
| **C 리뷰 필요** | `c_replay/Dockerfile` | job 대상에 `a_screening/registry.py` COPY 추가. PR #23 이후 OCR 코드가 registry를 import해서, 검증 Job이 시작하자마자 `ImportError`로 실패했다 |
| **D 리뷰 필요** | `d_platform/k8s/demo/` | B 레지스트리(RBAC 포함)·C 운영 화면 배포 파일, 검증 Job 양식(`REGISTRY_MODE=http`, data/docs·sanctions 볼륨) |
| **D 리뷰 필요** | `scripts/demo_up.sh`, `demo_reset.sh`, `demo_down.sh`, `demo_sign.sh`, `scripts/demo/` | 시연 키트 (k3d 생성부터 최초 번들 승인까지 한 번에, 처음 상태로 리셋, 데모 키·시연 ZIP 서명, 모델 설정) |
| — | `docs/demo-runbook.md`, `docs/demo-status.md` | 이 문서들 |

테스트: `a_screening/tests`, `c_replay/tests` 303 통과. `test_rules_v2_snapshot` 1건 실패는 main에서도 같다 (로컬 data/ 스냅샷 비교, 이 변경과 무관).

## 아직 고쳐야 하는 것 (담당별)

### 시연 중 실제로 부딪힌 버그

| 담당 | 문제 | 제안 |
|---|---|---|
| B (+C 화면) | 담당자 이름이 한글이면 B→D 배포 요청이 실패한다 (`X-Actor` 헤더는 ASCII만 가능 → `UnicodeEncodeError` → `SERVICE_UNAVAILABLE`) | 헤더 값을 인코딩하거나 본문 `actor`만 쓰기. 화면에서 영문만 받기 |
| B | D에 닿지도 못한 배포 요청이 `pending`으로 남아, 그 번들은 `Deployment outcome is unknown`이 되고 **이후 모든 배포가 막힌다** | 보내기 전에 난 오류는 실패로 기록. 막힌 요청을 푸는 방법(API) 추가 |
| B | 검증 Job이 실행 자체가 실패(`failed`)로 끝나면 다시 돌릴 API가 없다 (`ReleaseService.start`는 있으나 HTTP 미노출) | `POST /bundles/{id}/validations` 등 |
| C | 검증 중 화면에 진행 표시가 없다. 번들은 `candidate · 검증 전`, 검증은 `running`만 보임 (자동 재조회 표시는 빈 태그) | "검증 중 · 경과 N초" 문구. 진짜 진행률은 검증 Job이 처리 건수를 기록해야 함 |
| C | 화면에 현재 운영·canary 번들이 안 보이고, 롤백 뒤 "무엇으로 돌아갔는지"가 문장으로 안 나온다. 배포 진행 자동 갱신 없음 | 맨 위 상태 줄(`GET /api/b/bundles/active`), 롤백 결과 문구, 배포 칸 자동 조회 |
| C | 이미 취소된 번들에서 승인을 누르면 "승인됨 배포 요청함"으로 보인다 (실제로는 아무 일도 없음) | 번들 상태가 approved/validated가 아니면 버튼 비활성 |

### 시연 환경을 팀 공통으로 만들려면

| 담당 | 내용 |
|---|---|
| D | `d_platform/k8s/demo/`를 정식 위치로 옮기기. D 배포 API를 클러스터 안에서 띄우기 (지금은 호스트에서 kubeconfig로, README TODO) |
| D | 지표를 보고 스스로 canary를 멈추는 AnalysisTemplate (MON-02). 지금 롤백은 사람이 버튼으로 |
| D | A 차트에 data/docs 마운트 옵션. 없으면 클러스터에서 "샘플 ID로 접수"가 안 되고 업로드만 됨 |
| 데이터 | `golden_labels` 정식 스키마 (`common/schema/15_dataset.sql`). 지금은 A 임시 정의(`a_screening/db/golden_labels_tmp.sql`) |
| 데이터·D | `scripts/gen_data.sh`, `scripts/reset_demo.sh`는 아직 TODO. 키트의 `demo_reset.sh`를 바탕으로 채울 수 있음 |
| 데이터·B | 시연용 모델 원본 확정 (`docs/interfaces.md` "제공자 미정"). 키트는 `scripts/demo/models/`의 설정을 씀 |

## 시연용 모델

| 모델 | 설정 | 골든셋 결과 |
|---|---|---|
| model-v3 (기준, B-7) | tesseract, 0.7배 축소 | 필드 정확도 94.3%, 판정 일치 89/100 |
| 개선 (S4) | 0.9배 축소 | 필드 94.8%, 판정 일치 94/100, 미탐 0, 위험도 하향 0 |
| 저하 (S5) | 0.34배 + 흐림·잡음 | 판정 일치 23/100, 미탐 3 |

개선 모델은 0.5~0.9배 7가지를 100건으로 비교해 골랐습니다. 0.6배는 필드 정확도가 95.4%로 더 높았지만 고위험 보류 1건을 검토로 내려서 제외했습니다.

## 반입 ZIP을 다시 못 쓰는 이유 (설계대로)

B는 같은 파일이면 예전 반입 기록을 돌려주고, 같은 모델 ID는 한 번만 등록하며, 번들 상태는 앞으로만 갑니다.
리허설은 `scripts/demo_reset.sh`(처음 상태로)나 `START=30 scripts/demo_sign.sh`(새 번호로 서명)로 합니다.
