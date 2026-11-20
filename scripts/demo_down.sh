#!/usr/bin/env bash
# 시연 환경 끄기.
#
#   scripts/demo_down.sh            # 포트포워드·D 만 끄고 클러스터는 멈춤 (데이터 유지, demo_up.sh 로 다시 켬)
#   scripts/demo_down.sh --delete   # 클러스터까지 삭제 (DB·저장소 모두 지워짐)
set -euo pipefail
source "$(dirname "$0")/demo/lib.sh"
stop_bg
echo "포트포워드·D 배포 API 종료"
if [ "${1:-}" = --delete ]; then
  k3d cluster delete "$CLUSTER"
  rm -f "$KUBECONFIG" "$STATE/values-bundle.yaml"
else
  k3d cluster stop "$CLUSTER"
fi
