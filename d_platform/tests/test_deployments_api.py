"""배포 API 테스트 (메모리 저장소 + 가짜 드라이버 + 가짜 레지스트리, 시계는 테스트가 움직인다)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from d_platform.api.drivers import FakeDriver  # noqa: E402
from d_platform.api.main import create_app  # noqa: E402
from d_platform.api.registry import FakeRegistry, RegistryUnavailable, TransitionRejected  # noqa: E402
from d_platform.api.store import MemoryStore  # noqa: E402

ACTOR = {"X-Actor": "b-registry"}
STEP = 10  # 가짜 드라이버의 단계당 시간(초)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 13, 0, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def client(clock):
    app = create_app(
        store=MemoryStore(),
        driver=FakeDriver(step_seconds=STEP, fail_bundles=frozenset({"B-9"})),
        registry=FakeRegistry(frozenset({"B-8", "B-10", "B-9"})),
        clock=clock,
        env={"BUNDLE_ID": "B-7", "CORS_ALLOW_ORIGINS": "http://localhost:8200"},
    )
    return TestClient(app)


def start(client, bundle_id="B-8"):
    r = client.post("/deployments", json={"bundle_id": bundle_id}, headers=ACTOR)
    assert r.status_code == 202, r.text
    return r.json()


def assert_error(r, status, code):
    assert r.status_code == status, r.text
    body = r.json()
    assert set(body) == {"code", "message", "target_id"}
    assert body["code"] == code
    return body


# ---------------------------------------------------------------- POST /deployments
def test_create_returns_202_and_first_step(client):
    r = client.post("/deployments", json={"bundle_id": "B-8"}, headers=ACTOR)
    assert r.status_code == 202
    body = r.json()
    assert body["deployment_id"].startswith("DP-")
    assert r.headers["Location"] == f"/deployments/{body['deployment_id']}"
    assert body["bundle_id"] == "B-8"
    assert body["previous_bundle_id"] == "B-7"       # 이력이 없으면 BUNDLE_ID 가 stable
    assert body["status"] == "pending"
    assert body["steps"] == [10, 50, 100]
    assert body["requested_by"] == "b-registry"
    assert body["created_at"].endswith("+00:00")


def test_unapproved_bundle_is_409(client):
    client.app.state.registry.statuses["B-8"] = "validated"
    body = assert_error(client.post("/deployments", json={"bundle_id": "B-8"}, headers=ACTOR), 409, "CONFLICT")
    assert body["target_id"] == "B-8"


def test_unknown_bundle_is_404(client):
    assert_error(client.post("/deployments", json={"bundle_id": "B-404X"}, headers=ACTOR), 404, "NOT_FOUND")


def test_second_deployment_while_in_progress_is_409(client):
    first = start(client, "B-8")
    body = assert_error(client.post("/deployments", json={"bundle_id": "B-10"}, headers=ACTOR), 409, "CONFLICT")
    assert body["target_id"] == first["deployment_id"]


def test_new_deployment_allowed_after_previous_finished(client, clock):
    start(client, "B-8")
    clock.tick(STEP * 3)                               # 10 → 50 → 100 → 완료
    second = start(client, "B-10")
    assert second["previous_bundle_id"] == "B-8"       # 방금 반영된 번들이 stable


def test_already_stable_bundle_is_409(client, clock):
    start(client, "B-8")
    clock.tick(STEP * 3)
    assert_error(client.post("/deployments", json={"bundle_id": "B-8"}, headers=ACTOR), 409, "CONFLICT")


def test_missing_actor_or_body_is_422(client):
    assert_error(client.post("/deployments", json={"bundle_id": "B-8"}), 422, "VALIDATION_ERROR")
    assert_error(client.post("/deployments", json={}, headers=ACTOR), 422, "VALIDATION_ERROR")


def test_registry_down_is_503(client):
    def down(bundle_id):
        raise RegistryUnavailable("연결 거부")
    client.app.state.registry.bundle_status = down
    assert_error(client.post("/deployments", json={"bundle_id": "B-8"}, headers=ACTOR), 503, "SERVICE_UNAVAILABLE")


# ---------------------------------------------------------------- GET /deployments/{id}
def test_canary_steps_10_50_100_then_succeeded(client, clock):
    dep_id = start(client)["deployment_id"]
    seen = []
    for _ in range(4):
        body = client.get(f"/deployments/{dep_id}").json()
        seen.append((body["status"], body["weight"]))
        clock.tick(STEP)
    assert seen == [("progressing", 10), ("progressing", 50), ("progressing", 100), ("succeeded", 100)]
    assert body["analysis"]["result"] == "passed"
    assert [c["weight"] for c in body["analysis"]["checks"]] == [10, 50, 100]
    assert body["finished_at"] is not None


def test_analysis_failure_rolls_back_automatically(client, clock):
    dep_id = start(client, "B-9")["deployment_id"]     # B-9 는 50% 단계 분석이 실패하도록 설정
    clock.tick(STEP * 2)
    body = client.get(f"/deployments/{dep_id}").json()
    assert body["status"] == "rolled_back"
    assert body["rollback_reason"] == "analysis_failed"
    assert body["weight"] == 0
    assert body["analysis"]["result"] == "failed"
    start(client, "B-8")                               # 롤백 뒤에는 새 배포 가능, stable 은 그대로 B-1


def test_get_unknown_is_404(client):
    body = assert_error(client.get("/deployments/DP-nope"), 404, "NOT_FOUND")
    assert body["target_id"] == "DP-nope"


def test_list_returns_latest_first(client, clock):
    a = start(client, "B-8")["deployment_id"]
    clock.tick(STEP * 3)
    b = start(client, "B-10")["deployment_id"]
    assert [d["deployment_id"] for d in client.get("/deployments").json()] == [b, a]


# ---------------------------------------------------------------- POST /deployments/{id}/rollback
def test_rollback_in_progress_returns_200(client, clock):
    dep_id = start(client)["deployment_id"]
    clock.tick(STEP)                                   # 50% 단계
    r = client.post(f"/deployments/{dep_id}/rollback", headers={"X-Actor": "operator"})
    assert r.status_code == 200
    body = r.json()
    assert (body["status"], body["weight"], body["rollback_reason"]) == ("rolled_back", 0, "manual")
    clock.tick(STEP * 10)                              # 시간이 지나도 다시 진행되지 않는다
    assert client.get(f"/deployments/{dep_id}").json()["status"] == "rolled_back"


def test_rollback_after_success_restores_previous_bundle(client, clock):
    dep_id = start(client, "B-8")["deployment_id"]
    clock.tick(STEP * 3)
    assert client.post(f"/deployments/{dep_id}/rollback", headers=ACTOR).status_code == 200
    assert start(client, "B-10")["previous_bundle_id"] == "B-7"   # stable 이 B-1 로 돌아옴


def test_rollback_twice_is_409(client):
    dep_id = start(client)["deployment_id"]
    assert client.post(f"/deployments/{dep_id}/rollback", headers=ACTOR).status_code == 200
    assert_error(client.post(f"/deployments/{dep_id}/rollback", headers=ACTOR), 409, "CONFLICT")


def test_rollback_older_success_is_409(client, clock):
    old = start(client, "B-8")["deployment_id"]
    clock.tick(STEP * 3)
    start(client, "B-10")
    clock.tick(STEP * 3)
    assert_error(client.post(f"/deployments/{old}/rollback", headers=ACTOR), 409, "CONFLICT")


def test_rollback_unknown_is_404(client):
    assert_error(client.post("/deployments/DP-nope/rollback", headers=ACTOR), 404, "NOT_FOUND")


def test_rollback_accepts_actor_in_body_like_admin_ui(client):
    dep_id = start(client)["deployment_id"]
    r = client.post(f"/deployments/{dep_id}/rollback", json={"actor": "admin", "reason": "지표 이상"})
    assert r.status_code == 200
    assert r.json()["analysis"]["rollback"] == {"actor": "admin", "reason": "지표 이상"}


def test_rollback_without_actor_or_mismatch_is_422(client):
    dep_id = start(client)["deployment_id"]
    assert_error(client.post(f"/deployments/{dep_id}/rollback"), 422, "VALIDATION_ERROR")
    assert_error(client.post(f"/deployments/{dep_id}/rollback", json={"actor": "a"}, headers={"X-Actor": "b"}),
                 422, "VALIDATION_ERROR")


# ---------------------------------------------------------------- B 번들 상태 반영
def sent(client) -> list[tuple[str, str]]:
    return [(t["bundle_id"], t["to"]) for t in client.app.state.registry.transitions]


def test_start_does_not_send_canary_then_success_marks_active(client, clock):
    """canary 는 B 가 배포 요청의 202 를 받고 스스로 바꾼다. D 는 보내지 않는다 (B 가 409 로 막음)."""
    dep_id = start(client, "B-8")["deployment_id"]
    assert sent(client) == []
    clock.tick(STEP * 3)
    body = client.get(f"/deployments/{dep_id}").json()
    assert body["analysis"]["registry_sync"] == "ok"
    assert body["analysis"]["registry_change"]["to"] == "active"
    assert sent(client) == [("B-8", "active")]
    assert client.app.state.registry.transitions[0]["actor"] == "d-platform"


def test_rollback_in_progress_marks_bundle_cancelled(client, clock):
    auto = start(client, "B-9")["deployment_id"]            # 분석 실패 → 자동 롤백
    clock.tick(STEP * 2)
    client.get(f"/deployments/{auto}")
    manual = start(client, "B-8")["deployment_id"]
    client.post(f"/deployments/{manual}/rollback", headers=ACTOR)
    assert sent(client) == [("B-9", "cancelled"), ("B-8", "cancelled")]


def test_rollback_after_success_restores_previous_bundle_in_registry(client, clock):
    """100% 반영 뒤 롤백: 후보가 아니라 직전 번들을 active 로 보낸다 (B 가 지금 번들을 cancelled 로 바꾼다)."""
    dep_id = start(client, "B-8")["deployment_id"]
    clock.tick(STEP * 3)
    client.get(f"/deployments/{dep_id}")
    body = client.post(f"/deployments/{dep_id}/rollback", headers=ACTOR).json()
    assert sent(client)[-1] == ("B-7", "active")
    assert body["analysis"]["registry_change"]["bundle_id"] == "B-7"


def test_start_works_while_registry_refuses_transitions(client):
    """B 는 배포 요청의 응답을 기다리는 동안 모든 전이를 409 로 막는다. 그래도 시작은 된다."""
    def reject(bundle_id, to, reason):
        raise TransitionRejected("registry 409: A deployment request is awaiting confirmation")
    client.app.state.registry.transition = reject
    assert client.post("/deployments", json={"bundle_id": "B-8"}, headers=ACTOR).status_code == 202


def test_registry_down_keeps_result_and_retries_later(client, clock):
    reg = client.app.state.registry
    real = reg.transition
    dep_id = start(client, "B-8")["deployment_id"]

    def down(bundle_id, to, reason):
        raise RegistryUnavailable("연결 거부")
    reg.transition = down
    clock.tick(STEP * 3)
    body = client.get(f"/deployments/{dep_id}").json()
    assert (body["status"], body["analysis"]["registry_sync"]) == ("succeeded", "pending")   # 트래픽 결과는 확정

    reg.transition = real                                    # B 가 돌아오면 다음 조회 때 다시 알린다
    assert client.get(f"/deployments/{dep_id}").json()["analysis"]["registry_sync"] == "ok"
    assert reg.statuses["B-8"] == "active"


def test_registry_rejecting_transition_is_recorded(client, clock):
    dep_id = start(client, "B-8")["deployment_id"]

    def reject(bundle_id, to, reason):
        raise TransitionRejected("registry 409")
    client.app.state.registry.transition = reject
    clock.tick(STEP * 3)
    assert client.get(f"/deployments/{dep_id}").json()["analysis"]["registry_sync"] == "rejected"


# ---------------------------------------------------------------- healthz / livez / metrics / 공통
def test_cors_allows_admin_ui_origin_only(client):
    ok = client.options("/deployments/DP-x/rollback", headers={
        "Origin": "http://localhost:8200", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type"})
    assert ok.headers["access-control-allow-origin"] == "http://localhost:8200"
    other = client.get("/livez", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in other.headers


def test_health_endpoints(client):
    assert client.get("/livez").status_code == 200
    assert client.get("/healthz").json()["store"] == "memory"


def test_healthz_503_when_store_down(client):
    def down():
        raise ConnectionError("db down")
    client.app.state.store.ping = down
    assert_error(client.get("/healthz"), 503, "SERVICE_UNAVAILABLE")


def test_metrics_names_and_values(client, clock):
    start(client, "B-8")
    clock.tick(STEP)
    text = client.get("/metrics").text
    assert 'tradeproof_platform_deployments{status="progressing"} 1' in text
    assert 'tradeproof_platform_canary_weight{bundle_id="B-8"} 50' in text
    assert 'tradeproof_platform_stable_bundle_info{bundle_id="B-7"} 1' in text
    for line in text.splitlines():
        assert line.startswith("#") or line.startswith("tradeproof_platform_")


def test_request_id_is_echoed_or_generated(client):
    assert client.get("/livez", headers={"X-Request-ID": "rid-1"}).headers["X-Request-ID"] == "rid-1"
    assert client.get("/livez").headers["X-Request-ID"]
