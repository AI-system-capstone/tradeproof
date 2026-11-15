"""검증 API (C, 포트 8200). 명세: docs/openapi/c_replay.json

  POST /validations                 검증 등록 → 202   (같은 번들에 진행 중 검증 있음 409)
  GET  /validations                 최근 등록 순 목록
  GET  /validations/{id}            상태·결과
  GET  /validations/{id}/diffs      달라진 거래       (completed 가 아니면 409)
  GET  /admin                       운영 화면 (c_replay/ui/admin.html)
  GET  /healthz /livez /metrics

검증 자체(골든셋 검증·Replay)는 검증 Job 이 하고 c_replay.store 의 start/complete/fail 로 결과를 적는다.

환경변수:
  DATABASE_URL   있으면 Postgres, 없으면 메모리 저장소
  BUNDLE_ID      baseline_bundle_id 를 생략했을 때 쓰는 운영 번들 (기본 contracts.BASELINE_BUNDLE = B-7)
"""
from __future__ import annotations

import logging
import os
import uuid
from enum import Enum
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from common import contracts as C
from c_replay import store as S

ADMIN_HTML = Path(__file__).resolve().parent.parent / "ui" / "admin.html"

# TODO(계약): 지표 이름과 검증 상태값은 common/contracts.py 에 추가되면 그쪽 상수로 바꾼다
METRIC_RUNS = "tradeproof_validation_runs_total"              # counter {status}  끝난 검증(completed·failed) 수
METRIC_DURATION = "tradeproof_validation_duration_seconds"    # gauge   {bundle_id}  번들별 최근 완료 건

log = logging.getLogger("c_replay")

ValidationStatus = Literal["queued", "running", "completed", "failed"]
RiskDirection = Literal["up", "down"]
Classification = Enum("Classification", {v: v for v in C.CLASSIFICATIONS}, type=str)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, target_id: str | None = None):
        self.status, self.code, self.message, self.target_id = status, code, message, target_id


