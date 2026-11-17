import copy
from datetime import timedelta
import httpx
import pytest
from fastapi.testclient import TestClient
from b_registry.api.main import create_app
from b_registry.db import Bundle, now, iso
from b_registry.verify.gate import evaluate, policy
from b_registry.verify.release import ReleaseService
from b_registry.verify.replay import ValidationJobRunner, RunnerUnavailable
from b_registry.verify.deployment import request_deployment
from b_registry.verify.workflow import Conflict
from .test_registry import baseline, upload
from .test_deployment import Platform


class Runner:
    def __init__(self):
        self.runs = {}
        self.status = "running"
    def check(self):
        pass
    def state(self, run):
        return self.status
    def ensure(self, run):
        self.runs[run["validation_id"]] = run
        return self.status


class Results:
    def __init__(self):
        self.rows = {}
    def get(self, vid):
        return self.rows.get(vid)


def completed(run, accuracy=.95):
    return {"validation_id":run["validation_id"], "bundle_id":run["bundle_id"],
        "baseline_bundle_id":run["baseline_bundle_id"], "status":"completed", "error":None,
        "golden":{"golden_version":"golden-v1", "total":30,
            "accuracy":{"baseline":.9, "candidate":accuracy, "diff":accuracy-.9},
            "false_negative":{"baseline":2, "candidate":1, "diff":-1},
            "false_positive":{"baseline":1, "candidate":1, "diff":0}},
        "replay":{"total":100,"processed":100,"count_match":True,"changed":3,"changed_down":1}}


@pytest.fixture
def release(service):
    baseline(service)
    bid = service.registered_bundle(upload(service, "model-v4.zip"))
    runner, results = Runner(), Results()
    releases = ReleaseService(service.db, service.cfg, runner)
    return bid, releases, runner, results


def test_job_result_gate_approval_and_deployment(service, release):
    bid, releases, runner, results = release
    run = releases.start(bid, "operator", "validate")
    assert releases.start(bid, "operator", "duplicate")["validation_id"] == run["validation_id"]
    releases.run_once()
    assert len(runner.runs) == 1
    assert releases.get(bid)["status"] == "running"
    releases.receive(bid, run["validation_id"], completed(run))
    # A new worker/service resumes the persisted request.
    releases = ReleaseService(service.db, service.cfg, runner)
    releases.run_once()
    assert releases.get(bid)["gate"]["decision"] == "passed"
    releases.approve(bid, "approve", "reviewer", "checked")
    platform = Platform()
    assert request_deployment(service.db, bid, "operator", "deploy", platform)["status"] == "canary"
    assert len(platform.posts) == 1


@pytest.mark.parametrize("accuracy,expected", [(.8999,"blocked"),(.9,"passed"),(.95,"passed")])
def test_gate_90_percent(release, accuracy, expected):
    bid, releases, runner, results = release
    run = releases.start(bid, "operator", "validate")
    releases.receive(bid, run["validation_id"], completed(run, accuracy))
    releases.run_once()
    assert releases.get(bid)["gate"]["decision"] == expected
    if expected == "blocked":
        with pytest.raises(Conflict):
            releases.approve(bid, "approve", "reviewer", "bypass")


def test_execution_failure_retry_ignores_previous_run(service, release):
    bid, releases, runner, results = release
    old = releases.start(bid, "operator", "validate")
    runner.status = "failed"
    releases.run_once()
    assert releases.get(bid)["status"] == "failed"
    with service.db.sessions() as s:
        assert s.get(Bundle,bid).status == "candidate"
    new = releases.start(bid, "operator", "retry")
    assert old["validation_id"] != new["validation_id"]
    releases._finish(old, result=completed(old))
    assert releases.get(bid)["status"] == "queued"


