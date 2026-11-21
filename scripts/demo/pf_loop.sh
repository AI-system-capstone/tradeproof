#!/usr/bin/env bash
# 포트포워드 하나를 계속 살려 둔다 (scripts/demo/lib.sh 의 start_bg 가 띄운다. 직접 실행하지 않는다).
#
#   pf_loop.sh <context> <namespace> <service> <로컬 포트> <서비스 포트> <확인 경로 | ->
#
# kubectl port-forward 는 붙어 있던 파드가 바뀌면(롤백·재시작) 프로세스는 살아 있는데 요청이 전달되지 않는 상태로
# 남을 때가 있다. 그래서 5초마다 로컬 포트에 HTTP 로 물어보고, 두 번 연속 응답이 없으면 끊고 다시 붙는다.
# 확인 경로가 - 이면(예: Postgres) 프로세스가 끝났을 때만 다시 붙는다.
ctx=$1 ns=$2 svc=$3 port=$4 tport=$5 path=$6
while true; do
  kubectl port-forward --context "$ctx" -n "$ns" --address 127.0.0.1 "svc/$svc" "$port:$tport" &
  pid=$!
  sleep 3
  fails=0
  while kill -0 "$pid" 2>/dev/null; do
    if [ "$path" != - ]; then
      # 응답 코드와 상관없이 HTTP 응답이 오면 연결은 살아 있다 (A 의 /healthz 는 준비 전 503 일 수 있음)
      if curl -s -m 3 -o /dev/null "http://127.0.0.1:$port$path"; then fails=0; else fails=$((fails + 1)); fi
      if [ "$fails" -ge 2 ]; then
        echo "$(date +%H:%M:%S) $svc 응답 없음 → 다시 붙음"
        kill "$pid" 2>/dev/null
      fi
    fi
    sleep 5
  done
  wait "$pid" 2>/dev/null
  sleep 1
done
