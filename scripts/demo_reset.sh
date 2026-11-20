#!/usr/bin/env bash
# 시연 환경을 처음 상태(B-7 만 운영 중, 반입·검증·배포 기록 없음)로 되돌린다. 리허설마다 같은 ZIP 을 다시 쓸 수 있다.
#
#   scripts/demo_reset.sh
#
# 지우는 것: B 반입·번들·모델·리스트 기록, C 검증 기록, D 배포 기록, A 접수 작업, 검증 Job
# 다시 넣는 것: 과거 거래·판정·골든셋 (data/db/seed.sql), 최초 번들 B-7 반입·승인
# 그대로 두는 것: 클러스터, 이미지, 데이터 계층, 원본 저장소의 ZIP (내용 주소라 다시 반입하면 같은 키를 씀)
set -euo pipefail
source "$(dirname "$0")/demo/lib.sh"
T0=$(date +%s)
need_tools kubectl helm kubectl-argo-rollouts python3 curl
k get nodes >/dev/null 2>&1 || die "클러스터 $CLUSTER 에 연결 안 됨 → scripts/demo_up.sh"

say "1/5 진행 중인 검증 Job·배포 정리"
kn delete jobs -l app=c-validation --ignore-not-found >/dev/null
stop_bg

say "2/5 DB 기록 지우기 + 골든셋 다시 적재"
psql_in <<'SQL'
TRUNCATE imports, bundles, ai_models, sanctions_lists RESTART IDENTITY CASCADE;
TRUNCATE validation_diffs, validations RESTART IDENTITY CASCADE;
TRUNCATE deployments RESTART IDENTITY CASCADE;
TRUNCATE screening_jobs RESTART IDENTITY CASCADE;
SQL
psql_in < data/db/seed.sql
echo "  golden_labels $(sql 'select count(*) from golden_labels'), bundles $(sql 'select count(*) from bundles')"

say "3/5 A 를 B-7 로 (번들 값 파일 초기화 → helm, canary 가 남아 있으면 바로 끝까지)"
write_bundle_values
helm upgrade --install a-screening d_platform/helm/a-screening --kube-context "$CTX" -n $NS -f "$A_VALUES" -f "$STATE/values-bundle.yaml" >/dev/null
sleep 3
if [ "$(kn get rollout a-screening -o jsonpath='{.status.phase}')" != Healthy ]; then
  kubectl-argo-rollouts --context "$CTX" -n $NS promote a-screening --full >/dev/null 2>&1 || true
fi
# 번들 구성 캐시를 비우려고 다시 띄운다 (리셋 뒤에는 같은 번들 ID 가 다른 모델을 가리킬 수 있음).
# A 심사 API 는 B-7 이 B 에 다시 생긴 뒤(5단계)에 재시작해야 준비 상태가 된다.
kn rollout restart deploy/a-ocr-worker deploy/b-registry deploy/c-replay >/dev/null
kn rollout status deploy/b-registry --timeout=300s >/dev/null

say "4/5 포트포워드 + D 다시 시작"
start_bg

say "5/5 최초 번들 B-7 반입·승인"
initial_bundle
kubectl-argo-rollouts --context "$CTX" -n $NS restart a-screening >/dev/null
sleep 5
for i in $(seq 1 90); do
  [ "$(kn get rollout a-screening -o jsonpath='{.status.phase}')" = Healthy ] && [ "$(kn get rollout a-screening -o jsonpath='{.status.availableReplicas}')" = 2 ] && break; sleep 2
done
echo "  승인: $(approve_initial)"

say "완료 ($(( $(date +%s) - T0 ))초)"
status
