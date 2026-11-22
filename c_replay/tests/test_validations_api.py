"""검증 조회 API 테스트. 검증 Job 대신 저장소의 create/start/complete/fail 로 행을 넣고 API 응답을 확인한다.

store 픽스처(conftest)가 메모리·Postgres 두 저장소로 돌린다 (Postgres 는 TEST_DATABASE_URL 이 있을 때만).
"""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from common import contracts as C
from c_replay.api import main
from c_replay.store import InProgressExists, StoreUnavailable

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
    return TestClient(main.create_app(store=store, env={}))


def register(client, bundle_id="B-9", baseline="B-7"):
    """검증 Job 이 하듯 저장소에 검증 행을 만든다 (API 에는 등록이 없다)."""
    return client.app.state.store.create(bundle_id, baseline, "b_registry")


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


# ---------------------------------------------------------------- 등록은 API 에 없다 (검증 Job 이 만든다)
def test_no_register_api(client):
    r = client.post("/validations", json={"bundle_id": "B-9"})
    assert_error(r, 405, C.ERR_VALIDATION)


def test_new_validation_is_queued(client):
    vid = register(client)["validation_id"]
    assert re.fullmatch(r"RP-[0-9a-f]{12}", vid)
    body = client.get(f"/validations/{vid}").json()
    assert body == {
        "validation_id": vid, "bundle_id": "B-9", "baseline_bundle_id": "B-7",
        "status": "queued", "golden": None, "replay": None, "error": None, "progress": None,
        "requested_by": "b_registry",
        "created_at": body["created_at"], "started_at": None, "finished_at": None,
    }
    assert body["created_at"].endswith("+00:00")


def test_one_validation_in_progress_per_bundle(client):
    store = client.app.state.store
    vid = register(client)["validation_id"]
    with pytest.raises(InProgressExists):
        register(client)
    register(client, "B-10")                                   # 다른 번들은 가능
    store.start(vid)                                          # running 이어도 막힘
    with pytest.raises(InProgressExists):
        register(client)
    store.fail(vid, {"code": C.ERR_INTERNAL, "message": "x", "target_id": vid})
    register(client)                                          # 끝난 뒤에는 다시 만들 수 있다


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
    monkeypatch.setattr(client.app.state.store, "list", down)
    monkeypatch.setattr(client.app.state.store, "ping", down)
    assert_error(client.get("/validations"), 503, C.ERR_UNAVAILABLE)
    assert_error(client.get("/healthz"), 503, C.ERR_UNAVAILABLE)


def test_admin_page(client, monkeypatch, tmp_path):
    monkeypatch.setattr(main, "ADMIN_HTML", tmp_path / "admin.html")
    assert_error(client.get("/admin"), 404, C.ERR_NOT_FOUND)
    (tmp_path / "admin.html").write_text("<!doctype html><title>운영 화면</title>", encoding="utf-8")
    r = client.get("/admin")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "운영 화면" in r.text


# ---------------------------------------------------------------- 운영 화면 중계 (B·D)
def proxy_client(handler, env=None):
    """B·D 대신 handler(httpx.Request) -> httpx.Response 가 답한다."""
    import httpx
    from c_replay.store import MemoryStore
    env = {"B_URL": "http://b:8100", "D_URL": "http://d:8300/"} if env is None else env
    return TestClient(main.create_app(store=MemoryStore(), env=env, transport=httpx.MockTransport(handler)))


def test_proxy_passes_allowed_request_and_response_through():
    import httpx
    seen = []
    def handler(req):
        seen.append(req)
        return httpx.Response(202, json={"bundle_id": "B-9", "deployment_id": "DP-1"})
    c = proxy_client(handler)
    body = {"bundle_id": "B-9", "decision": "approve", "actor": "kim", "reason": "ok"}
    r = c.post("/api/b/approvals", json=body, headers={"X-Actor": "kim", "Cookie": "s=1"})
    assert r.status_code == 202 and r.json() == {"bundle_id": "B-9", "deployment_id": "DP-1"}
    (req,) = seen
    assert str(req.url) == "http://b:8100/approvals" and req.method == "POST"
    assert req.headers["x-actor"] == "kim" and "cookie" not in req.headers   # 정해 둔 헤더만 넘긴다
    assert req.read() == r.request.read()
    c.get("/api/d/deployments/DP-1")
    assert str(seen[-1].url) == "http://d:8300/deployments/DP-1"


def test_proxy_passes_active_bundles():
    import httpx
    seen = []
    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={"active": {"bundle_id": "B-7"}, "canary": None})
    r = proxy_client(handler).get("/api/b/bundles/active")
    assert r.status_code == 200 and r.json()["active"]["bundle_id"] == "B-7"
    assert str(seen[0].url) == "http://b:8100/bundles/active"


