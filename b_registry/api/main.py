import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.concurrency import run_in_threadpool
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from common import contracts as C
from b_registry import contracts as B
from b_registry.api.schemas import ActiveBundles, BundleResult, ErrorResponse, ImportAccepted, BundleRegistered, Transition, TransitionResult, VerificationResult
from b_registry.config import Settings
from b_registry.db import Bundle, AIModel, SanctionsList, Database, Import, iso
from b_registry.storage import Artifacts, StorageUnavailable, make_store
from b_registry.verify.replay import ReplayDispatcher
from b_registry.verify.workflow import Conflict, ImportService, NotFound, transition
from b_registry.verify.worker import ReplayWorker
from b_registry.verify.validation import ValidationFailure

logger = logging.getLogger(__name__)

def error_response(code, message, status, target=None):
    return JSONResponse({"code": code, "message": message, "target_id": target}, status_code=status)

class BodyLimitMiddleware:
    def __init__(self, app, limit):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in ("POST", "PUT", "PATCH"):
            return await self.app(scope, receive, send)
        limit = self.limit + 65536 if scope["path"] == "/imports" else 65536
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > limit:
                return await error_response(C.ERR_VALIDATION, "Request exceeds size limit", 413)(scope, receive, send)
            if not message.get("more_body", False):
                break
        delivered = False
        async def replay():
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}
        await self.app(scope, replay, send)

def asset_fields(asset, id_field):
    return {id_field: getattr(asset, id_field), "uri": asset.artifact_uri}

def bundle_fields(s, bundle):
    return {"bundle_id": bundle.bundle_id, "status": bundle.status,
            "model_id": bundle.model_id,
            "list_id": bundle.list_id, "created_at": iso(bundle.created_at),
            "model": asset_fields(s.get(AIModel, bundle.model_id), "model_id"),
            "list": asset_fields(s.get(SanctionsList, bundle.list_id), "list_id")}

