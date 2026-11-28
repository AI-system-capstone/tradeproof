#!/usr/bin/env bash
# 시연용 심사 트래픽: canary 입구(ingress)로 샘플 서류 심사를 계속 넣는다 (A 의 a_screening/tools/loadgen.py).
# canary 감시(오판율·실패율·처리 시간)는 후보 번들이 10건 이상 판정하고 담당자가 5건 이상 채점해야 판단한다.
# 트래픽만으로는 다음 단계로 올라가지 않는다 → 검토 화면에서 채점 (리허설은 scripts/demo_grade.sh).
#
#   scripts/demo_traffic.sh                    # 초당 1건, 5분 (Ctrl+C 로 멈춤)
#   RATE=2 DURATION=120 scripts/demo_traffic.sh
#
# 끝나면 번들별 접수 비율(canary 비율 확인용)과 처리 결과 요약이 나온다.
set -euo pipefail
source "$(dirname "$0")/demo/lib.sh"
need_tools python3
RATE=${RATE:-1}
DURATION=${DURATION:-300}
[ -d data/docs ] || die "data/docs 없음 (골든셋 데이터, $ROOT/d_platform/k8s/demo/README.md 2단계)"
say "심사 트래픽: 초당 $RATE 건, ${DURATION}초 → http://localhost:$INGRESS_PORT (canary 입구)"
exec python3 a_screening/tools/loadgen.py --url "http://localhost:$INGRESS_PORT" --mode sample \
  --rate "$RATE" --duration "$DURATION" --concurrency 2 --log-every 10
