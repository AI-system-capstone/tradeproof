"""ocr-worker + 큐 저장소 테스트 (실제 tesseract OCR 을 돌린다: 수십 초).

  python -m pytest ocr/test_worker.py -q                       # 메모리 저장소
  TEST_DATABASE_URL=postgresql://tradeproof:tradeproof@localhost:55432/tradeproof python -m pytest ocr/test_worker.py -q
                                                               # + Postgres (schema.sql 적재된 DB, 공유 DB 라 TRUNCATE 안 함)
Postgres 는 다른 사람 건을 건드리지 않게 이 테스트가 만든 tx_id 만 claim 한다 (claim(tx_ids=...)).
호스트 tesseract 버전에 따라 OCR 값이 달라질 수 있어 data/ocr/*/extracted.json 과 값 비교는 하지 않는다.
"""
from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common import contracts as C  # noqa: E402
from common.blob import LocalBlobStore  # noqa: E402
from a_screening.store import MemoryStore, PostgresStore  # noqa: E402
from a_screening.ocr import worker as W  # noqa: E402

TEST_DSN = os.environ.get("TEST_DATABASE_URL")
SAMPLE = "TX-0001"     # exact_match: list-v14/v15 모두 HIGH_RISK_HOLD
NEW_LISTING_NAME = C.NEW_LISTING["name"]   # v15 에만 있는 가상 'A사'
pytestmark = pytest.mark.skipif(not (C.DOCS_DIR / SAMPLE / "invoice.png").exists(),
                                reason="data/docs 없음: ./run_all.sh 먼저 실행")


class Env:
    def __init__(self, kind, store, blob):
        self.kind, self.store, self.blob = kind, store, blob
        self.mine: list[str] = []

    def new_tx(self) -> str:
        tx = f"TX-WTEST-{uuid.uuid4().hex[:10].upper()}"
        self.mine.append(tx)
        return tx

    def create(self, input_, bundle_id=C.BASELINE_BUNDLE, shadow=None, inv=None, bl=None, tx_id=None) -> str:
        tx = tx_id or self.new_tx()
        self.store.create(tx, bundle_id, input_, doc_invoice_uri=inv, doc_bl_uri=bl, shadow_bundle_id=shadow)
        return tx

    def run(self, tx_ids=None) -> int:
        """이 테스트가 만든 건만 처리 (공유 DB 의 다른 건은 건드리지 않음)."""
        return W.drain(self.store, self.blob, "w-test", tx_ids=tx_ids or self.mine)

    def once(self, tx_id) -> bool:
        return W.run_once(self.store, self.blob, "w-test", tx_ids=[tx_id])


@pytest.fixture(params=["memory", "postgres"])
def env(request, tmp_path):
    blob = LocalBlobStore(tmp_path / "blob")
    if request.param == "postgres":
        if not TEST_DSN:
            pytest.skip("TEST_DATABASE_URL 없음")
        return Env("postgres", PostgresStore(TEST_DSN), blob)
    return Env("memory", MemoryStore(), blob)


def sample_input(tx=SAMPLE):
    return {"source": C.INPUT_SAMPLE, "sample_tx_id": tx, "fields": None}


def fields_input(**over):
    f = {k: None for k in C.TX_FIELDS}
    f.update(seller_name="Plain Trading Co", buyer_name="Ordinary Goods Ltd", vessel_name="CALM WATERS")
    f.update(over)
    return {"source": C.INPUT_FIELDS, "sample_tx_id": None, "fields": f}


def upload(env, tx, ext):
    """data/docs/TX-0001 원문 바이트를 업로드한 것처럼 저장소에 넣는다."""
    ctype = {v: k for k, v in C.UPLOAD_CONTENT_TYPES.items()}[ext]
    return [env.blob.put(C.blob_key(tx, d, ext), (C.DOCS_DIR / SAMPLE / f"{d}{ext}").read_bytes(), ctype)
            for d in C.UPLOAD_DOC_TYPES]


def assert_reasons_ok(rec):
    for r in rec["reasons"]:
        assert set(r) == set(C.REASON_FIELDS)


