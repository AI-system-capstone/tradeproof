"""배포 API 테스트 (메모리 저장소 + 가짜 드라이버 + 가짜 레지스트리, 시계는 테스트가 움직인다)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from d_platform.api.drivers import FakeDriver  # noqa: E402
from d_platform.api.main import create_app  # noqa: E402
from d_platform.api.registry import FakeRegistry, RegistryUnavailable  # noqa: E402
from d_platform.api.store import MemoryStore  # noqa: E402

ACTOR = {"X-Actor": "c-replay"}
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
        registry=FakeRegistry(frozenset({"B-2", "B-3", "B-9"})),
        clock=clock,
        env={"BUNDLE_ID": "B-1"},
    )
    return TestClient(app)


def start(client, bundle_id="B-2"):
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
    r = client.post("/deployments", json={"bundle_id": "B-2"}, headers=ACTOR)
    assert r.status_code == 202
    body = r.json()
    assert body["deployment_id"].startswith("DP-")
    assert r.headers["Location"] == f"/deployments/{body['deployment_id']}"
    assert body["bundle_id"] == "B-2"
    assert body["previous_bundle_id"] == "B-1"       # 이력이 없으면 BUNDLE_ID 가 stable
    assert body["status"] == "pending"
    assert body["steps"] == [10, 50, 100]
    assert body["requested_by"] == "c-replay"
    assert body["created_at"].endswith("+00:00")


def test_unapproved_bundle_is_409(client):
    client.app.state.registry.approved = frozenset()
    client.app.state.registry.bundle_status = lambda bundle_id: "validated"
    body = assert_error(client.post("/deployments", json={"bundle_id": "B-2"}, headers=ACTOR), 409, "CONFLICT")
    assert body["target_id"] == "B-2"


def test_unknown_bundle_is_404(client):
    assert_error(client.post("/deployments", json={"bundle_id": "B-404"}, headers=ACTOR), 404, "NOT_FOUND")


def test_second_deployment_while_in_progress_is_409(client):
    first = start(client, "B-2")
    body = assert_error(client.post("/deployments", json={"bundle_id": "B-3"}, headers=ACTOR), 409, "CONFLICT")
    assert body["target_id"] == first["deployment_id"]


def test_new_deployment_allowed_after_previous_finished(client, clock):
    start(client, "B-2")
    clock.tick(STEP * 3)                               # 10 → 50 → 100 → 완료
    second = start(client, "B-3")
    assert second["previous_bundle_id"] == "B-2"       # 방금 반영된 번들이 stable


def test_already_stable_bundle_is_409(client, clock):
    start(client, "B-2")
    clock.tick(STEP * 3)
    assert_error(client.post("/deployments", json={"bundle_id": "B-2"}, headers=ACTOR), 409, "CONFLICT")


def test_missing_actor_or_body_is_422(client):
    assert_error(client.post("/deployments", json={"bundle_id": "B-2"}), 422, "VALIDATION_ERROR")
    assert_error(client.post("/deployments", json={}, headers=ACTOR), 422, "VALIDATION_ERROR")


def test_registry_down_is_503(client):
    def down(bundle_id):
        raise RegistryUnavailable("연결 거부")
    client.app.state.registry.bundle_status = down
    assert_error(client.post("/deployments", json={"bundle_id": "B-2"}, headers=ACTOR), 503, "SERVICE_UNAVAILABLE")


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
    start(client, "B-2")                               # 롤백 뒤에는 새 배포 가능, stable 은 그대로 B-1


def test_get_unknown_is_404(client):
    body = assert_error(client.get("/deployments/DP-nope"), 404, "NOT_FOUND")
    assert body["target_id"] == "DP-nope"


def test_list_returns_latest_first(client, clock):
    a = start(client, "B-2")["deployment_id"]
    clock.tick(STEP * 3)
    b = start(client, "B-3")["deployment_id"]
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
    dep_id = start(client, "B-2")["deployment_id"]
    clock.tick(STEP * 3)
    assert client.post(f"/deployments/{dep_id}/rollback", headers=ACTOR).status_code == 200
    assert start(client, "B-3")["previous_bundle_id"] == "B-1"   # stable 이 B-1 로 돌아옴


def test_rollback_twice_is_409(client):
    dep_id = start(client)["deployment_id"]
    assert client.post(f"/deployments/{dep_id}/rollback", headers=ACTOR).status_code == 200
    assert_error(client.post(f"/deployments/{dep_id}/rollback", headers=ACTOR), 409, "CONFLICT")


def test_rollback_older_success_is_409(client, clock):
    old = start(client, "B-2")["deployment_id"]
    clock.tick(STEP * 3)
    start(client, "B-3")
    clock.tick(STEP * 3)
    assert_error(client.post(f"/deployments/{old}/rollback", headers=ACTOR), 409, "CONFLICT")


def test_rollback_unknown_is_404(client):
    assert_error(client.post("/deployments/DP-nope/rollback", headers=ACTOR), 404, "NOT_FOUND")


# ---------------------------------------------------------------- healthz / livez / metrics / 공통
def test_health_endpoints(client):
    assert client.get("/livez").status_code == 200
    assert client.get("/healthz").json()["store"] == "memory"


def test_healthz_503_when_store_down(client):
    def down():
        raise ConnectionError("db down")
    client.app.state.store.ping = down
    assert_error(client.get("/healthz"), 503, "SERVICE_UNAVAILABLE")


def test_metrics_names_and_values(client, clock):
    start(client, "B-2")
    clock.tick(STEP)
    text = client.get("/metrics").text
    assert 'tradeproof_platform_deployments{status="progressing"} 1' in text
    assert 'tradeproof_platform_canary_weight{bundle_id="B-2"} 50' in text
    assert 'tradeproof_platform_stable_bundle_info{bundle_id="B-1"} 1' in text
    for line in text.splitlines():
        assert line.startswith("#") or line.startswith("tradeproof_platform_")


def test_request_id_is_echoed_or_generated(client):
    assert client.get("/livez", headers={"X-Request-ID": "rid-1"}).headers["X-Request-ID"] == "rid-1"
    assert client.get("/livez").headers["X-Request-ID"]