@pytest.mark.parametrize("change", [
    lambda r:r.update(bundle_id="wrong"),
    lambda r:r["golden"]["accuracy"].update(candidate=float("nan")),
    lambda r:r.update(golden=None),
    lambda r:r["golden"]["accuracy"].update(diff=42),
])
def test_bad_result_never_passes(release, change):
    bid, releases, runner, results = release
    run = releases.start(bid, "operator", "validate")
    result = completed(run)
    change(result)
    with pytest.raises((Conflict, ValueError, KeyError, TypeError, AttributeError)):
        releases.receive(bid, run["validation_id"], result)
    assert releases.get(bid)["status"] == "queued"
    with pytest.raises(Conflict):
        releases.approve(bid, "approve", "reviewer", "bad result")


def test_partial_replay_blocks_gate(release):
    bid, releases, runner, results = release
    run = releases.start(bid, "operator", "validate")
    result = completed(run)
    result["replay"].update(processed=90, count_match=False)
    releases.receive(bid, run["validation_id"], result)
    releases.run_once()
    assert releases.get(bid)["gate"]["decision"] == "blocked"


def test_initial_bundle_skips_c_and_requests_d(service):
    upload(service,"model-v3.zip")
    bid = service.registered_bundle(upload(service,"list-v14.zip"))
    platform = Platform()
    result = request_deployment(service.db,bid,"operator","initial",platform)
    assert result["status"] == "canary" and len(platform.posts) == 1
    with service.db.sessions() as s:
        bundle = s.get(Bundle,bid)
        assert bundle.validation_id is None and bundle.base_bundle_id is None
        assert bundle.history[0]["to"] == "approved"


def test_public_gate_bypass_is_blocked(service, release):
    bid, releases, runner, results = release
    client = TestClient(create_app(service.cfg,service.db,service.artifacts.store,
                                  validation_runner=runner))
    for target in ("validated","validation_failed","approved","rejected","canary"):
        assert client.post(f"/bundles/{bid}/transitions",json={"to":target,"actor":"x","reason":"bypass"}).status_code == 409
    assert client.post("/approvals",json={"bundle_id":bid,"decision":"approve","actor":"x","reason":"bypass"}).status_code == 409
    response=client.post(f"/bundles/{bid}/validation-runs",json={"actor":"operator","reason":"validate"})
    assert response.status_code == 404
    releases.run_once()
    assert client.get(f"/bundles/{bid}/validation").status_code == 200


def test_automatic_dispatch_never_polls_c(service, release, monkeypatch):
    bid, releases, runner, results = release
    def forbidden(*args, **kwargs):
        pytest.fail("C results must never be polled")
    monkeypatch.setattr(httpx, "get", forbidden)
    releases.run_once()
    run = releases.get(bid)
    assert run["status"] == "running"
    runner.ensure = forbidden
    releases.run_once()
    assert len(runner.runs) == 1
    releases.receive(bid, run["validation_id"], completed(run))
    assert releases.get(bid)["gate"]["decision"] == "passed"


def test_kubernetes_inputs_and_idempotency(service, monkeypatch):
    monkeypatch.setenv("VALIDATION_NAMESPACE","test")
    jobs={}
    def handler(request):
        import json
        if request.method == "POST":
            job=json.loads(request.content)
            jobs[job["metadata"]["name"]]=job
            return httpx.Response(201,json=job)
        job=jobs.get(request.url.path.rsplit("/",1)[-1])
        return httpx.Response(200,json=job) if job else httpx.Response(404)
    template={"apiVersion":"batch/v1","kind":"Job","spec":{"template":{"spec":{"containers":[{"name":"validation","image":"c-validation"}],"restartPolicy":"Never"}}}}
    run={"validation_id":"RP-test","bundle_id":"B-8","baseline_bundle_id":"B-7","actor":"operator"}
    with httpx.Client(base_url="https://kubernetes",transport=httpx.MockTransport(handler)) as client:
        runner=ValidationJobRunner(service.cfg,template,client)
        runner.ensure(run)
        runner.ensure(run)
        assert len(jobs)==1
        env=next(iter(jobs.values()))["spec"]["template"]["spec"]["containers"][0]["env"]
        assert {e["name"]:e["value"] for e in env} == {"VALIDATION_ID":"RP-test","BUNDLE_ID":"B-8","BASELINE_BUNDLE_ID":"B-7","REQUESTED_BY":"operator", "VALIDATION_CALLBACK_URL":"http://b-registry:8100/bundles/B-8/validation-results/RP-test"}
        with pytest.raises(RunnerUnavailable):
            runner.ensure({**run,"baseline_bundle_id":"B-other"})


