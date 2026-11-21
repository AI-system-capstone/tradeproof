#!/usr/bin/env bash
# 시연 환경을 처음부터 한 번에 올린다 (맥 / 리눅스 / WSL). 여러 번 실행해도 된다 (있는 것은 건너뜀).
#
#   scripts/demo_up.sh
#
# 하는 일 (d_platform/k8s/demo/README.md):
#   1. k3d 클러스터(k3s) 생성 — traefik 끔, 입구 포트 $INGRESS_PORT, data/ 를 노드의 /data/tradeproof 로 마운트
#   2. ingress-nginx + Argo Rollouts 설치
#   3. 이미지 빌드(A·B·C·D) → 클러스터로 import
#   4. 데이터 계층: scripts/k3s_data_up.sh (Postgres + SeaweedFS + 버킷 + Secret)
#   5. 골든셋·과거 거래 적재 (data/db/seed.sql)
#   6. B 레지스트리·C 운영 화면 (d_platform/k8s/demo/), D 배포 API (d_platform/k8s/platform/) 배포
#   7. 포트포워드 — 터미널을 닫아도 계속 돈다
#   8. 최초 번들: model-v3·list-v14 반입 → B-7 → A 차트 설치 → B-7 승인
# kubectl 은 이 클러스터 전용 kubeconfig(data/demo/kit/kubeconfig)와 --context k3d-$CLUSTER 로만 부른다.
set -euo pipefail
source "$(dirname "$0")/demo/lib.sh"
T0=$(date +%s)
need_tools docker k3d kubectl helm kubectl-argo-rollouts python3 curl

[ -f data/db/seed.sql ] && [ -d data/docs/TX-0001 ] && [ -f data/sanctions/list-v14.json ] \
  || die "골든셋 데이터가 없음 (data/docs, data/sanctions, data/db/seed.sql) → d_platform/k8s/demo/README.md 2단계 (드라이브 ZIP 을 저장소 최상위에 풀기)"
[ -f "$PACKAGES/initial/model-v3.zip" ] && [ -f "$KEYS/vendor.pub" ] || { say "서명 키·시연 ZIP 이 없어 먼저 만든다"; scripts/demo_sign.sh; }
[ -f .env ] || cp .env.example .env

say "1/8 k3d 클러스터 $CLUSTER"
if ! k3d cluster list -o json | python3 -c "import json,sys; sys.exit(0 if any(c['name']=='$CLUSTER' for c in json.load(sys.stdin)) else 1)"; then
  k3d cluster create "$CLUSTER" --servers 1 --agents 0 \
    --k3s-arg "--disable=traefik@server:0" -p "$INGRESS_PORT:80@loadbalancer" \
    -v "$ROOT/data:/data/tradeproof@server:0" \
    --kubeconfig-update-default=false --kubeconfig-switch-context=false --wait
else
  k3d cluster start "$CLUSTER" >/dev/null 2>&1 || true
  # PC·WSL·Docker 를 다시 켜면 노드에 붙은 data/ 가 빈 폴더로 남을 때가 있다 (Docker 가 먼저 컨테이너를 띄움).
  # 그러면 A 파드가 샘플 서류 볼륨을 못 붙여 Unknown·ContainerCreating 에 멈춘다 → 클러스터를 껐다 켜서 다시 붙인다.
  if ! docker exec "k3d-$CLUSTER-server-0" test -d /data/tradeproof/docs 2>/dev/null; then
    echo "  노드에서 data/ 가 안 보여 클러스터를 다시 시작한다"
    k3d cluster stop "$CLUSTER" >/dev/null && k3d cluster start "$CLUSTER" >/dev/null
  fi
fi
k3d kubeconfig get "$CLUSTER" > "$KUBECONFIG"
k get nodes

say "2/8 ingress-nginx $INGRESS_NGINX_VERSION, Argo Rollouts $ARGO_ROLLOUTS_VERSION"
k apply -f "https://raw.githubusercontent.com/kubernetes/ingress-nginx/$INGRESS_NGINX_VERSION/deploy/static/provider/cloud/deploy.yaml" >/dev/null
k create namespace argo-rollouts --dry-run=client -o yaml | k apply -f - >/dev/null
k apply --server-side -n argo-rollouts -f "https://github.com/argoproj/argo-rollouts/releases/download/$ARGO_ROLLOUTS_VERSION/install.yaml" >/dev/null
k -n ingress-nginx rollout status deploy/ingress-nginx-controller --timeout=300s
k -n argo-rollouts rollout status deploy/argo-rollouts --timeout=300s

