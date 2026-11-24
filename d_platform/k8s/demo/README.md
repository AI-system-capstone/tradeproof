# 시연 따라하기 (로컬 k3s)

이 문서대로 하면 각자 PC에 시연 환경을 올리고, 운영 화면에서 **S1(변조·손상 차단) → S5(성능 저하 모델 게이트 차단) → S4(개선 모델 canary·롤백)**를 직접 해 볼 수 있습니다.
k3s를 Docker 안에서 돌리는 k3d를 씁니다. D의 시연 환경(k3s + ingress-nginx + Argo Rollouts)과 같은 구성입니다.

처음 작성: A 지윤아(11/19, 맥 k3d에서 S1·S4·S5·canary·롤백 확인). 정리·관리: D. 키트 파일은 `scripts/demo_*.sh`, `scripts/demo/`, 이 폴더(`d_platform/k8s/demo/`)입니다.

## 1. 준비물

| 프로그램 | 확인 | 설치 (맥: Homebrew) |
|---|---|---|
| Docker Desktop (메모리 8GB 이상 권장) | `docker info` | docker.com |
| k3d | `k3d version` | `brew install k3d` |
| kubectl | `kubectl version --client` | `brew install kubectl` |
| helm | `helm version` | `brew install helm` |
| Argo Rollouts kubectl 플러그인 | `kubectl-argo-rollouts version` | `brew install argoproj/tap/kubectl-argo-rollouts` |
| python3 (3.11 이상), curl | `python3 --version` | |

윈도우는 WSL(우분투) 안에서 같은 명령으로 합니다. 리눅스용 설치는 각 프로그램 문서를 참고하세요.
이 키트는 자기 클러스터 전용 kubeconfig(`data/demo/kit/kubeconfig`)만 씁니다. 평소 쓰는 kubectl 컨텍스트는 바꾸지 않습니다.

## 2. 골든셋 데이터 받기

골든셋(합성 서류 100건 + 제재 리스트 + 정답 라벨)은 git에 없습니다 (`data/`는 올리지 않는 규칙).
드라이브의 **`tradeproof-golden-20261119.zip`**을 받아 **저장소 최상위**에서 풉니다.

```bash
cd tradeproof
unzip ~/Downloads/tradeproof-golden-20261119.zip
ls data/docs | wc -l            # 101 (거래 100 + manifest.json)
```

드라이브 대신 직접 만들 수도 있습니다 (`a_screening/run_all.sh`, data-pipeline 이미지, seed 42). 다만 tesseract 버전이 다르면 OCR 결과가 달라지므로, 처음에는 드라이브 ZIP을 권합니다.

## 3. 환경 올리기 (한 번)

```bash
scripts/demo_up.sh
```

걸리는 시간은 실측 약 3.5분입니다 (2026-11-19, 맥, 베이스 이미지가 이미 있을 때). 처음 받는 PC에서는 이미지 다운로드·빌드 때문에 10~15분 걸릴 수 있습니다. 하는 일:

1. k3d 클러스터 `tradeproof` 생성 (traefik 끔, 입구 포트 8081)
2. ingress-nginx, Argo Rollouts 설치
3. A·B·C·D 이미지 빌드 → 클러스터로 넣기
4. Postgres·SeaweedFS (`scripts/k3s_data_up.sh`)
5. 골든셋·과거 거래 적재 (`data/db/seed.sql`)
6. B 레지스트리, C 운영 화면 (`d_platform/k8s/demo/`), D 배포 API (`d_platform/k8s/platform/`) 배포
7. 포트포워드. 터미널을 닫아도 계속 돕니다.
8. 최초 번들: `model-v3`·`list-v14` 반입 → B-7 → A 설치 → B-7 승인

서명 키와 시연 ZIP이 없으면 `scripts/demo_sign.sh`가 먼저 자동으로 돕니다. 키는 각자 새로 만들기 때문에 드라이브에서 받을 필요가 없습니다.

끝나면 주소가 나옵니다.

