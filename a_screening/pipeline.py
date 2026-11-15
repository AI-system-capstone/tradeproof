"""심사 한 건의 처리 경로: 서류 이미지 → OCR → 필드 추출 → 판정.

ocr-worker(평시 심사)와 C(골든셋·Replay)가 **같은 함수**를 쓴다. 그래서 같은 번들·같은 서류면 같은 결과가 나온다 (SVC-04).
DB·원문 저장소는 쓰지 않는다 (입력은 서류 폴더, 출력은 dict). C 는 이 모듈만 import 하면 된다.

  from a_screening.pipeline import bundle_spec, screen_documents
  out = screen_documents("data/docs/TX-0001", bundle_spec("B-9"))
  out["result"]["classification"]      # PASS_CANDIDATE / MANUAL_REVIEW / HIGH_RISK_HOLD
  out["result"]["reasons"]             # [{rule, field, value, entry_id, matched_name, score, classification}]
  out["fields"]                        # {필드: 값} (contracts.TX_FIELDS 13개)

  # 번들 구성을 직접 줄 수도 있다 (B 의 GET /bundles/{id} 결과로 채우면 됨)
  screen_documents(tx_dir, {"bundle_id": "B-9", "model_version": "model-v4", "list_version": "list-v15",
                            "rules_version": "rules-v3"})

서류 폴더 형식: <tx_dir>/invoice.png, <tx_dir>/bl.png (한쪽만 있어도 됨, 폴더 이름 = tx_id).
필요한 것: tesseract-ocr 5.5.0 (a_screening/Dockerfile 의 ocr-base 와 같은 버전이어야 결과가 같음), poppler 는 불필요.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from common import contracts as C  # noqa: E402
from a_screening.ocr import extract as X  # noqa: E402
from a_screening.screening.match import load_index, screen  # noqa: E402

BUNDLE_KEYS = ("model_version", "list_version", "rules_version")

_INDEX: dict = {}
_INDEX_LOCK = threading.Lock()


def bundle_spec(bundle_id: str) -> dict:
    """번들 ID → {bundle_id, model_version, list_version, rules_version}. 지금은 contracts.BUNDLES 에서 찾는다."""
    if bundle_id not in C.BUNDLES:
        raise ValueError(f"bundle_id={bundle_id!r} 는 contracts.BUNDLES 에 없음 ({list(C.BUNDLES)})")
    b = C.BUNDLES[bundle_id]
    return {"bundle_id": bundle_id, **{k: b[k] for k in BUNDLE_KEYS}}


def _check_bundle(bundle: dict) -> None:
    missing = [k for k in BUNDLE_KEYS if not bundle.get(k)]
    if missing:
        raise ValueError(f"번들 구성에 {missing} 없음: {bundle}")


def index_for(list_version: str):
    """제재 리스트 색인 (버전별 캐시, 스레드 안전)."""
    with _INDEX_LOCK:
        if list_version not in _INDEX:
            _INDEX[list_version] = load_index(list_version)
        return _INDEX[list_version]


def extract_documents(tx_dir: str | Path, model_version: str) -> tuple[dict, list]:
    """OCR + 필드 추출. 반환: (extracted, conflicts)
    extracted = {필드: {value, source_doc, bbox, confidence}}, conflicts = [{field, invoice, bl}] (Invoice/B-L 불일치)"""
    r = X.extract_transaction(Path(tx_dir), model_version)
    return r["fields"], r.get("conflicts", [])


def screen_extracted(extracted: dict, conflicts: list, bundle: dict, from_ocr: bool = True) -> dict:
    """추출 결과 → 판정. from_ocr=False 면 OCR 불확실성 규칙(신뢰도 낮음·불일치, rules-v3)을 쓰지 않는다 (필드 직접 입력)."""
    _check_bundle(bundle)
    fields = {f: (extracted.get(f) or {}).get("value") for f in C.TX_FIELDS}
    ocr = None
    if from_ocr:
        ocr = {"confidence": {f: (extracted.get(f) or {}).get("confidence") for f in C.TX_FIELDS},
               "conflicts": conflicts}
    return screen(fields, index_for(bundle["list_version"]), rules_version=bundle["rules_version"], ocr=ocr)


def screen_documents(tx_dir: str | Path, bundle: dict) -> dict:
    """서류 폴더 하나를 번들 하나로 심사한다.
    반환: {bundle_id, model_version, fields, extracted, conflicts, source, result:{classification, reasons}}"""
    _check_bundle(bundle)
    model = bundle["model_version"]
    extracted, conflicts = extract_documents(tx_dir, model)
    return {
        "bundle_id": bundle.get("bundle_id"),
        "model_version": model,
        "fields": {f: (extracted.get(f) or {}).get("value") for f in C.TX_FIELDS},
        "extracted": extracted,
        "conflicts": conflicts,
        "source": C.extraction_source_ocr(model),
        "result": screen_extracted(extracted, conflicts, bundle),
    }
