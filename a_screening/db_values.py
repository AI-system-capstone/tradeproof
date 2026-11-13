"""추출 필드(문자열) -> DB 컬럼 타입 변환. 시드 생성(db/build_seed.py)과 심사 API 저장이 같이 쓴다.

OCR이 잘못 읽어 변환이 안 되면 None 을 돌려준다 (원문은 extracted_fields 에 그대로 남는다).
값을 억지로 고치지 않는다.
"""
from __future__ import annotations

from datetime import datetime


def amount_value(s: str | None) -> str | None:
    if not s:
        return None
    try:
        return f"{float(str(s).replace(',', '')):.2f}"
    except ValueError:
        return None


def date_value(s: str | None) -> str | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).date().isoformat()
    except ValueError:
        return None