def create_app(cfg=None, db=None, store=None):
    logs = logging.getLogger("b_registry")
    if not logs.handlers:
        logs.addHandler(logging.StreamHandler())
    logs.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
    logs.propagate = False
    cfg = cfg or Settings()
    db = db or Database(cfg.database_url)
    artifacts = Artifacts(store or make_store(cfg))
    service = ImportService(db, cfg, artifacts)
    worker = ReplayWorker(ReplayDispatcher(db))

    @asynccontextmanager
    async def lifespan(app):
        worker.start()
        try:
            yield
        finally:
            await run_in_threadpool(worker.stop)

    app = FastAPI(title="Tradeproof Registry", version="1.0.0", lifespan=lifespan,
        responses={n: {"model": ErrorResponse} for n in (400, 404, 409, 413, 422, 500, 503)})
    app.add_middleware(BodyLimitMiddleware, limit=cfg.max_zip_bytes)
    app.state.db, app.state.service, app.state.worker = db, service, worker

    @app.middleware("http")
    async def request_context(request, call_next):
        rid = request.headers.get("X-Request-ID", "")
        rid = rid if re.fullmatch(r"[a-zA-Z0-9_.-]{1,100}", rid) else str(uuid.uuid4())
        try:
            response = await call_next(request)
        except Exception as error:
            logger.error(json.dumps({"event": "registry_error", "request_id": rid, "error": type(error).__name__}))
            response = error_response(C.ERR_INTERNAL, "Internal error", 500)
        response.headers["X-Request-ID"] = rid
        logger.info(json.dumps({"event": "registry_request", "request_id": rid, "method": request.method,
                                "path": request.url.path, "status": response.status_code}))
        return response

    def target(request):
        return request.path_params.get("bundle_id") or request.path_params.get("import_id")

    @app.exception_handler(ValidationFailure)
    async def invalid_package(request, error):
        return error_response(C.ERR_VALIDATION, f"{error.code}: {error.message}", 422)

    @app.exception_handler(Conflict)
    async def conflict(request, error):
        return error_response(C.ERR_CONFLICT, str(error), 409, target(request))

    @app.exception_handler(NotFound)
    async def missing(request, error):
        return error_response(C.ERR_NOT_FOUND, str(error), 404, target(request))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, error):
        code = C.ERR_NOT_FOUND if error.status_code == 404 else C.ERR_VALIDATION
        return error_response(code, str(error.detail), error.status_code, target(request))

    @app.exception_handler(RequestValidationError)
    async def invalid(request, error):
        return error_response(C.ERR_VALIDATION, "Invalid request fields", 422, target(request))

    @app.exception_handler(SQLAlchemyError)
    @app.exception_handler(StorageUnavailable)
    async def unavailable(request, error):
        return error_response(C.ERR_UNAVAILABLE, "Storage unavailable", 503, target(request))

    @app.get("/healthz")
    def health():
        try:
            db.check_schema()
            artifacts.store.ping()
            # The shared S3 adapter caches bucket creation; readiness must check the live connection.
            if artifacts.store.kind == "minio" and not artifacts.store.client.bucket_exists(artifacts.store.bucket):
                raise RuntimeError("Bucket unavailable")
            cfg.trust_key.read_bytes()
        except Exception:
            return error_response(C.ERR_UNAVAILABLE, "Database, artifact storage or trust key unavailable", 503)
        return {"status": "ok"}

    @app.post("/imports", status_code=201, response_model=ImportAccepted)
    async def upload(request: Request, response: Response, package: UploadFile = File(...)):
        form = await request.form()
        if set(form) != {"package"} or len(form.getlist("package")) != 1:
            raise HTTPException(422, "Upload exactly one package ZIP")
        raw = await package.read(cfg.max_zip_bytes + 1)
        if len(raw) > cfg.max_zip_bytes:
            raise HTTPException(413, "Package exceeds size limit")
        imp = await run_in_threadpool(service.intake, raw)
        worker.notify()
        response.headers["Location"] = f"/imports/{imp.import_id}"
        return {"import_id": imp.import_id, "bundle_id": imp.bundle_id}

    @app.get("/imports/{import_id}", response_model=VerificationResult)
    def import_result(import_id: str):
        with db.sessions() as s:
            imp = s.get(Import, import_id)
            if not imp:
                raise NotFound("Import not found")
            return {"status": imp.status, "reason": imp.reason, "bundle_id": imp.bundle_id}

    @app.post("/imports/{import_id}/approve", response_model=BundleRegistered,
              summary="Return the registered bundle (idempotent compatibility endpoint)")
    async def approve(import_id: str, request: Request):
        if await request.body():
            raise HTTPException(422, "This request has no body")
        return {"bundle_id": await run_in_threadpool(service.registered_bundle, import_id)}

    @app.get("/bundles", response_model=list[BundleResult])
    def bundles():
        with db.sessions() as s:
            return [bundle_fields(s, b) for b in s.scalars(select(Bundle).order_by(Bundle.created_at))]

    @app.get("/bundles/active", response_model=ActiveBundles)
    def active_bundle():
        with db.sessions() as s:
            return {state: bundle_fields(s, bundle) if bundle else None
                    for state in ("active", "canary")
                    for bundle in [s.scalar(select(Bundle).where(Bundle.status == state))]}

    @app.get("/bundles/{bundle_id}", response_model=BundleResult)
    def get_bundle(bundle_id: str):
        with db.sessions() as s:
            bundle = s.get(Bundle, bundle_id)
            if not bundle:
                raise NotFound("Bundle not found")
            return bundle_fields(s, bundle)

    @app.post("/bundles/{bundle_id}/transitions", response_model=TransitionResult)
    def change_state(bundle_id: str, body: Transition, request: Request):
        actor = request.headers.get("X-Actor", body.actor)
        if actor != body.actor:
            raise HTTPException(422, "X-Actor and actor must match")
        result = transition(db, bundle_id, body.to, actor, body.reason)
        worker.notify()
        return result

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics():
        with db.sessions() as s:
            lines = [f"# TYPE {name} gauge" for name in (B.METRIC_IMPORTS, B.METRIC_BUNDLES)]
            counts = {}
            for imp in s.scalars(select(Import)):
                key = (imp.bundle_id or "unassigned", imp.status)
                counts[key] = counts.get(key, 0) + 1
            for (bid, status), count in sorted(counts.items()):
                lines.append(f'{B.METRIC_IMPORTS}{{bundle_id="{bid}",status="{status}"}} {count}')
            for bundle in s.scalars(select(Bundle)):
                lines.append(f'{B.METRIC_BUNDLES}{{bundle_id="{bundle.bundle_id}",status="{bundle.status}"}} 1')
            return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
    return app

app = create_app()
