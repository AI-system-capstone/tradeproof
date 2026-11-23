#!/usr/bin/env bash
# 시연용 서명 ZIP 만들기 (데모 전용 키). 드라이브에서 키를 받을 필요 없이 각자 만든다.
#
#   scripts/demo_sign.sh                 # 키가 없으면 새로 만들고, initial/ + team-A..D/ 세트를 만든다
#   SETS="A B" scripts/demo_sign.sh      # 세트 이름 지정
#   START=30 scripts/demo_sign.sh        # 모델 번호를 바꿔 새 세트 (같은 환경에서 또 해 볼 때: 한 번 올린 ZIP 은 다시 못 씀)
#
# 결과 (data/ 아래라 git 에 안 올라감):
#   data/demo/keys/vendor.pem, vendor.pub   서명 키 (B 가 vendor.pub 를 신뢰 키로 씀 → 키를 바꾸면 demo_up.sh 를 다시 실행)
#   data/demo/packages/initial/             model-v3.zip, list-v14.zip  (처음 B-7 을 만드는 데 씀, demo_up.sh 가 자동으로 올림)
#   data/demo/packages/team-X/              1-S1-tampered, 2-S1-corrupt, 3-S5-degraded-model-v2N, 4-S4-improved-model-v1N
set -euo pipefail
source "$(dirname "$0")/demo/lib.sh"
need_tools docker

SETS=${SETS:-"A B C D"}
START=${START:-10}
IMG=tradeproof/b-registry:dev
[ -f data/sanctions/list-v14.json ] || die "data/sanctions/list-v14.json 없음 → 골든셋 데이터를 먼저 (d_platform/k8s/demo/README.md 2단계)"

say "B 이미지 확인 (서명 도구)"
docker image inspect $IMG >/dev/null 2>&1 || docker build -q -f b_registry/Dockerfile -t $IMG . >/dev/null
run() { docker run --rm -v "$ROOT:/repo" -w /repo $IMG "$@"; }
rel() { python3 -c 'import os,sys; print(os.path.relpath(sys.argv[1], sys.argv[2]))' "$1" "$ROOT"; }
K=$(rel "$KEYS"); P=$(rel "$PACKAGES")

if [ ! -f "$KEYS/vendor.pem" ]; then
  say "데모 서명 키 생성 ($KEYS)"
  mkdir -p "$KEYS"
  run python -m b_registry.signer keygen --private-key "$K/vendor.pem" --public-key "$K/vendor.pub"
fi

sign() {  # sign 종류 ID 원본 출력
  run python -m b_registry.signer.package_release --private-key "$K/vendor.pem" --type "$1" --item-id "$2" --source "$3" --output "$4" >/dev/null
}
fresh() { rm -f "$1"; }

say "처음 반입용 (initial/)"
mkdir -p "$PACKAGES/initial" "$STATE/models/model-v3"
cp scripts/demo/models/baseline.json "$STATE/models/model-v3/model_config.json"
fresh "$PACKAGES/initial/model-v3.zip"; fresh "$PACKAGES/initial/list-v14.zip"
sign ocr_model model-v3 "$(rel "$STATE/models/model-v3")" "$P/initial/model-v3.zip"
sign sanctions_list list-v14 data/sanctions/list-v14.json "$P/initial/list-v14.zip"

i=0
for s in $SETS; do
  g=$((START + i)); d=$((START + 10 + i)); i=$((i + 1))
  out="$PACKAGES/team-$s"; mkdir -p "$out" "$STATE/models/model-v$g" "$STATE/models/model-v$d"
  rm -f "$out"/*.zip
  cp scripts/demo/models/improved.json "$STATE/models/model-v$g/model_config.json"
  cp scripts/demo/models/degraded.json "$STATE/models/model-v$d/model_config.json"
  say "team-$s: 개선 model-v$g, 저하 model-v$d"
  sign ocr_model "model-v$d" "$(rel "$STATE/models/model-v$d")" "$P/team-$s/3-S5-degraded-model-v$d.zip"
  sign ocr_model "model-v$g" "$(rel "$STATE/models/model-v$g")" "$P/team-$s/4-S4-improved-model-v$g.zip"
  run python -m b_registry.signer tamper "$P/team-$s/4-S4-improved-model-v$g.zip" "$P/team-$s/1-S1-tampered.zip"
  run python -m b_registry.signer corrupt "$P/team-$s/4-S4-improved-model-v$g.zip" "$P/team-$s/2-S1-corrupt.zip"
done

say "서버에 올리지 않고 B 반입 검사로 확인 (올리면 '쓴 ZIP' 이 되므로)"
docker run --rm -i -v "$ROOT:/repo" -w /repo -e TRUST_KEY="/repo/$K/vendor.pub" $IMG python - "$P" <<'PY'
import glob, sys
from b_registry.config import Settings
from b_registry.verify.validation import validate
cfg, bad = Settings(), 0
for f in sorted(glob.glob(f"{sys.argv[1]}/initial/*.zip") + glob.glob(f"{sys.argv[1]}/team-*/*.zip")):
    r = validate(open(f, "rb").read(), cfg)
    want_reject = "/1-S1-" in f or "/2-S1-" in f
    ok = bool(r.error) == want_reject
    bad += not ok
    print(f"  {'OK ' if ok else 'XX '} {f.split('/packages/')[1]:<40} {'거부 ' + r.error.code if r.error else '통과 ' + r.manifest.item_id}")
sys.exit(1 if bad else 0)
PY
say "완료: $PACKAGES"