| 화면 | 주소 |
|---|---|
| 운영 화면 (C) | http://localhost:18200/admin |
| 담당자 검토 화면 (A) | http://localhost:18000/review |
| A 심사 API (canary 입구) | `curl -H 'Host: tradeproof.local' localhost:8081/healthz` |
| Grafana (모니터링) | http://localhost:13000 (로그인 없음, "TradeProof 운영" 대시보드) |

## 4. 시연 (운영 화면)

먼저 **화면 맨 위 "담당자" 칸에 영문 이름**을 넣으세요. 한글이면 배포 요청이 실패합니다 (알려진 버그, B 담당이 고치는 중).

시연 ZIP은 `data/demo/packages/team-A` ~ `team-D`에 있습니다. 한 사람이 폴더 하나를 쓰고, 파일 이름 앞 번호 순서대로 **"1. 반입"**에 올립니다.

| 순서 | 파일 | 결과 | 걸리는 시간 |
|---|---|---|---|
| 1 | `1-S1-tampered.zip` | 즉시 거부 `PAYLOAD_HASH_MISMATCH` (서명 뒤 내용 변조) | 즉시 |
| 2 | `2-S1-corrupt.zip` | 즉시 거부 `ZIP_INVALID` (손상 파일) | 즉시 |
| 3 | `3-S5-degraded-model-v2N.zip` | 반입 통과 → 후보 번들 생성 → 자동 검증 → **게이트 차단** (정확도 23%, 미탐 3). canary 없음 | 검증 약 75초 |
| 4 | `4-S4-improved-model-v1N.zip` | 반입 → 자동 검증 → **게이트 통과** (정확도 94%, 미탐 0) | 검증 약 80초 |

- 반입하면 "2. 검증·승인" 칸에 번들 ID가 자동으로 들어갑니다. **조회**를 누르세요.
  - 검증 중에는 `candidate · 검증 전` / `running`으로만 보입니다 (진행 표시가 없는 화면 문제). 5초마다 자동으로 다시 조회합니다.
- 3번이 끝난 뒤 4번을 올리세요. 후보가 동시에 여럿이면 꼬일 수 있습니다.
- 검증은 골든셋 100건과 과거 거래 100건을 후보·운영 모델로 각각 OCR해서 전부 다시 처리합니다.

### 승인 → canary → 롤백

1. 4번 번들에서 사유를 적고 **"승인"** → 확인 창에서 **OK**
2. **바로 다른 터미널에서 심사 트래픽을 넣습니다** (`scripts/demo_traffic.sh`). canary는 시간이 아니라 **감시가 통과해야** 다음 비율로 올라갑니다. 트래픽이 없으면 10%에서 기다립니다(아래 "감시").
3. "3. 배포"에 배포 ID가 자동으로 들어갑니다. **조회**하면 weight가 10 → 50 → 100으로 올라가는 것이 보입니다(초당 1건이면 약 3~4분).
4. **"롤백"** → OK.
   - weight 10·50일 때 누르면 진행 중 롤백, 100이 된 뒤 누르면 반영 뒤 롤백입니다.
   - 약 8초 안에 모든 요청이 B-7로 돌아갑니다.
5. 확인: "3. 배포" 조회 → `rolled_back`, 그 번들 조회 → `cancelled`.

canary 비율을 직접 보려면 터미널에서:

```bash
for i in $(seq 40); do curl -s -H 'Host: tradeproof.local' localhost:8081/healthz | python3 -c 'import json,sys;print(json.load(sys.stdin)["bundle_id"])'; done | sort | uniq -c
```

### 감시: 다음 비율로 가는 조건 · 자동 롤백 (MON-02)

canary는 **시간으로 올라가지 않습니다.** 각 비율(10%, 50%)에서 Argo가 10초마다 후보 번들의 운영 지표를 Prometheus에서 읽어 판단합니다(`d_platform/helm/a-screening/templates/analysis.yaml`).

