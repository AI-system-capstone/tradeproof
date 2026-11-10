#!/usr/bin/env bash
# 통합 확인 (통합일에 실행, 담당: D)
# 1) 데이터 계층 기동 2) 모듈 헬스 체크 3) 모듈별 테스트
set -euo pipefail
cd "$(dirname "$0")/.."

docker compose up -d postgres minio minio-init
docker compose ps

for url in http://localhost:8000 http://localhost:8100 http://localhost:8200 http://localhost:8300; do
  printf '%-24s ' "$url/healthz"
  curl -s -o /dev/null -w '%{http_code}\n' "$url/healthz" || echo "down"
done

for m in common dataset a_screening b_registry c_replay d_platform; do
  if compgen -G "$m/tests/test_*.py" > /dev/null; then
    echo "== pytest $m"; pytest -q "$m/tests"
  else
    echo "== $m: 테스트 없음 (건너뜀)"
  fi
done
