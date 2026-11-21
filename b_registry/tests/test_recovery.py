"""Regression cases from the demo: unsent POSTs, operator recovery and validation retries."""
import httpx
import pytest
from fastapi.testclient import TestClient
from b_registry.api.main import create_app
from b_registry.db import Bundle
from b_registry.verify.deployment import (
    DeploymentClient, DeploymentNotSent, DeploymentNotFound, DeploymentUnavailable,
    request_deployment, recover_deployment, _attempt, _attempt_time,
)
from b_registry.verify.workflow import Conflict, deployment_attempt
from b_registry.verify.release import ReleaseService
from .test_deployment import Platform, ready
from .test_registry import upload, transition, baseline
from .test_release import Runner, completed


def client_for(service, platform=None, runner=None):
    return TestClient(create_app(service.cfg, service.db, service.artifacts.store,
                                 deployment_client=platform, validation_runner=runner))


def pending(service, platform, bid):
    platform.error = DeploymentUnavailable("response lost after transmission")
    with pytest.raises(DeploymentUnavailable):
        request_deployment(service.db, bid, "operator", "deploy", platform)
    with service.db.sessions() as s:
        return _attempt_time(s.get(Bundle, bid))


def missing(bid):
    raise DeploymentNotFound("No deployment on D")


def recovery_body(stamp):
    return {"actor": "operator", "reason": "Confirmed D has no accepted or in-flight request",
            "expected_changed_at": stamp, "confirmed_not_received": True}


def test_unicode_actor_is_not_sent_and_does_not_block_deployment(service, monkeypatch):
    bid = ready(service)
    calls = []
    def post(url, **kwargs):
        calls.append(kwargs)
        return httpx.Response(202, json={"bundle_id": bid, "deployment_id": "DP-retry", "status": "pending"})
    monkeypatch.setattr(httpx, "post", post)
    client = client_for(service, DeploymentClient("http://d:8300"))
    response = client.post("/approvals", json={"bundle_id":bid,"decision":"approve","actor":"지윤아","reason":"승인"})
    assert response.status_code == 503 and "ASCII X-Actor" in response.json()["message"]
    assert calls == []
    with service.db.sessions() as s:
        bundle = s.get(Bundle, bid)
        assert deployment_attempt(bundle)["state"] == "failed"
        assert bundle.history[-1]["actor"] == "지윤아"
    response = client.post("/approvals", json={"bundle_id":bid,"decision":"approve","actor":"yoona","reason":"retry"})
    assert response.status_code == 202 and len(calls) == 1


@pytest.mark.parametrize("error_type", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
def test_unsent_errors_release_lock_and_allow_retry(service, monkeypatch, error_type):
    bid = ready(service)
    def post(*args, **kwargs):
        raise error_type("not sent")
    monkeypatch.setattr(httpx, "post", post)
    with pytest.raises(DeploymentNotSent):
        request_deployment(service.db,bid,"operator","deploy",DeploymentClient("http://d:8300"))
    with service.db.sessions() as s:
        assert deployment_attempt(s.get(Bundle,bid))["state"] == "failed"
    platform = Platform()
    assert request_deployment(service.db,bid,"operator","retry",platform)["status"] == "canary"
    assert len(platform.posts) == 1


def test_unsent_failure_does_not_block_another_bundle(service):
    bid = ready(service)
    platform = Platform()
    platform.error = DeploymentNotSent("connection refused")
    with pytest.raises(DeploymentNotSent):
        request_deployment(service.db,bid,"operator","deploy",platform)
    other = service.registered_bundle(upload(service,"list-v15.zip"))
    for status in ("validated","approved"):
        transition(service.db,other,status,"operator","passed")
    platform.error = None
    assert request_deployment(service.db,other,"operator","deploy",platform)["status"] == "canary"


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ReadError, httpx.WriteTimeout, httpx.WriteError])
def test_possible_transmission_failure_stays_pending(service, monkeypatch, error_type):
    bid = ready(service)
    calls = []
    def post(*args, **kwargs):
        calls.append(1)
        raise error_type("outcome unknown")
    monkeypatch.setattr(httpx,"post",post)
    platform = DeploymentClient("http://d:8300")
    with pytest.raises(DeploymentUnavailable):
        request_deployment(service.db,bid,"operator","deploy",platform)
    with service.db.sessions() as s:
        assert deployment_attempt(s.get(Bundle,bid))["state"] == "pending"
    platform.find = lambda b: {"bundle_id":b,"deployment_id":"DP-found","status":"progressing"}
    assert request_deployment(service.db,bid,"operator","retry",platform)["deployment_id"] == "DP-found"
    assert len(calls) == 1


def test_operator_recovery_checks_confirmation_and_preserves_history(service):
    bid = ready(service)
    platform = Platform()
    stamp = pending(service,platform,bid)
    platform.find = missing
    client = client_for(service,platform)
    path = f"/bundles/{bid}/deployment-recovery"
    body = recovery_body(stamp)
    assert client.post(path,json={**body,"confirmed_not_received":False}).status_code == 422
    assert client.post(path,json={**body,"confirmed_not_received":1}).status_code == 422
    assert client.post(path,json={**body,"expected_changed_at":"old"}).status_code == 409
    response = client.post(path,json=body)
    assert response.status_code == 200
    assert response.json() == {"bundle_id":bid,"status":"approved","request_state":"failed"}
    with service.db.sessions() as s:
        bundle = s.get(Bundle,bid)
        assert bundle.history[-1]["recovery"]["pending_changed_at"] == stamp
        assert bundle.history[-2]["deployment_request"]["state"] == "pending"
    assert client.post(path,json=body).status_code == 409
    platform.error = None
    assert client.post(f"/bundles/{bid}/deployments",json={"actor":"operator","reason":"retry"}).status_code == 202
    assert len(platform.posts) == 2


