import time
from concurrent.futures import ThreadPoolExecutor
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from common import contracts as C
from b_registry.api.main import create_app
from b_registry.db import Bundle, AIModel, SanctionsList, Database, Import
from b_registry.storage import StorageUnavailable
from b_registry.verify.replay import ReplayDispatcher
from b_registry.verify.workflow import Conflict, ImportService, transition
from b_registry.verify.worker import VerificationWorker

def upload(service, name):
    imp = service.intake((service.cfg.trust_key.parent / "packages" / name).read_bytes())
    service.process(imp.import_id)
    return imp.import_id

def baseline(service):
    model = upload(service, "model-v3.zip")
    listing = upload(service, "list-v14.zip")
    bid = service.registered_bundle(listing)
    assert service.registered_bundle(model) == bid
    for state in ("approved", "active"):
        transition(service.db, bid, state, "test", "baseline")
    return bid

def client_for(service):
    return TestClient(create_app(service.cfg, service.db, service.artifacts.store))

def test_http_contract_and_bootstrap(service):
    client = client_for(service)
    assert client.get("/livez").status_code == 404
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/imports" in schema["paths"]
    assert not any(p["in"] == "query" for p in schema["paths"]["/imports/{import_id}"]["get"].get("parameters", []))
    assert "bundle_id" in schema["components"]["schemas"]["VerificationResult"]["required"]
    assert client.get("/healthz").status_code == 200
    assert client.get("/bundles/active").status_code == 404
    response = client.post("/imports", files={"package": ("model.zip", (service.cfg.trust_key.parent / "packages/model-v3.zip").read_bytes())},
                           headers={"X-Request-ID": "check-123"})
    assert response.status_code == 202 and response.headers["X-Request-ID"] == "check-123"
    iid = response.json()["import_id"]
    assert response.headers["Location"] == "/imports/" + iid
    assert client.get("/imports/" + iid).status_code == 202
    service.process(iid)
    assert client.get("/imports/" + iid).json() == {"status": "APPROVED", "reason": None, "bundle_id": None}
    assert client.post(f"/imports/{iid}/approve").status_code == 409
    listing = upload(service, "list-v14.zip")
    bid = service.registered_bundle(listing)
    assert bid == C.BASELINE_BUNDLE
    assert client.post(f"/imports/{iid}/approve").json() == {"bundle_id": bid}
    result = client.get(f"/imports/{listing}").json()
    assert result == {"status": "APPROVED", "reason": None, "bundle_id": bid}
    bundle = client.get("/bundles/" + bid).json()
    assert bundle["model_id"] == bundle["model"]["model_id"] == "model-v3"
    assert bundle["list_id"] == bundle["list"]["list_id"] == "list-v14"
    assert "model_component_id" not in bundle
    assert bundle["status"] == "validated"
    assert bundle["created_at"].endswith("+00:00")
    assert not {"rules_version", "history", "replay"} & bundle.keys()
    for field, filename in (("model", "model-v3.zip"), ("list", "list-v14.zip")):
        assert set(bundle[field]) == {field + "_id", "uri"}
        assert service.artifacts.get(bundle[field]["uri"]) == (service.cfg.trust_key.parent / "packages" / filename).read_bytes()
    assert "is_active" not in bundle and "model_version" not in bundle and "list_version" not in bundle
    assert "bundle_id=" in client.get("/metrics").text

def test_transition_activation_and_rollback(service):
    old = baseline(service)
    iid = upload(service, "model-v4.zip")
    new = service.registered_bundle(iid)
    with service.db.sessions() as s:
        candidate, active = s.get(Bundle, new), s.get(Bundle, old)
        assert candidate.list_id == active.list_id
    client = client_for(service)
    def change(state):
        return client.post(f"/bundles/{new}/transitions", json={"to": state, "actor": "C", "reason": "test"})
    assert change("active").status_code == 409
    assert change("approved").status_code == 200
    response = change("active")
    assert response.status_code == 200
    assert set(response.json()) == {"from", "to", "changed_at"}
    assert client.get("/bundles/" + old).json()["status"] == "superseded"
    assert client.get("/bundles/active").json()["bundle_id"] == new
    assert change("rolled_back").status_code == 200
    assert client.get("/bundles/active").json()["bundle_id"] == old
    assert change("active").status_code == 409

