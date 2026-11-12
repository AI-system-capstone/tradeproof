"""ocr-worker: Postgres 큐(screening_jobs)에서 심사 건을 꺼내 OCR -> 판정 -> 저장.

API 는 접수만 하고(status=queued), 이 워커가 처리한다. 워커를 여러 개 띄워도 같은 건을 두 번 잡지 않는다
(store.claim: SELECT ... FOR UPDATE SKIP LOCKED).

  python ocr/worker.py                      # 계속 돈다 (SIGTERM/SIGINT 를 받으면 지금 건을 끝내고 종료)
  python ocr/worker.py --drain              # 큐가 빌 때까지 처리하고 종료
  python ocr/worker.py --once               # 한 건만
  환경변수: DATABASE_URL(필수), BLOB_*(common.blob.make_blob_store), WORKER_ID(기본 호스트명)

한 건 처리:
  - 번들은 job 의 bundle_id(접수 시점)를 쓴다. ACTIVE_BUNDLE 은 읽지 않는다 (회의 3-3 버전 고정).
  - 주 번들을 먼저 OCR·판정하고 store.complete 한다 (처리 시간 지표 started_at~completed_at 에 섀도 시간이 안 들어감).
    그다음 섀도 번들(있으면)을 따로 판정해 is_shadow=true 로 남긴다. 섀도가 실패해도 주 결과·상태는 그대로
    (로그 + shadow 항목 error). 같은 모델이면 OCR 은 한 번만.
  - 펜싱: complete/requeue/give_up/add_shadow_result 에 claim 때의 (worker_id, attempts) 를 넘긴다.
    오래 걸려 회수(release_stale)된 뒤 다른 워커가 잡은 건이면 저장소가 0건 갱신 -> 결과를 버린다.
  - input.source: sample(data/docs/<id>/{invoice,bl}.png 실제 OCR) / upload(원문 저장소, PDF 는 PNG 로 렌더링해
    저장소에도 저장: 검토 화면 이미지) / fields(OCR 없음, 준 값 그대로)
  - 실패: attempts < MAX_ATTEMPTS 면 다시 queued, 닿으면 failed + 주 번들 MANUAL_REVIEW(processing_failed 근거).
    재시도해도 안 되는 오류(PermanentJobError: 스크리닝 필드 없음, 샘플 ID 이상, 원문 형식 이상 등)는 바로 failed.
    OCR 값은 고치지 않는다. 불확실하면 판정 규칙이 담당자 검토로 보낸다.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common import contracts as C  # noqa: E402
from common.blob import make_blob_store  # noqa: E402
from a_screening.store import PostgresStore, failed_reason  # noqa: E402
from a_screening import pipeline as P  # noqa: E402  (OCR → 판정 경로: C 의 골든셋·Replay 와 같은 함수)
from a_screening.screening.match import is_blank  # noqa: E402

STALE_CHECK_EVERY = 60.0     # run_forever: 이 간격(초)마다 죽은 워커가 잡고 있던 건을 되돌린다
DEFAULT_STALE_AFTER = 600.0  # processing 인 채로 이 시간(초)이 지나면 워커가 죽은 것으로 본다


class PermanentJobError(ValueError):
    """다시 시도해도 결과가 같은 입력 오류. MAX_ATTEMPTS 를 기다리지 않고 바로 give_up (담당자 검토)."""


def log(**kw) -> None:
    print(json.dumps(kw, ensure_ascii=False, default=str), flush=True)


def error_body(message: str, tx_id: str) -> dict:
    return dict(zip(C.ERROR_FIELDS, (C.ERR_SCREENING_FAILED, message, tx_id)))


# ---------------------------------------------------------------- 서류 준비 (OCR 입력 PNG)
def _source(job: dict) -> str:
    inp = job["input"] or {}
    return inp.get("source") or (C.INPUT_SAMPLE if inp.get("sample_tx_id") else C.INPUT_FIELDS)


def _sample_dir(sample_tx_id) -> Path:
    d = C.sample_dir(sample_tx_id)   # 형식(SAMPLE_TX_ID_RE)·DOCS_DIR 밖('..' 등)·없는 폴더 모두 None
    if d is None:
        raise PermanentJobError(f"샘플 거래 {sample_tx_id!r} 서류 없음 또는 형식이 틀림 ({C.DOCS_DIR})")
    return d


def _magic_ext(data: bytes) -> str | None:
    return next((ext for ext, magic in C.FILE_MAGIC.items() if data.startswith(magic)), None)


def render_pdf(pdf: bytes, workdir: Path) -> bytes:
    """PDF 첫 쪽을 contracts.PNG_DPI PNG 로 (합성 서류 생성과 같은 해상도)."""
    src = workdir / "in.pdf"
    src.write_bytes(pdf)
    out = workdir / "page"
    subprocess.run(["pdftoppm", "-png", "-r", str(C.PNG_DPI), "-f", "1", "-l", "1", "-singlefile", str(src), str(out)],
                   check=True, capture_output=True, timeout=120)
    return out.with_suffix(".png").read_bytes()


def prepare_upload(job: dict, blob, workdir: Path) -> Path:
    """업로드 원문 -> workdir/<tx_id>/{invoice,bl}.png. PDF 는 렌더링한 PNG 를 저장소에도 넣는다."""
    tx_id = job["tx_id"]
    tx_dir = workdir / tx_id
    tx_dir.mkdir(parents=True, exist_ok=True)
    n = 0
    for doc in C.UPLOAD_DOC_TYPES:
        uri = job.get(f"doc_{doc}_uri")
        if not uri:
            continue
        data = blob.get(uri)   # 저장소 일시 장애는 재시도 대상 (PermanentJobError 아님)
        ext = _magic_ext(data)
        if ext == ".pdf":
            tmp = workdir / f"render-{doc}"
            tmp.mkdir()
            data = render_pdf(data, tmp)
            blob.put(C.blob_key(tx_id, doc, ".png"), data, "image/png")
        elif ext != ".png":
            raise PermanentJobError(f"{doc} 원문이 {list(C.FILE_MAGIC)} 형식이 아님 ({uri})")
        (tx_dir / f"{doc}.png").write_bytes(data)
        n += 1
    if not n:
        raise PermanentJobError("업로드된 서류 없음 (doc_invoice_uri, doc_bl_uri 모두 비어 있음)")
    return tx_dir


def _given_fields(job: dict) -> dict:
    """fields 입력. 스크리닝 필드가 비어 있으면 재시도해도 소용없음 (API 가 422 로 막지만 이중 방어)."""
    given = (job["input"] or {}).get("fields")
    if not isinstance(given, dict):
        raise PermanentJobError("input.fields 가 없음 (fields 접수)")
    missing = [f for f in C.SCREENING_FIELDS if is_blank(given.get(f))]
    if missing:
        raise PermanentJobError(f"스크리닝 필드가 비어 있음: {missing}")
    return given


def run_extraction(tx_dir: Path, model_version: str) -> tuple[dict, list]:
    """OCR + 필드 추출 (pipeline.extract_documents 그대로). 반환: (extracted_fields, conflicts)"""
    return P.extract_documents(tx_dir, model_version)


# ---------------------------------------------------------------- 한 건 처리
def screen_job(job: dict, blob, workdir: Path, bundle_id: str | None = None, ctx: dict | None = None) -> dict:
    """한 번들 판정 (기본: 주 번들). ctx 를 같이 넘기면 서류 준비·모델별 OCR 결과를 번들끼리 재사용한다.
    반환: {bundle_id, model_version, extracted, conflicts, source, result}"""
    ctx = {} if ctx is None else ctx
    bid = bundle_id or job["bundle_id"]
    if "tx_dir" not in ctx:
        src = _source(job)
        if src == C.INPUT_SAMPLE:
            ctx["tx_dir"] = _sample_dir((job["input"] or {}).get("sample_tx_id"))
        elif src == C.INPUT_UPLOAD:
            ctx["tx_dir"] = prepare_upload(job, blob, workdir)
        elif src == C.INPUT_FIELDS:
            ctx["tx_dir"] = None
            ctx["given"] = _given_fields(job)
        else:
            raise PermanentJobError(f"알 수 없는 input.source {src!r} ({list(C.INPUT_SOURCES)} 중 하나)")
    tx_dir = ctx["tx_dir"]
    b = P.bundle_spec(bid)
    model = b["model_version"]
    if tx_dir is None:
        given = ctx["given"]
        extracted = {f: {"value": given.get(f), "source_doc": None, "bbox": None, "confidence": None}
                     for f in C.TX_FIELDS}
        conflicts, source, from_ocr = [], C.EXTRACTION_FROM_FIELDS, False
    else:
        cache = ctx.setdefault("ocr", {})
        if model not in cache:
            cache[model] = run_extraction(tx_dir, model)
        extracted, conflicts = cache[model]
        source, from_ocr = C.extraction_source_ocr(model), True
    res = P.screen_extracted(extracted, conflicts, b, from_ocr=from_ocr)   # pipeline.screen_documents 와 같은 판정
    return {"bundle_id": bid, "model_version": model, "extracted": extracted, "conflicts": conflicts,
            "source": source, "result": res}


def _run_shadow(job: dict, store, blob, workdir: Path, ctx: dict, key: dict) -> dict | None:
    """섀도 판정. 예외를 올리지 않는다 (주 결과·상태에 영향 없음). 반환: 결과 요약 (섀도 없으면 None).
    실패하면 섀도 결과를 MANUAL_REVIEW + processing_failed 근거로 남긴다 (get() 의 shadow 항목 error, 불일치 지표 제외)."""
    sb = job.get("shadow_bundle_id")
    if not sb:
        return None
    tx_id = job["tx_id"]
    try:
        result, err = screen_job(job, blob, workdir, bundle_id=sb, ctx=ctx)["result"], None
    except Exception as e:  # noqa: BLE001
        err = error_body(f"섀도 {sb}: {type(e).__name__}: {e}", tx_id)
        log(event="shadow_failed", tx_id=tx_id, shadow_bundle_id=sb, error=err)
        result = {"classification": C.REVIEW, "reasons": [failed_reason(err)]}
    try:
        if not store.add_shadow_result(tx_id, sb, result, **key):
            log(event="shadow_discarded", tx_id=tx_id, shadow_bundle_id=sb, reason="fenced", **key)
            return None
    except Exception as e:  # noqa: BLE001
        log(event="shadow_store_failed", tx_id=tx_id, shadow_bundle_id=sb, error=f"{type(e).__name__}: {e}")
        return None
    if err:
        return {"bundle_id": sb, "classification": None, "error": err}
    return {"bundle_id": sb, "classification": result["classification"]}


def process_job(job: dict, store, blob, worker_id: str) -> dict:
    """claim 한 job 하나를 끝까지 처리한다. 반환: {tx_id, status, classification, ...} (로그와 같은 내용).
    펜싱으로 결과를 버렸으면 status=None, discarded=True."""
    tx_id, t0 = job["tx_id"], time.perf_counter()
    key = {"worker_id": worker_id, "attempts": job["attempts"]}
    base = {"tx_id": tx_id, "bundle_id": job["bundle_id"], "attempts": job["attempts"], "worker_id": worker_id}
    completed, sh, out = False, None, None
    try:
        with tempfile.TemporaryDirectory(prefix="ocr-worker-") as tmp:
            ctx: dict = {}
            primary = screen_job(job, blob, Path(tmp), ctx=ctx)
            completed = store.complete(tx_id, primary["extracted"], primary["source"], primary["conflicts"],
                                       primary["result"], **key)
            if not completed:
                out = {**base, "status": None, "discarded": True, "classification": None,
                       "reason": "다른 워커가 다시 잡았거나 상태가 바뀜 (펜싱)"}
            else:
                sh = _run_shadow(job, store, blob, Path(tmp), ctx, key)
    except Exception as e:  # noqa: BLE001  실패 사유를 공통 에러 형식으로 남기고 재시도 / 담당자 검토
        if completed:   # 주 결과 저장 뒤의 예외 (임시 폴더 정리 등): 판정은 그대로 둔다
            log(event="post_complete_error", tx_id=tx_id, error=f"{type(e).__name__}: {e}")
        else:
            out = _handle_failure(job, store, base, key, e)
    if completed:
        cls = primary["result"]["classification"]
        out = {**base, "status": C.SCREENING_COMPLETED, "classification": cls, "shadow": sh}
    out["seconds"] = round(time.perf_counter() - t0, 3)
    log(**out)
    return out


def _handle_failure(job, store, base, key, e) -> dict:
    tx_id = job["tx_id"]
    err = error_body(f"{type(e).__name__}: {e}", tx_id)
    permanent = isinstance(e, PermanentJobError)
    if permanent or job["attempts"] >= C.MAX_ATTEMPTS:
        if not store.give_up(tx_id, err, **key):
            return {**base, "status": None, "discarded": True, "classification": None, "error": err}
        rec = store.get(tx_id) or {}
        cls = rec.get("classification") or C.REVIEW
        return {**base, "status": C.SCREENING_FAILED, "classification": cls, "error": err, "permanent": permanent}
    if not store.requeue(tx_id, err, **key):
        return {**base, "status": None, "discarded": True, "classification": None, "error": err}
    return {**base, "status": C.SCREENING_QUEUED, "classification": None, "error": err}


# ---------------------------------------------------------------- 회수 (죽은 워커가 잡고 있던 건)
def release_stale(store, worker_id: str, stale_after: float, tx_ids=None) -> list:
    """store.release_stale 로 죽은 워커가 잡고 있던 건을 되돌린다. 반환: [(tx_id, 새 상태)]"""
    released = store.release_stale(stale_after, tx_ids=tx_ids) if tx_ids is not None \
        else store.release_stale(stale_after)
    if released:
        log(event="released_stale", worker_id=worker_id, released=released)
    return released


def _release_quietly(store, worker_id, stale_after, tx_ids=None) -> None:
    if not stale_after:
        return
    try:
        release_stale(store, worker_id, stale_after, tx_ids)
    except Exception as e:  # noqa: BLE001  회수는 정리 작업: 실패해도 처리는 계속
        log(event="release_stale_error", worker_id=worker_id, error=f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------- 루프
def run_once(store, blob, worker_id: str, tx_ids=None, stale_after: float | None = DEFAULT_STALE_AFTER) -> bool:
    """(회수 한 번 후) 한 건 꺼내 처리. 처리했으면 True, 큐가 비었으면 False.
    tx_ids 는 테스트·점검용 (그 건들만). stale_after 가 0/None 이면 회수하지 않는다."""
    _release_quietly(store, worker_id, stale_after, tx_ids)
    job = store.claim(worker_id, tx_ids=tx_ids) if tx_ids is not None else store.claim(worker_id)
    if job is None:
        return False
    process_job(job, store, blob, worker_id)
    return True


def drain(store, blob, worker_id: str, tx_ids=None, max_jobs: int | None = None,
          stale_after: float | None = DEFAULT_STALE_AFTER) -> int:
    """(시작할 때 회수 한 번 후) 큐가 빌 때까지 처리. 반환: 처리 건수 (재시도로 다시 꺼낸 것도 한 번씩 센다)."""
    _release_quietly(store, worker_id, stale_after, tx_ids)
    n = 0
    while (max_jobs is None or n < max_jobs) and run_once(store, blob, worker_id, tx_ids, stale_after=None):
        n += 1
    return n


def run_forever(store, blob, worker_id: str, poll_interval: float = 1.0,
                stale_after: float = DEFAULT_STALE_AFTER, stop: threading.Event | None = None) -> int:
    """계속 처리. SIGTERM/SIGINT(또는 stop.set())면 지금 건을 끝내고 종료. 반환: 처리 건수."""
    stop = stop or threading.Event()
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
    n, last_stale = 0, 0.0
    log(event="worker_started", worker_id=worker_id, store=store.kind, poll=poll_interval)
    while not stop.is_set():
        try:
            if stale_after and time.monotonic() - last_stale >= STALE_CHECK_EVERY:
                last_stale = time.monotonic()
                release_stale(store, worker_id, stale_after)
            if run_once(store, blob, worker_id, stale_after=None):
                n += 1
                continue
        except Exception as e:  # noqa: BLE001  DB 일시 장애 등: 워커는 죽지 않고 다음 주기에 다시
            log(event="worker_error", worker_id=worker_id, error=f"{type(e).__name__}: {e}")
        stop.wait(poll_interval)
    log(event="worker_stopped", worker_id=worker_id, processed=n)
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="ocr-worker: screening_jobs 큐 처리")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--once", action="store_true", help="한 건만 처리")
    g.add_argument("--drain", action="store_true", help="큐가 빌 때까지 처리하고 종료")
    ap.add_argument("--worker-id", default=os.environ.get("WORKER_ID") or socket.gethostname())
    ap.add_argument("--poll", type=float, default=1.0, help="큐가 비었을 때 다시 볼 간격(초)")
    ap.add_argument("--stale-after", type=float, default=DEFAULT_STALE_AFTER,
                    help="processing 인 채로 이 시간(초)이 지나면 워커가 죽은 것으로 보고 되돌림 (0 이면 끔)")
    a = ap.parse_args(argv)
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL 이 필요함 (워커는 Postgres 큐를 씀)", file=sys.stderr)
        return 2
    store, blob = PostgresStore(dsn), make_blob_store()
    if a.once:
        run_once(store, blob, a.worker_id, stale_after=a.stale_after)
    elif a.drain:
        log(event="drained", worker_id=a.worker_id,
            processed=drain(store, blob, a.worker_id, stale_after=a.stale_after))
    else:
        run_forever(store, blob, a.worker_id, poll_interval=a.poll, stale_after=a.stale_after)
    return 0


if __name__ == "__main__":
    sys.exit(main())
