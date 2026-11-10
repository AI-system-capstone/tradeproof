#!/usr/bin/env bash
# 생성 데이터(data/)만 다시 만들기 (담당: 데이터 담당)
set -euo pipefail
cd "$(dirname "$0")/.."
echo "TODO: python -m dataset.generator --seed 42 --count 100 --out data/"
