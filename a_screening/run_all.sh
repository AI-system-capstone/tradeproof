#!/usr/bin/env bash
# 모듈 A 1주차 산출물 전체 생성: 제재 리스트 -> 합성 서류 -> OCR -> 평가 -> 과거 거래 DB
#   a_screening/run_all.sh            # sanctions/ofac_raw(실제 OFAC 파일)로 실행, 없으면 가짜 샘플
#   SDN_SRC=sanctions/fixtures a_screening/run_all.sh   # 가짜 샘플로 실행
#   DSN=postgresql://tradeproof:tradeproof-dev@localhost:5432/tradeproof a_screening/run_all.sh
set -euo pipefail
cd "$(dirname "$0")"

# 실제 OFAC 파일(sanctions/ofac_raw)이 있으면 그걸, 없으면 가짜 샘플을 쓴다
if [ -z "${SDN_SRC:-}" ]; then
  if [ -f sanctions/ofac_raw/SDN.CSV ]; then SDN_SRC=sanctions/ofac_raw; else SDN_SRC=sanctions/fixtures; fi
fi
DSN="${DSN:-postgresql://tradeproof:tradeproof@localhost:5432/tradeproof}"
COUNT="${COUNT:-100}"

echo "[1/5] 제재 리스트 v14/v15  (src: $SDN_SRC)"
python3 sanctions/build_lists.py --src "$SDN_SRC" --fetched-at "${FETCHED_AT:-2026-11-07}"

echo "[2/5] 합성 무역 서류 ${COUNT}건"
python3 docs/gen_docs.py --count "$COUNT"

echo "[3/5] OCR·필드 추출 (model-v3, model-v4)"
python3 ocr/extract.py --model model-v3
python3 ocr/extract.py --model model-v4

echo "[4/5] OCR 정확도·미탐 평가"
python3 ocr/evaluate.py --model model-v3

echo "[5/5] 과거 거래 DB 시드 (B-7, 정답 필드 기준) 적재"
if [ "${NO_LOAD:-0}" = "1" ]; then
  python3 db/build_seed.py
else
  python3 db/build_seed.py --load --dsn "$DSN"
fi
