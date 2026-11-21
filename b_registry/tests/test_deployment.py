import pytest
from fastapi.testclient import TestClient
from b_registry.api.main import create_app
from b_registry.db import Bundle
from b_registry.verify.workflow import Conflict, deployment_attempt
from b_registry.verify.deployment import request_deployment, DeploymentUnavailable, DeploymentRejected, DeploymentClient
from .test_registry import baseline, upload, transition


class Platform:
    def __init__(self):
        self.posts = []
        self.lookups = []
        self.error = None

    def request(self, bid, actor):
        self.posts.append((bid, actor))
        if self.error:
            raise self.error
        return {"bundle_id": bid, "deployment_id": "DP-test", "status": "progressing"}

    def find(self, bid):
        self.lookups.append(bid)
        return {"bundle_id": bid, "deployment_id": "DP-test", "status": "progressing"}


def ready(service):
    baseline(service)
    bid = service.registered_bundle(upload(service, "model-v4.zip"))
    for state in ("validated", "approved"):
        transition(service.db, bid, state, "reviewer", "passed")
    return bid


def test_b_requests_d_before_marking_canary(service):
    bid = ready(service)
    platform = Platform()
    original = platform.request
    def request(bundle_id, actor):
        with service.db.sessions() as s:
            assert s.get(Bundle, bid).status == "approved"
            assert deployment_attempt(s.get(Bundle, bid))["state"] == "pending"
        return original(bundle_id, actor)
    platform.request = request
    client = TestClient(create_app(service.cfg, service.db, service.artifacts.store, platform))
    result = client.post(f"/bundles/{bid}/deployments", json={"actor":"operator", "reason":"deploy"})
    assert result.status_code == 202
    assert result.json() == {"bundle_id":bid, "status":"canary", "deployment_id":"DP-test"}
    assert platform.posts == [(bid, "operator")]
    assert client.post(f"/bundles/{bid}/deployments", json={"actor":"operator", "reason":"retry"}).status_code == 202
    assert len(platform.posts) == 1


def test_stale_candidate_rebuilt_without_calling_d(service):
    bid = ready(service)
    listing = service.registered_bundle(upload(service, "list-v15.zip"))
    for state in ("validated", "approved", "canary", "active"):
        transition(service.db, listing, state, "operator", "new list")
    platform = Platform()
    result = request_deployment(service.db, bid, "operator", "deploy", platform)
    assert result["replacement_bundle_id"]
    assert platform.posts == []


def test_d_rejection_preserves_approval(service):
    bid = ready(service)
    platform = Platform()
    platform.error = DeploymentRejected("D 409")
    with pytest.raises(Conflict):
        request_deployment(service.db, bid, "operator", "deploy", platform)
    with service.db.sessions() as s:
        bundle = s.get(Bundle, bid)
        assert bundle.status == "approved"
        assert deployment_attempt(bundle)["state"] == "rejected"


def test_timeout_recovers_without_second_post(service):
    bid = ready(service)
    platform = Platform()
    platform.error = DeploymentUnavailable("timeout after D accepted")
    with pytest.raises(DeploymentUnavailable):
        request_deployment(service.db, bid, "operator", "deploy", platform)
    with pytest.raises(Conflict, match="awaiting confirmation"):
        transition(service.db, bid, "cancelled", "operator", "unsafe cancellation")
    result = request_deployment(service.db, bid, "operator", "retry", platform)
    assert result["status"] == "canary"
    assert len(platform.posts) == 1 and platform.lookups == [bid]


def test_unapproved_bundle_never_calls_d(service):
    baseline(service)
    bid = service.registered_bundle(upload(service, "model-v4.zip"))
    platform = Platform()
    with pytest.raises(Conflict):
        request_deployment(service.db, bid, "operator", "deploy", platform)
    assert platform.posts == []


def test_http_client_sends_d_contract(monkeypatch):
    import httpx
    seen = []
    def post(url, **kwargs):
        seen.append((url, kwargs))
        return httpx.Response(202, json={"bundle_id":"B-9", "deployment_id":"DP-1", "status":"pending"})
    monkeypatch.setattr(httpx, "post", post)
    result = DeploymentClient("http://platform:8300").request("B-9", "reviewer")
    assert result["deployment_id"] == "DP-1"
    assert seen[0][0] == "http://platform:8300/deployments"
    assert seen[0][1]["json"] == {"bundle_id":"B-9", "actor":"reviewer"}
    assert seen[0][1]["headers"] == {"X-Actor":"reviewer"}


def test_b_to_existing_d_api_and_success_callback(service, monkeypatch):
    from datetime import datetime, timedelta, timezone
    import httpx
    from d_platform.api.main import create_app as create_platform
    from d_platform.api.drivers import FakeDriver
    from d_platform.api.store import MemoryStore
    from d_platform.api.registry import TransitionRejected
    bid = ready(service)
    b = TestClient(create_app(service.cfg, service.db, service.artifacts.store,
                             DeploymentClient("http://platform:8300")))
    class Registry:
        kind = "integration-test"
        def bundle_status(self, bundle_id):
            response = b.get(f"/bundles/{bundle_id}")
            return response.json()["status"] if response.status_code == 200 else None
        def transition(self, bundle_id, to, reason):
            response = b.post(f"/bundles/{bundle_id}/transitions",
                              json={"to":to, "actor":"d-platform", "reason":reason})
            if response.status_code != 200:
                raise TransitionRejected(response.text)
    clock = [datetime.now(timezone.utc)]
    d = TestClient(create_platform(store=MemoryStore(), driver=FakeDriver(step_seconds=1),
                                  registry=Registry(), clock=lambda:clock[0], env={"BUNDLE_ID":"B-7"}))
    def post(url, **kwargs):
        assert url == "http://platform:8300/deployments"
        return d.post("/deployments", json=kwargs["json"], headers=kwargs["headers"])
    monkeypatch.setattr(httpx, "post", post)
    response = b.post(f"/bundles/{bid}/deployments", json={"actor":"operator", "reason":"deploy"})
    assert response.status_code == 202, response.text
    assert b.get(f"/bundles/{bid}").json()["status"] == "canary"
    clock[0] += timedelta(seconds=10)
    completed = d.get("/deployments/" + response.json()["deployment_id"])
    assert completed.json()["status"] == "succeeded"
    assert completed.json()["analysis"]["registry_sync"] == "ok"
    assert b.get(f"/bundles/{bid}").json()["status"] == "active"
