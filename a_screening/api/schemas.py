"""API 명세서(OpenAPI)용 응답 모양과 예시. 런타임 응답을 거르지 않는다 (문서 전용).

명세서 파일: docs/openapi/a_screening.json  ← `python -m a_screening.api.export_openapi` 로 다시 만든다.
응답 키가 실제와 같은지는 a_screening/tests/test_openapi.py 가 확인한다 (코드를 바꾸고 명세를 안 고치면 실패).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from common import contracts as C

SERVERS = [{"url": "http://localhost:8000", "description": "개발 환경 (compose: a-screening)"}]
TAGS = [{"name": "screenings", "description": "M6 심사: 접수·결과 조회·담당자 판단"},
        {"name": "ui", "description": "담당자 검토 화면과 화면이 읽는 값"},
        {"name": "ops", "description": "상태 확인·지표"}]
API_DESCRIPTION = """은행 수출입 서류(송장·선하증권)를 받아 OCR로 필드를 읽고, 제재 리스트와 대조해 3단계로 분류한다.
**AI는 OCR에만 쓰고** 필드 추출·매칭·분류는 모두 코드다.

**분류:** `PASS_CANDIDATE`(통과 후보) / `MANUAL_REVIEW`(담당자 검토) / `HIGH_RISK_HOLD`(고위험 보류).
판정 결과에는 항상 근거(`reasons`: rule, field, value, entry_id, matched_name, score)가 있다.

**흐름**
1. `POST /screenings`로 접수 → 202 + `tx_id`. 이 API는 판정하지 않고 큐(`screening_jobs`)에 넣기만 한다.
2. OCR 워커가 큐에서 꺼내 OCR → 필드 추출 → 판정 → 저장. 실패하면 재시도, 3번 넘게 실패하면 `MANUAL_REVIEW`.
3. `GET /screenings/{tx_id}`로 결과 조회. `status`가 `completed`(또는 `failed`)면 분류·근거가 있다.

**번들:** 접수 시점의 `BUNDLE_ID`(모델 + 리스트)로 거래마다 고정된다. canary 중에도 거래 하나는 한 번들로만 처리된다.
**규칙 버전:** 번들과 무관하게 `rules-v3`로 고정(contracts.RULES_FIXED).