def test_operator_can_reject_passing_bundle(release):
    bid, releases, runner, results = release
    run = releases.start(bid, "operator", "validate")
    releases.receive(bid, run["validation_id"], completed(run))
    releases.run_once()
    result = releases.approve(bid, "reject", "reviewer", "business reason")
    assert result["to"] == "rejected"
    with pytest.raises(Conflict):
        releases.approve(bid, "approve", "reviewer", "late approval")


def test_job_timeout_does_not_start_parallel_retry(service, release):
    bid, releases, runner, results = release
    run = releases.start(bid, "operator", "validate")
    with service.db.transaction() as s:
        bundle = s.get(Bundle,bid)
        bundle.validation = {**bundle.validation,"created_at":iso(now()-timedelta(hours=2))}
    releases.run_once()
    assert releases.get(bid)["status"] == "failed"
    with pytest.raises(Conflict, match="still running"):
        releases.start(bid, "operator", "retry")
    runner.status = "failed"
    assert releases.start(bid, "operator", "retry")["validation_id"] != run["validation_id"]


def test_missing_template_keeps_durable_run_for_retry(service):
    baseline(service)
    bid = service.registered_bundle(upload(service,"model-v4.zip"))
    releases = ReleaseService(service.db,service.cfg,ValidationJobRunner(service.cfg))
    releases.run_once()
    run = releases.get(bid)
    assert run["status"] == "queued" and run["error"] == "RunnerUnavailable"
    releases.run_once()
    assert releases.get(bid)["validation_id"] == run["validation_id"]


def test_local_docker_runner_passes_contract_once(service, monkeypatch):
    import json
    from types import SimpleNamespace
    from b_registry.verify import replay
    service.cfg.validation_runner = "docker"
    containers={}
    starts=[]
    def execute(args,**kwargs):
        if args[1] == "info":
            return SimpleNamespace(returncode=0,stdout="test")
        if args[1] == "inspect":
            return SimpleNamespace(returncode=0,stdout=json.dumps([containers[args[2]]])) if args[2] in containers else SimpleNamespace(returncode=1,stdout="")
        assert args[1] == "run"
        starts.append(args)
        name=args[args.index("--name")+1]
        env=[args[i+1] for i,a in enumerate(args) if a == "--env"]
        label=args[args.index("--label")+1].split("=",1)
        containers[name]={"Config":{"Env":env,"Labels":dict([label])},"State":{"Running":True}}
        return SimpleNamespace(returncode=0,stdout="container")
    monkeypatch.setattr(replay.subprocess,"run",execute)
    runner=ValidationJobRunner(service.cfg,{"image":"c-validation:test","command":["python","-m","validation"]})
    run={"validation_id":"RP-local","bundle_id":"B-8","baseline_bundle_id":"B-7","actor":"operator"}
    assert runner.ensure(run) == "running"
    assert runner.ensure(run) == "running"
    assert len(starts) == 1