@pytest.mark.parametrize("name", ["list-v16-large-change.zip"])
def test_rejections_leave_active_unchanged(service, name):
    bid = baseline(service)
    iid = upload(service, name)
    result = client_for(service).get("/imports/" + iid).json()
    assert result["status"] == "REJECTED" and result["reason"]
    assert result["bundle_id"] is None
    if "large-change" in name:
        assert "20%" in result["reason"] and "exceeds" in result["reason"]
    with service.db.sessions() as s:
        assert list(s.scalars(select(Bundle.bundle_id))) == [bid]
        assert len(list(s.scalars(select(AIModel)))) == 1
        assert len(list(s.scalars(select(SanctionsList)))) == 1

def test_intake_and_processing_are_idempotent_across_workers(service):
    baseline(service)
    raw = (service.cfg.trust_key.parent / "packages/model-v4.zip").read_bytes()
    with ThreadPoolExecutor(max_workers=4) as pool:
        ids = list(pool.map(lambda _: service.intake(raw).import_id, range(4)))
        assert len(set(ids)) == 1
        results = list(pool.map(lambda _: service.process(ids[0]), range(4)))
    assert results == ["APPROVED"] * 4
    with service.db.sessions() as s:
        assert len(list(s.scalars(select(Bundle)))) == 2

def test_storage_failure_retries_after_restart(service, monkeypatch):
    iid = service.intake((service.cfg.trust_key.parent / "packages/model-v3.zip").read_bytes()).import_id
    original = service.artifacts.get
    monkeypatch.setattr(service.artifacts, "get", lambda _: (_ for _ in ()).throw(StorageUnavailable("offline")))
    VerificationWorker(service).run_once()
    with service.db.sessions() as s:
        assert s.get(Import, iid).status == "RECEIVED"
    monkeypatch.setattr(service.artifacts, "get", original)
    restarted_db = Database(service.cfg.database_url)
    restarted = ImportService(restarted_db, service.cfg, service.artifacts)
    VerificationWorker(restarted).run_once()
    with restarted_db.sessions() as s:
        assert s.get(Import, iid).status == "APPROVED"
    restarted_db.engine.dispose()