**공통 규칙**
- 실패 응답은 모두 `{code, message, target_id}` (docs/interfaces.md 1절). 500·503 message 에는 요청 ID만 (상세는 서버 로그).
- 요청 ID는 `X-Request-ID` 헤더 (들어온 값을 이어 쓰고, 응답에도 돌려준다).
- 시각은 ISO 8601 UTC 문자열.
- 인증 없음 (시연용)."""

ErrorCode = Literal["VALIDATION_ERROR", "NOT_FOUND", "CONFLICT", "INTERNAL_ERROR", "SERVICE_UNAVAILABLE"]
Classification = Literal["PASS_CANDIDATE", "MANUAL_REVIEW", "HIGH_RISK_HOLD"]
Status = Literal["queued", "processing", "completed", "failed"]
Decision = Literal["APPROVE", "HOLD", "REJECT"]

assert set(ErrorCode.__args__) == {C.ERR_VALIDATION, C.ERR_NOT_FOUND, C.ERR_CONFLICT, C.ERR_INTERNAL,
                                   C.ERR_UNAVAILABLE}
assert set(Classification.__args__) == set(C.CLASSIFICATIONS)
assert set(Status.__args__) == set(C.SCREENING_STATUSES)
assert set(Decision.__args__) == set(C.REVIEW_DECISIONS)


# ---------------------------------------------------------------- 공통
class Error(BaseModel):
    """모든 실패 응답의 공통 형식 (docs/interfaces.md 1절)."""
    code: ErrorCode
    message: str = Field(description="사람이 읽는 설명. 500/503 은 일반 문구 + 요청 ID (상세는 서버 로그)")
    target_id: str | None = Field(description="관련 대상 ID (보통 tx_id). 없으면 null")


class Reason(BaseModel):
    """판정 근거 한 건. 분류가 PASS_CANDIDATE 가 아니면 최소 하나 있다."""
    rule: str = Field(description="규칙 코드 (GET /meta 의 rules). 예: party_exact, vessel_fuzzy, low_confidence")
    field: str | None = Field(description="근거가 된 필드 (contracts.TX_FIELDS)")
    value: str | None = Field(description="서류에서 읽은 값")
    entry_id: str | None = Field(description="일치한 제재 리스트 항목 ID")
    matched_name: str | None = Field(description="일치한 제재 리스트 이름")
    score: float | None = Field(description="유사도 0~1 (정확 일치 1.0)")
    classification: Classification = Field(description="이 근거 하나가 가리키는 분류")


class ExtractedField(BaseModel):
    value: str | None
    source_doc: Literal["invoice", "bl"] | None = Field(description="값을 읽은 서류")
    bbox: list[int] | None = Field(description="원본 PNG 픽셀 좌표 [x0, y0, x1, y1]")
    confidence: float | None = Field(description="OCR 신뢰도 0~100")


class ShadowResult(BaseModel):
    bundle_id: str
    classification: Classification | None
    reasons: list[Reason]
    error: Error | None = Field(description="섀도 처리가 실패했으면 공통 에러 형식 (code=SCREENING_FAILED)")


# ---------------------------------------------------------------- 접수
class Accepted(BaseModel):
    """접수 결과. 판정은 OCR 워커가 비동기로 한다 → status_url 을 조회."""
    tx_id: str = Field(description="TX-YYYYMMDD-8자리 16진수")
    status: Status
    bundle_id: str = Field(description="이 건을 처리할 번들 (접수 시점의 BUNDLE_ID 로 고정)")
    shadow_bundle_id: str | None
    status_url: str


# ---------------------------------------------------------------- 상세
class ScreeningDetail(BaseModel):
    """심사 건 하나. status 가 completed(또는 failed)가 되면 classification·reasons 가 채워진다."""
    tx_id: str
    status: Status
    bundle_id: str
    model_id: str
    rules_version: str
    list_id: str
    input: dict = Field(description="접수 내용 {source: upload|sample|fields, sample_tx_id, fields, files}")
    received_at: str
    submitted_at: str
    started_at: str | None
    completed_at: str | None
    attempts: int = Field(description="처리 시도 횟수 (실패 시 재시도, 최대 3)")
    worker_id: str | None
    documents: dict[str, str | None] | None = Field(description="원문 저장 위치 {invoice, bl}. 필드로 접수한 건은 값이 null")
    fields: dict[str, str | None] | None = Field(description="추출 필드 값 13개 (contracts.TX_FIELDS)")
    extracted_fields: dict[str, ExtractedField] | None = Field(description="필드별 값·서류·위치·신뢰도")
    extraction_source: str | None = Field(description="ocr:<모델 버전> 또는 api:fields")
    conflicts: list[dict] = Field(description="Invoice 와 B/L 값이 다른 필드 [{field, invoice, bl}]")
    classification: Classification | None
    reasons: list[Reason]
    error: dict | None = Field(description="처리 실패 사유 (공통 에러 형식, code=SCREENING_FAILED)")
    reviewer_decision: Decision | None
    reviewer: str | None
    reviewer_note: str | None
    corrected_fields: dict[str, str | None] | None = Field(description="담당자가 고친 필드만")
    reviewed_at: str | None
    shadow_bundle_id: str | None
    shadow: list[ShadowResult] = Field(description="섀도 판정 결과 (실제 분류에 쓰지 않음)")


class Rescreen(BaseModel):
    classification: Classification
    reasons: list[Reason]


class ReviewResult(ScreeningDetail):
    rescreen: Rescreen | None = Field(description="스크리닝 필드를 정정했으면 같은 번들로 다시 판정한 결과 (DB 에는 저장 안 함)")


class ListItem(BaseModel):
    tx_id: str
    status: Status
    bundle_id: str
    classification: Classification | None
    submitted_at: str
    completed_at: str | None
    reviewer_decision: Decision | None
    seller_name: str | None
    buyer_name: str | None
    vessel_name: str | None


class ListResult(BaseModel):
    items: list[ListItem]
    total: int = Field(description="같은 필터의 전체 건수 (페이지와 무관)")


# ---------------------------------------------------------------- 운영
class Health(BaseModel):
    status: Literal["ok"]
    bundle_id: str = Field(description="지금 접수에 쓰는 번들 (BUNDLE_ID)")
    model_id: str
    rules_version: str
    list_id: str
    rules_supported: list[str]
    shadow_bundle_id: str | None
    storage: Literal["postgres", "memory"]
    blob: Literal["minio", "local"]


class Live(BaseModel):
    status: Literal["ok"]


# ---------------------------------------------------------------- 예시 (실제 응답에서 가져옴: model-v3, B-7, TX-0001 서류)
_TX = "TX-20261113-639E013E"
_REASON = {"rule": "party_exact", "field": "seller_name", "value": "VALERIAN LABS DISTRIBUTION",
           "entry_id": "OFAC-45315", "matched_name": "VALERIAN LABS DISTRIBUTION CORP.", "score": 1.0,
           "classification": "HIGH_RISK_HOLD"}
_FIELDS = {"invoice_no": "INV-2026-5618", "invoice_date": "2026-08-01", "bl_no": "BLKHH263335",
           "seller_name": "VALERIAN LABS DISTRIBUTION", "seller_country": "Canada",
           "buyer_name": "Fuji Coral Electronics LLC", "buyer_country": "South Korea", "vessel_name": "EMERALD TRIUMPH",
           "port_of_loading": "Kaohsiung", "port_of_discharge": "Gwangyang",
           "goods_description": "Lithium-ion battery cells, 77,500 pcs", "currency": "USD", "amount": "363,517.00"}
_EXTRACTED = {f: {"value": v, "source_doc": "bl" if f in ("bl_no", "port_of_loading", "port_of_discharge") else "invoice",
                  "bbox": [296, 410, 754, 434], "confidence": 95.0} for f, v in _FIELDS.items()}

EX_ACCEPTED = {"tx_id": _TX, "status": "queued", "bundle_id": "B-7", "shadow_bundle_id": None,
               "status_url": f"/screenings/{_TX}"}
EX_DETAIL = {
    "tx_id": _TX, "status": "completed", "bundle_id": "B-7", "model_id": "model-v3", "rules_version": "rules-v3",
    "list_id": "list-v14",
    "input": {"source": "upload", "sample_tx_id": None, "fields": None,
              "files": {"invoice": {"content_type": "application/pdf", "size": 1955},
                        "bl": {"content_type": "application/pdf", "size": 1902}}},
    "received_at": "2026-11-13T05:54:02+00:00", "submitted_at": "2026-11-13T05:54:02+00:00",
    "started_at": "2026-11-13T05:54:02+00:00", "completed_at": "2026-11-13T05:54:04+00:00",
    "attempts": 1, "worker_id": "a-ocr-worker-1",
    "documents": {"invoice": f"s3://tradeproof-docs/{_TX}/invoice.pdf", "bl": f"s3://tradeproof-docs/{_TX}/bl.pdf"},
    "fields": _FIELDS, "extracted_fields": _EXTRACTED, "extraction_source": "ocr:model-v3", "conflicts": [],
    "classification": "HIGH_RISK_HOLD", "reasons": [_REASON], "error": None,
    "reviewer_decision": None, "reviewer": None, "reviewer_note": None, "corrected_fields": None, "reviewed_at": None,
    "shadow_bundle_id": None, "shadow": [],
}
EX_REVIEW = {**EX_DETAIL, "reviewer_decision": "HOLD", "reviewer": "kim", "reviewer_note": "제재 대상 일치",
             "reviewed_at": "2026-11-13T06:10:00+00:00", "rescreen": None}
EX_LIST = {"items": [{"tx_id": _TX, "status": "completed", "bundle_id": "B-7", "classification": "HIGH_RISK_HOLD",
                      "submitted_at": "2026-11-13T05:54:02+00:00", "completed_at": "2026-11-13T05:54:04+00:00",
                      "reviewer_decision": None, "seller_name": "VALERIAN LABS DISTRIBUTION",
                      "buyer_name": "Fuji Coral Electronics LLC", "vessel_name": "EMERALD TRIUMPH"}], "total": 1}
EX_HEALTH = {"status": "ok", "bundle_id": "B-7", "model_id": "model-v3", "rules_version": "rules-v3",
             "list_id": "list-v14", "rules_supported": ["rules-v2", "rules-v3"], "shadow_bundle_id": None,
             "storage": "postgres", "blob": "minio"}
EX_METRICS = """# TYPE tradeproof_screening_results_total counter
tradeproof_screening_results_total{bundle_id="B-7",classification="HIGH_RISK_HOLD"} 1
# TYPE tradeproof_screening_jobs gauge
tradeproof_screening_jobs{status="queued"} 0
# TYPE tradeproof_screening_processing_seconds_avg gauge
tradeproof_screening_processing_seconds_avg{bundle_id="B-7"} 0.571
# TYPE tradeproof_ocr_confidence_avg gauge
tradeproof_ocr_confidence_avg{bundle_id="B-7"} 95.89
"""

_ERR_EX = {
    422: {"code": "VALIDATION_ERROR", "message": "fields: 스크리닝 필드 ['seller_name'] 가 비어 있음", "target_id": None},
    404: {"code": "NOT_FOUND", "message": f"심사 건 {_TX} 없음", "target_id": _TX},
    409: {"code": "CONFLICT", "message": f"{_TX} 는 아직 판정이 없음 (status=queued, classification=None)",
          "target_id": _TX},
    500: {"code": "INTERNAL_ERROR", "message": "서버 내부 오류 (요청 ID: 3c1b7a41af2e4c3f)", "target_id": None},
    503: {"code": "SERVICE_UNAVAILABLE", "message": "저장소 연결 실패. 잠시 뒤 다시 시도하세요 (요청 ID: 3c1b7a41af2e4c3f)",
          "target_id": None},
}
_ERR_DESC = {422: "요청 형식·값 오류", 404: "대상 없음", 409: "지금 상태에서는 할 수 없음",
             500: "서버 내부 오류", 503: "DB·원문 저장소 연결 안 됨"}


def ok(model: type[BaseModel], example, description: str = "성공", status: int = 200) -> dict:
    return {status: {"model": model, "description": description, "content": {"application/json": {"example": example}}}}


def errors(*statuses: int, **why: str) -> dict:
    """공통 에러 형식 응답. why 로 상태별 설명을 덧붙인다 (예: e409='...')."""
    return {s: {"model": Error, "description": why.get(f"e{s}", _ERR_DESC[s]),
                "content": {"application/json": {"example": _ERR_EX[s]}}} for s in statuses}


def use_common_error_for_422(schema: dict) -> dict:
    """FastAPI 기본 422(HTTPValidationError)를 실제 응답인 공통 에러 형식으로 바꾼다."""
    ref = {"$ref": "#/components/schemas/Error"}
    for ops in schema.get("paths", {}).values():
        for op in ops.values():
            r = op.get("responses", {}).get("422")
            if r:
                r["content"] = {"application/json": {"schema": ref, "example": _ERR_EX[422]}}
                if r.get("description") == "Validation Error":
                    r["description"] = _ERR_DESC[422]
    comps = schema.get("components", {}).get("schemas", {})
    for name in ("HTTPValidationError", "ValidationError"):
        comps.pop(name, None)
    return schema