say "3/8 이미지 빌드 → k3d import"
for spec in "a_screening/Dockerfile screening-api tradeproof/a-screening:dev" \
            "a_screening/Dockerfile ocr-worker tradeproof/a-ocr-worker:dev" \
            "c_replay/Dockerfile api tradeproof/c-replay:dev" \
            "c_replay/Dockerfile job tradeproof/c-replay-job:dev" \
            "b_registry/Dockerfile - tradeproof/b-registry:dev" \
            "d_platform/Dockerfile - tradeproof/d-platform:dev"; do
  read -r f target tag <<<"$spec"
  echo "  build $tag"
  if [ "$target" = - ]; then docker build -q -f "$f" -t "$tag" . >/dev/null; else docker build -q -f "$f" --target "$target" -t "$tag" . >/dev/null; fi
done
k3d image import -c "$CLUSTER" tradeproof/a-screening:dev tradeproof/a-ocr-worker:dev tradeproof/c-replay:dev tradeproof/c-replay-job:dev tradeproof/b-registry:dev tradeproof/d-platform:dev

say "4/8 데이터 계층 (scripts/k3s_data_up.sh)"
ENV_FILE=.env scripts/k3s_data_up.sh >/dev/null
kn get pods

say "5/8 골든셋·과거 거래 적재"
if [ "$(sql "select to_regclass('golden_labels') is not null and (select count(*) from golden_labels) = 100" 2>/dev/null || true)" = t ]; then
  echo "  이미 있음 (golden_labels 100)"
else
  psql_in < common/schema/10_a_screening.sql
  psql_in < a_screening/db/golden_labels_tmp.sql
  psql_in < data/db/seed.sql
fi
echo "  golden_labels $(sql 'select count(*) from golden_labels'), transactions $(sql 'select count(*) from transactions')"

say "6/8 B 레지스트리·C 운영 화면·D 배포 API"
kn create configmap b-registry-files --from-file=publisher.pub="$KEYS/vendor.pub" \
  --from-file=validation-job.json=d_platform/k8s/demo/validation-job.json --dry-run=client -o yaml | k apply -f - >/dev/null
k apply -f d_platform/k8s/demo/b-registry.yaml -f d_platform/k8s/demo/c-replay.yaml >/dev/null
kn rollout restart deploy/b-registry deploy/c-replay >/dev/null   # 키·이미지가 바뀌었을 수 있음
kn rollout status deploy/b-registry --timeout=300s
kn rollout status deploy/c-replay --timeout=300s
deploy_platform

say "7/8 포트포워드"
[ -f "$STATE/values-bundle.yaml" ] || write_bundle_values
start_bg

say "8/8 최초 번들 B-7"
if [ "$(curl -s "http://localhost:$B_PORT/bundles" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)))')" = 0 ]; then
  initial_bundle
fi
helm upgrade --install a-screening d_platform/helm/a-screening --kube-context "$CTX" -n $NS -f "$A_VALUES" -f "$STATE/values-bundle.yaml" >/dev/null
kn rollout restart deploy/a-ocr-worker >/dev/null 2>&1 || true
echo "  A Rollout 준비 기다리는 중..."
for i in $(seq 1 90); do [ "$(kn get rollout a-screening -o jsonpath='{.status.phase}' 2>/dev/null)" = Healthy ] && break; sleep 2; done
[ "$(kn get rollout a-screening -o jsonpath='{.status.phase}')" = Healthy ] || die "A Rollout 이 Healthy 가 안 됨: kubectl --context $CTX -n $NS describe rollout a-screening"
if [ "$(curl -s "http://localhost:$B_PORT/bundles/active" | python3 -c 'import json,sys; print((json.load(sys.stdin).get("active") or {}).get("bundle_id"))')" != B-7 ]; then
  echo "  승인: $(approve_initial)"
fi

say "완료 ($(( $(date +%s) - T0 ))초)"
status
echo
echo "  시연 ZIP: $PACKAGES/team-A ~ team-D (번호 순서대로, d_platform/k8s/demo/README.md 4단계)"