| 판단 | 조건 | 그러면 |
|---|---|---|
| 통과 | 후보가 5건 이상 처리했고, 아래 기준 안의 측정이 **3번 연속** | 다음 비율로 (10% → 50% → 100%) |
| 실패 | 처리 시간이 운영 번들의 **2배 초과**, 또는 처리 실패가 **10% 초과**인 측정이 2번 | Argo가 canary 중단 → D가 5초 안에 알아채 직전 번들로 복원, B에 `cancelled` (화면을 안 열어 둬도 됨) |
| 판단 불가 | 후보 처리가 5건 미만 (트래픽 없음) | 그 비율에서 기다림. 약 10분(측정 60번) 지나면 Argo가 멈추고 D 기록에 이유가 남음 → 사람이 promote 또는 롤백 |

1. `5-MON-slow-model-v3N.zip`을 반입합니다. 정확도는 기준 이상이라 **게이트를 통과**합니다(처리 시간만 느린 모델, `scripts/demo/models/slow.json` — 임시, A 확인 필요).
2. 승인한 뒤 바로 다른 터미널에서 심사 트래픽을 넣습니다.

   ```bash
   scripts/demo_traffic.sh          # 초당 1건, 5분. 샘플 서류로 실제 OCR 심사 (A 의 loadgen.py)
   ```
3. 10% 단계에서 후보가 5건쯤 처리하면 처리 시간이 기준을 넘어 canary가 멈추고 B-7로 돌아갑니다. 운영 화면에서 배포는 `rolled_back`(`analysis_failed`), 번들은 `cancelled`.
4. 개선 모델(`4-S4-…`)은 같은 트래픽에서 감시를 통과해 끝까지 반영됩니다.

> canary 때 넣은 트래픽은 실제 거래로 쌓여 **다음 검증의 Replay 대상이 늘어납니다**(100 → 300건 가까이, 검증 시간이 몇 배). 장면 사이에는 `scripts/demo_reset.sh`로 과거 거래를 시드 100건으로 되돌리세요.

Prometheus 화면: `kubectl --context k3d-tradeproof -n tradeproof port-forward svc/prometheus 9090` → http://localhost:9090 (예: `tradeproof_screening_processing_seconds_avg`).

### 담당자 채점으로 canary 진행·롤백 (검토 목록 맞음/틀림)

