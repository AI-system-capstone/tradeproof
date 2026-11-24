# 시연 키트 공통 설정·함수 (scripts/demo_*.sh 가 source 한다). 직접 실행하지 않는다.
# 바꾸고 싶은 값은 실행할 때 환경변수로 준다. 예: INGRESS_PORT=9081 scripts/demo_up.sh

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$ROOT"

CLUSTER=${CLUSTER:-tradeproof}
CTX="k3d-$CLUSTER"
NS=tradeproof
STATE=${STATE:-$ROOT/data/demo/kit}          # kubeconfig·로그·pid·D 가상환경·번들 값 파일 (git 에 안 올라감)
KEYS=${KEYS:-$ROOT/data/demo/keys}            # 데모 서명 키 (vendor.pem 은 절대 커밋 금지)
PACKAGES=${PACKAGES:-$ROOT/data/demo/packages}
A_VALUES=$ROOT/d_platform/k8s/demo/a-screening-values.yaml   # A 차트 시연 값 (샘플 서류 볼륨)

INGRESS_PORT=${INGRESS_PORT:-8081}   # A 심사 API (canary 트래픽이 나뉘는 입구), Host: tradeproof.local
A_PORT=${A_PORT:-18000}              # A 검토 화면 /review (stable 쪽)
B_PORT=${B_PORT:-18100}              # B 레지스트리 API
C_PORT=${C_PORT:-18200}              # 운영 화면 /admin
DB_PORT=${DB_PORT:-15432}            # 클러스터 Postgres (D 가 씀)
D_PORT=${D_PORT:-8300}               # D 배포 API (클러스터 안 d-platform:8300 을 포트포워드)
GRAFANA_PORT=${GRAFANA_PORT:-13000}  # Grafana 대시보드 (3000 이 다른 것과 겹치는 PC 가 많아 13000)

INGRESS_NGINX_VERSION=controller-v1.12.1
ARGO_ROLLOUTS_VERSION=v1.10.2   # d_platform/README 의 k3s 시연 환경과 같은 버전
CANARY_PAUSE_SECONDS=${CANARY_PAUSE_SECONDS:-30}
ACTOR=${ACTOR:-demo}                 # 영문만 (한글이면 B→D 배포 요청이 실패하는 버그가 있음)

mkdir -p "$STATE/logs"
export KUBECONFIG="$STATE/kubeconfig"   # 이 키트는 k3d 클러스터 전용 kubeconfig 만 쓴다 (다른 클러스터 컨텍스트를 건드리지 않음)

