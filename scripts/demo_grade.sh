#!/usr/bin/env bash
# 리허설·확인용 채점: 후보 번들의 판정을 맞음/틀림으로 채점한다 (검토 화면의 맞음/틀림 버튼과 같은 A API).
# 실제 시연에서는 검토 화면(http://localhost:18000/review)에서 담당자가 직접 누른다.
#
# canary 감시는 후보가 10건 이상 판정하고 5건 이상 채점돼야 판단을 시작한다 (d_platform/helm/a-screening/values.yaml analysis).
#   scripts/demo_grade.sh B-9 correct 5      # 후보 B-9 판정 5건을 "맞음" → 오판율 0% → 승격
#   scripts/demo_grade.sh B-9 incorrect 5    # 5건을 "틀림" → 오판율 100% (기준 30% 초과) → 롤백
#   scripts/demo_grade.sh B-9 incorrect 2    # 섞어서 채점할 수도 있다 (예: 맞음 3 + 틀림 2 = 40% → 롤백)
#
# 아직 채점 안 된 건만, 최근 판정순으로 채점한다. 분류(통과 후보·검토·보류)는 가리지 않는다.
set -euo pipefail
source "$(dirname "$0")/demo/lib.sh"
need_tools curl python3

BUNDLE=${1:-}; VERDICT=${2:-}; N=${3:-5}
[ -n "$BUNDLE" ] && [ "$VERDICT" = correct -o "$VERDICT" = incorrect ] \
  || die "사용법: scripts/demo_grade.sh <후보 번들 ID> <correct|incorrect> [건수=5]"
A="http://localhost:$A_PORT"
curl -s -o /dev/null --max-time 3 "$A/livez" || die "A 심사 API($A)에 연결 안 됨 (scripts/demo_up.sh 로 포트포워드)"

python3 - "$A" "$BUNDLE" "$VERDICT" "$N" "$ACTOR" <<'PY'
import json, sys, urllib.request

base, bundle, verdict, n, actor = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4]), sys.argv[5]

def call(method, path, body=None):
    req = urllib.request.Request(base + path, method=method,
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)

todo, offset = [], 0
while len(todo) < n:
    page = call("GET", f"/screenings?bundle_id={bundle}&status=completed&limit=200&offset={offset}")
    todo += [it for it in page["items"] if it["classification"] and not it["ai_feedback"]]
    offset += 200
    if offset >= page["total"]:
        break
todo = todo[:n]
if len(todo) < n:
    print(f"채점할 수 있는 {bundle} 판정이 {len(todo)}건뿐 (요청 {n}건). 트래픽을 더 넣으세요: scripts/demo_traffic.sh")
for it in todo:
    call("POST", f"/screenings/{it['tx_id']}/feedback", {"verdict": verdict, "reviewer": actor})
    print(f"  {it['tx_id']}  {it['classification']:<15} → {verdict}")

counts = {"correct": 0, "incorrect": 0}
for line in urllib.request.urlopen(base + "/metrics", timeout=10).read().decode().splitlines():
    if line.startswith(f'tradeproof_review_feedback{{bundle_id="{bundle}",'):
        v = line.split('verdict="')[1].split('"')[0]
        counts[v] = int(float(line.rsplit(" ", 1)[1]))
total = sum(counts.values())
rate = f"{counts['incorrect'] / total:.0%}" if total else "-"
print(f"{bundle} 채점 누적: 맞음 {counts['correct']}, 틀림 {counts['incorrect']} → 오판율 {rate} (기준 30% 이하, 5건부터 판단)")
PY
