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
from b_registry.verify.validation import ValidationFailure

def upload(service, name):
    imp = service.intake((service.cfg.trust_key.parent / "packages" / name).read_bytes())
    return imp.import_id

def baseline(service):
    model = upload(service, "model-v3.zip")
    listing = upload(service, "list-v14.zip")
    bid = service.registered_bundle(listing)
    assert service.registered_bundle(model) == bid
    for state in ("validated", "approved", "canary", "active"):
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
    assert client.get("/bundles/active").json() == {"active": None, "canary": None}
    response = client.post("/imports", files={"package": ("model.zip", (service.cfg.trust_key.parent / "packages/model-v3.zip").read_bytes())},
                           headers={"X-Request-ID": "check-123"})
    assert response.status_code == 201 and response.headers["X-Request-ID"] == "check-123"
    iid = response.json()["import_id"]
    assert response.headers["Location"] == "/imports/" + iid
    assert client.get("/imports/" + iid).json() == {"status": "APPROVED", "reason": None, "bundle_id": None, "filename": "model.zip"}
    assert client.post(f"/imports/{iid}/approve").status_code == 409
    listing = upload(service, "list-v14.zip")
    bid = service.registered_bundle(listing)
    assert bid == C.BASELINE_BUNDLE
    assert client.post(f"/imports/{iid}/approve").json() == {"bundle_id": bid}
    result = client.get(f"/imports/{listing}").json()
    assert result == {"status": "APPROVED", "reason": None, "bundle_id": bid, "filename": None}
    bundle = client.get("/bundles/" + bid).json()
    assert bundle["model_id"] == bundle["model"]["model_id"] == "model-v3"
    assert bundle["list_id"] == bundle["list"]["list_id"] == "list-v14"
    assert "model_component_id" not in bundle
    assert bundle["status"] == "candidate"
    assert bundle["created_at"].endswith("+00:00")
    assert not {"rules_version", "replay"} & bundle.keys()
    for field, filename in (("model", "model-v3.zip"), ("list", "list-v14.zip")):
        assert set(bundle[field]) == {field + "_id", "uri"}
        assert service.artifacts.get(bundle[field]["uri"]) == (service.cfg.trust_key.parent / "packages" / filename).read_bytes()
    assert "is_active" not in bundle and "model_version" not in bundle and "list_version" not in bundle
    assert "bundle_id=" in client.get("/metrics").text

def test_transition_canary_cancel_and_promotion(service):
    old = baseline(service)
    new = service.registered_bundle(upload(service, "model-v4.zip"))
    client = client_for(service)
    def change(state):
        return client.post(f"/bundles/{new}/transitions", json={"to": state, "actor": "C", "reason": "test"})
    assert change("active").status_code == 409
    assert change("canary").status_code == 409
    assert change("validated").status_code == 200
    assert change("approved").status_code == 200
    assert change("active").status_code == 409
    assert change("canary").status_code == 409
    transition(service.db, new, "canary", "D", "accepted")
    deployed = client.get("/bundles/active").json()
    assert deployed["active"]["bundle_id"] == old
    assert deployed["canary"]["bundle_id"] == new
    assert change("active").status_code == 200
    assert client.get("/bundles/" + old).json()["status"] == "superseded"
    assert client.get("/bundles/active").json()["canary"] is None
    assert change("rolled_back").status_code == 422

@pytest.mark.parametrize("name", ["list-v16-large-change.zip"])
def test_rejections_leave_active_unchanged(service, name):
    bid = baseline(service)
    with service.db.sessions() as s:
        before = len(list(s.scalars(select(Import))))
    with pytest.raises(ValidationFailure, match="exceeds"):
        upload(service, name)
    with service.db.sessions() as s:
        assert len(list(s.scalars(select(Import)))) == before + 1
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
    with service.db.sessions() as s:
        assert len(list(s.scalars(select(Bundle)))) == 2

def test_storage_failure_never_registers(service, monkeypatch):
    monkeypatch.setattr(service.artifacts, "put", lambda _: (_ for _ in ()).throw(StorageUnavailable("offline")))
    with pytest.raises(StorageUnavailable):
        upload(service, "model-v3.zip")
    with service.db.sessions() as s:
        assert list(s.scalars(select(Import))) == []
        assert list(s.scalars(select(AIModel))) == []