k() { kubectl --context "$CTX" "$@"; }
kn() { kubectl --context "$CTX" -n "$NS" "$@"; }
say() { printf '\n\033[1m[%s] %s\033[0m\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf '\033[31m오류: %s\033[0m\n' "$*" >&2; exit 1; }

need_tools() {
  local miss=()
  for t in "$@"; do command -v "$t" >/dev/null 2>&1 || miss+=("$t"); done
  [ ${#miss[@]} -eq 0 ] || die "필요한 프로그램이 없음: ${miss[*]} (d_platform/k8s/demo/README.md '준비물' 참고)"
}

psql_in() { kn exec -i postgres-0 -- env PGOPTIONS='-c client_min_messages=warning' psql -U tradeproof -d tradeproof -v ON_ERROR_STOP=1 -q "$@"; }
sql() { kn exec postgres-0 -- psql -U tradeproof -d tradeproof -tAc "$1"; }

# 터미널·세션을 닫아도 계속 도는 프로세스로 띄운다 (새 세션). 로그는 $STATE/logs/<이름>.log
detach() {
  local name=$1; shift
  python3 - "$STATE/logs/$name.log" "$@" <<'PY'
import subprocess, sys
log = open(sys.argv[1], "ab")
p = subprocess.Popen(sys.argv[2:], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
print(p.pid)
PY
}

wait_http() {  # wait_http URL [초]
  local i
  for i in $(seq 1 "${2:-60}"); do curl -s -o /dev/null --max-time 2 "$1" && return 0; sleep 1; done
  return 1
}

stop_bg() {
  pkill -f "scripts/demo/pf_loop.sh" 2>/dev/null || true   # 감시 루프부터 (먼저 안 끄면 포트포워드를 다시 띄움)
  pkill -f "tradeproof-demo-pf" 2>/dev/null || true          # 예전 키트의 루프
  pkill -f "port-forward --context $CTX" 2>/dev/null || true
  pkill -f "uvicorn d_platform.api.main:app --host 0.0.0.0 --port $D_PORT" 2>/dev/null || true
}

start_bg() {  # 포트포워드만 (D 배포 API 는 클러스터 안에서 돈다: deploy_platform)
  stop_bg
  # 파드가 바뀌어 끊기거나 응답이 멈추면 다시 붙는다 (scripts/demo/pf_loop.sh)
  local spec svc port tport path
  for spec in "b-registry:$B_PORT:8100:/healthz" "c-replay:$C_PORT:8200:/healthz" "postgres:$DB_PORT:5432:-"               "a-screening-stable:$A_PORT:8000:/livez" "d-platform:$D_PORT:8300:/livez" \
              "grafana:$GRAFANA_PORT:3000:/api/health"; do
    IFS=: read -r svc port tport path <<<"$spec"
    detach "pf-$svc" bash "$ROOT/scripts/demo/pf_loop.sh" "$CTX" "$NS" "$svc" "$port" "$tport" "$path" >/dev/null
  done
  wait_http "http://localhost:$B_PORT/healthz" 60 || die "B 포트포워드가 안 붙음 ($STATE/logs/pf-b-registry.log)"
  wait_http "http://localhost:$D_PORT/healthz" 60 || die "D 포트포워드가 안 붙음 ($STATE/logs/pf-d-platform.log)"
}

deploy_platform() {  # Prometheus·Grafana + D 배포 API 를 클러스터 안에 (d_platform/k8s/monitoring/, d_platform/k8s/platform/)
  kn create configmap d-platform-helm-values --from-file=a-screening-values.yaml="$A_VALUES" \
    --dry-run=client -o yaml | k apply -f - >/dev/null
  k apply -f d_platform/k8s/monitoring/prometheus.yaml -f d_platform/k8s/platform/d-platform.yaml >/dev/null
  kn rollout status deploy/prometheus --timeout=300s
  kn create configmap grafana-dashboards --from-file=tradeproof.json=d_platform/k8s/monitoring/grafana-dashboard.json \
    --dry-run=client -o yaml | k apply -f - >/dev/null
  k apply -f d_platform/k8s/monitoring/grafana.yaml >/dev/null
  kn rollout restart deploy/grafana >/dev/null   # 대시보드 JSON 이 바뀌었을 수 있음
  kn rollout status deploy/grafana --timeout=300s
  kn rollout restart deploy/d-platform >/dev/null   # 이미지·값 파일이 바뀌었을 수 있음
  kn rollout status deploy/d-platform --timeout=300s
}

write_bundle_values() {  # write_bundle_values [운영 번들] → demo_up·reset 이 A 를 다시 설치할 때 쓰는 번들 값 (기본 B-7)
  # D 배포 API 는 클러스터 안에서 자기 파일(파드 안)을 따로 쓴다. 이 파일은 키트가 helm 으로 A 를 설치할 때만 쓴다
  printf '# 시연 키트용 (demo_up·demo_reset 이 A 차트에 넣는 번들 값)\nbundle:\n  stable: "%s"\n  candidate: ""\n' "${1:-B-7}" > "$STATE/values-bundle.yaml"
}

b_post_import() {  # b_post_import ZIP → import 응답 JSON
  curl -s -X POST -F "package=@$1" "http://localhost:$B_PORT/imports"
}

initial_bundle() {  # 최초 모델·리스트 반입 → B-7 → 승인(검증 없이 바로 반영)
  local z
  for z in "$PACKAGES/initial/model-v3.zip" "$PACKAGES/initial/list-v14.zip"; do
    [ -f "$z" ] || die "$z 없음 → 먼저 scripts/demo_sign.sh"
    echo "  반입 $(basename "$z"): $(b_post_import "$z")"
  done
}

approve_initial() {
  curl -s -X POST "http://localhost:$B_PORT/approvals" -H 'Content-Type: application/json' -H "X-Actor: $ACTOR" \
    -d "{\"bundle_id\":\"B-7\",\"decision\":\"approve\",\"actor\":\"$ACTOR\",\"reason\":\"initial bundle\"}"
  echo
}

status() {
  echo
  printf '  %-28s %s\n' "운영 화면 (C)" "http://localhost:$C_PORT/admin"
  printf '  %-28s %s\n' "담당자 검토 화면 (A)" "http://localhost:$A_PORT/review"
  printf '  %-28s %s\n' "A 심사 API (canary 입구)" "curl -H 'Host: tradeproof.local' localhost:$INGRESS_PORT/healthz"
  printf '  %-28s %s\n' "B 레지스트리" "http://localhost:$B_PORT/bundles/active"
  printf '  %-28s %s\n' "D 배포 API" "http://localhost:$D_PORT/deployments"
  printf '  %-28s %s\n' "Grafana (모니터링)" "http://localhost:$GRAFANA_PORT"
  echo
  printf '  운영 번들: %s\n' "$(curl -s --max-time 3 "http://localhost:$B_PORT/bundles/active" | python3 -c 'import json,sys
d=json.load(sys.stdin); print("active=%s canary=%s" % tuple((v or {}).get("bundle_id") for v in (d.get("active"), d.get("canary"))))' 2>/dev/null || echo '확인 안 됨')"
}
