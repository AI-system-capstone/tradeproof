"""screening-api 테스트 (API 는 접수만, 처리는 ocr.worker 를 같은 저장소로 불러서 한다).

  python -m pytest api -q                                   # 메모리 저장소만 (data/docs 필요: ./run_all.sh)
  TEST_DATABASE_URL=postgresql://tradeproof:tradeproof@localhost:55432/tradeproof python -m pytest api -q
                                                            # + Postgres 저장소 (schema.sql 적재된 공유 DB)
같은 테스트를 저장소마다 한 번씩 돌린다. 공유 DB 라 TRUNCATE 하지 않고, 이 테스트가 접수한 tx_id 만 워커로 처리한다.
샘플·업로드 건은 호스트 tesseract 로 실제 OCR 한다. 버전에 따라 값이 달라질 수 있어 미리 만든 extracted.json 과
OCR 값은 비교하지 않는다 (분류만 본다: TX-0001 은 정확 일치 HOLD).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from common import contracts as C  # noqa: E402
from common.blob import LocalBlobStore  # noqa: E402
from a_screening.store import MemoryStore, PostgresStore  # noqa: E402
import a_screening.api.main as A  # noqa: E402
from a_screening.ocr import worker as W  # noqa: E402

TEST_DSN = os.environ.get("TEST_DATABASE_URL")
SAMPLE = "TX-0001"     # exact_match: list-v14/v15 모두 HIGH_RISK_HOLD
PNG_MAGIC = C.FILE_MAGIC[".png"]
pytestmark = pytest.mark.skipif(not (C.DOCS_DIR / SAMPLE / "invoice.png").exists(),
                                reason="data/docs 없음: ./run_all.sh 먼저 실행")
client = TestClient(A.app)


class Env:
    def __init__(self, store, blob):
        self.store, self.blob = store, blob
        self.mine: list[str] = []

    def run(self, tx_ids=None) -> int:
        """이 테스트가 접수한 건만 처리 (공유 DB 의 다른 건은 건드리지 않음)."""
        return W.drain(self.store, self.blob, "w-api-test", tx_ids=tx_ids or self.mine)


@pytest.fixture(autouse=True, params=["memory", "postgres"])
def env(request, monkeypatch, tmp_path):
    if request.param == "postgres":
        if not TEST_DSN:
            pytest.skip("TEST_DATABASE_URL 없음")
        store = PostgresStore(TEST_DSN)
    else:
        store = MemoryStore()
    e = Env(store, LocalBlobStore(tmp_path / "blob"))
    monkeypatch.setattr(A, "STORE", e.store)
    monkeypatch.setattr(A, "BLOB", e.blob)
    monkeypatch.setattr(A, "ACTIVE_BUNDLE", C.BASELINE_BUNDLE)
    monkeypatch.setattr(A, "SHADOW_BUNDLE", None)
    yield e
    if e.mine:   # 공유 DB 에 queued 로 남겨 다른 워커가 잡지 않게, 이 테스트가 접수한 건은 끝까지 처리
        e.run()


@pytest.fixture
def bundle(monkeypatch):
    def use(bundle_id: str):
        monkeypatch.setattr(A, "ACTIVE_BUNDLE", bundle_id)
    return use


# ---------------------------------------------------------------- 도우미
def submit(env, **kw) -> dict:
    """POST /screenings (json=... 또는 files=...) -> 202 본문. tx_id 를 env.mine 에 기록."""
    r = client.post("/screenings", **kw)
    assert r.status_code == 202, r.text
    env.mine.append(r.json()["tx_id"])
    return r.json()


def get(tx_id: str) -> dict:
    r = client.get(f"/screenings/{tx_id}")
    assert r.status_code == 200, r.text
    return r.json()


def truth_fields(sample=SAMPLE) -> dict:
    """정답 필드 (OCR 결과 아님). fields 접수로 OCR 없이 결정적인 판정을 볼 때."""
    return json.loads((C.DOCS_DIR / sample / "truth.json").read_text(encoding="utf-8"))["fields"]


def clean_fields(**over) -> dict:
    f = {k: None for k in C.TX_FIELDS} | {"seller_name": "Plain Trading Co", "buyer_name": "Ordinary Goods Ltd",
                                         "vessel_name": "CALM WATERS", "amount": "1,234.50",
                                         "invoice_date": "2026-11-08"}
    return f | over


def doc_files(ext: str, **over) -> dict:
    """data/docs/TX-0001 원문을 multipart 파일로."""
    ctype = {v: k for k, v in C.UPLOAD_CONTENT_TYPES.items()}[ext]
    files = {d: (f"{d}{ext}", (C.DOCS_DIR / SAMPLE / f"{d}{ext}").read_bytes(), ctype) for d in C.UPLOAD_DOC_TYPES}
    files.update(over)
    return {k: v for k, v in files.items() if v is not None}


def assert_error(r, status: int, code: str, target_id=None):
    """11/01 회의 3-3 공통 에러 형식: {code, message, target_id}"""
    assert r.status_code == status, r.text
    body = r.json()
    assert set(body) == set(C.ERROR_FIELDS) and body["code"] == code and body["message"]
    assert body["target_id"] == target_id


class Down:
    kind = "down"

    def ping(self):
        raise ConnectionError("연결 거부")

    def stats(self):
        raise ConnectionError("연결 거부")


# ---------------------------------------------------------------- 헬스
def test_healthz(env):
    r = client.get("/healthz").json()
    assert r["status"] == "ok" and r["storage"] == env.store.kind and r["blob"] == env.blob.kind
    assert r["bundle_id"] == C.BASELINE_BUNDLE and r["list_version"] == C.LIST_BASE
    assert r["model_version"] == C.MODEL_BASE and r["rules_version"] == C.RULES_BASE
    assert r["rules_supported"] == list(C.RULES_VERSIONS) and r["shadow_bundle_id"] is None


def test_healthz_503_when_db_down(monkeypatch):
    monkeypatch.setattr(A, "STORE", PostgresStore("postgresql://nobody:x@127.0.0.1:1/none"))
    assert_error(client.get("/healthz"), 503, C.ERR_UNAVAILABLE)


def test_healthz_503_when_blob_down(monkeypatch):
    monkeypatch.setattr(A, "BLOB", Down())
    r = client.get("/healthz")
    assert_error(r, 503, C.ERR_UNAVAILABLE)
    assert "원문 저장소" in r.json()["message"]


def test_livez_ok_even_when_storage_down(monkeypatch):
    monkeypatch.setattr(A, "STORE", Down())
    monkeypatch.setattr(A, "BLOB", Down())
    r = client.get("/livez")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


# ---------------------------------------------------------------- 접수 (JSON)
def test_post_only_queues_then_worker_completes(env):
    body = submit(env, json={"sample_tx_id": SAMPLE})
    tx = body["tx_id"]
    assert tx.startswith("TX-") and body["status"] == C.SCREENING_QUEUED
    assert body["bundle_id"] == C.BASELINE_BUNDLE and body["shadow_bundle_id"] is None
    assert body["status_url"] == f"/screenings/{tx}"
    rec = get(tx)    # API 는 처리하지 않는다
    assert rec["status"] == C.SCREENING_QUEUED and rec["attempts"] == 0 and rec["classification"] is None
    assert env.run() == 1
    rec = get(tx)
    assert rec["status"] == C.SCREENING_COMPLETED and rec["classification"] == C.HOLD
    assert rec["reasons"] and all(set(x) == set(C.REASON_FIELDS) for x in rec["reasons"])
    assert rec["extracted_fields"]["seller_name"]["confidence"] is not None   # 검토 화면에 쓸 bbox·신뢰도
    assert rec["extraction_source"] == C.extraction_source_ocr(C.MODEL_BASE)
    assert rec["input"] == {"source": C.INPUT_SAMPLE, "sample_tx_id": SAMPLE, "fields": None}
    assert rec["documents"]["invoice"] == f"docs/{SAMPLE}/invoice.pdf"
    assert rec["worker_id"] == "w-api-test" and rec["attempts"] == 1


def test_direct_fields(env):
    tx = submit(env, json={"fields": clean_fields()})["tx_id"]
    env.run()
    rec = get(tx)
    assert rec["status"] == C.SCREENING_COMPLETED and rec["classification"] == C.PASS
    assert rec["input"]["source"] == C.INPUT_FIELDS and rec["extraction_source"] == C.EXTRACTION_FROM_FIELDS
    assert rec["fields"]["amount"] == "1,234.50" and rec["documents"] == {"invoice": None, "bl": None}


@pytest.mark.parametrize("body", [
    {},                                                         # 둘 다 없음
    {"sample_tx_id": SAMPLE, "fields": {"seller_name": "X"}},   # 둘 다 있음
    {"fields": {"sellerName": "X"}},                            # contracts 에 없는 필드
    [1, 2],                                                     # 객체가 아님
])
def test_bad_json_422(body):
    assert_error(client.post("/screenings", json=body), 422, C.ERR_VALIDATION)


def test_not_json_and_bad_content_type_422():
    r = client.post("/screenings", content=b"{nope", headers={"Content-Type": "application/json"})
    assert_error(r, 422, C.ERR_VALIDATION)
    r = client.post("/screenings", content=b"x", headers={"Content-Type": "text/plain"})
    assert_error(r, 422, C.ERR_VALIDATION)


@pytest.mark.parametrize("sample", ["TX-9999", "../docs", "TX-0001/../TX-0002",
                                    "TX-1", "tx-0001", "TX-0001 ", "/etc", "TX-0001/"])   # contracts.sample_dir
def test_unknown_sample_404(sample):
    assert_error(client.post("/screenings", json={"sample_tx_id": sample}), 404, C.ERR_NOT_FOUND, sample)


# ---------------------------------------------------------------- 접수 (멀티파트 업로드)
def test_upload_pdf(env):
    body = submit(env, files=doc_files(".pdf"))
    tx = body["tx_id"]
    rec = get(tx)
    assert rec["status"] == C.SCREENING_QUEUED
    assert rec["input"]["source"] == C.INPUT_UPLOAD and rec["input"]["sample_tx_id"] is None \
        and rec["input"]["fields"] is None
    for d in C.UPLOAD_DOC_TYPES:
        size = (C.DOCS_DIR / SAMPLE / f"{d}.pdf").stat().st_size
        assert rec["input"]["files"][d] == {"content_type": "application/pdf", "size": size}
        assert rec["documents"][d].endswith(C.blob_key(tx, d, ".pdf")) and env.blob.exists(rec["documents"][d])
        # 렌더링 전에는 검토 화면 이미지가 없다
        assert_error(client.get(f"/screenings/{tx}/documents/{d}"), 404, C.ERR_NOT_FOUND, tx)
    env.run()
    rec = get(tx)
    assert rec["status"] == C.SCREENING_COMPLETED and rec["classification"] == C.HOLD
    for d in C.UPLOAD_DOC_TYPES:
        r = client.get(f"/screenings/{tx}/documents/{d}")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content.startswith(PNG_MAGIC)


def test_upload_png(env):
    tx = submit(env, files=doc_files(".png"))["tx_id"]
    want = (C.DOCS_DIR / SAMPLE / "invoice.png").read_bytes()
    r = client.get(f"/screenings/{tx}/documents/invoice")   # PNG 원문이 곧 검토 화면 이미지
    assert r.status_code == 200 and r.content == want
    assert get(tx)["input"]["files"]["bl"]["content_type"] == "image/png"
    env.run()
    assert get(tx)["classification"] == C.HOLD


@pytest.mark.parametrize("case", ["text_type", "magic_mismatch", "missing_bl", "empty", "unknown_field", "not_file"])
def test_upload_bad_422(env, case):
    pdf = (C.DOCS_DIR / SAMPLE / "invoice.pdf").read_bytes()
    kw = {"files": {
        "text_type": doc_files(".pdf", bl=("bl.txt", b"hello", "text/plain")),
        "magic_mismatch": doc_files(".pdf", bl=("bl.pdf", PNG_MAGIC + b"rest", "application/pdf")),
        "missing_bl": doc_files(".pdf", bl=None),
        "empty": doc_files(".pdf", invoice=("invoice.pdf", b"", "application/pdf")),
        "unknown_field": doc_files(".pdf", extra=("x.pdf", pdf, "application/pdf")),
        "not_file": doc_files(".pdf", bl=None),
    }[case]}
    if case == "not_file":
        kw["data"] = {"bl": "글자"}
    before = env.store.list(limit=1)["total"] if env.store.kind == "memory" else None
    assert_error(client.post("/screenings", **kw), 422, C.ERR_VALIDATION)
    if before is not None:
        assert env.store.list(limit=1)["total"] == before   # 접수되지 않음
    assert not (env.blob.root.exists() and any(env.blob.root.rglob("*")))   # 원문도 저장 안 됨


def test_upload_too_large_422(monkeypatch):
    monkeypatch.setattr(A, "MAX_UPLOAD_BYTES", 1000)
    r = client.post("/screenings", files=doc_files(".pdf"))
    assert_error(r, 422, C.ERR_VALIDATION)
    assert "너무 큼" in r.json()["message"]


# ---------------------------------------------------------------- 목록
def test_list_filters_and_total(env):
    hold = submit(env, json={"fields": truth_fields()})["tx_id"]
    passes = [submit(env, json={"fields": clean_fields()})["tx_id"] for _ in range(2)]
    queued = submit(env, json={"fields": clean_fields()})["tx_id"]
    env.run([hold, *passes])
    page = client.get("/screenings", params={"limit": 200}).json()
    assert set(page) == {"items", "total"}
    item = next(i for i in page["items"] if i["tx_id"] == hold)
    assert {"tx_id", "status", "bundle_id", "classification", "submitted_at", "completed_at", "reviewer_decision",
            "seller_name", "buyer_name", "vessel_name"} <= set(item)
    assert item["classification"] == C.HOLD and item["seller_name"] == truth_fields()["seller_name"]
    for params, want_in, want_out in (
        ({"classification": C.PASS}, set(passes), {hold, queued}),
        ({"classification": C.HOLD}, {hold}, set(passes) | {queued}),
        ({"status": C.SCREENING_QUEUED}, {queued}, {hold, *passes}),
        ({"bundle_id": C.BASELINE_BUNDLE}, {hold, *passes, queued}, set()),
    ):
        got = client.get("/screenings", params={**params, "limit": 200}).json()
        ids = {i["tx_id"] for i in got["items"]}
        assert want_in <= ids and not (want_out & ids), params
        k, v = next(iter(params.items()))
        assert all(i[k] == v for i in got["items"])
        if env.store.kind == "memory":
            assert got["total"] == len(want_in)
        else:
            assert got["total"] >= len(want_in)
    if env.store.kind == "memory":
        assert client.get("/screenings").json()["total"] == 4
        assert [i["tx_id"] for i in client.get("/screenings", params={"limit": 1, "offset": 1}).json()["items"]] \
            == [passes[1]]   # 최신순


@pytest.mark.parametrize("params", [{"limit": 0}, {"limit": 201}, {"offset": -1}, {"status": "done"},
                                    {"classification": "PASS"}, {"bundle_id": "B-99"},
                                    {"classification": f"{C.HOLD},PASS"}, {"unreviewed_first": "maybe"}])
def test_list_bad_params_422(params):
    assert_error(client.get("/screenings", params=params), 422, C.ERR_VALIDATION)


# ---------------------------------------------------------------- 서류 이미지
def test_document_sample(env):
    tx = submit(env, json={"sample_tx_id": SAMPLE})["tx_id"]
    for d in C.UPLOAD_DOC_TYPES:
        r = client.get(f"/screenings/{tx}/documents/{d}")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        assert r.content == (C.DOCS_DIR / SAMPLE / f"{d}.png").read_bytes()
        assert r.headers["cache-control"] == "no-store"


def test_document_errors(env):
    tx = submit(env, json={"fields": clean_fields()})["tx_id"]
    assert_error(client.get(f"/screenings/{tx}/documents/invoice"), 404, C.ERR_NOT_FOUND, tx)   # 서류 없는 건
    assert_error(client.get(f"/screenings/{tx}/documents/packing"), 422, C.ERR_VALIDATION, tx)
    assert_error(client.get("/screenings/TX-NOPE/documents/invoice"), 404, C.ERR_NOT_FOUND, "TX-NOPE")


# ---------------------------------------------------------------- 검토
def test_review_flow(env):
    tx = submit(env, json={"fields": truth_fields()})["tx_id"]
    url = f"/screenings/{tx}/review"
    assert_error(client.post(url, json={"decision": "HOLD", "reviewer": "kim"}), 409, C.ERR_CONFLICT, tx)  # 판정 전
    env.run()
    assert_error(client.post(url, json={"decision": "MAYBE", "reviewer": "kim"}), 422, C.ERR_VALIDATION, tx)
    assert_error(client.post(url, json={"decision": "HOLD", "reviewer": " "}), 422, C.ERR_VALIDATION, tx)
    assert_error(client.post(url, json={"decision": "HOLD", "reviewer": "kim", "corrected_fields": {"nope": "x"}}),
                 422, C.ERR_VALIDATION, tx)
    r = client.post(url, json={"decision": "HOLD", "reviewer": "kim", "note": "Lic -> Llc 오인식",
                               "corrected_fields": {"seller_name": "Asteria Harbor Trading Llc"}})
    assert r.status_code == 200 and r.json()["reviewer_decision"] == "HOLD"
    got = get(tx)
    assert got["reviewer_decision"] == "HOLD" and got["reviewed_at"] and got["reviewer"] == "kim"
    assert got["corrected_fields"] == {"seller_name": "Asteria Harbor Trading Llc"}
    # seller_name 을 고쳤으므로 같은 번들(B-7, list-v14)로 재판정: 'A사' 는 v14 에 없어 PASS
    assert r.json()["rescreen"] == {"classification": C.PASS, "reasons": []}
    assert got["classification"] == C.HOLD   # 원래 판정은 그대로


def test_review_failed_job_allowed(env, monkeypatch):
    """MAX_ATTEMPTS 번 실패 -> failed + MANUAL_REVIEW. 담당자가 검토 처리할 수 있어야 한다."""
    monkeypatch.setattr(W, "screen_job", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("OCR 엔진 오류")))
    tx = submit(env, json={"fields": clean_fields()})["tx_id"]
    assert env.run() == C.MAX_ATTEMPTS
    rec = get(tx)
    assert rec["status"] == C.SCREENING_FAILED and rec["classification"] == C.REVIEW
    assert rec["attempts"] == C.MAX_ATTEMPTS and rec["reasons"][-1]["rule"] == C.RULE_PROCESSING_FAILED
    assert set(rec["error"]) == set(C.ERROR_FIELDS) and rec["error"]["code"] == C.ERR_SCREENING_FAILED
    assert rec["error"]["target_id"] == tx
    r = client.post(f"/screenings/{tx}/review", json={"decision": "APPROVE", "reviewer": "lee"})
    assert r.status_code == 200 and r.json()["reviewer_decision"] == "APPROVE"
    assert get(tx)["classification"] == C.REVIEW


def test_review_while_processing_409(env):
    tx = submit(env, json={"fields": clean_fields()})["tx_id"]
    job = env.store.claim("w-api-test", tx_ids=[tx])
    assert get(tx)["status"] == C.SCREENING_PROCESSING
    assert_error(client.post(f"/screenings/{tx}/review", json={"decision": "APPROVE", "reviewer": "kim"}),
                 409, C.ERR_CONFLICT, tx)
    W.process_job(job, env.store, env.blob, "w-api-test")   # 공유 DB 에 processing 으로 남기지 않음
    assert get(tx)["status"] == C.SCREENING_COMPLETED


def test_review_unknown_404():
    assert_error(client.post("/screenings/TX-NOPE/review", json={"decision": "APPROVE", "reviewer": "k"}),
                 404, C.ERR_NOT_FOUND, "TX-NOPE")


# ---------------------------------------------------------------- 번들 고정 / 섀도
def new_listing_fields() -> dict:
    return clean_fields(seller_name=C.NEW_LISTING["name"])   # v15 에만 있는 가상 'A사'


def test_bundle_fixed_at_submission(env, bundle):
    bundle("B-7")
    tx = submit(env, json={"fields": new_listing_fields()})["tx_id"]
    bundle("B-8")                       # 접수 뒤 배포가 바뀐 상황
    tx8 = submit(env, json={"fields": new_listing_fields()})["tx_id"]
    env.run()
    rec = get(tx)
    assert rec["bundle_id"] == "B-7" and rec["list_version"] == C.LIST_BASE and rec["classification"] == C.PASS
    rec = get(tx8)
    assert rec["bundle_id"] == "B-8" and rec["list_version"] == C.LIST_NEW and rec["classification"] == C.HOLD
    assert rec["reasons"][0]["entry_id"] == C.NEW_LISTING["entry_id"]


def test_shadow_bundle(env, monkeypatch):
    monkeypatch.setattr(A, "SHADOW_BUNDLE", "B-8")
    assert client.get("/healthz").json()["shadow_bundle_id"] == "B-8"
    body = submit(env, json={"fields": new_listing_fields()})
    assert body["shadow_bundle_id"] == "B-8"
    env.run()
    rec = get(body["tx_id"])
    assert rec["shadow_bundle_id"] == "B-8" and rec["classification"] == C.PASS     # 실제 판정은 B-7
    assert [(s["bundle_id"], s["classification"]) for s in rec["shadow"]] == [("B-8", C.HOLD)]
    text = client.get("/metrics").text
    line = next(x for x in text.splitlines()
                if x.startswith(C.METRIC_SHADOW_DISAGREE + '{bundle_id="B-7",shadow_bundle_id="B-8"}'))
    assert int(line.split()[-1]) >= 1


def test_shadow_same_as_active_ignored(env, monkeypatch):
    monkeypatch.setattr(A, "SHADOW_BUNDLE", C.BASELINE_BUNDLE)
    assert submit(env, json={"fields": clean_fields()})["shadow_bundle_id"] is None


@pytest.mark.parametrize("var", ["SHADOW_BUNDLE", "ACTIVE_BUNDLE", "BUNDLE_ID"])
def test_bad_bundle_env_fails_at_startup(var):
    envv = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "SHADOW_BUNDLE", "ACTIVE_BUNDLE", "BUNDLE_ID")}
    envv[var] = "B-99"
    p = subprocess.run([sys.executable, "-c", "import a_screening.api.main"], cwd=ROOT, env=envv, capture_output=True, text=True,
                       timeout=60)
    assert p.returncode != 0 and "RuntimeError" in p.stderr and var in p.stderr



def test_bundle_id_env_preferred():
    """팀 규약 이름 BUNDLE_ID 가 있으면 그것을, 없으면 예전 이름 ACTIVE_BUNDLE 을 쓴다."""
    base = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "SHADOW_BUNDLE", "ACTIVE_BUNDLE", "BUNDLE_ID")}
    code = "import a_screening.api.main as A; print(A.ACTIVE_BUNDLE)"
    for extra, want in (({"BUNDLE_ID": "B-9", "ACTIVE_BUNDLE": "B-8"}, "B-9"), ({"ACTIVE_BUNDLE": "B-8"}, "B-8"),
                        ({}, C.BASELINE_BUNDLE)):
        p = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env={**base, **extra}, capture_output=True,
                           text=True, timeout=60)
        assert p.returncode == 0, p.stderr
        assert p.stdout.strip().splitlines()[-1] == want

# ---------------------------------------------------------------- 지표
def parse_metrics(text: str) -> tuple[dict, dict]:
    meta, samples = {}, {}
    for line in text.splitlines():
        if line.startswith("# TYPE "):
            _, _, name, kind = line.split(" ", 3)
            meta[name] = kind
        elif line and not line.startswith("#"):
            key, val = line.rsplit(" ", 1)
            samples[key] = float(val)
    return meta, samples


def test_metrics(env):
    tx = submit(env, json={"fields": truth_fields()})["tx_id"]
    env.run()
    client.post(f"/screenings/{tx}/review", json={"decision": "HOLD", "reviewer": "kim"})
    r = client.get("/metrics")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain; version=0.0.4")
    meta, s = parse_metrics(r.text)
    want_types = {C.METRIC_RESULTS: "counter", C.METRIC_JOBS: "gauge", C.METRIC_PROCESSING_SECONDS: "gauge",
                  C.METRIC_OCR_CONFIDENCE: "gauge", C.METRIC_FAILED: "counter", C.METRIC_SHADOW_DISAGREE: "counter",
                  C.METRIC_REVIEWS: "gauge"}   # contracts: 현재 판단 건수 (판단을 바꾸면 줄 수 있음)
    assert meta == want_types
    for name in want_types:
        assert f"# HELP {name} " in r.text
    for st in C.SCREENING_STATUSES:
        assert f'{C.METRIC_JOBS}{{status="{st}"}}' in s
    for d in C.REVIEW_DECISIONS:
        assert f'{C.METRIC_REVIEWS}{{decision="{d}"}}' in s
    res = s[f'{C.METRIC_RESULTS}{{bundle_id="B-7",classification="{C.HOLD}"}}']
    proc = s[f'{C.METRIC_PROCESSING_SECONDS}{{bundle_id="B-7"}}']
    if env.store.kind == "memory":
        assert res == 1 and s[f'{C.METRIC_REVIEWS}{{decision="HOLD"}}'] == 1
        assert s[f'{C.METRIC_JOBS}{{status="completed"}}'] == 1 and s[f'{C.METRIC_JOBS}{{status="queued"}}'] == 0
    else:
        assert res >= 1 and s[f'{C.METRIC_REVIEWS}{{decision="HOLD"}}'] >= 1
    assert proc >= 0


def test_metrics_label_escape():
    stats = {"results": [{"bundle_id": 'B"7\\x\ny', "classification": C.HOLD, "count": 2}],
             "jobs": {}, "processing_seconds_avg": {"B-7": 0.25}, "ocr_confidence_avg": {}, "failed": {},
             "shadow_disagreements": [], "reviews": {}}
    text = A.render_metrics(stats)
    assert f'{C.METRIC_RESULTS}{{bundle_id="B\\"7\\\\x\\ny",classification="{C.HOLD}"}} 2' in text
    assert f'{C.METRIC_PROCESSING_SECONDS}{{bundle_id="B-7"}} 0.25' in text
    assert text.endswith("\n")


def test_metrics_503_when_db_down(monkeypatch):
    monkeypatch.setattr(A, "STORE", Down())
    assert_error(client.get("/metrics"), 503, C.ERR_UNAVAILABLE)


# ---------------------------------------------------------------- 검토 화면 / 공통 에러 / 로그
def test_review_page(monkeypatch, tmp_path):
    if A.REVIEW_HTML.is_file():
        r = client.get("/review")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    monkeypatch.setattr(A, "REVIEW_HTML", tmp_path / "none.html")
    assert_error(client.get("/review"), 404, C.ERR_NOT_FOUND)


def test_common_error_format():
    assert_error(client.get("/screenings/TX-NOPE"), 404, C.ERR_NOT_FOUND, "TX-NOPE")
    assert_error(client.get("/nope"), 404, C.ERR_NOT_FOUND)
    assert_error(client.delete("/screenings/TX-NOPE"), 405, "HTTP_405", "TX-NOPE")


def test_request_log_one_line_json(env, caplog):
    logger = logging.getLogger("tradeproof.api")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.INFO, logger="tradeproof.api"):
            tx = submit(env, json={"fields": clean_fields()})["tx_id"]
            client.get(f"/screenings/{tx}")
            client.get("/screenings/TX-NOPE")
    finally:
        logger.removeHandler(caplog.handler)
    recs = [json.loads(r.getMessage()) for r in caplog.records if r.name == "tradeproof.api"]
    assert all(re.fullmatch(r"[0-9a-f]{32}", x["request_id"]) for x in recs)
    assert len({x["request_id"] for x in recs}) == len(recs)
    assert recs[0]["method"] == "POST" and recs[0]["path"] == "/screenings" and recs[0]["status"] == 202
    assert recs[0]["tx_id"] == tx and isinstance(recs[0]["duration_ms"], float)
    assert {"method": "GET", "path": f"/screenings/{tx}", "status": 200, "tx_id": tx}.items() <= recs[1].items()
    assert recs[2]["status"] == 404 and recs[2]["tx_id"] == "TX-NOPE"


# ---------------------------------------------------------------- Postgres 에서만 의미 있는 것
def test_replica_sees_same_record(env, monkeypatch):
    """복제본 A 가 접수한 건을 복제본 B(다른 저장소 객체 = 다른 프로세스 흉내)가 조회할 수 있어야 한다."""
    if env.store.kind != "postgres":
        pytest.skip("메모리 저장소는 복제본끼리 공유되지 않음 (그래서 운영은 Postgres)")
    tx = submit(env, json={"fields": truth_fields()})["tx_id"]
    W.drain(PostgresStore(TEST_DSN), env.blob, "w-other", tx_ids=[tx])   # 다른 워커 프로세스
    monkeypatch.setattr(A, "STORE", PostgresStore(TEST_DSN))
    assert get(tx)["classification"] == C.HOLD


def test_rows_land_in_shared_tables(env):
    if env.store.kind != "postgres":
        pytest.skip("Postgres 전용")
    import psycopg
    tx = submit(env, json={"fields": truth_fields()})["tx_id"]
    up = submit(env, files=doc_files(".png"))["tx_id"]
    with psycopg.connect(TEST_DSN) as conn:
        j = conn.execute("SELECT status, bundle_id, input->>'source' FROM screening_jobs WHERE tx_id = %s",
                         (up,)).fetchone()
        u = conn.execute("SELECT doc_invoice_uri, doc_bl_uri FROM transactions WHERE tx_id = %s", (up,)).fetchone()
    assert j == (C.SCREENING_QUEUED, C.BASELINE_BUNDLE, C.INPUT_UPLOAD)
    assert u == tuple(f"file://{C.blob_key(up, d, '.png')}" for d in C.UPLOAD_DOC_TYPES)
    env.run([tx])
    tf = truth_fields()
    with psycopg.connect(TEST_DSN) as conn:
        t = conn.execute("SELECT seller_name, extraction_source FROM transactions WHERE tx_id = %s", (tx,)).fetchone()
        r = conn.execute("SELECT bundle_id, classification, is_shadow FROM screening_results WHERE tx_id = %s",
                         (tx,)).fetchone()
    assert t == (tf["seller_name"], C.EXTRACTION_FROM_FIELDS)
    assert r == (C.BASELINE_BUNDLE, C.HOLD, False)


# ================================================================ 검증 지적 보강 (11/08)
def sample_of(case_type: str) -> str:
    """data/docs 에서 case_type 이 맞는 첫 샘플."""
    for d in sorted(C.DOCS_DIR.iterdir()):
        t = d / "truth.json"
        if t.exists() and json.loads(t.read_text(encoding="utf-8"))["case_type"] == case_type:
            return d.name
    pytest.skip(f"{case_type} 샘플 없음")


def memory_total(env):
    return env.store.list(limit=1)["total"] if env.store.kind == "memory" else None


# ---------------------------------------------------------------- 1. fields 접수: 스크리닝 필드 필수
@pytest.mark.parametrize("field", C.SCREENING_FIELDS)
@pytest.mark.parametrize("value", [None, "", "   ", "\t\n", "<missing>"])
def test_fields_blank_screening_field_422(env, field, value):
    """비어 있는 스크리닝 필드는 제재 매칭이 안 돼 PASS 로 샌다 (미탐 경로) -> 접수 단계에서 422."""
    f = clean_fields()
    if value == "<missing>":
        del f[field]
    else:
        f[field] = value
    before = memory_total(env)
    r = client.post("/screenings", json={"fields": f})
    assert_error(r, 422, C.ERR_VALIDATION)
    assert field in r.json()["message"]
    if before is not None:
        assert memory_total(env) == before   # 접수되지 않음


def test_fields_non_screening_blank_ok(env):
    f = clean_fields(goods_description="  ", invoice_no=None)
    tx = submit(env, json={"fields": f})["tx_id"]
    env.run()
    assert get(tx)["classification"] == C.PASS


# ---------------------------------------------------------------- 3. 업로드 크기 (요청 전체)
def multipart(parts: dict) -> tuple[bytes, str]:
    boundary = "tradeproof-test-boundary"
    out = b""
    for name, (fn, data, ctype) in parts.items():
        out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{fn}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n").encode() + data + b"\r\n"
    return out + f"--{boundary}--\r\n".encode(), f"multipart/form-data; boundary={boundary}"


def chunks(body: bytes, n: int = 1000):
    for i in range(0, len(body), n):
        yield body[i:i + n]


def test_upload_limit_from_contracts():
    if "MAX_UPLOAD_BYTES" not in os.environ:
        assert A.MAX_UPLOAD_BYTES == C.MAX_UPLOAD_BYTES
    assert A.max_request_bytes() == len(C.UPLOAD_DOC_TYPES) * A.MAX_UPLOAD_BYTES + 1024 * 1024


def test_upload_content_length_over_limit_422_before_parsing(monkeypatch):
    monkeypatch.setattr(A, "MAX_UPLOAD_BYTES", 1000)
    monkeypatch.setattr(A, "UPLOAD_SLACK_BYTES", 500)   # 상한 2500, PDF 두 장은 약 3.9KB

    def boom(*a, **k):
        raise AssertionError("Content-Length 로 거를 수 있으면 form 파싱까지 가면 안 됨")
    monkeypatch.setattr(A.StarletteRequest, "form", boom)
    r = client.post("/screenings", files=doc_files(".pdf"))
    assert int(r.request.headers["content-length"]) > A.max_request_bytes()
    assert_error(r, 422, C.ERR_VALIDATION)    # 413 이 아니라 공통 형식 422
    assert "요청 본문이 너무 큼" in r.json()["message"] and str(A.max_request_bytes()) in r.json()["message"]


def test_upload_bad_content_length_422():
    body, ctype = multipart({d: (f"{d}.png", PNG_MAGIC + b"x", "image/png") for d in C.UPLOAD_DOC_TYPES})
    r = client.post("/screenings", content=body, headers={"Content-Type": ctype, "Content-Length": "abc"})
    assert_error(r, 422, C.ERR_VALIDATION)


def test_upload_chunked_over_limit_422(env, monkeypatch):
    """Content-Length 없이(chunked) 오면 읽으면서 누적 크기를 센다."""
    monkeypatch.setattr(A, "MAX_UPLOAD_BYTES", 1000)
    monkeypatch.setattr(A, "UPLOAD_SLACK_BYTES", 500)
    body, ctype = multipart({d: (f"{d}.png", PNG_MAGIC + b"x" * 3000, "image/png") for d in C.UPLOAD_DOC_TYPES})
    before = memory_total(env)
    r = client.post("/screenings", content=chunks(body), headers={"Content-Type": ctype})
    assert "content-length" not in r.request.headers
    assert_error(r, 422, C.ERR_VALIDATION)
    assert "요청 본문이 너무 큼" in r.json()["message"]
    if before is not None:
        assert memory_total(env) == before
    assert not (env.blob.root.exists() and any(env.blob.root.rglob("*")))


def test_upload_chunked_ok(env):
    files = {d: (f"{d}.png", (C.DOCS_DIR / SAMPLE / f"{d}.png").read_bytes(), "image/png") for d in C.UPLOAD_DOC_TYPES}
    body, ctype = multipart(files)
    r = client.post("/screenings", content=chunks(body, 4096), headers={"Content-Type": ctype})
    assert r.status_code == 202, r.text
    tx = r.json()["tx_id"]
    env.mine.append(tx)
    assert get(tx)["input"]["files"]["invoice"]["size"] == len(files["invoice"][1])


# ---------------------------------------------------------------- 4. 검토 정정 재판정
def review(tx, decision, **kw):
    return client.post(f"/screenings/{tx}/review", json={"decision": decision, "reviewer": "kim", **kw})


def test_review_rescreen_blocks_approve_when_hold(env):
    tx = submit(env, json={"fields": clean_fields()})["tx_id"]
    env.run()
    assert get(tx)["classification"] == C.PASS
    t = json.loads((C.DOCS_DIR / SAMPLE / "truth.json").read_text(encoding="utf-8"))
    fix = {"seller_name": t["fields"]["seller_name"]}           # 제재 대상 이름으로 정정
    r = review(tx, "APPROVE", corrected_fields=fix)
    assert_error(r, 409, C.ERR_CONFLICT, tx)
    msg = r.json()["message"]
    assert C.HOLD in msg and C.RULE_PARTY_EXACT in msg and t["matched_entry_id"] in msg
    assert get(tx)["reviewer_decision"] is None                  # 저장 안 됨
    r = review(tx, "HOLD", corrected_fields=fix)                 # 보류는 저장 (재판정 결과 같이)
    assert r.status_code == 200, r.text
    rs = r.json()["rescreen"]
    assert rs["classification"] == C.HOLD and rs["reasons"][0]["entry_id"] == t["matched_entry_id"]
    assert all(set(x) == set(C.REASON_FIELDS) for x in rs["reasons"])
    rec = get(tx)
    assert rec["reviewer_decision"] == "HOLD" and rec["classification"] == C.PASS   # 원래 판정은 그대로
    assert "rescreen" not in rec                                  # DB 컬럼 없음 (응답에만)


def test_review_rescreen_keeps_other_original_fields(env):
    """정정하지 않은 스크리닝 필드는 원래 값으로 재판정 (seller 가 제재 대상이면 buyer 를 고쳐도 HOLD)."""
    tx = submit(env, json={"fields": truth_fields()})["tx_id"]
    env.run()
    r = review(tx, "APPROVE", corrected_fields={"buyer_name": "Some Other Buyer Ltd"})
    assert_error(r, 409, C.ERR_CONFLICT, tx)
    assert truth_fields()["seller_name"] in r.json()["message"]


@pytest.mark.parametrize("bid, other, want", [("B-7", "B-8", C.PASS), ("B-8", "B-7", C.HOLD),
                                              ("B-10", "B-7", C.HOLD)])
def test_review_rescreen_uses_submission_bundle(env, bundle, bid, other, want):
    """재판정은 그 건의 번들(list/rules) 로. 'A사' 는 list-v15 에만 있다. 배포가 바뀌어도 접수 번들 그대로."""
    bundle(bid)
    tx = submit(env, json={"fields": clean_fields()})["tx_id"]
    env.run()
    bundle(other)
    r = review(tx, "APPROVE", corrected_fields={"seller_name": C.NEW_LISTING["name"]})
    if want == C.HOLD:
        assert_error(r, 409, C.ERR_CONFLICT, tx)
        assert C.NEW_LISTING["entry_id"] in r.json()["message"]
    else:
        assert r.status_code == 200, r.text
        assert r.json()["rescreen"] == {"classification": C.PASS, "reasons": []}


def test_review_without_screening_correction_no_rescreen(env):
    tx = submit(env, json={"fields": truth_fields()})["tx_id"]
    env.run()
    r = review(tx, "REJECT", corrected_fields={"amount": "1.00"})
    assert r.status_code == 200 and r.json()["rescreen"] is None
    r = review(tx, "HOLD")
    assert r.status_code == 200 and r.json()["rescreen"] is None


@pytest.mark.parametrize("value", [None, "", "   "])
def test_review_blank_screening_correction_422(env, value):
    tx = submit(env, json={"fields": clean_fields()})["tx_id"]
    env.run()
    r = review(tx, "APPROVE", corrected_fields={"vessel_name": value})
    assert_error(r, 422, C.ERR_VALIDATION, tx)
    assert "vessel_name" in r.json()["message"] and get(tx)["reviewer_decision"] is None


def test_review_rescreen_failed_job(env, monkeypatch):
    """처리 실패 건(추출 필드 없음)도 정정 값만으로 재판정한다."""
    monkeypatch.setattr(W, "screen_job", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("OCR 엔진 오류")))
    tx = submit(env, json={"fields": clean_fields()})["tx_id"]
    env.run()
    assert get(tx)["status"] == C.SCREENING_FAILED
    r = review(tx, "APPROVE", corrected_fields={"seller_name": truth_fields()["seller_name"]})
    assert_error(r, 409, C.ERR_CONFLICT, tx)
    r = review(tx, "APPROVE", corrected_fields={"seller_name": "Plain Trading Co"})
    # 구매자·선박은 한 번도 읽히지 않았으므로 통과 후보가 아니라 검토 (승인은 담당자 권한이라 허용)
    assert r.status_code == 200 and r.json()["rescreen"]["classification"] == C.REVIEW
    full = {"seller_name": "Plain Trading Co", "buyer_name": "Another Buyer Ltd", "vessel_name": "CALM WATERS"}
    r = review(tx, "APPROVE", corrected_fields=full)
    assert r.status_code == 200 and r.json()["rescreen"]["classification"] == C.PASS


# ---------------------------------------------------------------- 5. 목록: 분류 여러 개 + 미검토 먼저
def test_list_multi_classification_unreviewed_first(env):
    hold_old = submit(env, json={"fields": truth_fields()})["tx_id"]
    rev = submit(env, json={"fields": truth_fields(sample_of(C.CASE_FUZZY))})["tx_id"]
    passed = submit(env, json={"fields": clean_fields()})["tx_id"]
    hold_new = submit(env, json={"fields": truth_fields()})["tx_id"]
    env.run()
    assert [get(t)["classification"] for t in (hold_old, rev, passed, hold_new)] == [C.HOLD, C.REVIEW, C.PASS, C.HOLD]
    assert review(hold_new, "HOLD").status_code == 200
    for cls in (f"{C.HOLD},{C.REVIEW}", f" {C.REVIEW} , {C.HOLD} ,"):
        got = client.get("/screenings", params={"classification": cls, "unreviewed_first": "true",
                                                "limit": 200}).json()
        assert all(i["classification"] in (C.HOLD, C.REVIEW) for i in got["items"])
        decided = [i["reviewer_decision"] is not None for i in got["items"]]
        assert decided == sorted(decided)                         # 미검토가 모두 앞
        if env.store.kind == "memory":
            assert [i["tx_id"] for i in got["items"]] == [rev, hold_old, hold_new] and got["total"] == 3
        else:
            assert passed not in {i["tx_id"] for i in got["items"]} and got["total"] >= 3
    if env.store.kind == "memory":
        got = client.get("/screenings", params={"classification": f"{C.HOLD},{C.REVIEW}"}).json()
        assert [i["tx_id"] for i in got["items"]] == [hold_new, rev, hold_old]   # 기본: 최신 접수순
        page = client.get("/screenings", params={"classification": f"{C.HOLD},{C.REVIEW}", "unreviewed_first": "true",
                                                 "limit": 1, "offset": 1}).json()
        assert [i["tx_id"] for i in page["items"]] == [hold_old] and page["total"] == 3   # 서버 페이지네이션


def test_list_query_used_by_review_page():
    """review.html 의 기본 '검토 필요' 질의 그대로."""
    r = client.get(f"/screenings?classification={C.HOLD},{C.REVIEW}&unreviewed_first=true&limit=20&offset=0")
    assert r.status_code == 200 and set(r.json()) == {"items", "total"}


# ---------------------------------------------------------------- 6. 500/503: 일반 문구 + 요청 ID
class _Capture:
    def __init__(self, caplog):
        self.caplog, self.logger = caplog, logging.getLogger("tradeproof.api")

    def __enter__(self):
        self.logger.addHandler(self.caplog.handler)
        self.caplog.set_level(logging.INFO, logger="tradeproof.api")
        return self

    def __exit__(self, *a):
        self.logger.removeHandler(self.caplog.handler)

    def records(self):
        return [json.loads(r.getMessage()) for r in self.caplog.records if r.name == "tradeproof.api"]


def test_request_id_header():
    a, b = client.get("/livez"), client.get("/livez")
    assert re.fullmatch(r"[0-9a-f]{32}", a.headers["x-request-id"]) and a.headers["x-request-id"] != b.headers["x-request-id"]
    assert client.get("/livez", headers={"X-Request-ID": "ingress-abc.123"}).headers["x-request-id"] == "ingress-abc.123"
    for bad in ("has space", "x" * 65, "a;b"):
        rid = client.get("/livez", headers={"X-Request-ID": bad}).headers["x-request-id"]
        assert re.fullmatch(r"[0-9a-f]{32}", rid)
    r = client.get("/screenings/TX-NOPE")     # 에러 응답에도
    assert r.status_code == 404 and re.fullmatch(r"[0-9a-f]{32}", r.headers["x-request-id"])


def test_500_generic_message_with_request_id(monkeypatch, caplog):
    class Boom(MemoryStore):
        def get(self, tx_id):
            raise RuntimeError("password=hunter2 연결 문자열 노출")
    monkeypatch.setattr(A, "STORE", Boom())
    with _Capture(caplog) as cap:
        r = client.get("/screenings/TX-BOOM")
    assert_error(r, 500, C.ERR_INTERNAL, "TX-BOOM")
    rid = r.headers["x-request-id"]
    assert rid in r.json()["message"]
    assert "hunter2" not in r.text and "RuntimeError" not in r.text
    recs = cap.records()
    err = next(x for x in recs if x.get("event") == "unhandled_error")
    assert err["request_id"] == rid and "hunter2" in err["error"] and "Traceback" in err["traceback"]
    assert err["tx_id"] == "TX-BOOM"
    req = next(x for x in recs if x.get("path") == "/screenings/TX-BOOM" and "status" in x)
    assert req["status"] == 500 and req["request_id"] == rid


def test_503_generic_message_with_request_id(monkeypatch, caplog):
    monkeypatch.setattr(A, "BLOB", Down())
    with _Capture(caplog) as cap:
        r = client.get("/healthz")
        monkeypatch.setattr(A, "STORE", Down())
        m = client.get("/metrics")
    for resp in (r, m):
        assert_error(resp, 503, C.ERR_UNAVAILABLE)
        rid = resp.headers["x-request-id"]
        assert rid in resp.json()["message"] and "연결 거부" not in resp.text and "ConnectionError" not in resp.text
        ev = next(x for x in cap.records() if x.get("event") == "unavailable" and x["request_id"] == rid)
        assert "연결 거부" in ev["error"]
    assert "원문 저장소" in r.json()["message"]


# ---------------------------------------------------------------- 7. GET /meta
def test_meta(monkeypatch):
    monkeypatch.setattr(A, "SHADOW_BUNDLE", "B-8")
    m = client.get("/meta").json()
    assert m["tx_fields"] == list(C.TX_FIELDS) and m["screening_fields"] == list(C.SCREENING_FIELDS)
    assert m["classifications"] == list(C.CLASSIFICATIONS) and m["review_decisions"] == list(C.REVIEW_DECISIONS)
    assert m["statuses"] == list(C.SCREENING_STATUSES) and m["doc_types"] == list(C.UPLOAD_DOC_TYPES)
    assert m["ocr_confidence_min"] == C.OCR_CONFIDENCE_MIN
    assert set(m["rules"]) == set(C.RULES) | set(C.RULES_V3_EXTRA) | {C.RULE_PROCESSING_FAILED}
    assert {C.RULE_PARTY_OTHER_DOC, C.RULE_VESSEL_OTHER_DOC} <= set(m["rules"])
    assert m["bundles"] == C.BUNDLES and m["active_bundle"] == C.BASELINE_BUNDLE and m["shadow_bundle"] == "B-8"
    assert m["classification_roles"] == {"pass": C.PASS, "review": C.REVIEW, "hold": C.HOLD}
    assert m["reviewable_statuses"] == [C.SCREENING_COMPLETED, C.SCREENING_FAILED]
    assert set(m["decision_roles"].values()) <= set(C.REVIEW_DECISIONS)
    assert set(m["status_roles"].values()) == set(C.SCREENING_STATUSES)


# ---------------------------------------------------------------- 2·7. 검토 화면 (review.html)
def review_script() -> str:
    html = A.REVIEW_HTML.read_text(encoding="utf-8")
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert len(scripts) == 1
    return scripts[0]


def test_review_page_served():
    r = client.get("/review")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "<script>" in r.text


def test_review_script_syntax(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node 없음")
    js = tmp_path / "review.js"
    js.write_text(review_script(), encoding="utf-8")
    p = subprocess.run([node, "--check", str(js)], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr


def test_review_script_can_review_matches_api(tmp_path):
    """판단 버튼 활성 조건(canReview) = API 조건: 분류가 있고 status 가 /meta reviewable_statuses."""
    node = shutil.which("node")
    if not node:
        pytest.skip("node 없음")
    fn = re.search(r"function canReview\(d\) \{.*?\n\}", review_script(), re.S).group(0)
    meta = client.get("/meta").json()
    cases = [{"status": s, "classification": c} for s in C.SCREENING_STATUSES for c in (None, *C.CLASSIFICATIONS)]
    js = tmp_path / "can.js"
    js.write_text(f"const C = {{ REVIEWABLE: {json.dumps(meta['reviewable_statuses'])} }};\n{fn}\n"
                  f"console.log(JSON.stringify({json.dumps(cases)}.map(canReview)));", encoding="utf-8")
    p = subprocess.run([node, str(js)], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    want = [c["classification"] is not None and c["status"] in (C.SCREENING_COMPLETED, C.SCREENING_FAILED)
            for c in cases]
    assert json.loads(p.stdout) == want


def test_review_script_uses_meta_and_server_paging():
    js = review_script()
    assert 'api("/meta")' in js and "unreviewed_first" in js and "[C.HOLD, C.REVIEW].join(\",\")" in js
    assert "sortItems" not in js                                       # 정렬은 서버 (페이지 안 재정렬 없음)
    assert '"seller_name", "buyer_name"' not in js and "85.0" not in js   # 계약 값 하드코딩 없음
    labels = re.search(r"RULE_LABELS: \{(.*?)\n  \}", js, re.S).group(1)
    have = set(re.findall(r"(\w+):", labels))
    assert set(client.get("/meta").json()["rules"]) <= have            # 모든 규칙에 한국어 설명
    assert "다른 서류 값으로 일치" in labels
    assert "s.error" in js                                              # 섀도 실패 표시


# ---------------------------------------------------------------- 통합 담당이 재검증 지적으로 추가 (11/08)
@pytest.mark.parametrize("blank", ["-", ".", "​", "  ", "!!"])
def test_fields_symbol_only_screening_value_rejected(blank):
    fields = {f: None for f in C.TX_FIELDS} | {"seller_name": blank, "buyer_name": "Buyer Ltd", "vessel_name": "CALM WATERS"}
    r = client.post("/screenings", json={"fields": fields})
    assert r.status_code == 422 and r.json()["code"] == C.ERR_VALIDATION


def test_json_body_too_large_422():
    big = {"fields": {"seller_name": "X" * (70 * 1024), "buyer_name": "B", "vessel_name": "V"}}
    r = client.post("/screenings", json=big)
    assert r.status_code == 422 and r.json()["code"] == C.ERR_VALIDATION


def test_review_two_step_approve_bypass_blocked(env):
    """정정을 HOLD 로 저장한 뒤 정정 없이 APPROVE 해도 누적된 정정으로 재판정해 막는다 (재검증 지적)."""
    tx = submit(env, json={"fields": clean_fields()})["tx_id"]
    env.run()
    t = json.loads((C.DOCS_DIR / SAMPLE / "truth.json").read_text(encoding="utf-8"))
    fix = {"seller_name": t["fields"]["seller_name"]}
    assert review(tx, "REJECT", corrected_fields=fix).status_code == 200
    assert_error(review(tx, "APPROVE"), 409, C.ERR_CONFLICT, tx)
    assert get(tx)["corrected_fields"] == fix                     # 정정 근거가 지워지지 않음


def test_review_rescreen_unread_screening_field_is_review(env, monkeypatch):
    """처리 실패 건처럼 읽힌 적 없는 스크리닝 필드가 남으면 재판정은 통과 후보가 아니다."""
    tx = submit(env, json={"fields": clean_fields()})["tx_id"]
    env.run()
    rec = get(tx)
    rec_no_fields = {**rec, "fields": None}
    rs = A.rescreen(rec_no_fields, {"seller_name": "Some Clean Trading Co"})
    assert rs["classification"] == C.REVIEW
    assert {r["field"] for r in rs["reasons"] if r["rule"] == C.RULE_MISSING_FIELD} == {"buyer_name", "vessel_name"}
