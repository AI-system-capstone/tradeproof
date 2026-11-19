# 시연 따라하기 (로컬 k3s)

이 문서대로 하면 각자 PC에 시연 환경을 올리고, 운영 화면에서 **S1(변조·손상 차단) → S5(성능 저하 모델 게이트 차단) → S4(개선 모델 canary·롤백)**를 직접 해 볼 수 있습니다.
k3s를 Docker 안에서 돌리는 k3d를 씁니다. D의 시연 환경(k3s + ingress-nginx + Argo Rollouts)과 같은 구성입니다.

지금까지 고친 것과 아직 남은 것: [demo-status.md](demo-status.md)

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
3. A·B·C 이미지 빌드 → 클러스터로 넣기
4. Postgres·SeaweedFS (`scripts/k3s_data_up.sh`)
5. 골든셋·과거 거래 적재 (`data/db/seed.sql`)
6. B 레지스트리, C 운영 화면 배포 (`d_platform/k8s/demo/`)
7. 포트포워드 + D 배포 API 실행. 터미널을 닫아도 계속 돕니다.
8. 최초 번들: `model-v3`·`list-v14` 반입 → B-7 → A 설치 → B-7 승인

서명 키와 시연 ZIP이 없으면 `scripts/demo_sign.sh`가 먼저 자동으로 돕니다. 키는 각자 새로 만들기 때문에 드라이브에서 받을 필요가 없습니다.

끝나면 주소가 나옵니다.

| 화면 | 주소 |
|---|---|
| 운영 화면 (C) | http://localhost:18200/admin |
| 담당자 검토 화면 (A) | http://localhost:18000/review |
| A 심사 API (canary 입구) | `curl -H 'Host: tradeproof.local' localhost:8081/healthz` |

## 4. 시연 (운영 화면)

먼저 **화면 맨 위 "담당자" 칸에 영문 이름**을 넣으세요. 한글이면 배포 요청이 실패합니다 (알려진 버그, demo-status.md).

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
2. "3. 배포"에 배포 ID가 자동으로 들어갑니다. **조회**를 누를 때마다 weight가 10 → 50 → 100으로 올라갑니다 (단계당 30초, 약 65초면 끝).
3. **"롤백"** → OK.
   - weight 10·50일 때 누르면 진행 중 롤백, 100이 된 뒤 누르면 반영 뒤 롤백입니다.
   - 약 8초 안에 모든 요청이 B-7로 돌아갑니다.
4. 확인: "3. 배포" 조회 → `rolled_back`, 그 번들 조회 → `cancelled`.

canary 비율을 직접 보려면 터미널에서:

```bash
for i in $(seq 40); do curl -s -H 'Host: tradeproof.local' localhost:8081/healthz | python3 -c 'import json,sys;print(json.load(sys.stdin)["bundle_id"])'; done | sort | uniq -c
```

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
| 화면 주소가 안 열림 | 포트포워드·D가 꺼진 것입니다 (PC 재시작 등) → `scripts/demo_up.sh` 다시 실행 (있는 것은 건너뜀) |
| 포트가 이미 사용 중 | `INGRESS_PORT=9081 C_PORT=19200 scripts/demo_up.sh`처럼 바꿔서 실행 (scripts/demo/lib.sh 참고) |
| 검증이 `failed`로 끝남 | `kubectl --context k3d-tradeproof -n tradeproof logs job/<이름>` (KUBECONFIG=data/demo/kit/kubeconfig). 재실행 API가 없어 리셋으로 다시 합니다 |
| 검토 화면(A)에서 "샘플 ID로 접수"가 `NOT_FOUND` | 클러스터의 A 에는 샘플 서류가 마운트돼 있지 않습니다 (demo-todo D-4). 서류 PNG 를 업로드해서 접수하세요 |
| 로그 | `data/demo/kit/logs/` (D 배포 API, 포트포워드) |