# ---------------------------------------------------------------- 정상 처리
def test_sample_b7_hold(env):
    tx = env.create(sample_input())
    assert env.store.get(tx)["status"] == C.SCREENING_QUEUED
    assert env.run() == 1
    rec = env.store.get(tx)
    assert rec["status"] == C.SCREENING_COMPLETED and rec["attempts"] == 1 and rec["worker_id"] == "w-test"
    assert rec["classification"] == C.HOLD
    assert rec["extraction_source"] == C.extraction_source_ocr(C.BUNDLES["B-7"]["model_version"])
    assert any(r["rule"] in (C.RULE_PARTY_EXACT, C.RULE_VESSEL_EXACT) for r in rec["reasons"])
    assert_reasons_ok(rec)
    # OCR 결과라 bbox·confidence 가 있다
    assert rec["extracted_fields"]["buyer_name"]["confidence"] is not None
    assert rec["shadow"] == [] and rec["shadow_bundle_id"] is None and rec["error"] is None


def test_fields_job_no_ocr(env, monkeypatch):
    monkeypatch.setattr(W, "run_extraction", lambda *a: pytest.fail("fields 건은 OCR 하면 안 됨"))
    tx = env.create(fields_input())
    env.run()
    rec = env.store.get(tx)
    assert rec["status"] == C.SCREENING_COMPLETED and rec["classification"] == C.PASS
    assert rec["extraction_source"] == C.EXTRACTION_FROM_FIELDS and rec["conflicts"] == []
    assert rec["extracted_fields"]["seller_name"] == {"value": "Plain Trading Co", "source_doc": None,
                                                      "bbox": None, "confidence": None}


def test_upload_pdf_renders_and_stores_png(env):
    tx = env.new_tx()
    inv, bl = upload(env, tx, ".pdf")
    env.create({"source": C.INPUT_UPLOAD, "sample_tx_id": None, "fields": None}, inv=inv, bl=bl, tx_id=tx)
    env.run()
    rec = env.store.get(tx)
    assert rec["status"] == C.SCREENING_COMPLETED, rec["error"]
    assert rec["classification"] == C.HOLD
    assert rec["documents"] == {"invoice": inv, "bl": bl}
    for d in C.UPLOAD_DOC_TYPES:   # 검토 화면용 렌더링 PNG
        png = env.blob.get(f"file://{C.blob_key(tx, d, '.png')}")
        assert png.startswith(C.FILE_MAGIC[".png"])


def test_upload_png(env):
    tx = env.new_tx()
    inv, bl = upload(env, tx, ".png")
    env.create({"source": C.INPUT_UPLOAD, "sample_tx_id": None, "fields": None}, inv=inv, bl=bl, tx_id=tx)
    env.run()
    rec = env.store.get(tx)
    assert rec["status"] == C.SCREENING_COMPLETED, rec["error"]
    assert rec["classification"] == C.HOLD
    assert rec["extraction_source"] == C.extraction_source_ocr(C.BUNDLES[C.BASELINE_BUNDLE]["model_version"])


def test_upload_bad_format_goes_to_review(env, tmp_path):
    """원문 형식이 틀리면 다시 해도 같다: 재시도 없이 바로 failed + 담당자 검토."""
    tx = env.new_tx()
    uri = env.blob.put(C.blob_key(tx, "invoice", ".pdf"), b"not a document", "application/pdf")
    env.create({"source": C.INPUT_UPLOAD, "sample_tx_id": None, "fields": None}, inv=uri, tx_id=tx)
    env.run()
    rec = env.store.get(tx)
    assert rec["status"] == C.SCREENING_FAILED and rec["attempts"] == 1
    assert rec["classification"] == C.REVIEW and rec["reasons"][-1]["rule"] == C.RULE_PROCESSING_FAILED