def _label(value: str) -> str:
    """Prometheus 라벨 값 이스케이프."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _error(status: int, code: str, message: str, target_id: str | None = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"code": code, "message": message, "target_id": target_id})


class Error(BaseModel):
    code: str
    message: str
    target_id: str | None


class ValidationRequest(BaseModel):
    bundle_id: str = Field(min_length=1, max_length=64, description="검증할 후보 번들", examples=["B-9"])
    baseline_bundle_id: str | None = Field(default=None, description="비교 기준 번들. 생략하면 현재 운영 번들",
                                           examples=["B-7"])


class Metric(BaseModel):
    baseline: float
    candidate: float
    diff: float = Field(description="candidate - baseline")


class GoldenResult(BaseModel):
    golden_version: str
    total: int
    accuracy: Metric
    false_negative: Metric
    false_positive: Metric


class ReplayResult(BaseModel):
    total: int
    processed: int
    count_match: bool
    changed: int
    changed_down: int


class Validation(BaseModel):
    validation_id: str
    bundle_id: str
    baseline_bundle_id: str
    status: ValidationStatus
    golden: GoldenResult | None
    replay: ReplayResult | None
    error: Error | None
    requested_by: str
    created_at: str
    started_at: str | None
    finished_at: str | None


class ValidationList(BaseModel):
    items: list[Validation]
    total: int


class DiffSide(BaseModel):
    bundle_id: str
    classification: Classification
    reasons: list[dict]


class FieldChange(BaseModel):
    field: str
    before: str | None
    after: str | None


class Diff(BaseModel):
    tx_id: str
    direction: RiskDirection
    before: DiffSide
    after: DiffSide
    fields_changed: list[FieldChange]


class DiffList(BaseModel):
    items: list[Diff]
    total: int


_ERR = {"model": Error}
_Limit = Query(default=50, ge=1, le=200)
_Offset = Query(default=0, ge=0)


def create_app(store=None, env: dict | None = None) -> FastAPI:
    env = os.environ if env is None else env
    store = store or S.make_store(env.get("DATABASE_URL"))
    # 가정: B 의 GET /bundles/active 가 아직 없어서, 운영 번들은 환경변수 BUNDLE_ID 로 받는다
    default_baseline = env.get("BUNDLE_ID", C.BASELINE_BUNDLE)

    app = FastAPI(title="TradeProof c_replay — 검증 API (M3) + 운영 화면 (M8)", version="0.1.0")
    app.state.store = store

    # ------------------------------------------------------------ 공통: 요청 ID·에러 형식
    @app.middleware("http")
    async def request_id(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        try:
            response = await call_next(request)
        except Exception:  # noqa: BLE001  처리되지 않은 오류도 공통 형식으로
            log.exception("처리되지 않은 오류 request_id=%s %s %s", rid, request.method, request.url.path)
            response = _error(500, C.ERR_INTERNAL, "내부 오류")
        response.headers["X-Request-ID"] = rid
        return response

    @app.exception_handler(ApiError)
    async def on_api_error(_: Request, e: ApiError):
        return _error(e.status, e.code, e.message, e.target_id)

    @app.exception_handler(S.StoreUnavailable)
    async def on_store_down(_: Request, e: S.StoreUnavailable):
        return _error(503, C.ERR_UNAVAILABLE, f"저장소에 연결할 수 없음: {e}")

    @app.exception_handler(RequestValidationError)
    async def on_validation(_: Request, e: RequestValidationError):
        first = e.errors()[0] if e.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", []))
        return _error(422, C.ERR_VALIDATION, f"{where}: {first.get('msg', '요청 형식 오류')}")

    @app.exception_handler(StarletteHTTPException)
    async def on_http(_: Request, e: StarletteHTTPException):
        code = {404: C.ERR_NOT_FOUND, 409: C.ERR_CONFLICT, 503: C.ERR_UNAVAILABLE}.get(
            e.status_code, C.ERR_INTERNAL if e.status_code >= 500 else C.ERR_VALIDATION)
        return _error(e.status_code, code, str(e.detail))

    def load(validation_id: str) -> dict:
        v = store.get(validation_id)
        if v is None:
            raise ApiError(404, C.ERR_NOT_FOUND, "검증을 찾을 수 없음", validation_id)
        return v

    # ------------------------------------------------------------ 검증
    @app.post("/validations", status_code=202, response_model=Validation, tags=["validations"],
              responses={409: _ERR, 422: _ERR, 503: _ERR}, summary="검증 등록")
    def create_validation(body: ValidationRequest,
                          x_actor: str = Header(min_length=1, max_length=64, description="요청한 사람 또는 모듈")):
        try:
            return store.create(body.bundle_id, body.baseline_bundle_id or default_baseline, x_actor)
        except S.InProgressExists:
            raise ApiError(409, C.ERR_CONFLICT, "이 번들에 진행 중인 검증이 있음", body.bundle_id) from None

    @app.get("/validations", response_model=ValidationList, tags=["validations"],
             responses={422: _ERR}, summary="검증 목록")
    def list_validations(bundle_id: str | None = None, status: ValidationStatus | None = None,
                         limit: int = _Limit, offset: int = _Offset):
        items, total = store.list(bundle_id, status, limit, offset)
        return {"items": items, "total": total}

    @app.get("/validations/{validation_id}", response_model=Validation, tags=["validations"],
             responses={404: _ERR}, summary="검증 상태·결과")
    def get_validation(validation_id: str):
        return load(validation_id)

    @app.get("/validations/{validation_id}/diffs", response_model=DiffList, tags=["validations"],
             responses={404: _ERR, 409: _ERR}, summary="달라진 거래 (영향 거래)")
    def list_validation_diffs(validation_id: str, direction: RiskDirection | None = None,
                              limit: int = _Limit, offset: int = _Offset):
        v = load(validation_id)
        if v["status"] != S.COMPLETED:
            raise ApiError(409, C.ERR_CONFLICT, f"검증이 끝나지 않음 (상태: {v['status']})", validation_id)
        items, total = store.diffs(validation_id, direction, limit, offset)
        return {"items": items, "total": total}

    # ------------------------------------------------------------ 운영 화면
    @app.get("/admin", response_class=HTMLResponse, tags=["ui"], responses={404: _ERR}, summary="운영 화면 (M8)")
    def admin_page():
        if not ADMIN_HTML.is_file():
            raise ApiError(404, C.ERR_NOT_FOUND, "운영 화면 파일이 없음", "admin.html")
        return FileResponse(ADMIN_HTML, media_type="text/html")

    # ------------------------------------------------------------ 상태 확인·지표
    @app.get("/healthz", tags=["ops"], responses={503: _ERR}, summary="준비 상태 (DB 연결)")
    def healthz():
        try:
            store.ping()
        except Exception as e:  # noqa: BLE001
            raise ApiError(503, C.ERR_UNAVAILABLE, f"저장소에 연결할 수 없음: {type(e).__name__}") from e
        return {"status": "ok", "store": store.kind}

    @app.get("/livez", tags=["ops"], summary="생존 (항상 200)")
    def livez():
        return {"status": "ok"}

    @app.get("/metrics", response_class=PlainTextResponse, tags=["ops"], summary="Prometheus 지표")
    def metrics():
        stats = store.stats()
        lines = [f"# HELP {METRIC_RUNS} 끝난 검증 수 (상태별)", f"# TYPE {METRIC_RUNS} counter"]
        for s in (S.COMPLETED, S.FAILED):
            lines.append(f'{METRIC_RUNS}{{status="{s}"}} {stats["by_status"].get(s, 0)}')
        lines += [f"# HELP {METRIC_DURATION} 번들별 최근 완료 검증의 소요 시간(초)", f"# TYPE {METRIC_DURATION} gauge"]
        for bundle_id, seconds in sorted(stats["duration_seconds"].items()):
            lines.append(f'{METRIC_DURATION}{{bundle_id="{_label(bundle_id)}"}} {seconds:.3f}')
        return "\n".join(lines) + "\n"

    return app


app = create_app()
