# 시연용 OCR 모델 설정

반입 ZIP의 `payload/model_config.json`이 되는 tesseract 설정입니다. `scripts/demo_sign.sh`가 이 파일로 서명된 ZIP을 만듭니다.

| 파일 | 모델 ID | 내용 | 골든셋 100건 결과 (rules-v3, list-v14) |
|---|---|---|---|
| `baseline.json` | model-v3 | 기준 모델 (`a_screening/ocr/extract.py` MODELS 와 같음), 0.7배 축소 | 필드 정확도 94.3%, 판정 일치 89/100 |
| `improved.json` | model-v1N (S4) | 0.9배 축소. 0.5~0.9배 7가지를 비교해 고름 | 필드 94.8%, 판정 일치 94/100, 미탐 0, 위험도 하향 0 |
| `degraded.json` | model-v2N (S5) | 저해상도·흐림·잡음 (extract.py 의 model-v4 와 같음) | 판정 일치 23/100, 미탐 3 → 게이트 차단 |

0.6배는 필드 정확도가 95.4%로 더 높았지만 고위험 보류(HOLD) 1건을 검토로 내려서 제외했습니다.
| `slow.json` | model-v3N (canary 감시용, **임시: D 가 만듦, A 확인 필요**) | 기준 설정 그대로, 3.2배 확대(bicubic) → 정확도는 기준 이상, 처리 시간만 늘어남 | canary 중 처리 시간이 운영 번들의 1.5배를 넘어 자동 롤백되는지 확인용 |
