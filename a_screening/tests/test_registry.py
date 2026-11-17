"""번들 구성을 B 레지스트리에서 받는 경로 (REGISTRY_MODE=http). 작은 가짜 B 서버 + 로컬 원본 저장소로 확인한다.

B 응답 모양은 b_registry/README.md (11/17 main): GET /bundles/{id} -> {bundle_id, model_id, list_id, model:{model_id, uri}, list:{list_id, uri}}
ZIP 안 파일: 모델 payload/model_config.json, 리스트 payload/sanctions.json (b_registry/verify/profiles.py 가 검사하는 모양)
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from common import contracts as C
from common.blob import LocalBlobStore
from a_screening import registry as R
from a_screening import pipeline as P
from a_screening import store as ST
from a_screening.screening.match import ListIndex, screen
from a_screening.tests.test_match import SAMPLE_LIST

NEW_BUNDLE = "B-11"          # contracts.BUNDLES 에 없는 번들 (B 에서만 안다)
MODEL_CONFIG = {"engine": "tesseract", "lang": "eng", "config": "--oem 1 --psm 6",
                "preprocess": {"scale": 0.5, "resample": "bilinear"}}
LIST_ID = SAMPLE_LIST["list_version"]


def _zip(name: str, raw: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("manifest.json", b"{}")
        z.writestr(name, raw)
    return buf.getvalue()


def _put(blob: LocalBlobStore, raw: bytes) -> str:
    return blob.put("registry/" + hashlib.sha256(raw).hexdigest(), raw)   # B 와 같은 내용 주소 키


class FakeB:
    def __init__(self, bundles: dict):
        self.bundles, self.calls = bundles, []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.calls.append(self.path)
                bid = self.path.rsplit("/", 1)[-1]
                if self.path.startswith("/bundles/") and bid in outer.bundles:
                    body, code = json.dumps(outer.bundles[bid]).encode(), 200
                else:
                    body, code = b'{"code":"NOT_FOUND"}', 404
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"


@pytest.fixture()
def b(tmp_path, monkeypatch):
    blob = LocalBlobStore(tmp_path / "blob")
    model_uri = _put(blob, _zip(R.MODEL_FILE, json.dumps(MODEL_CONFIG).encode()))
    list_uri = _put(blob, _zip(R.LIST_FILE, json.dumps(SAMPLE_LIST).encode()))
    fake = FakeB({NEW_BUNDLE: {"bundle_id": NEW_BUNDLE, "status": "approved", "model_id": "model-v5",
                               "list_id": LIST_ID, "created_at": "2026-11-17T00:00:00Z",
                               "model": {"model_id": "model-v5", "uri": model_uri},
                               "list": {"list_id": LIST_ID, "uri": list_uri}}})
    monkeypatch.setenv("REGISTRY_MODE", "http")
    monkeypatch.setenv("REGISTRY_URL", fake.url)
    R.reset()
    R.set_blob_store(blob)
    fake.blob = blob
    yield fake
    fake.server.shutdown()
    R.reset()


def test_local_mode_uses_contracts(monkeypatch):
    monkeypatch.delenv("REGISTRY_MODE", raising=False)
    R.reset()
    want = C.BUNDLES[C.BASELINE_BUNDLE]
    assert R.bundle(C.BASELINE_BUNDLE) == {"bundle_id": C.BASELINE_BUNDLE, "model_id": want["model_id"],
                                           "list_id": want["list_id"]}
    assert R.model_config(want["model_id"]) is None and R.sanctions_list(want["list_id"]) is None
    with pytest.raises(R.BundleNotFound):
        R.bundle(NEW_BUNDLE)


def test_bundle_from_registry_cached(b):
    want = {"bundle_id": NEW_BUNDLE, "model_id": "model-v5", "list_id": LIST_ID}
    assert R.bundle(NEW_BUNDLE) == want
    assert R.bundle(NEW_BUNDLE) == want
    assert b.calls == [f"/bundles/{NEW_BUNDLE}"]          # 번들 구성은 안 바뀌므로 한 번만 묻는다
    assert P.bundle_spec(NEW_BUNDLE) == want               # 워커·C 가 쓰는 입구도 같은 값
    assert NEW_BUNDLE in R.known_bundles()


def test_unknown_bundle_and_b_down(b, monkeypatch):
    with pytest.raises(R.BundleNotFound):
        R.bundle("B-404")
    with pytest.raises(ValueError):                        # store 는 ValueError 로 422 를 낸다
        ST._check_bundles("B-404", None)
    monkeypatch.setenv("REGISTRY_URL", "http://127.0.0.1:9")   # 아무도 안 듣는 포트
    with pytest.raises(R.RegistryUnavailable):            # 재시도 대상 (PermanentJobError 아님)
        R.bundle("B-12")


def test_model_and_list_from_zip(b):
    R.bundle(NEW_BUNDLE)
    assert R.model_config("model-v5") == MODEL_CONFIG
    from a_screening.ocr import extract as X
    assert X.model_spec("model-v5") == MODEL_CONFIG        # OCR 은 B 모델 ZIP 설정으로 돈다
    assert R.sanctions_list(LIST_ID)["entries"] == SAMPLE_LIST["entries"]
    assert P.index_for(LIST_ID) is R.list_index(LIST_ID)   # 색인은 한 번만 만든다


def test_screen_with_registry_list_matches_local(b):
    """B 리스트 ZIP 으로 판정한 결과 == 같은 리스트를 직접 넣은 판정 (번들 ID 가 contracts 에 없어도)."""
    fields = {f: None for f in C.TX_FIELDS} | {"seller_name": SAMPLE_LIST["entries"][0]["name"],
                                                "buyer_name": "Foo GmbH", "vessel_name": "SEA STAR"}
    extracted = {f: {"value": v, "source_doc": None, "bbox": None, "confidence": None} for f, v in fields.items()}
    got = P.screen_extracted(extracted, [], P.bundle_spec(NEW_BUNDLE), from_ocr=False)
    want = screen(fields, ListIndex(copy.deepcopy(SAMPLE_LIST)), rules_version=C.RULES_FIXED)
    assert got == want and got["classification"] == C.HOLD


def test_tampered_zip_rejected(b):
    R.bundle(NEW_BUNDLE)
    uri = R._URIS[("list", LIST_ID)]
    path = b.blob.root / uri[len("file://"):]
    path.write_bytes(path.read_bytes() + b"x")             # 저장소 쪽에서 바뀜 -> sha256 이 uri 와 다름
    with pytest.raises(R.BadArtifact):
        R.sanctions_list(LIST_ID)


def test_store_accepts_registry_bundle(b):
    ST._check_bundles(NEW_BUNDLE, None)                    # contracts.BUNDLES 에 없어도 B 에 있으면 접수 가능
    assert ST._check_filters(None, None, NEW_BUNDLE) is None