def test_import_callback_and_approval_request_d(service):
    baseline(service)
    runner, platform = Runner(), Platform()
    app = create_app(service.cfg, service.db, service.artifacts.store,
                     validation_runner=runner, deployment_client=platform)
    client = TestClient(app)
    package = (service.cfg.trust_key.parent / "packages/model-v4.zip").read_bytes()
    response = client.post("/imports", files={"package": ("model.zip", package)})
    assert response.status_code == 201
    bid = response.json()["bundle_id"]
    # Execute the same durable worker invoked by lifespan/notify, deterministically.
    app.state.worker.run_once()
    run = client.get(f"/bundles/{bid}/validation").json()
    assert run["status"] == "running" and len(runner.runs) == 1
    callback = f"/bundles/{bid}/validation-results/{run['validation_id']}"
    body = completed(run)
    assert client.post(callback, json=body).status_code == 200
    assert client.post(callback, json=body).status_code == 200
    assert client.post(callback, json=completed(run, .91)).status_code == 409
    assert client.post(callback, json={**body, "bundle_id":"wrong"}).status_code == 409
    assert client.post(callback, json={**body, "status":"running"}).status_code == 422
    result = client.get(f"/bundles/{bid}/validation").json()
    assert result["result"] == body and result["gate"]["decision"] == "passed"
    approval = {"bundle_id":bid,"decision":"approve","actor":"operator","reason":"release"}
    response = client.post("/approvals",json=approval)
    assert response.status_code == 202 and response.json()["status"] == "canary"
    assert client.post("/approvals",json=approval).status_code == 202
    assert len(platform.posts) == 1
    assert client.post(callback, json=body).status_code == 200


def test_failed_callback_and_malformed_result_never_deploy(service, release):
    bid, releases, runner, _ = release
    platform = Platform()
    client = TestClient(create_app(service.cfg,service.db,service.artifacts.store,
                                  validation_runner=runner,deployment_client=platform))
    releases.run_once()
    run = releases.get(bid)
    callback = f"/bundles/{bid}/validation-results/{run['validation_id']}"
    assert client.post(callback, json={**completed(run),"golden":{}}).status_code == 422
    assert releases.get(bid)["status"] == "running"
    failed = {"validation_id":run["validation_id"],"bundle_id":bid,
              "baseline_bundle_id":run["baseline_bundle_id"],"status":"failed","error":"C execution failed"}
    assert client.post(callback,json=failed).status_code == 200
    assert client.post(callback,json=failed).status_code == 200
    assert client.post("/approvals",json={"bundle_id":bid,"decision":"approve","actor":"x","reason":"no"}).status_code == 409
    assert not platform.posts
    assert releases.get(bid)["error"] == "C execution failed"


def test_initial_approval_skips_job(service):
    upload(service,"model-v3.zip")
    bid = service.registered_bundle(upload(service,"list-v14.zip"))
    runner, platform = Runner(), Platform()
    app = create_app(service.cfg,service.db,service.artifacts.store,
                     validation_runner=runner,deployment_client=platform)
    app.state.worker.run_once()
    assert not runner.runs
    client = TestClient(app)
    response = client.post("/approvals",json={"bundle_id":bid,"decision":"approve","actor":"x","reason":"initial"})
    assert response.status_code == 202 and response.json()["status"] == "canary"
    assert len(platform.posts) == 1


def test_rebuilt_candidate_is_automatically_validated(service, release):
    from .test_registry import transition
    bid, releases, runner, _ = release
    releases.run_once()
    run = releases.get(bid)
    releases.receive(bid,run["validation_id"],completed(run))
    other = service.registered_bundle(upload(service,"list-v15.zip"))
    for status in ("validated","approved","canary","active"):
        transition(service.db,other,status,"test","new list")
    platform = Platform()
    app = create_app(service.cfg,service.db,service.artifacts.store,
                     validation_runner=runner,deployment_client=platform)
    response = TestClient(app).post("/approvals",json={"bundle_id":bid,"decision":"approve","actor":"x","reason":"release"})
    assert response.status_code == 200 and not platform.posts
    replacement = response.json()["replacement_bundle_id"]
    app.state.worker.run_once()
    assert releases.get(replacement)["baseline_bundle_id"] == other
    assert releases.get(replacement)["status"] == "running"
