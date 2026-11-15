"""screening-api: 서류 접수·조회·담당자 검토 API (모듈 A). 판정은 하지 않고 큐에 넣기만 한다.

  DATABASE_URL=postgresql://tradeproof:tradeproof@localhost:5432/tradeproof uvicorn api.app:app --port 8000
  DATABASE_URL=... python ocr/worker.py                 # 처리는 ocr-worker 가 한다 (따로 띄움)
  BUNDLE_ID=B-9 ...                                     # 접수 번들 (D 가 배포 때 주입. 예전 이름 ACTIVE_BUNDLE 도 받음)
  SHADOW_BUNDLE=B-9 ...                                 # 섀도 판정 번들 (결과는 참고용, 실제 분류에 안 씀)
  BLOB_ENDPOINT=minio:9000 BLOB_ACCESS_KEY=.. BLOB_SECRET_KEY=..   # 원문 저장소 (없으면 BLOB_DIR / data/blob)

구조 (2주차 큐)
  API  POST /screenings -> 원문은 common.blob, 접수는 screening_jobs(status=queued) -> 202 + tx_id
  ocr-worker  claim(FOR UPDATE SKIP LOCKED) -> OCR -> screen() -> 저장 (ocr/worker.py)
  상태는 모두 Postgres·원문 저장소에 있어서 API 복제본을 여러 개 띄워도 어느 것에 물어도 같은 답이다.
  DATABASE_URL 이 없으면 메모리 저장소(테스트용): 워커가 다른 프로세스라 처리되지 않고 queued 로 남는다.

엔드포인트
  POST /screenings                          접수. JSON {sample_tx_id} | {fields}, 또는 multipart 파일 invoice + bl
                                            fields 는 contracts.SCREENING_FIELDS 가 모두 있어야 함 (비면 422)
  GET  /screenings                          목록 (status, classification(쉼표로 여러 개), bundle_id, unreviewed_first,
                                            limit, offset)
  GET  /screenings/{tx_id}                  처리 상태, 분류, 근거, 섀도 결과
  GET  /screenings/{tx_id}/documents/{doc}  검토 화면용 서류 PNG (doc = contracts.UPLOAD_DOC_TYPES)
  POST /screenings/{tx_id}/review           담당자 최종 판단(+ 필드 정정) 저장
                                            스크리닝 필드를 정정하면 같은 번들로 재판정(rescreen). HOLD 인데 APPROVE 면 409
  GET  /meta                                검토 화면이 쓰는 계약 값 (필드·분류·규칙·번들)
  GET  /review                              담당자 검토 화면 (a_screening/ui/review.html)
  GET  /metrics                             Prometheus 지표 (contracts.METRIC_*)
  GET  /healthz                             readiness: 저장소·원문 저장소 연결 (실패 503)
  GET  /livez                               liveness: 프로세스만 (항상 200)

규칙
  - 번들은 접수 시점에 고정된다 (screening_jobs.bundle_id). 워커는 BUNDLE_ID 를 읽지 않는다.
  - 에러는 모두 공통 형식(11/01 회의 3-3, contracts.ERROR_FIELDS): {"code", "message", "target_id"}
  - 요청마다 한 줄 JSON 로그 (logger "tradeproof.api"). 요청 ID 는 응답 헤더 X-Request-ID 로도 나간다
    (들어온 X-Request-ID 가 안전한 형식이면 그대로 이어 씀: ingress 로그와 맞추기).
  - 500/503 응답 message 에는 일반 문구 + 요청 ID 만. 예외 상세는 같은 요청 ID 로 JSON 로그에.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ValidationError, field_validator, model_validator
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException
from starlette.requests import Request as StarletteRequest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common import contracts as C  # noqa: E402
from common.blob import make_blob_store  # noqa: E402
from a_screening.store import make_store  # noqa: E402
from a_screening.screening.match import is_blank, load_index, screen  # noqa: E402  (검토 정정 재판정: 워커와 같은 순수 함수)


def _check_bundle_env(active: str, shadow: str | None) -> None:
    if active not in C.BUNDLES:
        raise RuntimeError(f"{BUNDLE_ENV}={active!r} 는 contracts.BUNDLES 에 없음 ({list(C.BUNDLES)})")
    if shadow and shadow not in C.BUNDLES:
        raise RuntimeError(f"SHADOW_BUNDLE={shadow!r} 는 contracts.BUNDLES 에 없음 ({list(C.BUNDLES)})")


# 접수 번들: 팀 규약은 BUNDLE_ID (D 가 deploy/ 값으로 주입). 예전 이름 ACTIVE_BUNDLE 도 받는다
BUNDLE_ENV = "BUNDLE_ID" if os.environ.get("BUNDLE_ID") else "ACTIVE_BUNDLE"
ACTIVE_BUNDLE = os.environ.get(BUNDLE_ENV) or C.BASELINE_BUNDLE
SHADOW_BUNDLE = os.environ.get("SHADOW_BUNDLE") or None
_check_bundle_env(ACTIVE_BUNDLE, SHADOW_BUNDLE)

# 서류 한 장 상한 (기본 contracts.MAX_UPLOAD_BYTES, 운영에서 환경변수로 덮어쓸 수 있음)
MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(C.MAX_UPLOAD_BYTES)))
UPLOAD_SLACK_BYTES = 1024 * 1024   # 요청 전체 상한 = 장 수 x MAX_UPLOAD_BYTES + 이 여유(멀티파트 경계·헤더)
REVIEW_HTML = Path(__file__).resolve().parents[1] / "ui" / "review.html"

# 파일 형식은 Content-Type 만 믿지 않고 앞 바이트(contracts.FILE_MAGIC)로도 확인한다 (ocr/worker.py 도 같은 값)
assert set(C.FILE_MAGIC) == set(C.UPLOAD_CONTENT_TYPES.values()), "UPLOAD_CONTENT_TYPES 에 맞는 FILE_MAGIC 필요"

# 저장소·원문 저장소 (테스트에서 monkeypatch 로 바꾼다)
STORE = make_store(os.environ.get("DATABASE_URL"))
BLOB = make_blob_store()

LOG = logging.getLogger("tradeproof.api")
if not LOG.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(message)s"))
    LOG.addHandler(_h)
    LOG.setLevel(logging.INFO)
    LOG.propagate = False

app = FastAPI(title="TradeProof 심사 서비스 (모듈 A)", version="0.3.0")


def shadow_bundle() -> str | None:
    """지금 접수에 붙일 섀도 번들. 주 번들과 같으면 붙이지 않는다 (같은 번들을 두 번 판정할 이유 없음)."""
    return SHADOW_BUNDLE if SHADOW_BUNDLE and SHADOW_BUNDLE != ACTIVE_BUNDLE else None


# ---------------------------------------------------------------- 요청 ID / 요청 로그 (한 줄 JSON)
REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")   # 들어온 ID 는 이 형식일 때만 이어 쓴다 (로그 주입 방지)


def _incoming_request_id(scope) -> str | None:
    for k, v in scope.get("headers") or []:
        if k.lower() == REQUEST_ID_HEADER.lower().encode():
            rid = v.decode("latin-1").strip()
            return rid if _REQUEST_ID_RE.fullmatch(rid) else None
    return None


def request_id_of(request: Request) -> str:
    return getattr(request.state, "request_id", None) or "-"


def _internal_message(rid: str) -> str:
    return f"내부 오류가 발생했습니다. 관리자에게 요청 ID 를 알려 주세요 (요청 ID: {rid})"


def log_error(event: str, request_id: str, exc: BaseException, **kw) -> None:
    """상세 예외는 응답이 아니라 여기(JSON 로그)에만."""
    LOG.error(json.dumps({"event": event, "request_id": request_id, **kw,
                          "error": f"{type(exc).__name__}: {exc}",
                          "traceback": "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))},
                         ensure_ascii=False, default=str))


class RequestLogMiddleware:
    """요청 ID 를 붙이고(scope state + 응답 헤더 X-Request-ID), 끝나면 한 줄 JSON 로그
    (request_id, method, path, status, duration_ms, tx_id). 처리 안 된 예외는 여기서 공통 형식 500 으로 바꾼다
    (예외 상세는 로그에만). 순수 ASGI 라 응답 본문을 건드리지 않는다."""

    def __init__(self, app_):
        self.app = app_

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        t0, status, started = time.perf_counter(), [500], [False]
        rid = _incoming_request_id(scope) or uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = rid
        rid_header = (REQUEST_ID_HEADER.lower().encode(), rid.encode())

        async def _send(msg):
            if msg["type"] == "http.response.start":
                status[0], started[0] = msg["status"], True
                headers = [h for h in msg.get("headers") or [] if h[0].lower() != rid_header[0]]
                msg = {**msg, "headers": headers + [rid_header]}
            await send(msg)

        def _tx_id():
            return (scope.get("state") or {}).get("tx_id") or (scope.get("path_params") or {}).get("tx_id")

        try:
            await self.app(scope, receive, _send)
        except Exception as e:  # noqa: BLE001  공통 형식 500 + 로그 (클라이언트에는 상세를 주지 않음)
            log_error("unhandled_error", rid, e, method=scope["method"], path=scope["path"], tx_id=_tx_id())
            if started[0]:
                raise
            body = json.dumps(error_body(C.ERR_INTERNAL, _internal_message(rid), _tx_id()),
                              ensure_ascii=False).encode()
            await _send({"type": "http.response.start", "status": 500,
                         "headers": [(b"content-type", b"application/json"),
                                     (b"content-length", str(len(body)).encode())]})
            await _send({"type": "http.response.body", "body": body})
        finally:
            rec = {"request_id": rid, "method": scope["method"], "path": scope["path"], "status": status[0],
                   "duration_ms": round((time.perf_counter() - t0) * 1000, 1)}
            tx_id = _tx_id()
            if tx_id:
                rec["tx_id"] = tx_id
            LOG.info(json.dumps(rec, ensure_ascii=False))


app.add_middleware(RequestLogMiddleware)


# ---------------------------------------------------------------- 공통 에러 형식 (contracts.ERROR_FIELDS)
def error_body(code: str, message: str, target_id: str | None = None) -> dict:
    return dict(zip(C.ERROR_FIELDS, (code, message, target_id)))


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, target_id: str | None = None):
        self.status, self.body = status, error_body(code, message, target_id)


_HTTP_CODES = {404: C.ERR_NOT_FOUND, 409: C.ERR_CONFLICT, 422: C.ERR_VALIDATION, 503: C.ERR_UNAVAILABLE}


def _fmt_errors(errors: list[dict], skip: int = 0) -> str:
    return "; ".join(f"{'.'.join(str(x) for x in e['loc'][skip:]) or 'body'}: {e['msg']}" for e in errors)


@app.exception_handler(ApiError)
async def _api_error(_: Request, exc: ApiError):
    return JSONResponse(exc.body, status_code=exc.status)


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    return JSONResponse(error_body(C.ERR_VALIDATION, _fmt_errors(exc.errors(), 1), request.path_params.get("tx_id")),
                        status_code=422)


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):   # 없는 경로·메서드 등 프레임워크 오류
    code = _HTTP_CODES.get(exc.status_code, f"HTTP_{exc.status_code}")
    return JSONResponse(error_body(code, str(exc.detail), request.path_params.get("tx_id")),
                        status_code=exc.status_code)


@app.exception_handler(Exception)
async def _internal_error(request: Request, exc: Exception):
    """(대비용) 보통은 RequestLogMiddleware 가 먼저 500 으로 바꾼다. 상세는 로그에만."""
    rid = request_id_of(request)
    log_error("unhandled_error", rid, exc, method=request.method, path=request.url.path)
    return JSONResponse(error_body(C.ERR_INTERNAL, _internal_message(rid), request.path_params.get("tx_id")),
                        status_code=500, headers={REQUEST_ID_HEADER: rid})


def unavailable(request: Request, what: str, exc: Exception) -> ApiError:
    """503: 응답에는 일반 문구 + 요청 ID, 상세(예외·저장소 종류)는 로그에."""
    rid = request_id_of(request)
    log_error("unavailable", rid, exc, method=request.method, path=request.url.path, target=what)
    return ApiError(503, C.ERR_UNAVAILABLE, f"{what} 연결 실패. 잠시 뒤 다시 시도하세요 (요청 ID: {rid})")


def _check_field_names(d: dict | None) -> dict | None:
    if d is not None:
        unknown = sorted(set(d) - set(C.TX_FIELDS))
        if unknown:
            raise ValueError(f"알 수 없는 필드 {unknown}. 허용: {list(C.TX_FIELDS)}")
    return d


def _is_blank(v) -> bool:
    return is_blank(v)   # screening.match 와 같은 기준 ('-', '.', 폭 없는 공백도 빈 값)


def _check_screening_fields(d: dict, *, only_given: bool) -> None:
    """스크리닝 필드(contracts.SCREENING_FIELDS)가 비었거나 공백뿐이면 거부 (빈 값은 제재 매칭이 안 돼 통과로 샘: 미탐).
    only_given=True 면 d 에 들어 있는 스크리닝 필드만 본다 (검토 정정)."""
    names = [f for f in C.SCREENING_FIELDS if f in d] if only_given else list(C.SCREENING_FIELDS)
    blank = [f for f in names if _is_blank(d.get(f))]
    if blank:
        raise ValueError(f"스크리닝 필드 {blank} 가 비어 있음. {list(C.SCREENING_FIELDS)} 는 "
                         + ("비워서 정정할 수 없음" if only_given else "모두 값이 있어야 함")
                         + " (비어 있으면 제재 스크리닝을 할 수 없음)")


# ---------------------------------------------------------------- 요청 본문
class ScreeningRequest(BaseModel):
    sample_tx_id: str | None = None          # data/docs 의 샘플 거래 (예: TX-0027)
    fields: dict[str, str | None] | None = None  # 이미 추출된 필드를 직접 줄 때 (키는 contracts.TX_FIELDS)

    @field_validator("fields")
    @classmethod
    def _fields_ok(cls, v):
        if v is not None:
            _check_field_names(v)
            _check_screening_fields(v, only_given=False)
        return v

    @model_validator(mode="after")
    def _exactly_one(self):
        if (self.sample_tx_id is None) == (self.fields is None):
            raise ValueError("sample_tx_id 와 fields 중 정확히 하나만 주세요")
        return self


class ReviewRequest(BaseModel):
    decision: str                                       # contracts.REVIEW_DECISIONS
    reviewer: str
    note: str | None = None
    corrected_fields: dict[str, str | None] | None = None  # 담당자가 OCR 값을 고친 필드만

    @field_validator("corrected_fields")
    @classmethod
    def _fields_ok(cls, v):
        if v is not None:
            _check_field_names(v)
            _check_screening_fields(v, only_given=True)
        return v

    @field_validator("decision")
    @classmethod
    def _decision_ok(cls, v: str) -> str:
        if v not in C.REVIEW_DECISIONS:
            raise ValueError(f"decision 은 {list(C.REVIEW_DECISIONS)} 중 하나")
        return v

    @field_validator("reviewer")
    @classmethod
    def _reviewer_ok(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("reviewer 가 비어 있음 (판단한 사람)")
        return v


# ---------------------------------------------------------------- 도우미
def _new_tx_id() -> str:
    return f"TX-{datetime.now(timezone.utc):%Y%m%d}-{uuid.uuid4().hex[:8].upper()}"


def _get(tx_id: str) -> dict:
    rec = STORE.get(tx_id)
    if rec is None:
        raise ApiError(404, C.ERR_NOT_FOUND, f"심사 건 {tx_id} 없음", tx_id)
    return rec


def _sample_dir(sample_tx_id: str) -> Path | None:
    """샘플 거래 폴더 (워커가 이 폴더의 PNG 를 OCR 한다). contracts.sample_dir 로 형식(SAMPLE_TX_ID_RE)·
    DOCS_DIR 밖 여부를 검사하고, 서류 PNG 가 모두 있어야 한다. 아니면 None (-> 404)."""
    d = C.sample_dir(sample_tx_id)
    if d is None:
        return None
    return d if all((d / f"{doc}.png").is_file() for doc in C.UPLOAD_DOC_TYPES) else None


def _sample_docs(sample_tx_id: str) -> tuple[str | None, str | None]:
    """샘플 거래의 원문 서류 위치 (data/ 기준 상대경로)."""
    d = C.sample_dir(sample_tx_id)
    truth = d / "truth.json" if d else None
    if truth is None or not truth.exists():
        return None, None
    docs = json.loads(truth.read_text(encoding="utf-8"))["documents"]
    return docs["invoice"]["pdf"], docs["bl"]["pdf"]


def _accepted(request: Request, tx_id: str, rec: dict) -> dict:
    request.state.tx_id = tx_id
    return {"tx_id": tx_id, "status": rec["status"], "bundle_id": rec["bundle_id"],
            "shadow_bundle_id": rec.get("shadow_bundle_id"), "status_url": f"/screenings/{tx_id}"}


async def _create_from_json(request: Request) -> dict:
    cl = request.headers.get("content-length")
    if cl is not None and cl.strip().isdigit() and int(cl) > MAX_JSON_BYTES:
        raise _too_large_json()
    try:
        raw = b""
        async for chunk in request.stream():
            raw += chunk
            if len(raw) > MAX_JSON_BYTES:
                raise _too_large_json()
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as e:
        raise ApiError(422, C.ERR_VALIDATION, f"body: JSON 이 아님 ({type(e).__name__})") from e
    try:
        req = ScreeningRequest.model_validate(body)
    except ValidationError as e:
        raise ApiError(422, C.ERR_VALIDATION, _fmt_errors(e.errors())) from e
    if req.sample_tx_id is not None and _sample_dir(req.sample_tx_id) is None:
        raise ApiError(404, C.ERR_NOT_FOUND, f"샘플 {req.sample_tx_id} 의 서류 PNG 가 없음 ({C.DOCS_DIR})",
                       req.sample_tx_id)
    tx_id = _new_tx_id()
    inv, bl = _sample_docs(req.sample_tx_id) if req.sample_tx_id else (None, None)
    rec = STORE.create(tx_id, ACTIVE_BUNDLE,
                       {"source": C.INPUT_SAMPLE if req.sample_tx_id else C.INPUT_FIELDS,
                        "sample_tx_id": req.sample_tx_id, "fields": req.fields},
                       doc_invoice_uri=inv, doc_bl_uri=bl, shadow_bundle_id=shadow_bundle())
    return _accepted(request, tx_id, rec)


async def _read_upload(form, doc: str) -> tuple[bytes, str, str]:
    """파일 필드 하나 검증. 반환: (바이트, content_type, 확장자)"""
    f = form.get(doc)
    if f is None:
        raise ApiError(422, C.ERR_VALIDATION, f"{doc}: 파일이 없음 (필수: {list(C.UPLOAD_DOC_TYPES)})")
    if not isinstance(f, UploadFile):
        raise ApiError(422, C.ERR_VALIDATION, f"{doc}: 파일 필드가 아님")
    ctype = (f.content_type or "").split(";")[0].strip().lower()
    ext = C.UPLOAD_CONTENT_TYPES.get(ctype)
    if ext is None:
        raise ApiError(422, C.ERR_VALIDATION,
                       f"{doc}: Content-Type {ctype or '(없음)'} 은 허용 안 됨 ({list(C.UPLOAD_CONTENT_TYPES)})")
    data = await f.read(MAX_UPLOAD_BYTES + 1)
    if not data:
        raise ApiError(422, C.ERR_VALIDATION, f"{doc}: 빈 파일")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ApiError(422, C.ERR_VALIDATION, f"{doc}: 파일이 너무 큼 (상한 {MAX_UPLOAD_BYTES} 바이트)")
    if not data.startswith(C.FILE_MAGIC[ext]):
        raise ApiError(422, C.ERR_VALIDATION, f"{doc}: 내용이 {ctype} 형식이 아님 (파일 앞 바이트 불일치)")
    return data, ctype, ext


def max_request_bytes() -> int:
    """업로드 요청 전체 상한 = 서류 장 수 x 한 장 상한 + 여유."""
    return len(C.UPLOAD_DOC_TYPES) * MAX_UPLOAD_BYTES + UPLOAD_SLACK_BYTES


class _BodyTooLarge(Exception):
    pass


MAX_JSON_BYTES = 64 * 1024   # JSON 접수 본문 상한 (필드 13개면 충분)


def _too_large_json() -> ApiError:
    return ApiError(422, C.ERR_VALIDATION, f"body: JSON 본문이 너무 큼 (상한 {MAX_JSON_BYTES} 바이트)")


def _too_large(limit: int) -> ApiError:
    return ApiError(422, C.ERR_VALIDATION, f"body: 요청 본문이 너무 큼 (상한 {limit} 바이트 = 서류 "
                                           f"{len(C.UPLOAD_DOC_TYPES)}장 x {MAX_UPLOAD_BYTES} + 여유 {UPLOAD_SLACK_BYTES})")


def _limited(request: Request, limit: int) -> StarletteRequest:
    """본문을 읽으면서 누적 크기를 센다 (Content-Length 가 없는 chunked 요청 대비). 넘으면 _BodyTooLarge."""
    seen = [0]
    receive = request.receive

    async def _receive():
        msg = await receive()
        if msg["type"] == "http.request":
            seen[0] += len(msg.get("body") or b"")
            if seen[0] > limit:
                raise _BodyTooLarge()
        return msg

    return StarletteRequest(request.scope, _receive)


async def _create_from_upload(request: Request) -> dict:
    """회의 3-2: 서류 파일 -> tx_id. 원문은 원문 저장소에 두고, OCR 은 워커가 한다.
    크기: Content-Length 가 상한을 넘으면 본문을 읽기 전에 422. 없으면 읽으면서 누적 검사, 파싱 뒤 장마다 다시 검사."""
    limit = max_request_bytes()
    cl = request.headers.get("content-length")
    if cl is not None:
        if not cl.strip().isdigit():
            raise ApiError(422, C.ERR_VALIDATION, f"Content-Length 형식 오류 ({cl!r})")
        if int(cl) > limit:
            raise _too_large(limit)
    try:
        form = await _limited(request, limit).form(max_files=len(C.UPLOAD_DOC_TYPES) + 2, max_fields=20)
    except _BodyTooLarge as e:
        raise _too_large(limit) from e
    except (MultiPartException, StarletteHTTPException) as e:
        raise ApiError(422, C.ERR_VALIDATION, f"body: multipart 형식 오류 ({getattr(e, 'message', None) or e})") from e
    try:
        unknown = sorted(set(form.keys()) - set(C.UPLOAD_DOC_TYPES))
        if unknown:
            raise ApiError(422, C.ERR_VALIDATION, f"알 수 없는 필드 {unknown}. 허용: {list(C.UPLOAD_DOC_TYPES)}")
        files = {doc: await _read_upload(form, doc) for doc in C.UPLOAD_DOC_TYPES}
    finally:
        await form.close()
    tx_id = _new_tx_id()
    uris = {doc: BLOB.put(C.blob_key(tx_id, doc, ext), data, ctype) for doc, (data, ctype, ext) in files.items()}
    rec = STORE.create(tx_id, ACTIVE_BUNDLE,
                       {"source": C.INPUT_UPLOAD, "sample_tx_id": None, "fields": None,
                        "files": {doc: {"content_type": ctype, "size": len(data)}
                                  for doc, (data, ctype, _) in files.items()}},
                       doc_invoice_uri=uris["invoice"], doc_bl_uri=uris["bl"], shadow_bundle_id=shadow_bundle())
    return _accepted(request, tx_id, rec)


def _upload_png_uri(rec: dict, doc: str) -> str | None:
    """업로드 원문 URI 에서 검토 화면용 PNG URI (같은 위치의 blob_key(tx, doc, '.png'))."""
    tx_id, uri = rec["tx_id"], (rec.get("documents") or {}).get(doc)
    if not uri:
        return None
    for ext in C.UPLOAD_CONTENT_TYPES.values():
        key = C.blob_key(tx_id, doc, ext)
        if uri.endswith(key):
            return uri[: -len(key)] + C.blob_key(tx_id, doc, ".png")
    return None


# ---------------------------------------------------------------- 지표 (Prometheus text exposition 0.0.4)
# 종류는 contracts.METRIC_* 주석 기준
_METRIC_META = {
    C.METRIC_RESULTS: ("counter", "심사 서비스가 접수한 건의 판정 결과 수 (시드·Replay·섀도 제외)"),
    C.METRIC_JOBS: ("gauge", "처리 상태별 심사 건 수"),
    C.METRIC_PROCESSING_SECONDS: ("gauge", "번들별 최근 완료 건 평균 처리 시간(초)"),
    C.METRIC_OCR_CONFIDENCE: ("gauge", "번들별 최근 완료 건 스크리닝 필드 OCR 신뢰도 평균"),
    C.METRIC_FAILED: ("counter", "처리 실패(MAX_ATTEMPTS 회)로 담당자 검토로 간 건 수"),
    C.METRIC_SHADOW_DISAGREE: ("counter", "주 번들과 섀도 번들의 분류가 다른 건 수"),
    C.METRIC_REVIEWS: ("gauge", "담당자 현재 판단 건수 (판단을 바꾸면 줄 수 있음)"),
}


def _escape_label(v) -> str:
    return str(v).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(d: dict) -> str:
    return ("{" + ",".join(f'{k}="{_escape_label(v)}"' for k, v in d.items()) + "}") if d else ""


def _num(v) -> str:
    return str(v) if isinstance(v, int) else repr(float(v))


def render_metrics(stats: dict) -> str:
    """store.stats() -> Prometheus 텍스트."""
    samples = {
        C.METRIC_RESULTS: [({"bundle_id": r["bundle_id"], "classification": r["classification"]}, r["count"])
                           for r in stats["results"]],
        C.METRIC_JOBS: [({"status": s}, n) for s, n in stats["jobs"].items()],
        C.METRIC_PROCESSING_SECONDS: [({"bundle_id": b}, v) for b, v in sorted(stats["processing_seconds_avg"].items())],
        C.METRIC_OCR_CONFIDENCE: [({"bundle_id": b}, v) for b, v in sorted(stats["ocr_confidence_avg"].items())],
        C.METRIC_FAILED: [({"bundle_id": b}, n) for b, n in sorted(stats["failed"].items())],
        C.METRIC_SHADOW_DISAGREE: [({"bundle_id": r["bundle_id"], "shadow_bundle_id": r["shadow_bundle_id"]},
                                    r["count"]) for r in stats["shadow_disagreements"]],
        C.METRIC_REVIEWS: [({"decision": d}, n) for d, n in stats["reviews"].items()],
    }
    lines = []
    for name, (kind, help_) in _METRIC_META.items():
        lines.append(f"# HELP {name} {help_.replace(chr(92), chr(92) * 2)}")
        lines.append(f"# TYPE {name} {kind}")
        lines.extend(f"{name}{_labels(lb)} {_num(v)}" for lb, v in samples[name])
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- 엔드포인트
@app.get("/livez")
def livez():
    """K8s liveness: 프로세스가 요청을 받을 수 있으면 200 (DB 장애로 재시작시키지 않음)."""
    return {"status": "ok"}


@app.get("/healthz")
def healthz(request: Request):
    """K8s readiness: 저장소(DB)·원문 저장소에 못 붙으면 503 (트래픽을 빼도록). 상세는 로그에."""
    for name, target in (("저장소", STORE), ("원문 저장소", BLOB)):
        try:
            target.ping()
        except Exception as e:  # noqa: BLE001
            raise unavailable(request, name, e) from e
    b = C.BUNDLES[ACTIVE_BUNDLE]
    return {"status": "ok", "bundle_id": ACTIVE_BUNDLE, "model_version": b["model_version"],
            "rules_version": b["rules_version"], "list_version": b["list_version"],
            "rules_supported": list(C.RULES_VERSIONS), "shadow_bundle_id": shadow_bundle(),
            "storage": STORE.kind, "blob": BLOB.kind}


@app.get("/metrics")
def metrics(request: Request):
    """Prometheus 스크레이프. 상태가 DB 에 있으므로 어느 복제본에서 긁어도 같은 값."""
    try:
        stats = STORE.stats()
    except Exception as e:  # noqa: BLE001
        raise unavailable(request, "저장소(지표 조회)", e) from e
    return Response(render_metrics(stats), media_type="text/plain; version=0.0.4; charset=utf-8")


# 담당자 판단 코드 중 화면·재판정 차단에 쓰는 것 (contracts.REVIEW_DECISIONS 에 이름 붙은 상수가 없어 여기서 확인)
DECISION_APPROVE, DECISION_HOLD = "APPROVE", "HOLD"
assert {DECISION_APPROVE, DECISION_HOLD} <= set(C.REVIEW_DECISIONS), "contracts.REVIEW_DECISIONS 와 맞춰야 함"
REVIEWABLE_STATUSES = (C.SCREENING_COMPLETED, C.SCREENING_FAILED)   # 검토 가능 상태 (분류도 있어야 함)


def all_rules() -> list[str]:
    """판정 근거에 나올 수 있는 규칙 코드 전부 (rules-v2 + rules-v3 추가 + 워커의 처리 실패)."""
    return list(dict.fromkeys((*C.RULES, *C.RULES_V3_EXTRA, C.RULE_PROCESSING_FAILED)))


@app.get("/meta")
def meta():
    """검토 화면(a_screening/ui/review.html)이 시작할 때 읽는 계약 값. 화면에 계약을 하드코딩하지 않으려고 둔다."""
    return {"tx_fields": list(C.TX_FIELDS), "screening_fields": list(C.SCREENING_FIELDS),
            "classifications": list(C.CLASSIFICATIONS), "review_decisions": list(C.REVIEW_DECISIONS),
            "statuses": list(C.SCREENING_STATUSES), "doc_types": list(C.UPLOAD_DOC_TYPES),
            "ocr_confidence_min": C.OCR_CONFIDENCE_MIN, "rules": all_rules(),
            "classification_roles": {"pass": C.PASS, "review": C.REVIEW, "hold": C.HOLD},
            "status_roles": {"queued": C.SCREENING_QUEUED, "processing": C.SCREENING_PROCESSING,
                             "completed": C.SCREENING_COMPLETED, "failed": C.SCREENING_FAILED},
            "decision_roles": {"approve": DECISION_APPROVE, "hold": DECISION_HOLD},
            "reviewable_statuses": list(REVIEWABLE_STATUSES),
            "bundles": {bid: dict(b) for bid, b in C.BUNDLES.items()},
            "active_bundle": ACTIVE_BUNDLE, "shadow_bundle": shadow_bundle()}


@app.get("/review")
def review_page():
    if not REVIEW_HTML.is_file():
        raise ApiError(404, C.ERR_NOT_FOUND, "검토 화면 파일(a_screening/ui/review.html) 없음")
    return FileResponse(REVIEW_HTML, media_type="text/html; charset=utf-8")


_POST_OPENAPI = {"requestBody": {"required": True, "content": {
    "application/json": {"schema": ScreeningRequest.model_json_schema()},
    "multipart/form-data": {"schema": {"type": "object", "required": list(C.UPLOAD_DOC_TYPES), "properties": {
        doc: {"type": "string", "format": "binary", "description": f"{list(C.UPLOAD_CONTENT_TYPES)}"}
        for doc in C.UPLOAD_DOC_TYPES}}},
}}}


@app.post("/screenings", status_code=202, openapi_extra=_POST_OPENAPI)
async def create_screening(request: Request):
    """접수만 한다 (status=queued). Content-Type 으로 JSON / multipart 업로드를 나눈다."""
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype == "application/json":
        return await _create_from_json(request)
    if ctype == "multipart/form-data":
        return await _create_from_upload(request)
    raise ApiError(422, C.ERR_VALIDATION,
                   f"Content-Type {ctype or '(없음)'} 은 지원 안 됨 (application/json 또는 multipart/form-data)")


@app.get("/screenings")
def list_screenings(status: str | None = None, classification: str | None = None, bundle_id: str | None = None,
                    unreviewed_first: bool = False,
                    limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)):
    """classification 은 쉼표로 여러 개 (예: HIGH_RISK_HOLD,MANUAL_REVIEW). unreviewed_first=true 면
    담당자 판단이 없는 건 먼저 (그 안에서는 최신 접수순). 페이지네이션은 서버에서 (total 은 같은 필터)."""
    try:
        return STORE.list(status=status, classification=classification, bundle_id=bundle_id,
                          limit=limit, offset=offset, unreviewed_first=unreviewed_first)
    except ValueError as e:
        raise ApiError(422, C.ERR_VALIDATION, str(e)) from e


@app.get("/screenings/{tx_id}")
def get_screening(tx_id: str):
    return _get(tx_id)


@app.get("/screenings/{tx_id}/documents/{doc_type}")
def get_document(tx_id: str, doc_type: str):
    """검토 화면용 서류 PNG. 샘플은 data/docs, 업로드는 원문 저장소(PDF 는 워커가 렌더링한 PNG)."""
    if doc_type not in C.UPLOAD_DOC_TYPES:
        raise ApiError(422, C.ERR_VALIDATION, f"doc_type 은 {list(C.UPLOAD_DOC_TYPES)} 중 하나", tx_id)
    rec = _get(tx_id)
    inp = rec.get("input") or {}
    headers = {"Cache-Control": "no-store"}   # 은행 서류: 브라우저·프록시에 남기지 않음
    if inp.get("source") == C.INPUT_SAMPLE or (inp.get("source") is None and inp.get("sample_tx_id")):
        d = _sample_dir(inp.get("sample_tx_id") or "")
        if d is None:
            raise ApiError(404, C.ERR_NOT_FOUND, f"샘플 {inp.get('sample_tx_id')} 의 {doc_type} 서류 없음", tx_id)
        return FileResponse(d / f"{doc_type}.png", media_type="image/png", headers=headers)
    if inp.get("source") == C.INPUT_UPLOAD:
        uri = _upload_png_uri(rec, doc_type)
        if uri is None or not BLOB.exists(uri):
            raise ApiError(404, C.ERR_NOT_FOUND,
                           f"{tx_id} 의 {doc_type} 이미지 없음 (PDF 는 워커가 렌더링한 뒤 생김, status={rec['status']})",
                           tx_id)
        return Response(BLOB.get(uri), media_type="image/png", headers=headers)
    raise ApiError(404, C.ERR_NOT_FOUND, f"{tx_id} 는 서류 없이 필드로 접수된 건", tx_id)


_INDEX: dict = {}
_INDEX_LOCK = threading.Lock()


def index_for(list_version: str):
    """제재 리스트 색인 (버전별 캐시, 재판정용)."""
    with _INDEX_LOCK:
        if list_version not in _INDEX:
            _INDEX[list_version] = load_index(list_version)
        return _INDEX[list_version]


def rescreen(rec: dict, corrected: dict | None) -> dict | None:
    """스크리닝 필드를 정정했으면 원래 필드 + 정정 값으로 같은 번들(list/rules)로 다시 판정한다 (ocr=None:
    담당자가 직접 입력한 값). 스크리닝 필드 정정이 없으면 None. 반환: {classification, reasons}"""
    if not corrected or not set(corrected) & set(C.SCREENING_FIELDS):
        return None
    b = C.BUNDLES[rec["bundle_id"]]
    original = rec.get("fields") or {}
    fields = {f: original.get(f) for f in C.TX_FIELDS} | dict(corrected)
    res = screen(fields, index_for(b["list_version"]), rules_version=b["rules_version"], ocr=None)
    cls, reasons = res["classification"], list(res["reasons"])
    # 한 번도 읽힌 적 없는 스크리닝 필드가 남아 있으면 (예: 처리 실패 건에서 일부만 정정) 통과로 보지 않는다
    for f in C.SCREENING_FIELDS:
        if is_blank(fields.get(f)):
            reasons.append({"rule": C.RULE_MISSING_FIELD, "field": f, "value": fields.get(f), "entry_id": None,
                            "matched_name": None, "score": None, "classification": C.REVIEW})
            if cls == C.PASS:
                cls = C.REVIEW
    return {"classification": cls, "reasons": reasons}


def _reason_summary(reasons: list[dict], cls: str, n: int = 3) -> str:
    hits = [r for r in reasons if r.get("classification") == cls] or reasons
    parts = [f"{r.get('rule')} {r.get('field')}={r.get('value')!r} -> {r.get('matched_name')!r}"
             f" ({r.get('entry_id')})" for r in hits[:n]]
    more = f" 외 {len(hits) - n}건" if len(hits) > n else ""
    return "; ".join(parts) + more


@app.post("/screenings/{tx_id}/review")
def review_screening(tx_id: str, req: ReviewRequest):
    """분류가 있는 건만 (completed, 또는 반복 실패로 MANUAL_REVIEW 가 붙은 failed). 저장.
    스크리닝 필드를 정정했으면 재판정해서, 결과가 HIGH_RISK_HOLD 인데 APPROVE 면 저장하지 않고 409.
    재판정 결과는 응답 rescreen 에 남는다 (DB 컬럼은 없음: 원래 판정은 그대로)."""
    rec = _get(tx_id)
    if rec["classification"] is None or rec["status"] not in REVIEWABLE_STATUSES:
        raise ApiError(409, C.ERR_CONFLICT,
                       f"{tx_id} 는 아직 판정이 없음 (status={rec['status']}, classification={rec['classification']})",
                       tx_id)
    # 정정은 누적한다: 이번에 안 보낸 필드는 이전 정정을 유지 (두 번 나눠 요청해서 재판정을 피하지 못하게)
    corrected = {**(rec.get("corrected_fields") or {}), **(req.corrected_fields or {})} or None
    rs = rescreen(rec, corrected)
    if rs and rs["classification"] == C.HOLD and req.decision == DECISION_APPROVE:
        raise ApiError(409, C.ERR_CONFLICT,
                       f"정정한 값으로 재판정하면 {C.HOLD} 라서 승인할 수 없음: "
                       f"{_reason_summary(rs['reasons'], C.HOLD)}", tx_id)
    STORE.review(tx_id, req.decision, req.reviewer, req.note, corrected)
    return {**_get(tx_id), "rescreen": rs}
