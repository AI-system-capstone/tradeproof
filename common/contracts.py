"""모듈 A의 모든 약속(필드 이름, 분류 코드, 경로, 서류 라벨)을 한 곳에 모은 파일.

팀 공통 스키마가 확정되면 이 파일만 고치면 된다.
다른 스크립트는 여기 있는 상수만 import 해서 쓴다.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

# ---------------------------------------------------------------- 경로
SANCTIONS_DIR = DATA / "sanctions"          # list-v14.json, list-v15.json
DOCS_DIR = DATA / "docs"                    # docs/<tx_id>/invoice.pdf ... truth.json
OCR_DIR = DATA / "ocr"                      # ocr/<tx_id>/<doc>.raw.json, extracted.json
DB_OUT_DIR = DATA / "db"                    # seed.sql

def list_path(version: str) -> Path:
    return SANCTIONS_DIR / f"{version}.json"

# ---------------------------------------------------------------- 버전 / 번들 (11/01 회의 번들 표 기준)
LIST_BASE = "list-v14"
LIST_NEW = "list-v15"
MODEL_BASE = "model-v3"      # 기성 OCR 모델 그대로 (정상)
MODEL_NEW = "model-v4"       # 시연 장면 4용으로 일부러 성능을 떨어뜨린 버전 (11/01 회의: 새로 학습하지 않음)
RULES_BASE = "rules-v2"
RULES_NEW = "rules-v3"       # 11/08: OCR 불확실성 규칙 추가 (신뢰도 낮음·Invoice/B-L 불일치·필드 누락 -> 검토, OCR 혼동 글자 감안 일치 -> 보류)
RULES_VERSIONS = (RULES_BASE, RULES_NEW)
BUNDLES = {
    "B-7": {"model_version": MODEL_BASE, "rules_version": RULES_BASE, "list_version": LIST_BASE, "status": "active"},     # 현재 활성
    "B-8": {"model_version": MODEL_BASE, "rules_version": RULES_BASE, "list_version": LIST_NEW, "status": "candidate"},   # 리스트 갱신 (장면 2)
    "B-9": {"model_version": MODEL_NEW, "rules_version": RULES_BASE, "list_version": LIST_NEW, "status": "candidate"},    # 모델 갱신 (장면 4, canary)
    # 11/08 추가 (팀 공유 필요): 규칙 갱신. 리스트는 B-8과 같고 규칙만 다름
    "B-10": {"model_version": MODEL_BASE, "rules_version": RULES_NEW, "list_version": LIST_NEW, "status": "candidate"},
}
BASELINE_BUNDLE = "B-7"


def ocr_dir(model_version: str, base: Path | str | None = None) -> Path:
    """OCR 결과 폴더. 기준 모델(model-v3)은 base 그대로, 다른 모델은 base/<model>/ (모델끼리 섞이지 않게).
    OCR 서비스와 심사 API가 같이 쓴다 (API가 OCR 코드를 import하지 않도록 여기 둔다)."""
    base = Path(base) if base else OCR_DIR
    return base if model_version == MODEL_BASE else base / model_version

# ---------------------------------------------------------------- 판정 분류 (통과 후보 / 담당자 검토 / 고위험 보류)
PASS = "PASS_CANDIDATE"
REVIEW = "MANUAL_REVIEW"
HOLD = "HIGH_RISK_HOLD"
CLASSIFICATIONS = (PASS, REVIEW, HOLD)

# 판정 근거(reasons) 한 건의 키. screening/match.py 가 이 키로 남긴다.
REASON_FIELDS = ("rule", "field", "value", "entry_id", "matched_name", "score", "classification")
RULE_PARTY_EXACT = "party_exact"      # -> HOLD
RULE_VESSEL_EXACT = "vessel_exact"    # -> HOLD
RULE_PARTY_FUZZY = "party_fuzzy"      # -> REVIEW
RULE_VESSEL_FUZZY = "vessel_fuzzy"    # -> REVIEW
RULES = (RULE_PARTY_EXACT, RULE_VESSEL_EXACT, RULE_PARTY_FUZZY, RULE_VESSEL_FUZZY)
# rules-v3 에서 추가되는 규칙 (rules-v2 에서는 절대 나오지 않음)
# OCR 혼동 글자(I/l/1/!, O/0 등)를 같은 글자로 보면 정확 일치. OCR 입력(ocr 있음)에서만 적용.
#   HOLD: 접기 전 유사도 >= FUZZY_THRESHOLD 이고 비교 키가 짧지 않고(> 5자) 숫자가 없을 때 / 그 밖에는 REVIEW (오탐 방지)
RULE_PARTY_EXACT_OCR = "party_exact_ocr"
RULE_VESSEL_EXACT_OCR = "vessel_exact_ocr"
# Invoice/B-L 이 다르게 읽혔을 때 '다른 서류 쪽 값'으로 매칭된 근거 (v2 규칙 이름과 구분). classification 은 매칭 결과대로
RULE_PARTY_OTHER_DOC = "party_match_other_doc"
RULE_VESSEL_OTHER_DOC = "vessel_match_other_doc"
RULE_LOW_CONFIDENCE = "low_confidence"        # -> REVIEW 스크리닝 필드의 OCR 신뢰도 < OCR_CONFIDENCE_MIN (신뢰도가 없거나 숫자가 아니어도). score = 신뢰도/100 (0~1)
RULE_DOC_CONFLICT = "doc_conflict"            # -> REVIEW 스크리닝 필드가 Invoice 와 B/L 에서 다르게 읽힘
RULE_MISSING_FIELD = "missing_field"          # -> REVIEW 스크리닝 필드를 못 읽음 / ocr 부가 정보 형식이 이상함
RULE_PROCESSING_FAILED = "processing_failed"  # -> REVIEW 처리(OCR 등)가 MAX_ATTEMPTS 번 실패 (worker 가 남김, rules 무관)
RULES_V3_EXTRA = (RULE_PARTY_EXACT_OCR, RULE_VESSEL_EXACT_OCR, RULE_PARTY_OTHER_DOC, RULE_VESSEL_OTHER_DOC,
                  RULE_LOW_CONFIDENCE, RULE_DOC_CONFLICT, RULE_MISSING_FIELD)
OCR_CONFIDENCE_MIN = 85.0     # tesseract 단어 신뢰도(0~100) 평균. 맞은 필드 평균 94.7, 틀린 필드 80.6 (Docker, model-v3)
MAX_ATTEMPTS = 3              # 같은 거래 처리 실패가 이 횟수에 닿으면 담당자 검토로 보냄

# 담당자 최종 판단 (screening_results.reviewer_decision)
REVIEW_DECISIONS = ("APPROVE", "HOLD", "REJECT")

# 심사 API 처리 상태 (POST /screenings 후 GET /screenings/{tx_id} 의 status)
SCREENING_QUEUED = "queued"
SCREENING_PROCESSING = "processing"
SCREENING_COMPLETED = "completed"
SCREENING_FAILED = "failed"
SCREENING_STATUSES = (SCREENING_QUEUED, SCREENING_PROCESSING, SCREENING_COMPLETED, SCREENING_FAILED)

# 공통 에러 형식 (11/01 회의 3-3: 모든 모듈이 같은 JSON = 코드, 메시지, 대상 ID)
#   {"code": "NOT_FOUND", "message": "...", "target_id": "TX-..."}   target_id 는 없으면 null
ERROR_FIELDS = ("code", "message", "target_id")
ERR_VALIDATION = "VALIDATION_ERROR"        # 422 요청 형식·값 오류
ERR_NOT_FOUND = "NOT_FOUND"                # 404 대상 없음
ERR_CONFLICT = "CONFLICT"                  # 409 지금 상태에서는 할 수 없음 (예: 판정 전 검토)
ERR_SCREENING_FAILED = "SCREENING_FAILED"  # 판정 처리 실패 (GET 응답의 error 에 담김)
ERR_INTERNAL = "INTERNAL_ERROR"            # 500
ERR_UNAVAILABLE = "SERVICE_UNAVAILABLE"    # 503 저장소(DB) 연결 안 됨 -> K8s 가 트래픽을 빼도록

# ---------------------------------------------------------------- 원문 저장소 (MinIO / 로컬 폴더)
BLOB_BUCKET = "tradeproof-docs"
UPLOAD_DOC_TYPES = ("invoice", "bl")          # POST /screenings 멀티파트 파일 필드 이름
UPLOAD_CONTENT_TYPES = {"application/pdf": ".pdf", "image/png": ".png"}
FILE_MAGIC = {".pdf": b"%PDF", ".png": b"\x89PNG\r\n\x1a\n"}   # 파일 첫 바이트로 형식 확인
MAX_UPLOAD_BYTES = 20 * 1024 * 1024                              # 서류 한 장 상한 (요청 전체 상한 = 장 수 x 이 값 + 여유)

# 심사 입력 출처 (screening_jobs.input.source) 와 추출 출처 (transactions.extraction_source)
INPUT_SAMPLE = "sample"     # data/docs/<sample_tx_id> 의 합성 서류
INPUT_UPLOAD = "upload"     # 업로드한 서류 파일
INPUT_FIELDS = "fields"     # 이미 추출된 필드를 직접 입력
INPUT_SOURCES = (INPUT_SAMPLE, INPUT_UPLOAD, INPUT_FIELDS)
EXTRACTION_FROM_FIELDS = "api:fields"

def extraction_source_ocr(model_version: str) -> str:
    return f"ocr:{model_version}"

SAMPLE_TX_ID_RE = r"^TX-[0-9]{4,}$"   # 샘플 거래 ID 형식 (경로 탐색 방지)

def sample_dir(sample_tx_id: str) -> Path | None:
    """샘플 거래 폴더. 형식이 틀리거나 DOCS_DIR 밖이거나 없으면 None."""
    import re
    if not isinstance(sample_tx_id, str) or not re.fullmatch(SAMPLE_TX_ID_RE, sample_tx_id):
        return None
    d = (DOCS_DIR / sample_tx_id).resolve()
    if d.parent != DOCS_DIR.resolve() or not d.is_dir():
        return None
    return d

def blob_key(tx_id: str, doc_type: str, ext: str) -> str:
    """원문 저장 키: <tx_id>/<doc_type><ext>  예) TX-20261108-AB12CD34/invoice.pdf, .../invoice.png(검토 화면용 렌더링)"""
    return f"{tx_id}/{doc_type}{ext}"

# ---------------------------------------------------------------- 모니터링 지표 (Prometheus, GET /metrics). D 의 대시보드·자동 롤백이 쓴다
METRIC_RESULTS = "tradeproof_screening_results_total"        # counter {bundle_id, classification}  심사 서비스가 접수한 건만 (시드·Replay·섀도 제외)
METRIC_JOBS = "tradeproof_screening_jobs"                    # gauge   {status}
METRIC_PROCESSING_SECONDS = "tradeproof_screening_processing_seconds_avg"  # gauge {bundle_id} 최근 완료 100건 평균
METRIC_OCR_CONFIDENCE = "tradeproof_ocr_confidence_avg"      # gauge   {bundle_id} 최근 완료 100건, 스크리닝 필드 평균
METRIC_FAILED = "tradeproof_screening_failed_total"          # counter {bundle_id}
METRIC_SHADOW_DISAGREE = "tradeproof_shadow_disagreements_total"  # counter {bundle_id, shadow_bundle_id}
METRIC_REVIEWS = "tradeproof_review_decisions"               # gauge   {decision}  현재 판단 건수 (판단을 바꾸면 줄 수 있음)

# 번들 상태 (모듈 B 소유. B의 정의가 확정되면 그 값으로 맞춘다)
BUNDLE_STATUSES = ("candidate", "validated", "validation_failed", "approved", "rejected", "active", "rolled_back")

# ---------------------------------------------------------------- 제재 리스트 항목
# list JSON = {list_version, source, source_fetched_at, created_at, entry_count, entries:[ENTRY]}
ENTRY_FIELDS = ("entry_id", "name", "aliases", "entity_type", "country", "programs", "call_sign", "vessel_flag", "synthetic")
ENTITY = "entity"
VESSEL = "vessel"

# v15에서 새로 추가되는 가상 제재 대상 'A사' (실존 기업이 아닌 가상 이름)
NEW_LISTING = {
    "entry_id": "DEMO-A001",
    "name": "ASTERIA HARBOR TRADING LLC",
    "aliases": ["ASTERIA HARBOUR TRADING"],
    "entity_type": ENTITY,
    "country": "Panama",
    "programs": ["DEMO"],
    "call_sign": None,
    "vessel_flag": None,
    "synthetic": True,
}

# ---------------------------------------------------------------- 거래 필드 (거래 레코드의 추출 필드)
TX_FIELDS = (
    "invoice_no", "invoice_date", "bl_no",
    "seller_name", "seller_country",
    "buyer_name", "buyer_country",
    "vessel_name", "port_of_loading", "port_of_discharge",
    "goods_description", "currency", "amount",
)
# 제재 스크리닝에 쓰는 필드
PARTY_FIELDS = ("seller_name", "buyer_name")
VESSEL_FIELDS = ("vessel_name",)
SCREENING_FIELDS = PARTY_FIELDS + VESSEL_FIELDS   # fields 로 접수할 때 셋 다 필수 (비어 있으면 422)

# ---------------------------------------------------------------- 서류 레이아웃 (생성기와 OCR이 같이 쓰는 약속)
# 서류 한 줄 = "<라벨>: <값>". 라벨 문자열은 정확히 이 값이어야 한다.
DOC_TITLES = {"invoice": "COMMERCIAL INVOICE", "bl": "BILL OF LADING"}
DOC_LABELS = {
    "invoice": [
        ("Invoice No", "invoice_no"),
        ("Invoice Date", "invoice_date"),
        ("Seller", "seller_name"),
        ("Seller Country", "seller_country"),
        ("Buyer", "buyer_name"),
        ("Buyer Country", "buyer_country"),
        ("Vessel", "vessel_name"),
        ("Goods", "goods_description"),
        ("Currency", "currency"),
        ("Amount", "amount"),
    ],
    "bl": [
        ("B/L No", "bl_no"),
        ("Shipper", "seller_name"),
        ("Consignee", "buyer_name"),
        ("Vessel", "vessel_name"),
        ("Port of Loading", "port_of_loading"),
        ("Port of Discharge", "port_of_discharge"),
        ("Goods", "goods_description"),
    ],
}
DOC_TYPES = tuple(DOC_LABELS)
PNG_DPI = 200

# ---------------------------------------------------------------- 합성 케이스 (truth.json의 case_type)
CASE_CLEAN = "clean"
CASE_EXACT = "exact_match"
CASE_FUZZY = "fuzzy_match"
CASE_NEW = "new_listing"
CASE_VESSEL = "sanctioned_vessel"
CASE_MIX = {CASE_CLEAN: 70, CASE_EXACT: 10, CASE_FUZZY: 10, CASE_NEW: 5, CASE_VESSEL: 5}
EXPECTED = {  # case_type -> {list_version: 기대 분류}
    CASE_CLEAN: {LIST_BASE: PASS, LIST_NEW: PASS},
    CASE_EXACT: {LIST_BASE: HOLD, LIST_NEW: HOLD},
    CASE_FUZZY: {LIST_BASE: REVIEW, LIST_NEW: REVIEW},
    CASE_NEW: {LIST_BASE: PASS, LIST_NEW: HOLD},
    CASE_VESSEL: {LIST_BASE: HOLD, LIST_NEW: HOLD},
}

# truth.json = {
#   tx_id, received_at, case_type, matched_entry_id, expected:{list-v14:..., list-v15:...},
#   fields:{TX_FIELDS...}, documents:{invoice:{pdf,png}, bl:{pdf,png}}   (경로는 DATA 기준 상대경로)
# }