def test_api_lifespan_processes_automatically(service):
    with client_for(service) as client:
        response = client.post("/imports", files={"package": ("list.zip", (service.cfg.trust_key.parent / "packages/list-v14.zip").read_bytes())})
        iid = response.json()["import_id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = client.get("/imports/" + iid)
            if result.status_code == 200:
                break
            time.sleep(0.05)
        assert result.json() == {"status": "APPROVED", "reason": None, "bundle_id": None}

def test_errors_readiness_and_size_limit(service, monkeypatch):
    client = client_for(service)
    for response in (client.get("/imports/missing"), client.post("/imports"),
                     client.post("/bundles/B-7/transitions", json={"to": "active", "actor": "", "reason": ""})):
        assert set(response.json()) == set(C.ERROR_FIELDS)
    monkeypatch.setattr(service.artifacts.store, "ping", lambda: (_ for _ in ()).throw(OSError("offline")))
    assert client.get("/healthz").status_code == 503
    service.cfg.max_zip_bytes = 10
    client = client_for(service)
    response = client.post("/imports", files={"package": ("large.zip", b"x" * 11)})
    assert response.status_code == 413
    assert response.json()["code"] == C.ERR_VALIDATION
    response = client.post("/imports", content=b"x" * 70000, headers={"Content-Type": "application/octet-stream"})
    assert response.status_code == 413

def test_same_version_different_package_rejected(service, signer):
    raw = signer[1]("model-v3.zip")
    first = service.intake(raw)
    assert service.process(first.import_id) == "APPROVED"
    second = service.intake(signer[1]("model-v3.zip", lambda f, m: m.update(issued_at="2026-11-13T00:00:00+00:00")))
    assert service.process(second.import_id) == "REJECTED"
    with service.db.sessions() as s:
        assert len(list(s.scalars(select(AIModel)))) == 1

def test_no_update_before_baseline_activation(service):
    upload(service, "model-v3.zip")
    upload(service, "list-v14.zip")
    iid = upload(service, "model-v4.zip")
    with service.db.sessions() as s:
        assert s.get(Import, iid).status == "REJECTED"
        assert s.get(AIModel, "model-v4") is None

def test_replay_job_idempotency_and_retry(service, monkeypatch):
    upload(service, "model-v3.zip")
    iid = upload(service, "list-v14.zip")
    bid = service.registered_bundle(iid)
    monkeypatch.setenv("REPLAY_NAMESPACE", "test")
    template = {"apiVersion": "batch/v1", "kind": "Job", "spec": {"template": {"spec": {
        "restartPolicy": "Never", "containers": [{"name": "replay", "image": "team-replay",
        "env": [{"name": "BUNDLE_ID", "value": "wrong"}]}]}}}}
    mode = ["offline"]
    jobs = {}
    def handler(request):
        if mode[0] == "offline":
            return httpx.Response(503)
        if request.method == "POST":
            import json
            job = json.loads(request.content)
            name = job["metadata"]["name"]
            if name in jobs:
                return httpx.Response(409)
            jobs[name] = job
            return httpx.Response(201, json=job)
        return httpx.Response(200, json=jobs[request.url.path.rsplit("/", 1)[-1]])
    with httpx.Client(base_url="https://kubernetes", transport=httpx.MockTransport(handler)) as client:
        dispatcher = ReplayDispatcher(service.db, template, client)
        dispatcher.run_once()
        assert jobs == {}
        mode[0] = "online"
        dispatcher.run_once()
        dispatcher.dispatch(bid)  # K8s succeeded but DB acknowledgement was lost.
        dispatcher.run_once()  # Existing Job handles repeated reconciliation.
        assert len(jobs) == 1
        job = next(iter(jobs.values()))
        assert job["spec"]["template"]["spec"]["containers"][0]["env"] == [{"name": "BUNDLE_ID", "value": bid}]

def test_database_readiness_without_schema(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 503

def test_model_and_list_have_independent_identifiers(service, signer):
    model = service.intake(signer[1]("model-v3.zip"))
    assert service.process(model.import_id) == "APPROVED"
    listing = service.intake(signer[1]("list-v14.zip", lambda f, m: m.update(item_id="model-v3")))
    assert service.process(listing.import_id) == "APPROVED"
    with service.db.sessions() as s:
        assert s.get(AIModel, "model-v3") is not None
        assert s.get(SanctionsList, "model-v3") is not None
        model_import = s.get(Import, model.import_id)
        list_import = s.get(Import, listing.import_id)
        assert (model_import.model_id, model_import.list_id) == ("model-v3", None)
        assert (list_import.model_id, list_import.list_id) == (None, "model-v3")
        assert model_import.bundle_id == list_import.bundle_id
    result = client_for(service).get("/bundles/" + service.registered_bundle(listing.import_id)).json()
    assert result["model"]["model_id"] == result["list"]["list_id"] == "model-v3"

def test_restoring_previous_stable_via_transition(service):
    old = baseline(service)
    new = service.registered_bundle(upload(service, "model-v4.zip"))
    transition(service.db, new, "approved", "C", "passed")
    transition(service.db, new, "active", "D", "deploy")
    transition(service.db, old, "active", "D", "rollback")
    with service.db.sessions() as s:
        assert s.get(Bundle, new).status == "rolled_back"
        assert s.get(Bundle, old).status == "active"

def test_artifact_tampering_is_rejected(service):
    imp = service.intake((service.cfg.trust_key.parent / "packages/model-v3.zip").read_bytes())
    service.artifacts.store._path(imp.artifact_uri).write_bytes(b"tampered")
    assert service.process(imp.import_id) == "REJECTED"


def test_import_cannot_reference_both_asset_types(service):
    from sqlalchemy.exc import IntegrityError
    baseline(service)
    with pytest.raises(IntegrityError):
        with service.db.transaction() as s:
            s.add(Import(import_id="invalid", artifact_uri="file://invalid",
                         model_id="model-v3", list_id="list-v14"))


def test_bundle_foreign_keys_require_correct_asset_type(service):
    from sqlalchemy.exc import IntegrityError
    baseline(service)
    with pytest.raises(IntegrityError):
        with service.db.transaction() as s:
            s.add(Bundle(bundle_id="B-999", model_id="list-v14", list_id="model-v3"))


def test_legacy_schema_is_rejected_without_changes(cfg):
    from sqlalchemy import inspect, text
    db = Database(cfg.database_url)
    try:
        with db.engine.begin() as conn:
            conn.execute(text("CREATE TABLE components (component_id VARCHAR(80) PRIMARY KEY)"))
            conn.execute(text("INSERT INTO components VALUES ('legacy-model')"))
        with pytest.raises(RuntimeError, match="Missing ai_models"):
            db.check_schema()
        assert inspect(db.engine).get_table_names() == ["components"]
        with db.engine.connect() as conn:
            assert conn.scalar(text("SELECT component_id FROM components")) == "legacy-model"
    finally:
        db.engine.dispose()


@pytest.mark.parametrize("name", ["model-v4-bad-signature.zip", "list-v15-tampered.zip",
                                   "list-v15-bad-schema.zip", "model-v4-incompatible.zip", None])
def test_invalid_intake_never_persists(service, monkeypatch, name):
    from b_registry.verify.validation import ValidationFailure
    raw = (service.cfg.trust_key.parent / "packages" / name).read_bytes() if name else b"invalid ZIP"
    writes = []
    monkeypatch.setattr(service.artifacts, "put", lambda data: writes.append(data))
    with pytest.raises(ValidationFailure):
        service.intake(raw)
    response = client_for(service).post("/imports", files={"package": ("bad.zip", raw)})
    assert response.status_code == 422
    assert response.json()["code"] == C.ERR_VALIDATION
    assert writes == []
    with service.db.sessions() as s:
        assert list(s.scalars(select(Import))) == []
        assert list(s.scalars(select(AIModel))) == []
        assert list(s.scalars(select(SanctionsList))) == []
        assert list(s.scalars(select(Bundle))) == []


def test_trust_key_unavailable_never_persists(service, monkeypatch):
    raw = (service.cfg.trust_key.parent / "packages/model-v3.zip").read_bytes()
    service.cfg.trust_key = service.cfg.storage / "missing.pub"
    writes = []
    monkeypatch.setattr(service.artifacts, "put", lambda data: writes.append(data))
    response = client_for(service).post("/imports", files={"package": ("model.zip", raw)})
    assert response.status_code == 503
    assert writes == []
    with service.db.sessions() as s:
        assert list(s.scalars(select(Import))) == []


def test_metadata_schema_is_minimal_and_legacy_split_schema_rejected(cfg):
    from sqlalchemy import inspect, text
    from b_registry.db import AIModel, SanctionsList, Import, WorkflowLock
    assert set(Bundle.__table__.columns.keys()) == {"bundle_id", "model_id", "list_id", "status", "previous_active_id", "created_at"}
    assert set(AIModel.__table__.columns.keys()) == {"model_id", "artifact_uri", "created_at"}
    assert set(SanctionsList.__table__.columns.keys()) == {"list_id", "artifact_uri", "created_at"}
    assert "package_hash" not in Import.__table__.columns and "steps" not in Import.__table__.columns
    assert set(WorkflowLock.__table__.columns.keys()) == {"id"}
    db = Database(cfg.database_url)
    try:
        with db.engine.begin() as conn:
            conn.execute(text("CREATE TABLE ai_models (model_id VARCHAR(80) PRIMARY KEY, sha256 VARCHAR(64))"))
        with pytest.raises(RuntimeError, match="Legacy ai_models schema"):
            db.check_schema()
        assert inspect(db.engine).get_table_names() == ["ai_models"]
    finally:
        db.engine.dispose()
