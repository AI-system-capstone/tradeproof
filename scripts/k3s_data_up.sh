#!/usr/bin/env bash
# 시연 환경(k3s)의 데이터 계층을 올린다: Postgres + SeaweedFS(minio:9000) + 버킷 (담당: D)
# 여러 번 실행해도 된다. 데이터(PVC)는 지우지 않는다.
#
#   scripts/k3s_data_up.sh            # 네임스페이스 tradeproof
#   ENV_FILE=.env scripts/k3s_data_up.sh
#
# 접속 정보는 ENV_FILE(기본 .env.example — 개발용 값)에서 읽어 Secret tradeproof-env 로 만든다.
# 모듈 파드는 envFrom 으로 이 Secret 을 받으면 compose 와 같은 환경변수(DATABASE_URL, BLOB_* ...)를 쓴다.
set -euo pipefail
cd "$(dirname "$0")/.."

NS=${NS:-tradeproof}
ENV_FILE=${ENV_FILE:-.env.example}

kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f -

# 주석·빈 줄을 뺀 KEY=VALUE 만 Secret 으로 (윈도우 줄바꿈도 제거)
grep -Ev '^\s*(#|$)' "$ENV_FILE" | tr -d '\r' > /tmp/tradeproof-env.$$
kubectl -n "$NS" create secret generic tradeproof-env --from-env-file=/tmp/tradeproof-env.$$ \
  --dry-run=client -o yaml | kubectl apply -f -
rm -f /tmp/tradeproof-env.$$

kubectl -n "$NS" create secret generic seaweedfs-s3 --from-file=s3.json=d_platform/compose/seaweedfs-s3.json \
  --dry-run=client -o yaml | kubectl apply -f -

# 스키마 파일 묶음. Postgres 는 데이터 폴더가 비어 있을 때만 이 파일들을 실행한다.
kubectl -n "$NS" create configmap pg-schema --from-file=common/schema \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n "$NS" apply -f d_platform/k8s/data/postgres.yaml
kubectl -n "$NS" apply -f d_platform/k8s/data/seaweedfs.yaml

kubectl -n "$NS" rollout status statefulset/postgres --timeout=180s
kubectl -n "$NS" rollout status statefulset/seaweedfs --timeout=180s
kubectl -n "$NS" wait --for=condition=complete job/minio-init --timeout=180s
kubectl -n "$NS" logs job/minio-init | tail -1
kubectl -n "$NS" get pods,pvc