# ---------------------------------------------------------------- 섀도 판정
def test_shadow_b9_runs_ocr_per_model(env, monkeypatch):
    calls = []
    real = W.run_extraction

    def spy(tx_dir, model):
        calls.append(model)
        return real(tx_dir, model)

    monkeypatch.setattr(W, "run_extraction", spy)
    tx = env.create(sample_input(), bundle_id="B-7", shadow="B-9")
    env.run()
    assert sorted(calls) == sorted([C.BUNDLES["B-7"]["model_version"], C.BUNDLES["B-9"]["model_version"]])
    rec = env.store.get(tx)
    assert rec["status"] == C.SCREENING_COMPLETED and rec["bundle_id"] == "B-7" and rec["shadow_bundle_id"] == "B-9"
    assert rec["classification"] == C.HOLD                       # 주 번들 결과만 실제 판정
    assert rec["extraction_source"] == C.extraction_source_ocr(C.BUNDLES["B-7"]["model_version"])
    assert len(rec["shadow"]) == 1 and rec["shadow"][0]["bundle_id"] == "B-9"
    assert rec["shadow"][0]["classification"] in C.CLASSIFICATIONS
    assert rec["shadow"][0]["error"] is None


def test_shadow_same_model_ocr_once(env, monkeypatch):
    calls = []
    real = W.run_extraction
    monkeypatch.setattr(W, "run_extraction", lambda d, m: calls.append(m) or real(d, m))
    tx = env.create(sample_input(), bundle_id="B-7", shadow="B-8")
    env.run()
    assert calls == [C.MODEL_BASE]
    assert env.store.get(tx)["shadow"][0]["bundle_id"] == "B-8"


def test_shadow_disagreement_counted(env):
    """v15 에만 있는 'A사': 주 B-7(list-v14) PASS, 섀도 B-8(list-v15) HOLD -> 불일치 1건."""
    before = {(d["bundle_id"], d["shadow_bundle_id"]): d["count"] for d in env.store.stats()["shadow_disagreements"]}
    tx = env.create(fields_input(buyer_name=NEW_LISTING_NAME), bundle_id="B-7", shadow="B-8")
    env.run()
    rec = env.store.get(tx)
    assert rec["classification"] == C.PASS and rec["shadow"][0]["classification"] == C.HOLD
    after = {(d["bundle_id"], d["shadow_bundle_id"]): d["count"] for d in env.store.stats()["shadow_disagreements"]}
    assert after[("B-7", "B-8")] == before.get(("B-7", "B-8"), 0) + 1


def test_shadow_must_differ(env):
    with pytest.raises(ValueError):
        env.store.create(env.new_tx(), "B-7", fields_input(), shadow_bundle_id="B-7")


