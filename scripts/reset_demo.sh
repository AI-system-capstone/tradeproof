#!/usr/bin/env bash
# 시연 초기화 (리허설마다 같은 상태로, 담당: D / 각 단계는 해당 담당자가 제공)
set -euo pipefail
cd "$(dirname "$0")/.."

echo "[1/5] DB·버킷 초기화"            # D: docker compose down -v 후 재기동 (k3s에서는 초기화 Job)
echo "[2/5] 합성 데이터 생성"           # 데이터 담당: python -m dataset.generator --seed 42 ...
echo "[3/5] 최초 기준 번들 반입"         # B: 초기 모델·리스트를 서명 패키지로 POST /imports (ING-02)
echo "[4/5] 과거 거래 적재"             # 데이터 담당: python -m dataset.seed --bundle B-1
echo "[5/5] 골든셋 적재"                # 데이터 담당: golden-list-v1 → golden_labels
echo "TODO: 각 단계 명령을 담당자가 채워 넣으세요"
