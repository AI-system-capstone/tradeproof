"""골든셋 검증·Replay 계산과 검증 Job 진입점. OCR 없이 process 를 주입해서 돈다.

실제 OCR 테스트(A 의 평시 경로와 같은 결과)는 tesseract 와 data/docs·data/sanctions 가 있을 때만.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from datetime import timedelta

import pytest

from common import contracts as C
from c_replay.job import __main__ as J
from c_replay.job.__main__ import Terminated, main as job_main, run
from c_replay.replay import validate as V
from c_replay.store import MemoryStore

BASE, CAND = "B-7", "B-9"          # list-v14 / list-v14 -> list-v15 (B-9), model-v3 -> model-v4
P, R, H = C.PASS, C.REVIEW, C.HOLD


def table(results: dict):
    """{(tx_id, bundle_id): 분류 또는 (분류, fields)} -> process(tx, bundle_id)"""
    def process(tx, bundle_id):
        v = results[(tx["tx_id"], bundle_id)]
        if isinstance(v, Exception):
            raise v
        cls, fields = v if isinstance(v, tuple) else (v, {})
        return {"classification": cls, "reasons": [{"rule": "r", "classification": cls}], "fields": fields}
    return process


def label(tx_id, v14, v15=None):
    return {"tx_id": tx_id, "expected": {C.LIST_BASE: v14, C.LIST_NEW: v15 or v14}}


def past(tx_id, cls, fields=None):
    return {"tx_id": tx_id, "bundle_id": BASE, "classification": cls, "reasons": [], "fields": fields or {}}


# ---------------------------------------------------------------- 골든셋
def test_golden_accuracy_fn_fp():
    items = [label("TX-0001", P), label("TX-0002", R), label("TX-0003", H), label("TX-0004", P)]
    proc = table({("TX-0001", BASE): P, ("TX-0002", BASE): R, ("TX-0003", BASE): H, ("TX-0004", BASE): P,
                  ("TX-0001", CAND): R,      # 오탐 (PASS -> REVIEW)
                  ("TX-0002", CAND): P,      # 미탐 (REVIEW -> PASS)
                  ("TX-0003", CAND): R,      # 미탐 (HOLD -> REVIEW: 기대보다 낮게 분류)
                  ("TX-0004", CAND): H})     # 오탐 (PASS -> HOLD)
    g = V.golden_result(items, proc, BASE, CAND)
    assert g["total"] == 4
    assert g["accuracy"] == {"baseline": 1.0, "candidate": 0.0, "diff": -1.0}
    assert g["false_negative"] == {"baseline": 0, "candidate": 2, "diff": 2}
    assert g["false_positive"] == {"baseline": 0, "candidate": 2, "diff": 2}


def test_golden_downgrade_to_review_is_false_negative():
    # S5: 저하 모델은 rules-v3 에서 '보류 -> 통과'가 아니라 '보류 -> 검토'로 나타난다. 이것도 미탐으로 세야 게이트가 막는다
    items = [label("TX-0001", H), label("TX-0002", H), label("TX-0003", R)]
    proc = table({("TX-0001", BASE): H, ("TX-0002", BASE): H, ("TX-0003", BASE): R,
                  ("TX-0001", CAND): R, ("TX-0002", CAND): H,
                  ("TX-0003", CAND): H})     # 오탐 (REVIEW -> HOLD: 기대보다 높게 분류)
    g = V.golden_result(items, proc, BASE, CAND)
    assert g["false_negative"] == {"baseline": 0, "candidate": 1, "diff": 1}
    assert g["false_positive"] == {"baseline": 0, "candidate": 1, "diff": 1}


def test_golden_hold_expected_as_pass_is_false_negative():
    items = [label("TX-0001", H), label("TX-0002", H)]
    proc = table({("TX-0001", BASE): P, ("TX-0002", BASE): H, ("TX-0001", CAND): P, ("TX-0002", CAND): P})
    g = V.golden_result(items, proc, BASE, CAND)
    assert g["false_negative"] == {"baseline": 1, "candidate": 2, "diff": 1}
    assert g["false_positive"]["candidate"] == 0
    assert g["accuracy"] == {"baseline": 0.5, "candidate": 0.0, "diff": -0.5}


def test_golden_uses_each_bundles_list_version():
    # new_listing: v14 기대 PASS, v15 기대 HOLD. B-7(v14)은 PASS, B-8(v15)은 HOLD 가 정답
    items = [label("TX-0001", C.EXPECTED[C.CASE_NEW][C.LIST_BASE], C.EXPECTED[C.CASE_NEW][C.LIST_NEW])]
    proc = table({("TX-0001", "B-7"): P, ("TX-0001", "B-8"): H})
    g = V.golden_result(items, proc, "B-7", "B-8")
    assert g["accuracy"] == {"baseline": 1.0, "candidate": 1.0, "diff": 0.0}
    assert g["false_negative"]["candidate"] == 0 and g["false_positive"]["baseline"] == 0
    assert g["golden_version"] == "golden-list-v14,golden-list-v15"


def test_golden_accepts_label_schema_expected_objects():
    items = [{"tx_id": "TX-0001", "expected": {C.LIST_BASE: {"classification": H, "matched_entry_id": "X"}}}]
    g = V.golden_result(items, table({("TX-0001", BASE): H, ("TX-0001", "B-7"): H}), BASE, BASE)
    assert g["accuracy"]["candidate"] == 1.0 and g["golden_version"] == "golden-list-v14"


def test_golden_missing_expected_or_empty_raises():
    with pytest.raises(ValueError):
        V.golden_result([{"tx_id": "TX-0001", "expected": {C.LIST_BASE: P}}], table({("TX-0001", BASE): P}),
                        BASE, CAND)
    with pytest.raises(V.EmptyGolden):
        V.golden_result([], table({}), BASE, CAND)


def test_golden_accuracy_rounded():
    items = [label(f"TX-000{i}", P) for i in range(3)]
    proc = table({**{(f"TX-000{i}", BASE): P for i in range(3)},
                  ("TX-0000", CAND): P, ("TX-0001", CAND): P, ("TX-0002", CAND): R})
    g = V.golden_result(items, proc, BASE, CAND)
    assert g["accuracy"] == {"baseline": 1.0, "candidate": 0.6667, "diff": -0.3333}


# ---------------------------------------------------------------- Replay
def test_replay_direction_and_order():
    rows = [past("TX-0003", P), past("TX-0001", H), past("TX-0002", R), past("TX-0004", R)]
    proc = table({("TX-0003", CAND): H, ("TX-0001", CAND): R, ("TX-0002", CAND): P, ("TX-0004", CAND): R})
    r, diffs = V.replay_result(rows, proc, CAND)
    assert r == {"total": 4, "processed": 4, "count_match": True, "changed": 3, "changed_down": 2}
    assert [(d["tx_id"], d["direction"]) for d in diffs] == [("TX-0001", "down"), ("TX-0002", "down"),
                                                              ("TX-0003", "up")]
    d = diffs[2]
    assert d["before"] == {"bundle_id": BASE, "classification": P, "reasons": []}
    assert d["after"]["bundle_id"] == CAND and d["after"]["classification"] == H


def test_replay_count_mismatch_when_tx_fails():
    rows = [past("TX-0001", P), past("TX-0002", P)]
    proc = table({("TX-0001", CAND): P, ("TX-0002", CAND): RuntimeError("tesseract")})
    seen = []
    r, diffs = V.replay_result(rows, proc, CAND, log=lambda **kw: seen.append(kw))
    assert r == {"total": 2, "processed": 1, "count_match": False, "changed": 0, "changed_down": 0}
    assert diffs == [] and seen[0]["tx_id"] == "TX-0002"


def test_replay_fields_only_change_is_not_a_diff():
    rows = [past("TX-0001", P, {"buyer_name": "ACME LLC"}),
            past("TX-0002", P, {"buyer_name": "ACME LLC", "amount": "1.00"})]
    proc = table({("TX-0001", CAND): (P, {"buyer_name": "ACME LIC"}),
                  ("TX-0002", CAND): (R, {"buyer_name": "ACME LIC", "amount": "1.00"})})
    r, diffs = V.replay_result(rows, proc, CAND)
    assert r["changed"] == 1 and [d["tx_id"] for d in diffs] == ["TX-0002"]
    assert diffs[0]["fields_changed"] == [{"field": "buyer_name", "before": "ACME LLC", "after": "ACME LIC"}]


def test_replay_fields_changed_order_and_none():
    rows = [past("TX-0001", P, {"vessel_name": "SEA", "seller_name": "S", "amount": 5})]
    proc = table({("TX-0001", CAND): (H, {"vessel_name": None, "seller_name": "S2", "amount": "5"})})
    _, diffs = V.replay_result(rows, proc, CAND)
    assert diffs[0]["fields_changed"] == [{"field": "seller_name", "before": "S", "after": "S2"},
                                          {"field": "vessel_name", "before": "SEA", "after": None}]


def test_same_input_same_result():
    items = [label("TX-0002", R), label("TX-0001", P)]
    rows = [past("TX-0002", R), past("TX-0001", P)]
    proc = table({("TX-0001", BASE): P, ("TX-0002", BASE): R, ("TX-0001", CAND): R, ("TX-0002", CAND): P})
    a = (V.golden_result(items, proc, BASE, CAND), V.replay_result(rows, proc, CAND))
    b = (V.golden_result(list(reversed(items)), proc, BASE, CAND), V.replay_result(list(reversed(rows)), proc, CAND))
    assert a == b


def test_flat_handles_ocr_and_truth_shapes():
    assert V._flat({"buyer_name": {"value": "A", "confidence": 90}, "amount": "1"}) == {"buyer_name": "A", "amount": "1"}
    assert V._flat(None) == {}


def test_golden_failure_names_the_tx_and_fails_everything():
    items = [label("TX-0001", P), label("TX-0002", H)]
    proc = table({("TX-0001", BASE): P, ("TX-0002", BASE): RuntimeError("OCR 실패")})
    with pytest.raises(V.GoldenFailed) as e:
        V.golden_result(items, proc, BASE, CAND)
    assert "TX-0002" in str(e.value) and BASE in str(e.value) and "OCR 실패" in str(e.value)


def test_job_golden_failure_fails_validation_with_tx_id():
    items, rows, _ = _load()
    proc = table({("TX-0001", BASE): P, ("TX-0002", BASE): ValueError("깨진 서류")})
    ok, s, sent = _run(load=lambda: (items, rows, proc))
    v = _only(s)
    assert ok is False and v["status"] == "failed" and v["golden"] is None and v["replay"] is None
    assert "TX-0002" in v["error"]["message"] and v["error"]["code"] == C.ERR_INTERNAL
    assert [x["status"] for x in sent] == ["failed"]


def test_replay_duplicate_tx_id_raises():
    proc = table({("TX-0001", CAND): P})
    with pytest.raises(ValueError, match="TX-0001"):
        V.replay_result([past("TX-0001", P), past("TX-0001", H)], proc, CAND)


# ---------------------------------------------------------------- 과거 기록 읽기 (Postgres 가 있을 때만)
@pytest.fixture
def pg():
    import os
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL 없음")
    import psycopg
    schema = Path(__file__).resolve().parents[2] / "common" / "schema"
    with psycopg.connect(dsn) as c:
        c.execute((schema / "10_a_screening.sql").read_text(encoding="utf-8"))
        c.execute("TRUNCATE transactions CASCADE")
        yield c, dsn


def _tx(c, tx_id):
    c.execute("INSERT INTO transactions (tx_id, received_at, extracted_fields, extraction_source) "
              "VALUES (%s, now(), '{}', 'ground_truth')", (tx_id,))


def _res(c, tx_id, bundle, cls, at, shadow=False):
    c.execute("INSERT INTO screening_results (tx_id, bundle_id, classification, is_shadow, screened_at) "
              "VALUES (%s, %s, %s, %s, %s)", (tx_id, bundle, cls, shadow, at))


def _job(c, tx_id, bundle, status):
    c.execute("INSERT INTO screening_jobs (tx_id, bundle_id, status, input) VALUES (%s, %s, %s, '{}')",
              (tx_id, bundle, status))


def test_read_past_one_row_per_tx(pg):
    c, dsn = pg
    for t in ("TX-0001", "TX-0002", "TX-0003", "TX-0004", "TX-0005"):
        _tx(c, t)
    # 0001: Job 없음, 기록 둘 -> 가장 최근 것
    _res(c, "TX-0001", "B-7", P, "2026-11-01"); _res(c, "TX-0001", "B-8", H, "2026-11-02")
    # 0002: Job 이 B-7 로 처리 -> Job 의 번들 기록 (더 최근 B-8 기록이 있어도)
    _res(c, "TX-0002", "B-7", R, "2026-11-01"); _res(c, "TX-0002", "B-8", H, "2026-11-02")
    _job(c, "TX-0002", "B-7", "completed")
    # 0003: 처리 중 -> 뺀다
    _res(c, "TX-0003", "B-7", P, "2026-11-01"); _job(c, "TX-0003", "B-7", "processing")
    # 0004: 섀도 기록만 -> 뺀다 (기록 없음)
    _res(c, "TX-0004", "B-9", P, "2026-11-01", shadow=True)
    # 0005: 같은 시각 두 건 -> result_id 가 큰 것
    _res(c, "TX-0005", "B-7", P, "2026-11-01"); _res(c, "TX-0005", "B-8", R, "2026-11-01")
    c.commit()
    rows = V.read_past(dsn)
    assert [(r["tx_id"], r["bundle_id"], r["classification"]) for r in rows] == [
        ("TX-0001", "B-8", H), ("TX-0002", "B-7", R), ("TX-0005", "B-8", R)]
    V.replay_result(rows, table({(r["tx_id"], CAND): P for r in rows}), CAND)   # 중복이 없어 오류 없이 돈다


# ---------------------------------------------------------------- 검증 Job
def _load():
    items = [label("TX-0001", P), label("TX-0002", H)]
    rows = [past("TX-0001", P), past("TX-0002", H)]
    proc = table({("TX-0001", BASE): P, ("TX-0002", BASE): H, ("TX-0001", CAND): R, ("TX-0002", CAND): P})
    return items, rows, proc


def _run(store=None, bundle_id=CAND, baseline=lambda: BASE, load=_load):
    """run 한 번. 반환: (성공 여부, 저장소, 알림으로 받은 검증 목록)"""
    store, sent = store or MemoryStore(), []
    ok = run(bundle_id, baseline, store, load, notify=sent.append)
    return ok, store, sent


def _only(store):
    items, total = store.list()
    assert total == 1
    return items[0]


def test_job_creates_validation_and_completes():
    ok, s, sent = _run()
    v = _only(s)
    assert ok is True
    assert (v["bundle_id"], v["baseline_bundle_id"], v["requested_by"]) == (CAND, BASE, "b_registry")
    assert v["status"] == "completed" and v["error"] is None and v["started_at"]
    assert v["replay"] == {"total": 2, "processed": 2, "count_match": True, "changed": 2, "changed_down": 1}
    assert v["golden"]["false_negative"] == {"baseline": 0, "candidate": 1, "diff": 1}
    items, total = s.diffs(v["validation_id"])
    assert total == 2 and items[0]["tx_id"] == "TX-0002" and items[0]["direction"] == "down"
    assert sent == [v]                                            # 끝나면 완료 알림 한 번


def test_job_logs_past_records_made_by_other_bundle(capsys):
    items, rows, proc = _load()
    rows[1]["bundle_id"] = "B-8"
    _run(load=lambda: (items, rows, proc))
    line = [x for x in capsys.readouterr().out.splitlines() if "past_bundle_mismatch" in x]
    assert len(line) == 1 and '"count": 1' in line[0] and '"total": 2' in line[0]


def test_job_fails_on_exception_and_notifies():
    def boom():
        raise RuntimeError("DB 없음")
    ok, s, sent = _run(load=boom)
    v = _only(s)
    assert ok is False and v["status"] == "failed" and v["golden"] is None
    assert v["error"] == {"code": C.ERR_INTERNAL, "message": "RuntimeError: DB 없음", "target_id": v["validation_id"]}
    assert [x["status"] for x in sent] == ["failed"]


def test_job_fails_on_sigterm_even_inside_replay():
    """시간 제한(SIGTERM)은 Replay 의 거래별 예외 처리에 잡히지 않고 failed 로 남는다."""
    items, rows, _ = _load()
    def process(tx, bundle_id):
        if bundle_id == CAND and "classification" in tx:          # Replay 도중
            raise Terminated()
        return {"classification": P, "reasons": [], "fields": {}}
    ok, s, sent = _run(load=lambda: (items, rows, process))
    v = _only(s)
    assert ok is False and v["status"] == "failed" and v["replay"] is None
    assert "SIGTERM" in v["error"]["message"]
    assert [x["status"] for x in sent] == ["failed"]


def test_job_fails_on_empty_golden():
    ok, s, _ = _run(load=lambda: ([], [], table({})))
    assert ok is False and _only(s)["error"]["code"] == C.ERR_NOT_FOUND


def test_job_fails_on_unknown_bundle_without_loading():
    def never():
        raise AssertionError("번들이 없으면 데이터를 읽지 않는다")
    ok, s, sent = _run(bundle_id="B-404", load=never)
    v = _only(s)
    assert ok is False and v["status"] == "failed" and v["started_at"] is None
    assert v["error"] == {"code": C.ERR_NOT_FOUND, "message": "번들 B-404 를 찾을 수 없음", "target_id": "B-404"}
    assert len(sent) == 1


def test_job_fails_when_b_is_unreachable():
    def down():
        raise OSError("connection refused")
    ok, s, sent = _run(baseline=down, load=lambda: (_ for _ in ()).throw(AssertionError("읽지 않는다")))
    v = _only(s)
    assert ok is False and v["baseline_bundle_id"] == "" and v["error"]["code"] == C.ERR_UNAVAILABLE
    assert len(sent) == 1


class _KillAt(MemoryStore):
    """지정한 저장소 호출에서 SIGTERM 이 온 것처럼 Terminated 를 낸다. after=True 면 호출을 마친 뒤에 낸다."""

    def __init__(self, method, after=False):
        super().__init__()
        self._method, self._after = method, after

    def __getattribute__(self, name):
        attr = super().__getattribute__(name)
        if name != super().__getattribute__("_method"):
            return attr

        def wrapped(*a, **k):
            if not self._after:
                raise Terminated()
            attr(*a, **k)
            raise Terminated()
        return wrapped


def test_sigterm_right_after_create_fails_validation():
    ok, s, sent = _run(store=_KillAt("start"))
    v = _only(s)
    assert ok is False and v["status"] == "failed" and "SIGTERM" in v["error"]["message"]
    assert [x["status"] for x in sent] == ["failed"]


def test_sigterm_while_saving_result_fails_validation():
    ok, s, sent = _run(store=_KillAt("complete"))                 # 저장하기 전에 신호 -> 결과 없음
    v = _only(s)
    assert ok is False and v["status"] == "failed" and v["golden"] is None
    assert [x["status"] for x in sent] == ["failed"]


def test_sigterm_after_result_saved_keeps_completed():
    ok, s, sent = _run(store=_KillAt("complete", after=True))     # 저장은 끝났고 신호가 그 뒤에
    v = _only(s)
    assert ok is True and v["status"] == "completed" and v["error"] is None and v["golden"]
    assert [x["status"] for x in sent] == ["completed"]


def test_sigterm_while_notifying_sends_again_and_keeps_result():
    s, calls = MemoryStore(), []
    def notify(v):
        calls.append(v["status"])
        if len(calls) == 1:
            raise Terminated()
    ok = run(CAND, lambda: BASE, s, _load, notify=notify)
    assert ok is True and calls == ["completed", "completed"] and _only(s)["status"] == "completed"


def test_sigterm_handler_ignores_signals_after_the_first(monkeypatch):
    monkeypatch.setattr(J, "_signaled", False)
    with pytest.raises(Terminated):
        J._on_sigterm(15, None)
    assert J._on_sigterm(15, None) is None                          # 정리 중 두 번째 신호는 무시


def test_notification_fits_in_termination_grace_period():
    import json
    t = json.loads((Path(J.__file__).parent / "job-template.json").read_text(encoding="utf-8"))
    waits = sum(2 ** i for i in range(J.NOTIFY_TRIES - 1))
    assert J.NOTIFY_TRIES * J.NOTIFY_TIMEOUT + waits < t["spec"]["template"]["spec"]["terminationGracePeriodSeconds"]


def test_job_does_nothing_when_same_bundle_in_progress():
    s = MemoryStore()
    s.create(CAND, BASE, "b_registry")
    ok, s, sent = _run(store=s)
    assert ok is False and sent == []
    assert s.list()[1] == 1                                        # 새 행을 만들지 않는다


def _age(store, validation_id, seconds):
    store._rows[validation_id]["created_at"] -= timedelta(seconds=seconds)


def test_job_cleans_up_old_running_validation_then_starts():
    s = MemoryStore()
    old = s.create(CAND, BASE, "b_registry")["validation_id"]
    s.start(old)
    _age(s, old, J.STALE_AFTER.total_seconds() + 1)
    ok, s, sent = _run(store=s)
    assert ok is True
    first, second = s.get(old), _only_other(s, old)
    assert first["status"] == "failed" and first["finished_at"]
    assert first["error"]["code"] == C.ERR_INTERNAL and "정리" in first["error"]["message"]
    assert second["status"] == "completed"
    assert [x["validation_id"] for x in sent] == [second["validation_id"]]


def test_job_cleans_up_old_queued_validation_of_same_bundle_only():
    s = MemoryStore()
    old = s.create(CAND, BASE, "b_registry")["validation_id"]
    other = s.create("B-8", BASE, "b_registry")["validation_id"]
    for v in (old, other):
        _age(s, v, J.STALE_AFTER.total_seconds() + 1)
    ok, s, _ = _run(store=s)
    assert ok is True and s.get(old)["status"] == "failed" and s.get(other)["status"] == "queued"


def test_job_leaves_recent_running_validation_alone():
    s = MemoryStore()
    recent = s.create(CAND, BASE, "b_registry")["validation_id"]
    s.start(recent)
    _age(s, recent, J.STALE_AFTER.total_seconds() - 60)
    ok, s, sent = _run(store=s)
    assert ok is False and sent == [] and s.get(recent)["status"] == "running" and s.list()[1] == 1


def test_stale_limit_is_longer_than_job_deadline():
    import json
    from pathlib import Path
    t = json.loads((Path(J.__file__).parent / "job-template.json").read_text(encoding="utf-8"))
    assert J.STALE_AFTER.total_seconds() > t["spec"]["activeDeadlineSeconds"]


def _only_other(store, validation_id):
    (v,) = [x for x in store.list()[0] if x["validation_id"] != validation_id]
    return v


def test_job_main_requires_bundle_id_and_database_url():
    assert job_main({}) == 2
    assert job_main({"BUNDLE_ID": "B-9"}) == 2
    assert job_main({"DATABASE_URL": "postgresql://x"}) == 2


def test_active_bundle_without_b_url_is_contracts_baseline():
    assert J.active_bundle(None) == C.BASELINE_BUNDLE


def test_notifier_posts_and_retries(monkeypatch):
    calls, waits = [], []
    def fake_http(method, url, body=None, timeout=10):
        assert timeout == J.NOTIFY_TIMEOUT
        calls.append((method, url, body))
        if len(calls) < 3:
            raise OSError("down")
        return {}
    monkeypatch.setattr(J, "_http", fake_http)
    v = {"validation_id": "RP-000000000001", "bundle_id": CAND, "status": "completed", "golden": None}
    assert J.notifier("http://b:8100/", sleep=waits.append)(v) is True
    assert calls[-1] == ("POST", "http://b:8100/bundles/B-9/validation-results",
                         {"validation_id": "RP-000000000001", "bundle_id": CAND, "status": "completed"})
    assert len(calls) == 3 and waits == [1, 2]
    calls.clear()
    monkeypatch.setattr(J, "_http", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    assert J.notifier("http://b:8100", sleep=waits.append)(v) is False   # 끝내 실패해도 예외 없음
    assert J.notifier(None)(v) is False                                   # B 주소가 없으면 보내지 않음


def test_job_template_is_what_b_expects():
    import json
    from pathlib import Path
    t = json.loads((Path(J.__file__).parent / "job-template.json").read_text(encoding="utf-8"))
    assert (t["apiVersion"], t["kind"]) == ("batch/v1", "Job")
    assert t["spec"]["backoffLimit"] == 0 and t["spec"]["activeDeadlineSeconds"] > 0
    assert "ttlSecondsAfterFinished" not in t["spec"]                 # 끝난 Job 을 지우면 B 가 다시 만든다
    (c,) = t["spec"]["template"]["spec"]["containers"]
    assert c["command"] == ["python", "-m", "c_replay.job"]
    assert all(e["name"] != "BUNDLE_ID" for e in c.get("env", []))   # BUNDLE_ID 는 B 가 넣는다


# ---------------------------------------------------------------- A 의 처리 경로(pipeline) 연결 (OCR 은 가짜로 바꿔 끼움)
def test_ocr_process_uses_a_pipeline(monkeypatch, tmp_path):
    pytest.importorskip("pytesseract")
    from a_screening import pipeline as W
    from a_screening.ocr import extract as X
    from a_screening.screening.match import ListIndex

    (tmp_path / "docs" / "TX-0001").mkdir(parents=True)
    monkeypatch.setattr(C, "DOCS_DIR", tmp_path / "docs")
    lists = {lv: ListIndex({"list_version": lv, "entries": [
        {"entry_id": "E1", "name": "ACME TRADING LLC", "entity_type": C.ENTITY}]}) for lv in (C.LIST_BASE, C.LIST_NEW)}
    monkeypatch.setattr(W, "_INDEX", lists)
    calls = []

    def fake_extract(tx_dir, model):
        calls.append(model)
        name = "ACME TRADING LLC" if model == C.MODEL_BASE else "ACNE TRADlNG"
        return {"fields": {f: {"value": name if f == "buyer_name" else "x", "confidence": 95.0}
                           for f in C.TX_FIELDS}, "conflicts": []}
    monkeypatch.setattr(X, "extract_transaction", fake_extract)

    proc = V.ocr_process(blob=None, workdir=tmp_path)
    tx = {"tx_id": "TX-0001"}
    a, b, c = proc(tx, "B-7"), proc(tx, "B-8"), proc(tx, "B-9")
    assert calls == [C.MODEL_BASE, C.MODEL_NEW]          # 같은 모델 OCR 은 거래당 한 번
    assert (a["classification"], b["classification"]) == (H, H)
    assert c["fields"]["buyer_name"] == "ACNE TRADlNG"
    # A 의 pipeline.screen_documents 가 같은 서류·같은 번들로 낸 판정과 같다 (SVC-04)
    a_out = W.screen_documents(tmp_path / "docs" / "TX-0001", W.bundle_spec("B-9"))
    assert (a_out["result"]["classification"], a_out["result"]["reasons"]) == (c["classification"], c["reasons"])
    assert a_out["fields"] == c["fields"]


def test_ocr_process_upload_input_reads_originals_from_blob(monkeypatch, tmp_path):
    pytest.importorskip("pytesseract")
    from a_screening import pipeline as W
    from a_screening.ocr import extract as X
    from a_screening.screening.match import ListIndex
    monkeypatch.setattr(W, "_INDEX", {lv: ListIndex({"list_version": lv, "entries": []})
                                      for lv in (C.LIST_BASE, C.LIST_NEW)})
    seen = []

    def fake_extract(tx_dir, model):
        seen.append(sorted(p.name for p in tx_dir.iterdir()))
        return {"fields": {f: {"value": "x", "confidence": 95.0} for f in C.TX_FIELDS}, "conflicts": []}
    monkeypatch.setattr(X, "extract_transaction", fake_extract)

    class Blob:
        def get(self, uri):
            return C.FILE_MAGIC[".png"] + b"..."
        def put(self, *a, **k):
            raise AssertionError("A 의 원문 저장소에 쓰면 안 된다")
    tx = {"tx_id": "TX-0001", "input": {"source": C.INPUT_UPLOAD},
          "doc_invoice_uri": "s3://b/TX-0001/invoice.png", "doc_bl_uri": None}
    out = V.ocr_process(Blob(), tmp_path)(tx, "B-7")
    assert seen == [["invoice.png"]] and out["classification"] == P


def test_ocr_process_fields_input_skips_ocr(monkeypatch):
    pytest.importorskip("pytesseract")
    from a_screening import pipeline as W
    from a_screening.ocr import extract as X
    from a_screening.screening.match import ListIndex
    monkeypatch.setattr(W, "_INDEX", {lv: ListIndex({"list_version": lv, "entries": [
        {"entry_id": "E1", "name": "ACME TRADING LLC", "entity_type": C.ENTITY}]}) for lv in (C.LIST_BASE, C.LIST_NEW)})
    monkeypatch.setattr(X, "extract_transaction", lambda *a: pytest.fail("fields 입력은 OCR 을 하지 않는다"))
    fields = {f: "x" for f in C.TX_FIELDS} | {"buyer_name": "ACME TRADING LLC"}
    out = V.ocr_process(blob=None, workdir=None)(
        {"tx_id": "TX-0009", "input": {"source": C.INPUT_FIELDS, "fields": fields}}, "B-7")
    assert out["classification"] == H and out["fields"]["buyer_name"] == "ACME TRADING LLC"


def test_read_only_blob_never_writes():
    class Blob:
        def get(self, uri):
            return b"x"
        def put(self, *a, **k):
            raise AssertionError("A 의 원문 저장소에 쓰면 안 된다")
    ro = V._ReadOnlyBlob(Blob())
    assert ro.get("file://k") == b"x" and ro.put("k", b"y", "image/png") == "k"


# ---------------------------------------------------------------- 실데이터 (tesseract + data/ 가 있을 때만)
def test_real_ocr_matches_a_pipeline(tmp_path):
    pytest.importorskip("pytesseract")
    if not shutil.which("tesseract"):
        pytest.skip("tesseract 없음")
    txs = sorted(p.name for p in C.DOCS_DIR.glob("TX-*") if (p / "invoice.png").exists()) if C.DOCS_DIR.is_dir() else []
    if not txs or not C.list_path(C.LIST_BASE).exists():
        pytest.skip("data/docs 또는 data/sanctions 없음")
    from a_screening import pipeline as A
    proc = V.ocr_process(blob=None, workdir=tmp_path)
    tx_id = txs[0]
    expected = A.screen_documents(C.DOCS_DIR / tx_id, A.bundle_spec(C.BASELINE_BUNDLE))["result"]
    got = proc({"tx_id": tx_id}, C.BASELINE_BUNDLE)
    assert (got["classification"], got["reasons"]) == (expected["classification"], expected["reasons"])
