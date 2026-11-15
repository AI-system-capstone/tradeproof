"""pipeline.py: 워커와 C(골든셋·Replay)가 같이 쓰는 처리 경로.

SVC-04: 같은 번들·같은 서류 → 몇 번을 돌려도 같은 추출값·같은 판정.
워커 경로(screen_job)와 pipeline.screen_documents 가 같은 결과를 내는지도 확인한다 (C 가 import 해서 돌려도 같은 결과).
tesseract 와 data/docs 가 있어야 OCR 테스트를 돌린다 (CI 에서는 건너뜀).
"""
import shutil
from pathlib import Path

import pytest

from common import contracts as C
from a_screening import pipeline as P
from a_screening.ocr import worker as W

SAMPLES = ("TX-0001", "TX-0002", "TX-0003")
have_ocr = pytest.mark.skipif(
    shutil.which("tesseract") is None or not all((C.DOCS_DIR / s / "invoice.png").exists() for s in SAMPLES),
    reason="tesseract 또는 data/docs 없음")
have_lists = pytest.mark.skipif(not C.list_path(C.LIST_BASE).exists(), reason="data/sanctions 없음")


def comparable(out: dict) -> dict:
    """시간 값(extracted_at, ocr_seconds)을 뺀, 결과로 비교할 부분."""
    return {k: out[k] for k in ("bundle_id", "model_version", "fields", "extracted", "conflicts", "source", "result")}


# ---------------------------------------------------------------- 번들 구성
def test_bundle_spec():
    b = P.bundle_spec(C.BASELINE_BUNDLE)
    want = C.BUNDLES[C.BASELINE_BUNDLE]
    assert b == {"bundle_id": C.BASELINE_BUNDLE, "model_version": want["model_version"],
                 "list_version": want["list_version"], "rules_version": want["rules_version"]}
    with pytest.raises(ValueError):
        P.bundle_spec("B-99")


def test_bundle_missing_keys_rejected():
    with pytest.raises(ValueError, match="model_version"):
        P.screen_documents("/nonexistent", {"bundle_id": "B-x", "list_version": C.LIST_BASE,
                                            "rules_version": C.RULES_BASE})


@have_lists
def test_screen_extracted_without_ocr_matches_match_screen():
    """필드 직접 입력(from_ocr=False)은 screening.match.screen 을 그대로 부른 것과 같다."""
    from a_screening.screening.match import load_index, screen
    fields = {f: None for f in C.TX_FIELDS} | {"seller_name": "Acme Ltd", "buyer_name": "Foo GmbH",
                                                "vessel_name": "SEA STAR"}
    extracted = {f: {"value": v, "source_doc": None, "bbox": None, "confidence": None} for f, v in fields.items()}
    b = P.bundle_spec(C.BASELINE_BUNDLE)
    assert P.screen_extracted(extracted, [], b, from_ocr=False) == \
        screen(fields, load_index(b["list_version"]), rules_version=b["rules_version"])


# ---------------------------------------------------------------- SVC-04: 같은 입력 → 같은 결과 (3회)
@have_ocr
@pytest.mark.parametrize("bundle_id", ["B-7", "B-9", "B-10"])   # model-v3 / model-v4(저하) / rules-v3
def test_same_documents_same_result_three_times(bundle_id):
    b = P.bundle_spec(bundle_id)
    for s in SAMPLES:
        runs = [comparable(P.screen_documents(C.DOCS_DIR / s, b)) for _ in range(3)]
        assert runs[0] == runs[1] == runs[2], f"{s} {bundle_id}: 반복 결과가 다름"
        assert runs[0]["result"]["classification"] in C.CLASSIFICATIONS


# ---------------------------------------------------------------- 워커 경로 == pipeline (C 가 돌려도 같은 결과)
@have_ocr
@pytest.mark.parametrize("bundle_id", ["B-7", "B-9"])
def test_worker_path_equals_pipeline(bundle_id, tmp_path: Path):
    for s in SAMPLES:
        job = {"tx_id": f"TX-PIPE-{s}", "bundle_id": bundle_id,
               "input": {"source": C.INPUT_SAMPLE, "sample_tx_id": s, "fields": None}}
        w = W.screen_job(job, blob=None, workdir=tmp_path)
        p = P.screen_documents(C.DOCS_DIR / s, P.bundle_spec(bundle_id))
        assert w["result"] == p["result"]
        assert w["extracted"] == p["extracted"] and w["conflicts"] == p["conflicts"]
        assert (w["model_version"], w["source"]) == (p["model_version"], p["source"])