검토 화면(http://localhost:18000/review) 목록의 각 거래 줄에 **[맞음] [틀림]** 버튼이 있습니다. AI 판정(분류 배지)이 맞았는지 채점하는 버튼이고, 거래 승인·거절과는 따로 저장됩니다. 다시 누르면 취소됩니다.

- 채점은 그 거래를 처리한 번들로 셉니다 → A 지표 `tradeproof_review_feedback{bundle_id, verdict}`.
- 시연 값(`a-screening-values.yaml`)은 감시 항목 `reviewer-incorrect-rate`를 켭니다. **후보 번들 건에 채점이 3건 이상 모여야** 그 비율을 통과하고, 틀림이 50%를 넘는 측정이 2번이면 자동 롤백됩니다 (처리 시간·실패율 감시는 그대로).
- 순서: 승인 → `scripts/demo_traffic.sh` → 목록 위 "번들"에서 후보(B-8 등)를 고르고 채점
  - 맞음 3번: 11/25 맥 실측, 승인 뒤 약 80초에 50%, 95초에 100%
  - 틀림 2·맞음 1: 세 번째 채점 뒤 약 28초에 자동 롤백 (`analysis_failed`, 번들 `cancelled`)
- 화면 맨 위 오른쪽에 번들별 맞음·틀림 합계, Grafana 에 "AI 판정 채점"·"틀림 비율(기준 0.5)" 패널.
- 채점 없이 트래픽만으로 진행시키려면 `analysis.feedback.enabled: false`.

### Grafana 보기 (모니터링)

`scripts/demo_up.sh`가 Grafana를 같이 띄우고 **http://localhost:13000** 으로 연결합니다 (로그인 없음, 시연 전용 익명 관리자). 열면 바로 "TradeProof 운영" 대시보드가 나오고 5초마다 새로고침합니다.

- 위: canary 비율(%), 운영 번들, 사유별 롤백 수(manual·analysis_failed)
- 번들·분류별 판정 건수, 심사 대기열(상태별), 번들별 처리 시간·OCR 신뢰도 (최근 100건 평균)
- 감시 값: 처리 시간 비율(후보 ÷ 운영, 기준선 2.0 = A 차트 `analysis.maxProcessingTimeRatio`), 번들별 담당자 검토 비율
- 시연 때 운영 화면(/admin) 옆에 띄워 두면, 승인 → canary 비율 상승, 느린 모델 → 처리 시간 비율이 2.0을 넘고 롤백 수 +1 이 그래프로 보입니다. 시간 범위는 "Last 15 minutes" 추천.
- 13000 이 이미 쓰이고 있으면 `GRAFANA_PORT=13001 scripts/demo_up.sh`처럼 바꿉니다 (Prometheus를 9090으로 열 때도 다른 프로그램과 겹치면 `... port-forward svc/prometheus 19090:9090`).
- 처음 한 번 `grafana/grafana:11.2.2` 이미지(약 600MB)를 클러스터가 받습니다.

## 5. 다시 하기 / 끄기

```bash
scripts/demo_reset.sh        # 처음 상태(B-7 만 운영 중)로. 같은 ZIP 을 다시 쓸 수 있음 (실측 13초, 끝나고 몇 초 뒤 화면이 열림)
scripts/demo_down.sh         # 끄기 (클러스터 멈춤, 데이터 유지). 다시 켤 때 scripts/demo_up.sh
scripts/demo_down.sh --delete  # 클러스터까지 삭제
START=30 scripts/demo_sign.sh  # 리셋 없이 또 해 보고 싶을 때: 새 번호로 시연 ZIP 다시 만들기
```

한 번 올린 ZIP은 그 환경에서 다시 쓸 수 없습니다. B는 같은 파일이면 예전 반입 기록을 돌려주고, 같은 모델 ID는 한 번만 등록하기 때문입니다 (반입 무결성). 리셋하거나 새 번호로 서명하세요.

## 6. 자주 나는 문제

| 증상 | 원인 → 해결 |
|---|---|
| 승인하면 `SERVICE_UNAVAILABLE: D deployment acceptance could not be confirmed` | 담당자 칸이 한글이라 생긴 일입니다. 이후 그 번들이 `Deployment outcome is unknown`으로 막히면 `scripts/demo_reset.sh` 하세요 |
| 반입했는데 예전 번들·결과가 나옴 | 이미 쓴 ZIP입니다 → 리셋 또는 `START=30 scripts/demo_sign.sh` |
| 취소된 번들에서 승인을 누르면 "승인됨"이라고 나옴 | 화면 표시 문제, 실제로는 아무 일도 안 일어남. 새 ZIP으로 하세요 |
| 화면 주소가 안 열림 | 포트포워드가 꺼진 것입니다 (PC 재시작 등) → `scripts/demo_up.sh` 다시 실행 (있는 것은 건너뜀) |
| PC·WSL·Docker 재시작 뒤 A 파드가 `Unknown`·`ContainerCreating`에 멈춤 (`hostPath type check failed: /data/tradeproof/docs`) | 노드에 붙은 `data/`가 빈 채로 다시 붙은 것입니다 → `scripts/demo_up.sh` 다시 실행 (노드에서 `data/`가 안 보이면 클러스터를 껐다 켭니다) |
| 포트가 이미 사용 중 | `INGRESS_PORT=9081 C_PORT=19200 scripts/demo_up.sh`처럼 바꿔서 실행 (scripts/demo/lib.sh 참고) |
| 검증이 `failed`로 끝남 | `kubectl --context k3d-tradeproof -n tradeproof logs job/<이름>` (KUBECONFIG=data/demo/kit/kubeconfig). 재실행 API가 없어 리셋으로 다시 합니다 |
| 검토 화면(A)에서 "샘플 ID로 접수"가 `NOT_FOUND` | 샘플 서류(`data/docs`)가 없거나 A 가 예전 값으로 설치됨 → `scripts/demo_up.sh` 다시 실행 (A 차트에 `a-screening-values.yaml` 로 붙임) |
| 로그 | 포트포워드: `data/demo/kit/logs/`, D 배포 API: `kubectl --context k3d-tradeproof -n tradeproof logs deploy/d-platform` |

## 7. 윈도우(WSL)에서 확인한 결과 (2026-11-20, D)

WSL2 우분투, 메모리 8GB·CPU 4개(`.wslconfig`), Docker Desktop, k3d v5.8.3. 골든셋은 드라이브 대신 data-pipeline 이미지로 다시 만들었다(드라이브 README 4절).

| 확인 | 결과 |
|---|---|
| `scripts/demo_up.sh` (클러스터 없음 → B-7 운영) | 516초, 오류 없음 (이미지 빌드 포함) |
| S1 변조·손상 | 즉시 거부 `PAYLOAD_HASH_MISMATCH`, `ZIP_INVALID` (3세트 모두) |
| S5 저하 모델 | 게이트 차단, `validation_failed` (검증 161~270초) |
| S4 개선 모델 | 게이트 통과, `validated` (검증 192~327초) |
| canary (ingress 40건) | 10%: 4~5건, 50%: 11~24건, 100%: 40건 |
| 50%에서 롤백 | 응답 3.8초, 9.2초 뒤 40건 모두 B-7, B: B-7 `active`·후보 `cancelled` |
| 100% 뒤 롤백 | 응답 2.0~4.2초, 8.9~15.4초 뒤 40건 모두 B-7 |
| 샘플 ID 접수 (`TX-0001`) | `completed`, B-7, `HIGH_RISK_HOLD` |
| `scripts/demo_reset.sh` | 144초, B-7만 운영 |

- 검증이 맥(약 75초)보다 2~4배 느리다. CPU 4개라 그렇다. 리허설 때는 미리 걸어 두자.
- 롤백·재시작으로 파드가 바뀌면 포트포워드가 최대 10초쯤 끊긴다. `scripts/demo/pf_loop.sh`가 5초마다 확인해 응답이 없으면 다시 붙인다(11/21 확인: D·A 재시작 뒤 9초 안에 4개 모두 회복).
- 이 확인은 main에 아직 없는 A #24와 C `Dockerfile` 한 줄을 합친 이미지로 했다. 두 변경이 main에 들어가야 main만으로 같은 결과가 나온다.

### D 배포 API를 클러스터 안에서 (2026-11-21)

| 확인 | 결과 |
|---|---|
| D 위치 | `d-platform` 파드(클러스터 안), 호스트 D 없음. B·C 모두 `http://d-platform:8300`으로 부름 |
| 운영 화면(C) 중계로 승인 → canary | 10%: 5/40, 50%: 14/40, 100%: 40/40 (B-12) |
| **canary 10%에서 D 파드 재시작** | 5초 만에 새 파드, 배포는 끊기지 않고 50% → `succeeded`. B에 `active` 전달 |
| 100% 뒤 롤백 (C 중계) | 응답 1.6초, 10.1초 뒤 40건 모두 B-7, B: B-7 `active`·B-12 `cancelled` |
| D 파드 안 번들 값 파일 | 반영 뒤 `stable: B-12`, 롤백 뒤 직전 번들로 다시 씀 |
| `scripts/demo_reset.sh` 뒤 | D 조회 정상(최초 번들 B-7 `succeeded`), 샘플 접수 정상 |

### 감시 → 자동 롤백 (2026-11-21)

| 확인 | 결과 |
|---|---|
| 느린 모델(2.4배 확대) 검증 | 게이트 통과: 정확도 0.89 → 0.93, 미탐 0 (검증 354초) |
| 느린 모델 canary + 트래픽(초당 1건) | 50% 단계에서 처리 시간 3.9배 → `processing-time-ratio` 실패 2회 → Argo 중단 → **D가 스스로 롤백**(조회 없이), B: 후보 `cancelled`. 승인부터 롤백까지 약 70초 |
| 개선 모델 canary + 트래픽 | 처리 시간 0.9배, 감시 통과 → `succeeded`, B: `active` |
| Host 헤더 입구 비율 | 10%: 1~2/20, 50%: 9/20, 100%: 20/20 |
| `demo_up.sh` 재실행 | 지금 운영 번들(B의 active)로 A를 다시 설치 — 운영 번들이 바뀌지 않음 |

- 처음 기준(1.5배, 최소 3건)에서는 같은 설정의 모델끼리도 6건일 때 1.49배까지 흔들렸다 → 2배·최소 5건으로 올렸다(느린 모델은 3.9배라 여유가 있다). 바꾼 기준으로 느린 모델을 다시 돌려 보지는 않았다.
- 분석 쿼리는 `argo-rollouts` 네임스페이스의 컨트롤러가 보낸다 → Prometheus 주소는 네임스페이스까지 적는다(`prometheus.tradeproof.svc`). 짧은 이름이면 연속 오류로 중단된다(처음 확인 때 겪음).
- 호스트 없는 ingress 규칙만 두면 nginx가 `/healthz`를 가로챈다 → 호스트 규칙을 남기고 호스트 없는 규칙을 더한다(`ingress.matchAnyHost`).

### 시간 대신 감시 통과로 진행 (2026-11-22~23)

| 확인 | 결과 |
|---|---|
| Argo 단계 분석 동작 (분석 실행만 따로) | 기준 안 측정이 연속 3번이면 바로 `Successful`(40초), 트래픽 없으면 측정마다 판단 불가 → 정한 횟수에서 `Inconclusive` |
| 개선 모델, 승인 뒤 60초 동안 트래픽 없음 | 10%에서 **그대로 머묾** → 트래픽 뒤 감시 통과로 50% → 100%, B `active` |
| 느린 모델(3.2배 확대) | 게이트 통과(정확도 0.93) → 10%에서 후보 5건 뒤 처리 시간 **5.9배** → 감시 실패 → D가 스스로 롤백, B `cancelled` |

- 단계 분석은 측정 횟수(`count`)가 꼭 있어야 한다. 없으면 Argo가 Rollout을 `InvalidSpec`으로 거부한다(처음 시도 때 겪음) → `count` = 최대 대기 측정 수(60), 연속 3번 통과하면 일찍 끝난다.
- 느린 모델을 2.4배 확대로 했을 때는 어떤 때 3.9배, 어떤 때 1.83배라 기준(2배)을 넘지 못한 적이 있다 → 3.2배로 올렸다(5.9배). 대신 검증이 길어진다(이 PC 약 14분).

## 7. 이 폴더의 파일 (D)

| 파일 | 내용 |
|---|---|
| `b-registry.yaml` | B 레지스트리 Deployment·Service, 검증 Job을 만드는 권한(ServiceAccount·Role: `batch/jobs` get·list·create). 공개키·검증 Job 양식은 ConfigMap `b-registry-files`(demo_up.sh가 만듦) |
| `c-replay.yaml` | C 검증 조회 API + 운영 화면(`/admin`). C가 B·D로 중계 |
| `validation-job.json` | B가 후보마다 만드는 C 검증 Job 양식. `c_replay/job/job-template.json`에 `REGISTRY_MODE=http`, 샘플 서류·제재 리스트 볼륨을 더한 것 (C 양식이 바뀌면 같이 맞춘다) |
| `../platform/d-platform.yaml` | D 배포 API: Deployment·Service, 배포 권한(Role: helm 릴리스 Secret, Service·Ingress·워커 Deployment·Rollout). 번들 값 파일은 파드 안 임시 폴더(emptyDir) |
| `a-screening-values.yaml` | A 차트 시연 값: 샘플 서류(`data/docs`) 볼륨. demo_up/reset 의 helm과 D 배포 API(`HELM_VALUES_FILES`)가 같이 쓴다 |

D 배포 API는 `d_platform/k8s/platform/d-platform.yaml`로 클러스터 안에서 돈다(11/20부터). B·C는 `http://d-platform:8300`으로 부른다.

감시·자동 롤백: 위 "감시 → 자동 롤백". Prometheus는 `d_platform/k8s/monitoring/prometheus.yaml`.
