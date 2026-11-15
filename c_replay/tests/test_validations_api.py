"""검증 API 테스트. 검증 Job 대신 저장소의 start/complete/fail 로 결과를 넣고 API 응답을 확인한다.

store 픽스처(conftest)가 메모리·Postgres 두 저장소로 돌린다 (Postgres 는 TEST_DATABASE_URL 이 있을 때만).
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from common import contracts as C
from c_replay.api import main
from c_replay.store import StoreUnavailable

ACTOR = {"X-Actor": "b_registry"}

GOLDEN = {
    "golden_version": "golden-list-v1", "total": 30,
    "accuracy": {"baseline": 0.9, "candidate": 0.95, "diff": 0.05},
    "false_negative": {"baseline": 2, "candidate": 1, "diff": -1},
    "false_positive": {"baseline": 1, "candidate": 1, "diff": 0},
}
REPLAY = {"total": 100, "processed": 100, "count_match": True, "changed": 3, "changed_down": 1}


def diff(tx_id, direction, before, after):
    return {"tx_id": tx_id, "direction": direction,
            "before": {"bundle_id": "B-7", "classification": before, "reasons": []},
            "after": {"bundle_id": "B-9", "classification": after,
                      "reasons": [{"rule": C.RULE_PARTY_EXACT, "field": "buyer_name"}]},
            "fields_changed": [{"field": "buyer_name", "before": "ACME", "after": None}]}


DIFFS = [diff("TX-0030", "up", C.PASS, C.HOLD),
         diff("TX-0020", "down", C.HOLD, C.PASS),
         diff("TX-0010", "up", C.PASS, C.REVIEW)]


@pytest.fixture
def client(store):
    return TestClient(main.create_app(store=store, env={}))   # BUNDLE_ID 없음 -> 기본 기준 번들 B-7


def register(client, bundle_id="B-9", **extra):
    r = client.post("/validations", json={"bundle_id": bundle_id, **extra}, headers=ACTOR)
    assert r.status_code == 202, r.text
    return r.json()


def completed(client, bundle_id="B-9", diffs=DIFFS):
    vid = register(client, bundle_id)["validation_id"]
    assert client.app.state.store.start(vid)
    assert client.app.state.store.complete(vid, GOLDEN, REPLAY, diffs)
    return vid


def assert_error(r, status, code):
    assert r.status_code == status, r.text
    body = r.json()
    assert set(body) == set(C.ERROR_FIELDS)
    assert body["code"] == code
    return body


# ---------------------------------------------------------------- POST /validations
def test_register_returns_202_queued(client):
    body = register(client)
    assert re.fullmatch(r"RP-[0-9a-f]{12}", body["validation_id"])
    assert body == {
        "validation_id": body["validation_id"], "bundle_id": "B-9", "baseline_bundle_id": "B-7",
        "status": "queued", "golden": None, "replay": None, "error": None, "requested_by": "b_registry",
        "created_at": body["created_at"], "started_at": None, "finished_at": None,
    }
    assert body["created_at"].endswith("+00:00")


def test_register_with_explicit_baseline(client):
    assert register(client, baseline_bundle_id="B-8")["baseline_bundle_id"] == "B-8"


def test_default_baseline_is_contracts_baseline_and_env_overrides(store):
    assert C.BASELINE_BUNDLE in C.BUNDLES          # 검증 Job 이 찾을 수 있는 번들이어야 한다
    assert register(TestClient(main.create_app(store=store, env={})))["baseline_bundle_id"] == C.BASELINE_BUNDLE
    other = TestClient(main.create_app(store=store, env={"BUNDLE_ID": "B-8"}))
    assert register(other, "B-10")["baseline_bundle_id"] == "B-8"


def test_missing_actor_or_bundle_is_422(client):
    assert_error(client.post("/validations", json={"bundle_id": "B-9"}), 422, C.ERR_VALIDATION)
    assert_error(client.post("/validations", json={}, headers=ACTOR), 422, C.ERR_VALIDATION)


def test_second_validation_of_same_bundle_in_progress_is_409(client):
    vid = register(client)["validation_id"]
    body = assert_error(client.post("/validations", json={"bundle_id": "B-9"}, headers=ACTOR), 409, C.ERR_CONFLICT)
    assert body["target_id"] == "B-9"
    register(client, "B-10")                                   # 다른 번들은 가능
    client.app.state.store.start(vid)                         # running 이어도 409
    assert_error(client.post("/validations", json={"bundle_id": "B-9"}, headers=ACTOR), 409, C.ERR_CONFLICT)
    client.app.state.store.fail(vid, {"code": C.ERR_INTERNAL, "message": "x", "target_id": vid})
    register(client)                                          # 끝난 뒤에는 다시 등록 가능


# ---------------------------------------------------------------- GET /validations/{id}
def test_get_unknown_is_404(client):
    body = assert_error(client.get("/validations/RP-000000000000"), 404, C.ERR_NOT_FOUND)
    assert body["target_id"] == "RP-000000000000"


def test_result_is_null_until_completed(client):
    vid = register(client)["validation_id"]
    store = client.app.state.store
    store.start(vid)
    body = client.get(f"/validations/{vid}").json()
    assert (body["status"], body["golden"], body["replay"]) == ("running", None, None)
    assert body["started_at"] and body["finished_at"] is None

    assert store.complete(vid, GOLDEN, REPLAY, DIFFS)
    body = client.get(f"/validations/{vid}").json()
    assert body["status"] == "completed"
    assert body["golden"] == GOLDEN
    assert body["replay"] == REPLAY
    assert body["error"] is None and body["finished_at"]


def test_failed_validation_has_error(client):
    vid = register(client)["validation_id"]
    store = client.app.state.store
    error = {"code": C.ERR_INTERNAL, "message": "골든셋을 읽을 수 없음", "target_id": vid}
    assert store.fail(vid, error)
    body = client.get(f"/validations/{vid}").json()
    assert (body["status"], body["error"], body["golden"]) == ("failed", error, None)
    assert not store.start(vid)                               # 끝난 검증은 다시 바뀌지 않는다
    assert not store.complete(vid, GOLDEN, REPLAY, [])
    assert client.get(f"/validations/{vid}").json()["status"] == "failed"


def test_complete_requires_running(client):
    vid = register(client)["validation_id"]
    assert not client.app.state.store.complete(vid, GOLDEN, REPLAY, DIFFS)
    assert client.get(f"/validations/{vid}").json()["status"] == "queued"


# ---------------------------------------------------------------- GET /validations
def test_list_latest_first_with_filters_and_paging(client):
    a = completed(client, "B-9")
    b = register(client, "B-10")["validation_id"]
    c = register(client, "B-9")["validation_id"]

    body = client.get("/validations").json()
    assert [v["validation_id"] for v in body["items"]] == [c, b, a]
    assert body["total"] == 3

    body = client.get("/validations", params={"bundle_id": "B-9"}).json()
    assert [v["validation_id"] for v in body["items"]] == [c, a]
    body = client.get("/validations", params={"status": "completed"}).json()
    assert [v["validation_id"] for v in body["items"]] == [a] and body["total"] == 1
    body = client.get("/validations", params={"bundle_id": "B-9", "status": "queued"}).json()
    assert [v["validation_id"] for v in body["items"]] == [c]

    body = client.get("/validations", params={"limit": 1, "offset": 1}).json()
    assert [v["validation_id"] for v in body["items"]] == [b] and body["total"] == 3


def test_list_bad_query_is_422(client):
    assert_error(client.get("/validations", params={"status": "nope"}), 422, C.ERR_VALIDATION)
    assert_error(client.get("/validations", params={"limit": 201}), 422, C.ERR_VALIDATION)
    assert_error(client.get("/validations", params={"offset": -1}), 422, C.ERR_VALIDATION)


# ---------------------------------------------------------------- GET /validations/{id}/diffs
def test_diffs_before_completed_is_409(client):
    vid = register(client)["validation_id"]
    assert_error(client.get(f"/validations/{vid}/diffs"), 409, C.ERR_CONFLICT)
    client.app.state.store.start(vid)
    body = assert_error(client.get(f"/validations/{vid}/diffs"), 409, C.ERR_CONFLICT)
    assert body["target_id"] == vid
    assert_error(client.get("/validations/RP-000000000000/diffs"), 404, C.ERR_NOT_FOUND)


def test_diffs_down_first_then_tx_id(client):
    vid = completed(client)
    body = client.get(f"/validations/{vid}/diffs").json()
    assert [(d["direction"], d["tx_id"]) for d in body["items"]] == [
        ("down", "TX-0020"), ("up", "TX-0010"), ("up", "TX-0030")]
    assert body["total"] == 3
    assert body["items"][0] == DIFFS[1]


def test_diffs_direction_filter_and_paging(client):
    vid = completed(client)
    body = client.get(f"/validations/{vid}/diffs", params={"direction": "up"}).json()
    assert [d["tx_id"] for d in body["items"]] == ["TX-0010", "TX-0030"] and body["total"] == 2
    body = client.get(f"/validations/{vid}/diffs", params={"direction": "down"}).json()
    assert [d["tx_id"] for d in body["items"]] == ["TX-0020"]
    body = client.get(f"/validations/{vid}/diffs", params={"limit": 1, "offset": 2}).json()
    assert [d["tx_id"] for d in body["items"]] == ["TX-0030"] and body["total"] == 3
    assert_error(client.get(f"/validations/{vid}/diffs", params={"direction": "sideways"}), 422, C.ERR_VALIDATION)


def test_diffs_of_other_validation_are_separate(client):
    completed(client, "B-9")
    other = completed(client, "B-10", diffs=[])
    assert client.get(f"/validations/{other}/diffs").json() == {"items": [], "total": 0}


def test_complete_rejects_unknown_direction_or_classification(client):
    vid = register(client)["validation_id"]
    store = client.app.state.store
    store.start(vid)
    with pytest.raises(ValueError):
        store.complete(vid, GOLDEN, REPLAY, [diff("TX-0001", "sideways", C.PASS, C.HOLD)])
    with pytest.raises(ValueError):
        store.complete(vid, GOLDEN, REPLAY, [diff("TX-0001", "up", C.PASS, "BLOCKED")])
    assert client.get(f"/validations/{vid}").json()["status"] == "running"


# ---------------------------------------------------------------- 공통: 요청 ID·에러 형식·운영 화면
def test_request_id_is_echoed_or_generated(client):
    assert client.get("/livez", headers={"X-Request-ID": "req-1"}).headers["X-Request-ID"] == "req-1"
    assert client.get("/livez").headers["X-Request-ID"]
    assert client.get("/validations/RP-000000000000", headers={"X-Request-ID": "req-2"}).headers["X-Request-ID"] == "req-2"


def test_unknown_path_and_unhandled_error_use_common_format(client, monkeypatch):
    assert_error(client.get("/nope"), 404, C.ERR_NOT_FOUND)

    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(client.app.state.store, "get", boom)
    r = client.get("/validations/RP-000000000000")
    assert_error(r, 500, C.ERR_INTERNAL)
    assert r.headers["X-Request-ID"]


def test_store_down_is_503(client, monkeypatch):
    def down(*a, **k):
        raise StoreUnavailable("OperationalError")
    monkeypatch.setattr(client.app.state.store, "create", down)
    monkeypatch.setattr(client.app.state.store, "ping", down)
    assert_error(client.post("/validations", json={"bundle_id": "B-9"}, headers=ACTOR), 503, C.ERR_UNAVAILABLE)
    assert_error(client.get("/healthz"), 503, C.ERR_UNAVAILABLE)


def test_admin_page(client, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "ADMIN_HTML", tmp_path / "admin.html")
    assert_error(client.get("/admin"), 404, C.ERR_NOT_FOUND)
    (tmp_path / "admin.html").write_text("<!doctype html><title>운영 화면</title>", encoding="utf-8")
    r = client.get("/admin")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "운영 화면" in r.text


# ---------------------------------------------------------------- 상태 확인·지표
def test_healthz_and_livez(client):
    assert client.get("/livez").json() == {"status": "ok"}
    body = client.get("/healthz").json()
    assert body["status"] == "ok" and body["store"] == client.app.state.store.kind


def test_metrics(client):
    text = client.get("/metrics").text
    assert f'{main.METRIC_RUNS}{{status="completed"}} 0' in text
    assert main.METRIC_DURATION + "{" not in text

    completed(client, "B-9")
    failed = register(client, "B-10")["validation_id"]
    client.app.state.store.fail(failed, {"code": C.ERR_INTERNAL, "message": "x", "target_id": failed})
    register(client, "B-8")                                   # 진행 중인 건은 세지 않는다

    r = client.get("/metrics")
    assert r.headers["content-type"].startswith("text/plain")
    assert f"# TYPE {main.METRIC_RUNS} counter" in r.text
    assert f'{main.METRIC_RUNS}{{status="completed"}} 1' in r.text
    assert f'{main.METRIC_RUNS}{{status="failed"}} 1' in r.text
    assert f"# TYPE {main.METRIC_DURATION} gauge" in r.text
    m = re.search(main.METRIC_DURATION + r'\{bundle_id="B-9"\} ([0-9.]+)\n', r.text)
    assert m and float(m.group(1)) >= 0
    assert 'bundle_id="B-10"' not in r.text