@pytest.mark.parametrize("path,body,status", [
    # 다시 검증: 실행 오류로 failed 인 후보만 (B 가 판단). 응답은 새 검증 정보
    ("bundles/B-9/validations", {"actor": "김민성", "reason": "검증 이미지 수정 후 재실행"}, 202),
    # 배포 잠금 풀기: 운영자가 D 에서 미접수를 확인한 뒤
    ("bundles/B-9/deployment-recovery", {"actor": "김민성", "reason": "D 에 요청 없음 확인",
     "expected_changed_at": "2026-11-21T00:00:00+00:00", "confirmed_not_received": True}, 200),
])
def test_proxy_passes_revalidation_and_deployment_recovery(path, body, status):
    import json
    import httpx
    seen = []
    def handler(req):
        seen.append(req)
        return httpx.Response(status, json={"bundle_id": "B-9"})
    r = proxy_client(handler).post(f"/api/b/{path}", json=body)
    assert r.status_code == status
    (req,) = seen
    assert str(req.url) == f"http://b:8100/{path}" and req.method == "POST"
    assert json.loads(req.read()) == body   # 한글 담당자 이름도 본문 그대로


def test_proxy_keeps_upstream_error_status_and_body():
    import httpx
    c = proxy_client(lambda req: httpx.Response(409, json={"code": "CONFLICT", "message": "m", "target_id": None}))
    r = c.get("/api/b/bundles/B-9/validation")
    assert r.status_code == 409 and r.json()["code"] == "CONFLICT"


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/b/bundles"),                       # 목록은 화면이 쓰지 않는다
    ("POST", "/api/b/bundles/B-9/transitions"),      # 상태 직접 변경은 넘기지 않는다
    ("GET", "/api/b/bundles/../imports"),            # 경로 조작
    ("GET", "/api/x/bundles/B-9"),                   # 모르는 서비스
    ("POST", "/api/d/deployments"),                  # 배포 시작은 B 가 한다
    ("GET", "/api/b/bundles/B-9/validations"),       # 다시 검증·잠금 풀기는 POST 만
    ("POST", "/api/b/bundles/B-9/validations/x"),
    ("POST", "/api/b/bundles/../deployment-recovery"),
])
def test_proxy_rejects_paths_the_screen_does_not_use(method, path):
    c = proxy_client(lambda req: pytest.fail("B·D 를 부르면 안 된다"))
    assert_error(c.request(method, path), 404, C.ERR_NOT_FOUND)


def test_proxy_is_503_when_upstream_is_not_set_or_down():
    import httpx
    c = proxy_client(lambda req: pytest.fail("부르면 안 된다"), env={})
    assert_error(c.get("/api/b/bundles/B-9"), 503, C.ERR_UNAVAILABLE)
    def down(req):
        raise httpx.ConnectError("refused")
    assert_error(proxy_client(down).get("/api/b/bundles/B-9"), 503, C.ERR_UNAVAILABLE)


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


# ---------------------------------------------------------------- 멈춘 검증 정리 (메모리·Postgres 공통)
def test_fail_stale_only_old_in_progress_of_same_bundle(store):
    from datetime import timedelta
    err = {"code": C.ERR_INTERNAL, "message": "정리", "target_id": "B-9"}
    running = store.create("B-9", "B-7", "b_registry")["validation_id"]
    store.start(running)
    queued = store.create("B-8", "B-7", "b_registry")["validation_id"]
    done = store.create("B-6", "B-7", "b_registry")["validation_id"]
    store.start(done)
    store.complete(done, GOLDEN, REPLAY, [])
    assert store.fail_stale("B-9", timedelta(hours=1), err) == 0          # 최근 것은 그대로
    assert store.get(running)["status"] == "running"
    assert store.fail_stale("B-9", timedelta(seconds=-60), err) == 1      # 기준보다 오래된 것으로 본다
    v = store.get(running)
    assert v["status"] == "failed" and v["error"] == err and v["finished_at"]
    assert store.get(queued)["status"] == "queued"                        # 다른 번들은 그대로
    store.fail_stale("B-6", timedelta(seconds=-60), err)
    assert store.get(done)["status"] == "completed"                       # 끝난 것은 그대로
    store.create("B-9", "B-7", "b_registry")                              # 정리 뒤에는 새 검증을 만들 수 있다


# ---------------------------------------------------------------- 진행 상황 (검증 Job 이 running 동안 적음)
def test_progress_is_null_until_job_writes_it(client):
    vid = register(client)["validation_id"]
    assert client.get(f"/validations/{vid}").json()["progress"] is None
    assert client.app.state.store.progress(vid, "golden", 0, 10) is False      # queued 에는 적지 않는다


def test_progress_is_returned_while_running(client):
    s = client.app.state.store
    vid = register(client)["validation_id"]
    s.start(vid)
    assert s.progress(vid, "golden", 37, 200) is True
    p = client.get(f"/validations/{vid}").json()["progress"]
    assert (p["stage"], p["done"], p["total"]) == ("golden", 37, 200) and p["updated_at"]
    assert s.progress(vid, "replay", 5, 120) is True
    assert client.get("/validations").json()["items"][0]["progress"]["stage"] == "replay"


def test_progress_not_written_after_finish(client):
    s = client.app.state.store
    vid = register(client)["validation_id"]
    s.start(vid)
    s.fail(vid, {"code": C.ERR_INTERNAL, "message": "x", "target_id": vid})
    assert s.progress(vid, "replay", 1, 2) is False


@pytest.mark.parametrize("args", [("ocr", 1, 2), ("golden", 3, 2), ("golden", -1, 2)])
def test_progress_rejects_bad_values(client, args):
    s = client.app.state.store
    vid = register(client)["validation_id"]
    s.start(vid)
    with pytest.raises(ValueError):
        s.progress(vid, *args)
