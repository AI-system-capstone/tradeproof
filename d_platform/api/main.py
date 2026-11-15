"""배포 API (D, 포트 8300).

  POST /deployments                  배포 시작 → 202   (approved 아님 409, 진행 중 배포 있음 409)
  GET  /deployments/{id}             상태·비율(weight)·분석 결과
  POST /deployments/{id}/rollback    직전 번들로 되돌림 → 200
  GET  /deployments                  최근 배포 목록
  GET  /healthz /livez /metrics

환경변수:
  DATABASE_URL           있으면 Postgres, 없으면 메모리 저장소
  DEPLOY_DRIVER          fake (기본). 실제 Argo Rollouts 드라이버는 k3s 단계에서 추가
  FAKE_STEP_SECONDS      가짜 드라이버의 단계당 시간(초), 기본 5
  FAKE_FAIL_BUNDLES      가짜 드라이버에서 분석이 실패하는 번들(쉼표 구분) → 자동 롤백 흉내
  REGISTRY_MODE          fake (기본) | http
  REGISTRY_URL           http 일 때 B 주소
  FAKE_APPROVED_BUNDLES  fake 일 때 승인된 것으로 볼 번들(쉼표 구분)
  BUNDLE_ID              배포 이력이 없을 때의 최초 stable 번들 (기본 B-1)
  CANARY_STEPS           canary 단계(쉼표 구분), 기본 10,50,100
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime
from typing import Callable

from fastapi import FastAPI, Header, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from d_platform.api import models as M
from d_platform.api.drivers import make_driver
from d_platform.api.registry import APPROVED, RegistryUnavailable, make_registry
from d_platform.api.store import InProgressExists, finish, make_store

ERR_VALIDATION = "VALIDATION_ERROR"
ERR_NOT_FOUND = "NOT_FOUND"
ERR_CONFLICT = "CONFLICT"
ERR_UNAVAILABLE = "SERVICE_UNAVAILABLE"
ERR_INTERNAL = "INTERNAL_ERROR"

log = logging.getLogger("d_platform")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, target_id: str | None = None):
        self.status, self.code, self.message, self.target_id = status, code, message, target_id


def _error(status: int, code: str, message: str, target_id: str | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": message, "target_id": target_id})


def _csv(value: str | None) -> frozenset[str]:
    return frozenset(v.strip() for v in (value or "").split(",") if v.strip())


def _log(**fields) -> None:
    fields.setdefault("ts", M.iso(M.now_utc()))
    fields.setdefault("module", "d_platform")
    log.info(json.dumps(fields, ensure_ascii=False))


class DeploymentRequest(BaseModel):
    # TODO(B 확인 필요): 요청은 B(M2)가 보낸다. 지금은 bundle_id 만 받는다 (approval_id 등은 정해지면 추가)
    bundle_id: str = Field(min_length=1, max_length=64, examples=["B-2"])


class Analysis(BaseModel):
    result: str | None = Field(default=None, description="running | passed | failed")
    checks: list[dict] = Field(default_factory=list, description="단계별 분석 결과")


class DeploymentResponse(BaseModel):
    deployment_id: str
    bundle_id: str
    previous_bundle_id: str
    status: str = Field(description=" | ".join(M.STATUSES))
    weight: int = Field(description="후보 번들로 가는 트래픽 비율(%)")
    step_index: int
    steps: list[int]
    analysis: Analysis
    rollback_reason: str | None = Field(default=None, description="manual | analysis_failed")
    requested_by: str
    created_at: str
    updated_at: str
    finished_at: str | None = None


class ErrorResponse(BaseModel):
    code: str
    message: str
    target_id: str | None = None


_ERR = {"model": ErrorResponse}


def create_app(store=None, driver=None, registry=None,
               clock: Callable[[], datetime] = M.now_utc, env: dict | None = None) -> FastAPI:
    env = os.environ if env is None else env
    store = store or make_store(env.get("DATABASE_URL"))
    driver = driver or make_driver(env.get("DEPLOY_DRIVER", "fake"),
                                   float(env.get("FAKE_STEP_SECONDS", "5")),
                                   _csv(env.get("FAKE_FAIL_BUNDLES")))
    registry = registry or make_registry(env.get("REGISTRY_MODE", "fake"), env.get("REGISTRY_URL"),
                                         _csv(env.get("FAKE_APPROVED_BUNDLES")))
    initial_bundle = env.get("BUNDLE_ID", "B-1")
    steps = [int(s) for s in env.get("CANARY_STEPS", "10,50,100").split(",")]

    app = FastAPI(title="TradeProof d_platform — 배포 API", version="0.1.0")
    app.state.store, app.state.driver, app.state.registry = store, driver, registry

    # ------------------------------------------------------------ 공통: 로그·요청 ID·에러 형식
    @app.middleware("http")
    async def request_log(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        request.state.request_id = rid
        t0 = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception as e:  # noqa: BLE001  처리되지 않은 오류도 공통 형식으로
            _log(level="error", request_id=rid, method=request.method, path=request.url.path,
                 status=500, error=repr(e))
            response = _error(500, ERR_INTERNAL, "내부 오류")
        response.headers["X-Request-ID"] = rid
        if request.url.path not in ("/healthz", "/livez", "/metrics"):
            _log(level="info", request_id=rid, actor=request.headers.get("X-Actor"),
                 method=request.method, path=request.url.path, status=response.status_code,
                 duration_ms=round((time.perf_counter() - t0) * 1000, 1))
        return response

    @app.exception_handler(ApiError)
    async def on_api_error(_: Request, e: ApiError):
        return _error(e.status, e.code, e.message, e.target_id)

    @app.exception_handler(RequestValidationError)
    async def on_validation(_: Request, e: RequestValidationError):
        first = e.errors()[0] if e.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", []))
        return _error(422, ERR_VALIDATION, f"{where}: {first.get('msg', '요청 형식 오류')}")

    @app.exception_handler(StarletteHTTPException)
    async def on_http(_: Request, e: StarletteHTTPException):
        code = {404: ERR_NOT_FOUND, 409: ERR_CONFLICT, 422: ERR_VALIDATION, 503: ERR_UNAVAILABLE}.get(
            e.status_code, ERR_INTERNAL if e.status_code >= 500 else ERR_VALIDATION)
        return _error(e.status_code, code, str(e.detail))

    # ------------------------------------------------------------ 상태 맞추기
    def reconcile(dep: M.Deployment) -> M.Deployment:
        """진행 중인 배포를 드라이버가 본 실제 상태에 맞춰 deployments 에 적는다."""
        if not dep.in_progress:
            return dep
        obs = driver.observe(dep, clock())
        before = (dep.status, dep.weight, dep.step_index, dep.analysis)
        dep.weight, dep.step_index, dep.analysis = obs.weight, obs.step_index, obs.analysis
        if obs.status in (M.SUCCEEDED, M.ROLLED_BACK, M.FAILED):
            dep.rollback_reason = obs.rollback_reason
            finish(dep, obs.status, clock())
            if obs.status == M.ROLLED_BACK:
                driver.rollback(dep)
            # TODO(B 확인 필요): 반영 완료·자동 롤백을 B 의 POST /bundles/{id}/transitions 로 알린다
            _log(level="info", event="deployment_finished", deployment_id=dep.deployment_id,
                 bundle_id=dep.bundle_id, status=dep.status, rollback_reason=dep.rollback_reason)
        else:
            dep.status = obs.status
        if before != (dep.status, dep.weight, dep.step_index, dep.analysis):
            store.save(dep)
        return dep

    def load(deployment_id: str) -> M.Deployment:
        dep = store.get(deployment_id)
        if dep is None:
            raise ApiError(404, ERR_NOT_FOUND, "배포를 찾을 수 없음", deployment_id)
        return reconcile(dep)

    def stable_bundle() -> str:
        return store.stable_bundle_id() or initial_bundle

    # ------------------------------------------------------------ 배포
    @app.post("/deployments", status_code=202, response_model=DeploymentResponse,
              responses={404: _ERR, 409: _ERR, 422: _ERR, 503: _ERR}, summary="배포 시작")
    def create_deployment(body: DeploymentRequest, response: Response,
                          x_actor: str = Header(min_length=1, description="요청한 사람·모듈")):
        running = store.in_progress()
        if running is not None and reconcile(running).in_progress:
            raise ApiError(409, ERR_CONFLICT, "진행 중인 배포가 있음", running.deployment_id)

        try:
            bundle_status = registry.bundle_status(body.bundle_id)
        except RegistryUnavailable as e:
            raise ApiError(503, ERR_UNAVAILABLE, f"번들 레지스트리에 연결할 수 없음: {e}", body.bundle_id) from e
        if bundle_status is None:
            raise ApiError(404, ERR_NOT_FOUND, "번들을 찾을 수 없음", body.bundle_id)
        if bundle_status != APPROVED:
            raise ApiError(409, ERR_CONFLICT, f"승인되지 않은 번들 (상태: {bundle_status})", body.bundle_id)

        previous = stable_bundle()
        if previous == body.bundle_id:
            raise ApiError(409, ERR_CONFLICT, "이미 운영 중인 번들", body.bundle_id)

        now = clock()
        dep = M.Deployment(
            deployment_id=M.new_deployment_id(), bundle_id=body.bundle_id, previous_bundle_id=previous,
            status=M.PENDING, requested_by=x_actor, steps=list(steps),
            analysis={"result": "running", "checks": []}, created_at=now, updated_at=now,
        )
        try:
            store.create(dep)
        except InProgressExists:
            other = store.in_progress()
            raise ApiError(409, ERR_CONFLICT, "진행 중인 배포가 있음",
                           other.deployment_id if other else None) from None
        driver.start(dep)
        _log(level="info", event="deployment_started", deployment_id=dep.deployment_id,
             bundle_id=dep.bundle_id, previous_bundle_id=previous, actor=x_actor)
        response.headers["Location"] = f"/deployments/{dep.deployment_id}"
        return dep.to_json()

    @app.get("/deployments", response_model=list[DeploymentResponse], summary="최근 배포 목록")
    def list_deployments(limit: int = 20):
        return [reconcile(d).to_json() for d in store.latest(max(1, min(limit, 100)))]

    @app.get("/deployments/{deployment_id}", response_model=DeploymentResponse,
             responses={404: _ERR}, summary="배포 상태")
    def get_deployment(deployment_id: str):
        return load(deployment_id).to_json()

    @app.post("/deployments/{deployment_id}/rollback", response_model=DeploymentResponse,
              responses={404: _ERR, 409: _ERR, 422: _ERR}, summary="직전 번들로 롤백")
    def rollback(deployment_id: str, x_actor: str = Header(min_length=1)):
        dep = load(deployment_id)
        if dep.status == M.SUCCEEDED:
            # 반영이 끝난 배포는 "지금 운영 중인 것"일 때만 되돌릴 수 있다 (DEP-05: 직전 번들로)
            if store.in_progress() is not None or stable_bundle() != dep.bundle_id \
                    or store.latest(1)[0].deployment_id != dep.deployment_id:
                raise ApiError(409, ERR_CONFLICT, "가장 최근에 반영된 배포만 롤백할 수 있음", deployment_id)
        elif not dep.in_progress:
            raise ApiError(409, ERR_CONFLICT, f"롤백할 수 없는 상태: {dep.status}", deployment_id)

        driver.rollback(dep)
        dep.weight = 0
        dep.rollback_reason = M.ROLLBACK_MANUAL
        dep.analysis = {**dep.analysis, "result": dep.analysis.get("result", "running")}
        finish(dep, M.ROLLED_BACK, clock())
        store.save(dep)
        # TODO(B 확인 필요): B 의 POST /bundles/{id}/transitions 로 rolled_back 기록 (k3s 에서는 Argo 알림이 담당)
        _log(level="info", event="deployment_rolled_back", deployment_id=dep.deployment_id,
             bundle_id=dep.bundle_id, restored_bundle_id=dep.previous_bundle_id, actor=x_actor)
        return dep.to_json()

    # ------------------------------------------------------------ 상태 확인·지표
    @app.get("/livez", include_in_schema=False)
    def livez():
        return {"status": "ok"}

    @app.get("/healthz", responses={503: _ERR}, summary="준비 상태 (저장소 연결)")
    def healthz():
        try:
            store.ping()
        except Exception as e:  # noqa: BLE001
            raise ApiError(503, ERR_UNAVAILABLE, f"저장소에 연결할 수 없음: {type(e).__name__}") from e
        return {"status": "ok", "store": store.kind, "driver": driver.kind, "registry": registry.kind}

    @app.get("/metrics", response_class=PlainTextResponse, summary="Prometheus 지표")
    def metrics():
        running = store.in_progress()
        if running is not None:
            running = reconcile(running)   # 집계 전에 진행 중인 배포를 최신 상태로
        lines = [
            "# HELP tradeproof_platform_deployments 상태별 배포 수",
            "# TYPE tradeproof_platform_deployments gauge",
        ]
        by_status = store.count_by_status()
        for s in M.STATUSES:
            lines.append(f'tradeproof_platform_deployments{{status="{s}"}} {by_status.get(s, 0)}')
        lines += ["# HELP tradeproof_platform_rollbacks 사유별 롤백 수",
                  "# TYPE tradeproof_platform_rollbacks gauge"]
        by_reason = store.count_rollbacks()
        for r in (M.ROLLBACK_MANUAL, M.ROLLBACK_ANALYSIS_FAILED):
            lines.append(f'tradeproof_platform_rollbacks{{reason="{r}"}} {by_reason.get(r, 0)}')
        lines += ["# HELP tradeproof_platform_canary_weight 진행 중인 배포의 후보 번들 트래픽 비율(%)",
                  "# TYPE tradeproof_platform_canary_weight gauge"]
        if running is not None and running.in_progress:
            lines.append(f'tradeproof_platform_canary_weight{{bundle_id="{running.bundle_id}"}} {running.weight}')
        lines += ["# HELP tradeproof_platform_stable_bundle_info 현재 운영(stable) 번들",
                  "# TYPE tradeproof_platform_stable_bundle_info gauge",
                  f'tradeproof_platform_stable_bundle_info{{bundle_id="{stable_bundle()}"}} 1']
        return "\n".join(lines) + "\n"

    return app


def _setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))  # 메시지가 이미 한 줄 JSON
    log.handlers[:] = [handler]
    log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))
    log.propagate = False


_setup_logging()
app = create_app()