def test_api_returns_only_after_registration(service):
    with client_for(service) as client:
        response = client.post("/imports", files={"package": ("list.zip", (service.cfg.trust_key.parent / "packages/list-v14.zip").read_bytes())})
        iid = response.json()["import_id"]
        assert response.status_code == 201
        result = client.get("/imports/" + iid)
        assert result.status_code == 200
        assert result.json() == {"status": "APPROVED", "reason": None, "bundle_id": None, "filename": "list.zip"}

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
    assert first.status == "APPROVED"
    with pytest.raises(ValidationFailure, match="Another package"):
        service.intake(signer[1]("model-v3.zip", lambda f, m: m.update(issued_at="2026-11-13T00:00:00+00:00")))
    with service.db.sessions() as s:
        assert len(list(s.scalars(select(AIModel)))) == 1

def test_no_update_before_baseline_activation(service):
    upload(service, "model-v3.zip")
    upload(service, "list-v14.zip")
    with pytest.raises(Conflict):
        upload(service, "model-v4.zip")
    with service.db.sessions() as s:
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
    assert model.status == "APPROVED"
    listing = service.intake(signer[1]("list-v14.zip", lambda f, m: m.update(item_id="model-v3")))
    assert listing.status == "APPROVED"
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
    transition(service.db, new, "validated", "C", "passed")
    transition(service.db, new, "approved", "C", "passed")
    transition(service.db, new, "canary", "D", "start")
    transition(service.db, new, "active", "D", "deploy")
    transition(service.db, old, "active", "D", "rollback")
    with service.db.sessions() as s:
        assert s.get(Bundle, new).status == "cancelled"
        assert s.get(Bundle, old).status == "active"

def test_artifact_tampering_is_rejected(service):
    imp = service.intake((service.cfg.trust_key.parent / "packages/model-v3.zip").read_bytes())
    service.artifacts.store._path(imp.artifact_uri).write_bytes(b"tampered")
    with pytest.raises(StorageUnavailable):
        upload(service, "model-v3.zip")


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
def test_invalid_intake_records_metadata_without_file(service, monkeypatch, name):
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
        imports = list(s.scalars(select(Import)))
        assert len(imports) == 2
        assert all(i.status == "REJECTED" and i.reason and i.artifact_uri is None for i in imports)
        imp = s.get(Import, response.json()["target_id"])
        assert imp.filename == "bad.zip" and imp.completed_at
        assert imp.model_id is None and imp.list_id is None and imp.bundle_id is None
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
    assert set(Bundle.__table__.columns.keys()) == {"bundle_id", "model_id", "list_id", "status", "previous_active_id", "base_bundle_id", "history", "created_at"}
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


@pytest.mark.parametrize("outcome", ["validation_failed", "rejected"])
def test_additional_validation_failure_never_deploys(service, outcome):
    old = baseline(service)
    new = service.registered_bundle(upload(service, "model-v4.zip"))
    if outcome == "rejected":
        transition(service.db, new, "validated", "C", "passed")
    transition(service.db, new, outcome, "C", "failed gate")
    for state in ("canary", "active"):
        with pytest.raises(Conflict):
            transition(service.db, new, state, "D", "deploy")
    assert client_for(service).get("/bundles/active").json()["active"]["bundle_id"] == old


def test_canary_cancellation_and_single_canary(service):
    old = baseline(service)
    new = service.registered_bundle(upload(service, "model-v4.zip"))
    other = service.registered_bundle(upload(service, "list-v15.zip"))
    for bid in (new, other):
        for state in ("validated", "approved"):
            transition(service.db, bid, state, "C", "passed")
    transition(service.db, new, "canary", "D", "start")
    with pytest.raises(Conflict, match="already running"):
        transition(service.db, other, "canary", "D", "start")
    transition(service.db, new, "cancelled", "D", "cancel during canary")
    result = client_for(service).get("/bundles/active").json()
    assert result["active"]["bundle_id"] == old and result["canary"] is None
    with pytest.raises(Conflict):
        transition(service.db, new, "active", "D", "late callback")
    transition(service.db, other, "canary", "D", "next")


def test_initial_canary_without_stable(service):
    upload(service, "model-v3.zip")
    bid = service.registered_bundle(upload(service, "list-v14.zip"))
    for state in ("validated", "approved", "canary"):
        transition(service.db, bid, state, "D", "initial")
    result = client_for(service).get("/bundles/active").json()
    assert result["active"] is None and result["canary"]["bundle_id"] == bid
    transition(service.db, bid, "active", "D", "promote")