@pytest.mark.parametrize("lookup", ["existing", "unavailable"])
def test_recovery_cannot_ignore_d_result(service, lookup):
    bid = ready(service)
    platform = Platform()
    stamp = pending(service,platform,bid)
    if lookup == "unavailable":
        def down(bid):
            raise DeploymentUnavailable("D unavailable")
        platform.find = down
    response = client_for(service,platform).post(f"/bundles/{bid}/deployment-recovery",json=recovery_body(stamp))
    assert response.status_code == (409 if lookup == "existing" else 503)
    with service.db.sessions() as s:
        assert deployment_attempt(s.get(Bundle,bid))["state"] == "pending"


def test_recovery_rechecks_pending_identity_after_lookup(service):
    bid = ready(service)
    platform = Platform()
    stamp = pending(service,platform,bid)
    def changed(bid):
        with service.db.transaction() as s:
            _attempt(s.get(Bundle,bid),"other","different request","pending")
        raise DeploymentNotFound("none")
    platform.find = changed
    with pytest.raises(Conflict):
        recover_deployment(service.db,bid,"operator","checked",stamp,platform)
    with service.db.sessions() as s:
        assert deployment_attempt(s.get(Bundle,bid))["state"] == "pending"


@pytest.mark.parametrize("rows,expected", [
    ([],DeploymentNotFound),
    ([{"bundle_id":"other","deployment_id":"DP-other","status":"succeeded"}]*100,DeploymentUnavailable),
    ([{}],DeploymentUnavailable),
    ([{"bundle_id":"other"}],DeploymentUnavailable),
])
def test_listing_does_not_treat_truncation_as_proof(monkeypatch, rows, expected):
    monkeypatch.setattr(httpx,"get",lambda *a,**kw:httpx.Response(200,json=rows,request=httpx.Request("GET","http://d/deployments")))
    with pytest.raises(expected) as error:
        DeploymentClient("http://d").find("B-8")
    assert type(error.value) is expected


def test_cancelled_accepted_deployment_cannot_be_reapproved(service):
    bid = ready(service)
    platform = Platform()
    request_deployment(service.db,bid,"operator","deploy",platform)
    transition(service.db,bid,"cancelled","d-platform","rollback")
    response = client_for(service,platform).post("/approvals",json={"bundle_id":bid,"decision":"approve","actor":"operator","reason":"again"})
    assert response.status_code == 409 and len(platform.posts) == 1


def failed_validation(service):
    baseline(service)
    bid = service.registered_bundle(upload(service,"model-v4.zip"))
    runner = Runner()
    releases = ReleaseService(service.db,service.cfg,runner)
    releases.run_once()
    old = releases.get(bid)
    releases.receive(bid,old["validation_id"],{"validation_id":old["validation_id"],"bundle_id":bid,
        "baseline_bundle_id":old["baseline_bundle_id"],"status":"failed","error":"bad image"})
    runner.status = "failed"
    return bid,runner,releases,old


def test_retry_api_creates_new_run_and_ignores_stale_callback(service):
    bid,runner,releases,old = failed_validation(service)
    client = client_for(service,runner=runner)
    response = client.post(f"/bundles/{bid}/validations",json={"actor":"시우","reason":"이미지 수정"})
    assert response.status_code == 202
    new = response.json()
    assert new["status"] == "queued" and new["validation_id"] != old["validation_id"]
    assert new["actor"] == "시우"
    assert client.post(f"/bundles/{bid}/validations",json={"actor":"x","reason":"duplicate"}).status_code == 409
    assert client.post(f"/bundles/{bid}/validation-results/{old['validation_id']}",json=completed(old)).status_code == 409
    runner.status = "running"
    releases.run_once()
    assert new["validation_id"] in runner.runs
    assert client.post(f"/bundles/{bid}/validation-results/{new['validation_id']}",json=completed(new)).status_code == 200
    with service.db.sessions() as s:
        bundle = s.get(Bundle,bid)
        assert bundle.status == "validated"
        archived = [h for h in bundle.history if "previous_validation" in h]
        assert archived[0]["previous_validation"]["error"] == "bad image"


def test_retry_cannot_start_while_previous_job_is_running(service):
    bid,runner,releases,old = failed_validation(service)
    runner.status = "running"
    response = client_for(service,runner=runner).post(f"/bundles/{bid}/validations",json={"actor":"x","reason":"retry"})
    assert response.status_code == 409 and releases.get(bid)["validation_id"] == old["validation_id"]


def test_retry_cannot_start_when_job_state_unknown(service):
    bid,runner,releases,old = failed_validation(service)
    def down(run):
        raise httpx.ConnectError("kubernetes unavailable")
    runner.state = down
    response = client_for(service,runner=runner).post(f"/bundles/{bid}/validations",json={"actor":"x","reason":"retry"})
    assert response.status_code == 503 and releases.get(bid)["validation_id"] == old["validation_id"]


def test_retry_does_not_bypass_gate_failure(service):
    baseline(service)
    bid = service.registered_bundle(upload(service,"model-v4.zip"))
    runner = Runner()
    releases = ReleaseService(service.db,service.cfg,runner)
    releases.run_once()
    run = releases.get(bid)
    releases.receive(bid,run["validation_id"],completed(run,.89))
    response = client_for(service,runner=runner).post(f"/bundles/{bid}/validations",json={"actor":"x","reason":"bypass"})
    assert response.status_code == 409 and releases.get(bid)["validation_id"] == run["validation_id"]