# ---------------------------------------------------------------- 재시도 / 실패
def test_retry_then_manual_review(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("OCR 엔진 응답 없음")

    monkeypatch.setattr(W, "screen", boom)
    tx = env.create(fields_input())
    for i in range(1, C.MAX_ATTEMPTS):
        assert env.once(tx)
        rec = env.store.get(tx)
        assert rec["status"] == C.SCREENING_QUEUED and rec["attempts"] == i
        assert rec["error"]["code"] == C.ERR_SCREENING_FAILED and rec["error"]["target_id"] == tx
    assert env.once(tx)
    rec = env.store.get(tx)
    assert rec["status"] == C.SCREENING_FAILED and rec["attempts"] == C.MAX_ATTEMPTS
    assert set(rec["error"]) == set(C.ERROR_FIELDS) and "OCR 엔진 응답 없음" in rec["error"]["message"]
    assert rec["classification"] == C.REVIEW
    assert rec["reasons"][-1]["rule"] == C.RULE_PROCESSING_FAILED
    assert rec["reasons"][-1]["classification"] == C.REVIEW and rec["reasons"][-1]["value"]
    assert_reasons_ok(rec)
    assert not env.once(tx)   # 더 이상 큐에 없음


def test_give_up_never_downgrades_hold(env):
    """이미 HOLD 로 저장된 건이 나중에 실패 처리돼도 HOLD 유지 (미탐 방지)."""
    tx = env.create(fields_input())
    job = env.store.claim("w-test", tx_ids=[tx])
    hold = {"classification": C.HOLD, "reasons": [dict(zip(C.REASON_FIELDS, (
        C.RULE_PARTY_EXACT, "buyer_name", "X", "E-1", "X", 1.0, C.HOLD)))]}
    extracted = {f: {"value": v, "source_doc": None, "bbox": None, "confidence": None}
                 for f, v in fields_input()["fields"].items()}
    env.store.complete(tx, extracted, C.EXTRACTION_FROM_FIELDS, [], hold)
    env.store.give_up(job["tx_id"], W.error_body("저장 실패", tx))
    rec = env.store.get(tx)
    assert rec["status"] == C.SCREENING_FAILED and rec["classification"] == C.HOLD
    assert [r["rule"] for r in rec["reasons"]] == [C.RULE_PARTY_EXACT, C.RULE_PROCESSING_FAILED]


def test_release_stale(env):
    tx = env.create(fields_input())
    assert env.store.claim("dead-worker", tx_ids=[tx])["attempts"] == 1
    assert env.store.release_stale(3600, tx_ids=[tx]) == []          # 아직 안 오래됨
    assert env.store.release_stale(-1, tx_ids=[tx]) == [(tx, C.SCREENING_QUEUED)]   # 죽은 워커로 보고 되돌림
    assert env.store.get(tx)["status"] == C.SCREENING_QUEUED
    env.run()
    rec = env.store.get(tx)
    assert rec["status"] == C.SCREENING_COMPLETED and rec["attempts"] == 2


# ---------------------------------------------------------------- 동시성
def test_concurrent_claim_no_overlap(env):
    txs = [env.create(fields_input()) for _ in range(12)]
    got: dict[str, list] = {"a": [], "b": [], "c": []}

    def grab(name):
        while True:
            job = env.store.claim(name, tx_ids=txs)
            if job is None:
                return
            got[name].append(job["tx_id"])

    th = [threading.Thread(target=grab, args=(n,)) for n in got]
    for t in th:
        t.start()
    for t in th:
        t.join()
    allgot = [x for v in got.values() for x in v]
    assert sorted(allgot) == sorted(txs) and len(set(allgot)) == len(allgot)
    for tx in txs:
        assert env.store.get(tx)["attempts"] == 1


def test_claim_oldest_first(env):
    a, b = env.create(fields_input()), env.create(fields_input())
    assert env.store.claim("w", tx_ids=[a, b])["tx_id"] == a
    assert env.store.claim("w", tx_ids=[a, b])["tx_id"] == b


def test_claim_job_shape(env):
    tx = env.create(fields_input(), shadow="B-8", inv="file://x/invoice.pdf")
    job = env.store.claim("w", tx_ids=[tx])
    assert set(job) == {"tx_id", "bundle_id", "shadow_bundle_id", "input", "attempts", "doc_invoice_uri",
                        "doc_bl_uri"}
    assert job["shadow_bundle_id"] == "B-8" and job["doc_invoice_uri"] == "file://x/invoice.pdf"
    assert job["input"]["source"] == C.INPUT_FIELDS


# ---------------------------------------------------------------- 목록 / 지표
def test_list_shape_and_filters(env):
    a = env.create(fields_input(seller_name="LIST SELLER"))
    b = env.create(fields_input(buyer_name=NEW_LISTING_NAME), bundle_id="B-8")
    env.run()
    page = env.store.list(limit=500)
    assert set(page) == {"items", "total"} and page["total"] >= 2
    keys = {"tx_id", "status", "bundle_id", "classification", "submitted_at", "completed_at", "reviewer_decision",
            "seller_name", "buyer_name", "vessel_name"}
    assert all(set(i) == keys for i in page["items"])
    ids = [i["tx_id"] for i in page["items"]]
    assert ids.index(b) < ids.index(a)                     # 최신 접수 먼저
    row = next(i for i in page["items"] if i["tx_id"] == a)
    assert row["seller_name"] == "LIST SELLER" and row["classification"] == C.PASS
    held = env.store.list(classification=C.HOLD, bundle_id="B-8", limit=500)
    assert b in [i["tx_id"] for i in held["items"]] and all(i["classification"] == C.HOLD for i in held["items"])
    assert env.store.list(status=C.SCREENING_COMPLETED, limit=1)["items"].__len__() == 1
    with pytest.raises(ValueError):
        env.store.list(status="nope")


def test_stats_shape(env):
    tx = env.create(sample_input())
    env.run()
    env.store.review(tx, "HOLD", "kim", "확인", None)
    s = env.store.stats()
    assert set(s) == {"results", "jobs", "processing_seconds_avg", "ocr_confidence_avg", "failed",
                      "shadow_disagreements", "reviews"}
    assert all(set(r) == {"bundle_id", "classification", "count"} for r in s["results"])
    assert any(r["bundle_id"] == "B-7" and r["classification"] == C.HOLD and r["count"] >= 1 for r in s["results"])
    assert set(s["jobs"]) >= set(C.SCREENING_STATUSES) and s["jobs"][C.SCREENING_COMPLETED] >= 1
    assert isinstance(s["processing_seconds_avg"]["B-7"], float) and s["processing_seconds_avg"]["B-7"] >= 0
    assert 0 <= s["ocr_confidence_avg"]["B-7"] <= 100
    assert s["reviews"]["HOLD"] >= 1
    assert all(set(d) == {"bundle_id", "shadow_bundle_id", "count"} for d in s["shadow_disagreements"])


def test_stats_failed_counted(env, monkeypatch):
    monkeypatch.setattr(W, "screen", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    before = env.store.stats()["failed"].get("B-9", 0)
    env.create(fields_input(), bundle_id="B-9")
    env.run()
    assert env.store.stats()["failed"]["B-9"] == before + 1


# ---------------------------------------------------------------- 기존 API 경로 호환 (api/app.py 가 아직 쓰는 것)
def test_legacy_start_and_fail_still_work(env):
    tx = env.create(fields_input())
    job = env.store.start(tx)
    assert job["tx_id"] == tx and job["bundle_id"] == "B-7" and job["input"]["source"] == C.INPUT_FIELDS
    env.store.fail(tx, W.error_body("x", tx))
    assert env.store.get(tx)["status"] == C.SCREENING_FAILED


# ---------------------------------------------------------------- run_forever 종료
def test_run_forever_stops(env):
    tx = env.create(fields_input())
    stop = threading.Event()

    class OnlyMine:   # 공유 DB 에서 다른 건을 잡지 않도록 claim 범위를 제한
        kind = env.store.kind

        def claim(self, worker_id):
            job = env.store.claim(worker_id, tx_ids=[tx])
            if job is None:
                stop.set()
            return job

        def release_stale(self, secs):
            return env.store.release_stale(secs, tx_ids=[tx])

        def __getattr__(self, name):
            return getattr(env.store, name)

    n = W.run_forever(OnlyMine(), env.blob, "w-test", poll_interval=0.01, stop=stop)
    assert n == 1 and env.store.get(tx)["status"] == C.SCREENING_COMPLETED


def test_cli_requires_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert W.main(["--once"]) == 2


# ---------------------------------------------------------------- 섀도 격리 (11/08 검증 지적 1)
def _shadow_raises(monkeypatch, events=None):
    """rules-v3 판정(섀도 B-10)만 실패하게. events 에 판정 순서를 남긴다."""
    real = W.screen

    def fake(fields, index, rules_version=None, ocr=None):
        if events is not None:
            events.append(("screen", rules_version))
        if rules_version == C.RULES_NEW:
            raise RuntimeError("섀도 판정 엔진 오류")
        return real(fields, index, rules_version=rules_version, ocr=ocr)

    monkeypatch.setattr(W, "screen", fake)


def test_shadow_failure_does_not_touch_primary(env, monkeypatch):
    events = []
    _shadow_raises(monkeypatch, events)
    real_complete = env.store.complete
    monkeypatch.setattr(env.store, "complete", lambda *a, **k: events.append(("complete",)) or real_complete(*a, **k))
    before = {(d["bundle_id"], d["shadow_bundle_id"]): d["count"] for d in env.store.stats()["shadow_disagreements"]}
    tx = env.create(fields_input(), bundle_id="B-7", shadow="B-10")
    assert env.run() == 1
    # 주 번들 판정 -> complete -> 섀도 판정 순서 (처리 시간 지표에 섀도 시간이 안 들어감)
    assert events == [("screen", C.RULES_BASE), ("complete",), ("screen", C.RULES_NEW)]
    rec = env.store.get(tx)
    assert rec["status"] == C.SCREENING_COMPLETED and rec["attempts"] == 1 and rec["error"] is None
    assert rec["classification"] == C.PASS
    [sh] = rec["shadow"]
    assert sh["bundle_id"] == "B-10" and sh["classification"] == C.REVIEW
    assert sh["error"]["code"] == C.ERR_SCREENING_FAILED and sh["error"]["target_id"] == tx
    assert "섀도 판정 엔진 오류" in sh["error"]["message"]
    assert sh["reasons"][-1]["rule"] == C.RULE_PROCESSING_FAILED
    # 섀도 실패는 불일치 지표에 안 들어간다
    after = {(d["bundle_id"], d["shadow_bundle_id"]): d["count"] for d in env.store.stats()["shadow_disagreements"]}
    assert after.get(("B-7", "B-10"), 0) == before.get(("B-7", "B-10"), 0)


# ---------------------------------------------------------------- 펜싱 (지적 2)
def test_fencing_reclaimed_job(env):
    """w1 claim -> 회수 -> w2 claim -> w1 의 늦은 결과는 버려지고 w2 결과만 남는다."""
    tx = env.create(fields_input(), shadow="B-8")
    j1 = env.store.claim("w1", tx_ids=[tx])
    assert env.store.release_stale(0, tx_ids=[tx]) == [(tx, C.SCREENING_QUEUED)]
    j2 = env.store.claim("w2", tx_ids=[tx])
    assert (j1["attempts"], j2["attempts"]) == (1, 2)
    hold = {"classification": C.HOLD, "reasons": [dict(zip(C.REASON_FIELDS, (
        C.RULE_PARTY_EXACT, "buyer_name", "X", "E-1", "X", 1.0, C.HOLD)))]}
    extracted = {f: {"value": v, "source_doc": None, "bbox": None, "confidence": None}
                 for f, v in fields_input()["fields"].items()}
    old = {"worker_id": "w1", "attempts": j1["attempts"]}
    err = W.error_body("늦은 실패", tx)
    assert env.store.complete(tx, extracted, C.EXTRACTION_FROM_FIELDS, [], hold, **old) is False
    assert env.store.add_shadow_result(tx, "B-8", hold, **old) is False
    assert env.store.requeue(tx, err, **old) is False
    assert env.store.give_up(tx, err, **old) is False
    assert env.store.complete(tx, extracted, C.EXTRACTION_FROM_FIELDS, [], hold, worker_id="w2", attempts=1) is False
    rec = env.store.get(tx)
    assert rec["status"] == C.SCREENING_PROCESSING and rec["worker_id"] == "w2" and rec["classification"] is None
    assert rec["shadow"] == []
    # 워커 경로로도: w1 처리 결과는 버려짐, w2 결과만 남음
    out1 = W.process_job(j1, env.store, env.blob, "w1")
    assert out1["status"] is None and out1["discarded"]
    out2 = W.process_job(j2, env.store, env.blob, "w2")
    assert out2["status"] == C.SCREENING_COMPLETED
    rec = env.store.get(tx)
    assert rec["status"] == C.SCREENING_COMPLETED and rec["worker_id"] == "w2" and rec["attempts"] == 2
    assert rec["classification"] == C.PASS and [s["bundle_id"] for s in rec["shadow"]] == ["B-8"]


def test_fencing_failure_path(env, monkeypatch):
    """회수된 뒤 옛 워커가 실패해도 requeue/give_up 이 새 워커 건을 건드리지 않는다."""
    monkeypatch.setattr(W, "screen", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    tx = env.create(fields_input())
    j1 = env.store.claim("w1", tx_ids=[tx])
    env.store.release_stale(0, tx_ids=[tx])
    env.store.claim("w2", tx_ids=[tx])
    out = W.process_job(j1, env.store, env.blob, "w1")
    assert out["discarded"] and out["status"] is None
    rec = env.store.get(tx)
    assert rec["status"] == C.SCREENING_PROCESSING and rec["worker_id"] == "w2" and rec["attempts"] == 2
    env.store.release_stale(0, tx_ids=[tx])   # 공유 DB 에 processing 으로 남기지 않음
    monkeypatch.undo()
    env.run([tx])
    assert env.store.get(tx)["status"] == C.SCREENING_COMPLETED


# ---------------------------------------------------------------- 회수 (지적 4)
def test_release_stale_give_up(env):
    tx = env.create(fields_input())
    for _ in range(C.MAX_ATTEMPTS - 1):
        env.store.claim("dead", tx_ids=[tx])
        assert env.store.release_stale(-1, tx_ids=[tx]) == [(tx, C.SCREENING_QUEUED)]
    assert env.store.claim("dead", tx_ids=[tx])["attempts"] == C.MAX_ATTEMPTS
    assert W.release_stale(env.store, "w-test", 0, tx_ids=[tx]) == [(tx, C.SCREENING_FAILED)]
    rec = env.store.get(tx)
    assert rec["status"] == C.SCREENING_FAILED and rec["classification"] == C.REVIEW
    assert rec["reasons"][-1]["rule"] == C.RULE_PROCESSING_FAILED


def test_drain_releases_stale_first(env):
    tx = env.create(fields_input())
    env.store.claim("dead", tx_ids=[tx])
    time.sleep(0.2)
    assert env.run([tx]) == 0                                       # 기본 기준(600초)으로는 아직 안 회수
    assert env.store.get(tx)["status"] == C.SCREENING_PROCESSING
    assert W.drain(env.store, env.blob, "w-test", tx_ids=[tx], stale_after=0.05) == 1
    rec = env.store.get(tx)
    assert rec["status"] == C.SCREENING_COMPLETED and rec["attempts"] == 2 and rec["worker_id"] == "w-test"


# ---------------------------------------------------------------- 지표: 심사 서비스 접수 건만 (지적 5)
def test_stats_exclude_rows_without_job(env):
    """시드·Replay 처럼 screening_jobs 없이 screening_results 만 있는 줄은 results/reviews 에 안 센다."""
    if env.kind != "postgres":
        pytest.skip("메모리 저장소에는 시드·Replay 행이 없음")
    import psycopg
    from psycopg.types.json import Jsonb
    tx = env.new_tx().replace("WTEST", "WSEED")
    key = lambda s: ({(r["bundle_id"], r["classification"]): r["count"] for r in s["results"]}, s["reviews"])
    before = key(env.store.stats())
    with psycopg.connect(TEST_DSN) as conn:
        conn.execute("INSERT INTO transactions (tx_id, received_at, extracted_fields, extraction_source) "
                     "VALUES (%s, now(), '{}'::jsonb, 'ground_truth')", (tx,))
        conn.execute("INSERT INTO screening_results (tx_id, bundle_id, classification, reasons, reviewer_decision) "
                     "VALUES (%s, 'B-10', %s, %s, 'REJECT')", (tx, C.HOLD, Jsonb([])))
    try:
        assert key(env.store.stats()) == before
    finally:
        with psycopg.connect(TEST_DSN) as conn:   # 이 테스트가 넣은 줄만 지운다
            conn.execute("DELETE FROM screening_results WHERE tx_id = %s", (tx,))
            conn.execute("DELETE FROM transactions WHERE tx_id = %s", (tx,))


# ---------------------------------------------------------------- 목록 필터 (지적 6)
def test_list_multi_classification_and_unreviewed_first(env):
    a = env.create(fields_input())
    b = env.create(fields_input(buyer_name=NEW_LISTING_NAME), bundle_id="B-8")
    c = env.create(fields_input())
    env.run()
    env.store.review(c, "APPROVE", "kim", None, None)
    mine = {a, b, c}
    for flt in ([C.PASS, C.HOLD], (C.PASS, C.HOLD), f"{C.PASS},{C.HOLD}", f" {C.HOLD} , {C.PASS} "):
        n = env.store.list(classification=flt, limit=1)["total"]
        page = env.store.list(classification=flt, limit=n + 1000)
        assert mine <= {i["tx_id"] for i in page["items"]}
        assert all(i["classification"] in (C.PASS, C.HOLD) for i in page["items"])
        if env.kind == "memory":
            assert page["total"] == len(page["items"]) == 3
    only = env.store.list(classification=C.HOLD, limit=10_000)
    assert b in {i["tx_id"] for i in only["items"]} and not ({a, c} & {i["tx_id"] for i in only["items"]})
    for bad in ("NOPE", [C.PASS, "NOPE"], 3):
        with pytest.raises(ValueError):
            env.store.list(classification=bad)
    n = env.store.list(limit=1)["total"]
    items = env.store.list(unreviewed_first=True, limit=n + 1000)["items"]
    flags = [i["reviewer_decision"] is not None for i in items]
    assert flags == sorted(flags)                               # 검토 안 된 것 먼저
    ids = [i["tx_id"] for i in items]
    assert ids.index(b) < ids.index(a) < ids.index(c)           # 그 안에서는 최신 접수순
    ids = [i["tx_id"] for i in env.store.list(limit=n + 1000)["items"]]
    assert ids.index(c) < ids.index(b) < ids.index(a)           # 기본은 최신 접수순 그대로


# ---------------------------------------------------------------- 재시도 무의미한 입력 (지적 7, 8)
@pytest.mark.parametrize("over", [{"vessel_name": None}, {"seller_name": "   "}, {"buyer_name": 123}])
def test_fields_missing_screening_field_gives_up_now(env, monkeypatch, over):
    monkeypatch.setattr(W, "screen", lambda *a, **k: pytest.fail("스크리닝 필드가 없으면 판정하지 않음"))
    tx = env.create(fields_input(**over))
    assert env.run([tx]) == 1
    rec = env.store.get(tx)
    assert rec["status"] == C.SCREENING_FAILED and rec["attempts"] == 1 and rec["classification"] == C.REVIEW
    assert rec["reasons"][-1]["rule"] == C.RULE_PROCESSING_FAILED
    assert "PermanentJobError" in rec["error"]["message"] and next(iter(over)) in rec["error"]["message"]


@pytest.mark.parametrize("inp", [
    {"source": C.INPUT_SAMPLE, "sample_tx_id": "../TX-0001", "fields": None},
    {"source": C.INPUT_SAMPLE, "sample_tx_id": "TX-0001/../TX-0002", "fields": None},
    {"source": C.INPUT_SAMPLE, "sample_tx_id": "TX-99999999", "fields": None},
    {"source": "nope", "sample_tx_id": None, "fields": None},
    {"source": C.INPUT_FIELDS, "sample_tx_id": None, "fields": None},
])
def test_bad_input_gives_up_now(env, monkeypatch, inp):
    monkeypatch.setattr(W, "run_extraction", lambda *a: pytest.fail("OCR 하면 안 됨"))
    tx = env.create(inp)
    env.run([tx])
    rec = env.store.get(tx)
    assert rec["status"] == C.SCREENING_FAILED and rec["attempts"] == 1 and rec["classification"] == C.REVIEW


def test_transient_error_still_retries(env, monkeypatch):
    """원문 저장소 일시 장애 같은 것은 재시도한다 (PermanentJobError 아님)."""
    tx = env.new_tx()
    inv, bl = upload(env, tx, ".png")
    env.create({"source": C.INPUT_UPLOAD, "sample_tx_id": None, "fields": None}, inv=inv, bl=bl, tx_id=tx)
    real = env.blob.get
    monkeypatch.setattr(env.blob, "get", lambda uri: (_ for _ in ()).throw(ConnectionError("MinIO 끊김")))
    assert env.once(tx)
    assert env.store.get(tx)["status"] == C.SCREENING_QUEUED
    monkeypatch.setattr(env.blob, "get", real)
    env.run([tx])
    rec = env.store.get(tx)
    assert rec["status"] == C.SCREENING_COMPLETED and rec["attempts"] == 2


# ---------------------------------------------------------------- rules-v3 에 OCR 부가 정보 (지적 9)
def test_rules_v3_gets_ocr_info(env, monkeypatch):
    seen = []
    real = W.screen

    def spy(fields, index, rules_version=None, ocr=None):
        seen.append((rules_version, ocr))
        return real(fields, index, rules_version=rules_version, ocr=ocr)

    monkeypatch.setattr(W, "screen", spy)
    tx = env.create(sample_input(), bundle_id="B-10")
    f = env.create(fields_input(), bundle_id="B-10")
    env.run([tx, f])
    (rv, ocr), (rv2, ocr2) = seen
    assert rv == rv2 == C.RULES_NEW
    assert set(ocr) == {"confidence", "conflicts"} and set(ocr["confidence"]) == set(C.TX_FIELDS)
    assert all(isinstance(ocr["confidence"][k], (int, float)) for k in C.SCREENING_FIELDS)
    assert isinstance(ocr["conflicts"], list)
    assert ocr2 is None                                         # 직접 입력한 필드는 OCR 정보 없음
    assert env.store.get(tx)["classification"] == C.HOLD