def test_policy_rejection_does_not_write_files_or_rows(service, monkeypatch):
    baseline(service)
    writes = []
    monkeypatch.setattr(service.artifacts, "put", lambda raw: writes.append(raw))
    raw = (service.cfg.trust_key.parent / "packages/list-v16-large-change.zip").read_bytes()
    response = client_for(service).post("/imports", files={"package": ("list.zip", raw)})
    assert response.status_code == 422
    assert writes == []
    with service.db.sessions() as s:
        imports = list(s.scalars(select(Import)))
        assert len(imports) == 3
        rejected = s.get(Import, response.json()["target_id"])
        assert rejected.status == "REJECTED" and rejected.reason and rejected.filename == "list.zip"
        assert rejected.artifact_uri is None


def test_stale_model_bundle_cannot_drop_new_list(service):
    old = baseline(service)
    list_bundle = service.registered_bundle(upload(service, "list-v15.zip"))
    model_bundle = service.registered_bundle(upload(service, "model-v4.zip"))
    for bid in (list_bundle, model_bundle):
        for state in ("validated", "approved"):
            transition(service.db, bid, state, "reviewer", "checked")
    for state in ("canary", "active"):
        transition(service.db, list_bundle, state, "deployer", "promote list")
    client = client_for(service)
    before = client.get(f"/bundles/{model_bundle}").json()
    assert before["base_bundle_id"] == old
    response = client.post(f"/bundles/{model_bundle}/deployments",
                           json={"actor":"deployer", "reason":"promote model"})
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    replacement_id = response.json()["replacement_bundle_id"]
    after = client.get(f"/bundles/{model_bundle}").json()
    assert after["status"] == "cancelled"
    assert after["model_id"] == before["model_id"] and after["list_id"] == before["list_id"]
    replacement = client.get(f"/bundles/{replacement_id}").json()
    assert replacement["status"] == "candidate"
    assert (replacement["model_id"], replacement["list_id"]) == ("model-v4", "list-v15")
    assert replacement["base_bundle_id"] == list_bundle
    assert replacement["history"] == []
    assert after["history"][-1]["replacement_bundle_id"] == replacement_id
    assert client.get("/bundles/active").json()["canary"] is None
    assert client.get("/bundles/active").json()["active"]["list_id"] == "list-v15"
    with pytest.raises(Conflict):
        transition(service.db, replacement_id, "canary", "D", "must revalidate")
    for state in ("validated", "approved", "canary", "active"):
        transition(service.db, replacement_id, state, "test", "new combination passed")


def test_transition_history_persists_both_sides_of_restore(service):
    old = baseline(service)
    new = service.registered_bundle(upload(service, "model-v4.zip"))
    for state in ("validated", "approved", "canary", "active"):
        result = transition(service.db, new, state, "operator-42", "verified rollout")
    transition(service.db, old, "active", "operator-17", "restore stable")
    with service.db.sessions() as s:
        changed, restored = s.get(Bundle, new), s.get(Bundle, old)
        assert len(changed.history) == 5
        assert changed.history[3] == {**result, "actor":"operator-42", "reason":"verified rollout"}
        for bundle in (changed, restored):
            assert bundle.history[-1]["actor"] == "operator-17"
            assert bundle.history[-1]["reason"] == "restore stable"
            assert bundle.history[-1]["changed_at"].endswith("+00:00")
        assert changed.history[-1]["to"] == "cancelled"
        assert restored.history[-1]["to"] == "active"


def test_rejected_filename_is_metadata_only(service):
    client = client_for(service)
    response = client.post("/imports", files={"package": ("../../untrusted.zip", b"not zip")})
    assert response.status_code == 422
    result = client.get("/imports/" + response.json()["target_id"])
    assert result.status_code == 200
    assert result.json()["filename"] == "untrusted.zip"
    assert result.json()["status"] == "REJECTED"
    assert result.json()["reason"]


def test_stale_list_bundle_keeps_list_and_uses_current_model(service):
    baseline(service)
    listing = service.registered_bundle(upload(service, "list-v15.zip"))
    model = service.registered_bundle(upload(service, "model-v4.zip"))
    for bid in (listing, model):
        for state in ("validated", "approved"):
            transition(service.db, bid, state, "reviewer", "approved")
    for state in ("canary", "active"):
        transition(service.db, model, state, "D", "deploy")
    from b_registry.verify.deployment import request_deployment
    result = request_deployment(service.db, listing, "D", "deploy list", None)
    with service.db.sessions() as s:
        candidate = s.get(Bundle, result["replacement_bundle_id"])
        assert (candidate.model_id, candidate.list_id, candidate.base_bundle_id) == ("model-v4", "list-v15", model)
        assert candidate.status == "candidate"
        assert s.get(Bundle, listing).status == "cancelled"
