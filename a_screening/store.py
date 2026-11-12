"""심사 건 저장소 + 처리 큐. DATABASE_URL 이 있으면 Postgres, 없으면 메모리(로컬 테스트용, 프로세스 하나만).

Postgres 에 쓰는 곳 (common/schema/10_a_screening.sql):
  transactions        거래 레코드 (공통 스키마 3-1): 접수 때 만들고, 판정 끝나면 추출 필드를 채운다
  screening_results   판정 결과 (공통 스키마 3-1): (tx_id, bundle_id) 한 줄, 담당자 판단·정정도 여기.
                      섀도 판정 결과는 is_shadow=true 로 (tx_id, 섀도 번들) 한 줄 (실제 판정에는 쓰지 않음)
  screening_jobs      처리 큐 (A 내부): queued → processing → completed / failed, 접수 시점 번들 고정.
                      ocr-worker 가 claim() 으로 꺼낸다 (SELECT ... FOR UPDATE SKIP LOCKED: 워커·프로세스가
                      여러 개여도 같은 건을 두 번 주지 않음)

  API     create() -> (워커) claim() -> complete() -> add_shadow_result() / requeue() / give_up()
  화면    get(), list(), review()      모니터링  stats()

펜싱: claim() 이 준 (worker_id, attempts) 를 complete/requeue/give_up/add_shadow_result 에 넘기면
  'status=processing AND worker_id AND attempts' 가 맞을 때만 쓴다 (add_shadow_result 는 complete 뒤라 completed 도 허용).
  오래 걸려 release_stale 로 회수된 뒤 다른 워커가 다시 잡은 건에, 늦게 끝난 옛 워커가 결과를 덮어쓰지 못하게 한다.
  조건이 안 맞으면 아무것도 쓰지 않고 False. (worker_id/attempts 를 안 주면 조건 없이 쓴다: 기존 호출 호환)

섀도 처리 실패: 섀도 결과를 MANUAL_REVIEW + processing_failed 근거로 남기고(실제 판정 무관), get() 의 shadow 항목
  error 에 공통 에러 형식으로 보인다. 섀도 불일치 지표에서는 뺀다.

두 저장소는 get()/list()/stats() 가 같은 모양의 dict 를 돌려준다 (API 응답 그대로).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from itertools import count
from threading import Lock

from common import contracts as C
from a_screening.db_values import amount_value, date_value

_SEVERITY = {C.PASS: 0, C.REVIEW: 1, C.HOLD: 2}
RECENT_N = 100                                        # stats(): 번들별 최근 완료 N건 평균
_FAILED_REASON_JSON = [{"rule": C.RULE_PROCESSING_FAILED}]   # jsonb @> 로 처리 실패 근거 찾기


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _iso(v):
    return v.isoformat(timespec="seconds") if isinstance(v, datetime) else v


def _check_bundles(bundle_id: str, shadow_bundle_id: str | None) -> None:
    if bundle_id not in C.BUNDLES:
        raise ValueError(f"bundle_id={bundle_id!r} 는 contracts.BUNDLES 에 없음")
    if shadow_bundle_id is not None:
        if shadow_bundle_id not in C.BUNDLES:
            raise ValueError(f"shadow_bundle_id={shadow_bundle_id!r} 는 contracts.BUNDLES 에 없음")
        if shadow_bundle_id == bundle_id:
            raise ValueError("shadow_bundle_id 는 주 번들과 달라야 함 (screening_results 가 (tx_id, bundle_id) 한 줄)")


def _classifications(classification) -> list[str] | None:
    """분류 필터: None / 문자열 하나 / 쉼표 구분 문자열 / list·tuple. 반환: 검증된 목록 (필터 없음이면 None)."""
    if classification is None:
        return None
    if isinstance(classification, str):
        vals = [v.strip() for v in classification.split(",")]
    elif isinstance(classification, (list, tuple, set, frozenset)):
        vals = []
        for v in classification:
            if not isinstance(v, str):
                raise ValueError(f"classification 은 {C.CLASSIFICATIONS} 중에서 (문자열)")
            vals += [x.strip() for x in v.split(",")]
    else:
        raise ValueError(f"classification 은 {C.CLASSIFICATIONS} 중 하나 또는 여러 개")
    vals = list(dict.fromkeys(v for v in vals if v))
    for v in vals:
        if v not in C.CLASSIFICATIONS:
            raise ValueError(f"classification 은 {C.CLASSIFICATIONS} 중에서 ({v!r} 는 없음)")
    return vals or None


def _check_filters(status, classification, bundle_id) -> list[str] | None:
    if status is not None and status not in C.SCREENING_STATUSES:
        raise ValueError(f"status 는 {C.SCREENING_STATUSES} 중 하나")
    if bundle_id is not None and bundle_id not in C.BUNDLES:
        raise ValueError(f"bundle_id 는 {list(C.BUNDLES)} 중 하나")
    return _classifications(classification)


def failed_reason(error: dict) -> dict:
    """처리 실패 근거 (주 번들: MAX_ATTEMPTS 회 / 재시도 무의미, 섀도: 한 번). CLAUDE.md: 반복 실패 거래는 담당자 검토로."""
    return dict(zip(C.REASON_FIELDS, (C.RULE_PROCESSING_FAILED, None, (error or {}).get("message"), None, None,
                                      None, C.REVIEW)))


def _is_failed_result(reasons) -> bool:
    return any(isinstance(r, dict) and r.get("rule") == C.RULE_PROCESSING_FAILED for r in reasons or [])


def _escalate(classification: str | None) -> str:
    """실패 처리로 분류를 내리지 않는다 (이미 HOLD 면 HOLD 유지: 미탐 방지)."""
    if classification and _SEVERITY[classification] > _SEVERITY[C.REVIEW]:
        return classification
    return C.REVIEW


def _error(tx_id: str, message: str) -> dict:
    return dict(zip(C.ERROR_FIELDS, (C.ERR_SCREENING_FAILED, message, tx_id)))


def _shadow_item(tx_id: str, s: dict) -> dict:
    """get() 의 shadow 항목. 처리 실패로 남은 섀도는 error 에 공통 에러 형식."""
    err = None
    if _is_failed_result(s.get("reasons")):
        msg = next(r.get("value") for r in s["reasons"] if r.get("rule") == C.RULE_PROCESSING_FAILED)
        err = _error(tx_id, msg or "섀도 처리 실패")
    return {"bundle_id": s["bundle_id"], "classification": s["classification"], "reasons": s.get("reasons") or [],
            "error": err}


def _record(tx_id: str, bundle_id: str, status: str, input_: dict, *, submitted_at, started_at=None,
            completed_at=None, received_at=None, doc_invoice_uri=None, doc_bl_uri=None, extracted_fields=None,
            extraction_source=None, conflicts=None, classification=None, reasons=None, error=None,
            reviewer_decision=None, reviewer=None, reviewer_note=None, corrected_fields=None, reviewed_at=None,
            attempts=0, worker_id=None, shadow_bundle_id=None, shadow=None) -> dict:
    """API 응답 한 건. 두 저장소가 같이 쓴다."""
    b = C.BUNDLES[bundle_id]
    fields = None
    if extracted_fields:
        fields = {f: (extracted_fields.get(f) or {}).get("value") for f in C.TX_FIELDS}
    return {
        "tx_id": tx_id, "status": status, "bundle_id": bundle_id,
        "model_version": b["model_version"], "rules_version": b["rules_version"], "list_version": b["list_version"],
        "input": input_, "received_at": _iso(received_at),
        "submitted_at": _iso(submitted_at), "started_at": _iso(started_at), "completed_at": _iso(completed_at),
        "attempts": attempts or 0, "worker_id": worker_id,
        "documents": {"invoice": doc_invoice_uri, "bl": doc_bl_uri},
        "fields": fields, "extracted_fields": extracted_fields or None, "extraction_source": extraction_source,
        "conflicts": conflicts or [], "classification": classification, "reasons": reasons or [], "error": error,
        "reviewer_decision": reviewer_decision, "reviewer": reviewer, "reviewer_note": reviewer_note,
        "corrected_fields": corrected_fields, "reviewed_at": _iso(reviewed_at),
        "shadow_bundle_id": shadow_bundle_id, "shadow": [_shadow_item(tx_id, s) for s in shadow or []],
    }


def _empty_stats() -> dict:
    return {"results": [], "jobs": {s: 0 for s in C.SCREENING_STATUSES}, "processing_seconds_avg": {},
            "ocr_confidence_avg": {}, "failed": {}, "shadow_disagreements": [],
            "reviews": {d: 0 for d in C.REVIEW_DECISIONS}}


def _fenced(worker_id, attempts) -> bool:
    return worker_id is not None or attempts is not None


# ---------------------------------------------------------------- 메모리
class MemoryStore:
    kind = "memory"
    _INTERNAL = ("seq", "t_started", "t_completed", "shadow_results")   # get() 에 내보내지 않는 키

    def __init__(self):
        self._rows: dict[str, dict] = {}
        self._lock = Lock()
        self._seq = count()

    def ping(self) -> None:
        pass

    def create(self, tx_id, bundle_id, input_, doc_invoice_uri=None, doc_bl_uri=None, shadow_bundle_id=None) -> dict:
        _check_bundles(bundle_id, shadow_bundle_id)
        t = now_iso()
        with self._lock:
            if tx_id in self._rows:
                raise ValueError(f"tx_id {tx_id} 이미 있음")
            self._rows[tx_id] = dict(tx_id=tx_id, bundle_id=bundle_id, status=C.SCREENING_QUEUED, input_=input_,
                                     submitted_at=t, received_at=t, doc_invoice_uri=doc_invoice_uri,
                                     doc_bl_uri=doc_bl_uri, shadow_bundle_id=shadow_bundle_id, attempts=0,
                                     seq=next(self._seq), shadow_results={})
        return self.get(tx_id)

    def set_status(self, tx_id, status) -> None:
        self._rows[tx_id]["status"] = status

    def start(self, tx_id) -> dict:
        """(api BackgroundTasks 용, 큐 전환 전 호환) 지정한 건을 processing 으로."""
        with self._lock:
            r = self._rows[tx_id]
            r.update(status=C.SCREENING_PROCESSING, started_at=now_iso(), t_started=time.time())
        return {"tx_id": tx_id, "bundle_id": r["bundle_id"], "input": r["input_"]}

    def _job(self, r: dict) -> dict:
        return {"tx_id": r["tx_id"], "bundle_id": r["bundle_id"], "shadow_bundle_id": r.get("shadow_bundle_id"),
                "input": r["input_"], "attempts": r["attempts"], "doc_invoice_uri": r.get("doc_invoice_uri"),
                "doc_bl_uri": r.get("doc_bl_uri")}

    def _owned(self, tx_id, worker_id, attempts, statuses=(C.SCREENING_PROCESSING,)) -> dict | None:
        """펜싱 조건이 맞는 행 (조건을 안 주면 그냥 그 행). 잠금 안에서 부른다."""
        r = self._rows.get(tx_id)
        if r is None:
            return None
        if _fenced(worker_id, attempts) and not (r["status"] in statuses and r.get("worker_id") == worker_id
                                                  and r["attempts"] == attempts):
            return None
        return r

    def claim(self, worker_id, tx_ids=None) -> dict | None:
        """가장 오래된 queued 하나를 processing 으로 (attempts += 1). tx_ids 를 주면 그 안에서만 (테스트·운영 점검용)."""
        with self._lock:
            queued = [r for r in self._rows.values() if r["status"] == C.SCREENING_QUEUED
                      and (tx_ids is None or r["tx_id"] in tx_ids)]
            if not queued:
                return None
            r = min(queued, key=lambda x: x["seq"])
            r.update(status=C.SCREENING_PROCESSING, attempts=r["attempts"] + 1, started_at=now_iso(),
                     t_started=time.time(), worker_id=worker_id)
            return self._job(r)

    def complete(self, tx_id, extracted_fields, extraction_source, conflicts, result, *,
                 worker_id=None, attempts=None) -> bool:
        with self._lock:
            r = self._owned(tx_id, worker_id, attempts)
            if r is None:
                return False
            r.update(status=C.SCREENING_COMPLETED, completed_at=now_iso(), t_completed=time.time(),
                     extracted_fields=extracted_fields, extraction_source=extraction_source,
                     conflicts=conflicts, classification=result["classification"],
                     reasons=result["reasons"], error=None)
            return True

    def add_shadow_result(self, tx_id, bundle_id, result, *, worker_id=None, attempts=None) -> bool:
        with self._lock:
            r = self._owned(tx_id, worker_id, attempts, (C.SCREENING_PROCESSING, C.SCREENING_COMPLETED))
            if r is None:
                return False
            r["shadow_results"][bundle_id] = {"bundle_id": bundle_id, "classification": result["classification"],
                                              "reasons": result["reasons"]}
            return True

    @staticmethod
    def _do_requeue(r, error) -> None:
        r.update(status=C.SCREENING_QUEUED, error=error)

    @staticmethod
    def _do_give_up(r, error) -> None:
        r.update(status=C.SCREENING_FAILED, error=error, completed_at=now_iso(), t_completed=time.time(),
                 classification=_escalate(r.get("classification")),
                 reasons=list(r.get("reasons") or []) + [failed_reason(error)])

    def requeue(self, tx_id, error, *, worker_id=None, attempts=None) -> bool:
        with self._lock:
            r = self._owned(tx_id, worker_id, attempts)
            if r is None:
                return False
            self._do_requeue(r, error)
            return True

    def give_up(self, tx_id, error, *, worker_id=None, attempts=None) -> bool:
        with self._lock:
            r = self._owned(tx_id, worker_id, attempts)
            if r is None:
                return False
            self._do_give_up(r, error)
            return True

    def release_stale(self, older_than_seconds, tx_ids=None) -> list[tuple[str, str]]:
        """processing 인 채로 오래된 건(워커가 죽음)을 되돌린다. attempts 가 다 찼으면 give_up.
        반환: [(tx_id, 새 상태)]  새 상태 = queued / failed"""
        cutoff = time.time() - older_than_seconds
        out = []
        with self._lock:
            for r in self._rows.values():
                if r["status"] != C.SCREENING_PROCESSING or r.get("t_started", 0) >= cutoff:
                    continue
                if tx_ids is not None and r["tx_id"] not in tx_ids:
                    continue
                err = _error(r["tx_id"], f"처리 중 워커 응답 없음 ({older_than_seconds:g}초 초과)")
                if r["attempts"] >= C.MAX_ATTEMPTS:
                    self._do_give_up(r, err)
                else:
                    self._do_requeue(r, err)
                out.append((r["tx_id"], r["status"]))
        return out

    def fail(self, tx_id, error) -> None:
        self._rows[tx_id].update(status=C.SCREENING_FAILED, completed_at=now_iso(), t_completed=time.time(),
                                 error=error)

    def review(self, tx_id, decision, reviewer, note, corrected_fields) -> None:
        self._rows[tx_id].update(reviewer_decision=decision, reviewer=reviewer, reviewer_note=note,
                                 corrected_fields=corrected_fields, reviewed_at=now_iso())

    def get(self, tx_id) -> dict | None:
        with self._lock:
            r = self._rows.get(tx_id)
            if r is None:
                return None
            r = dict(r)
            shadow = sorted((dict(s) for s in r["shadow_results"].values()), key=lambda s: s["bundle_id"])
        for k in self._INTERNAL:
            r.pop(k, None)
        return _record(r.pop("tx_id"), r.pop("bundle_id"), r.pop("status"), r.pop("input_"), shadow=shadow, **r)

    def list(self, status=None, classification=None, bundle_id=None, limit=50, offset=0,
             unreviewed_first=False) -> dict:
        classes = _check_filters(status, classification, bundle_id)
        with self._lock:
            rows = [dict(r) for r in self._rows.values()]
        rows = [r for r in rows if (status is None or r["status"] == status)
                and (classes is None or r.get("classification") in classes)
                and (bundle_id is None or r["bundle_id"] == bundle_id)]
        rows.sort(key=lambda r: r["seq"], reverse=True)          # 최신 접수 먼저
        if unreviewed_first:                                      # 안정 정렬: 그 안에서는 최신 접수순 유지
            rows.sort(key=lambda r: r.get("reviewer_decision") is not None)
        items = []
        for r in rows[offset:offset + limit]:
            ext = r.get("extracted_fields") or {}
            items.append({"tx_id": r["tx_id"], "status": r["status"], "bundle_id": r["bundle_id"],
                          "classification": r.get("classification"), "submitted_at": r["submitted_at"],
                          "completed_at": r.get("completed_at"), "reviewer_decision": r.get("reviewer_decision"),
                          **{f: (ext.get(f) or {}).get("value") for f in C.SCREENING_FIELDS}})
        return {"items": items, "total": len(rows)}

    def stats(self) -> dict:
        """메모리 저장소의 행은 모두 심사 서비스가 접수한 건 (시드·Replay 없음)."""
        with self._lock:
            rows = [dict(r, shadow_results=dict(r["shadow_results"])) for r in self._rows.values()]
        out = _empty_stats()
        results, failed, disagree, secs, confs = {}, {}, {}, {}, {}
        for r in rows:
            out["jobs"][r["status"]] += 1
            b = r["bundle_id"]
            if r.get("classification"):
                results[(b, r["classification"])] = results.get((b, r["classification"]), 0) + 1
            if r["status"] == C.SCREENING_FAILED:
                failed[b] = failed.get(b, 0) + 1
            if r.get("reviewer_decision"):
                out["reviews"][r["reviewer_decision"]] = out["reviews"].get(r["reviewer_decision"], 0) + 1
            sb = r.get("shadow_bundle_id")
            sh = r["shadow_results"].get(sb) if sb else None
            if sh and r.get("classification") and not _is_failed_result(sh["reasons"]) \
                    and sh["classification"] != r["classification"]:
                disagree[(b, sb)] = disagree.get((b, sb), 0) + 1
            if r["status"] == C.SCREENING_COMPLETED and r.get("t_started") and r.get("t_completed"):
                secs.setdefault(b, []).append((r["t_completed"], r["t_completed"] - r["t_started"], r))
        for b, lst in secs.items():
            recent = sorted(lst, key=lambda x: x[0], reverse=True)[:RECENT_N]
            out["processing_seconds_avg"][b] = round(sum(s for _, s, _ in recent) / len(recent), 3)
            vals = [((r.get("extracted_fields") or {}).get(f) or {}).get("confidence")
                    for _, _, r in recent for f in C.SCREENING_FIELDS]
            vals = [v for v in vals if isinstance(v, (int, float))]
            if vals:
                confs[b] = round(sum(vals) / len(vals), 2)
        out["results"] = [{"bundle_id": b, "classification": c, "count": n} for (b, c), n in sorted(results.items())]
        out["failed"] = failed
        out["ocr_confidence_avg"] = confs
        out["shadow_disagreements"] = [{"bundle_id": b, "shadow_bundle_id": sb, "count": n}
                                       for (b, sb), n in sorted(disagree.items())]
        return out


# ---------------------------------------------------------------- Postgres
_UPSERT_RESULT = (
    "INSERT INTO screening_results (tx_id, bundle_id, classification, reasons, screened_at, is_shadow) "
    "VALUES (%s, %s, %s, %s, now(), %s) ON CONFLICT (tx_id, bundle_id) DO UPDATE SET "
    "classification = EXCLUDED.classification, reasons = EXCLUDED.reasons, screened_at = now(), "
    "is_shadow = EXCLUDED.is_shadow")


def _fence_sql(worker_id, attempts, statuses=(C.SCREENING_PROCESSING,)) -> tuple[str, list]:
    """펜싱 WHERE 조각 (조건을 안 주면 빈 조각)."""
    if not _fenced(worker_id, attempts):
        return "", []
    return " AND status = ANY(%s) AND worker_id IS NOT DISTINCT FROM %s AND attempts = %s", \
        [list(statuses), worker_id, attempts]


class PostgresStore:
    """호출마다 연결 하나 (psycopg_pool 은 의존성에 없음. 워커는 건당 수 초라 연결 비용은 무시할 수준)."""
    kind = "postgres"

    def __init__(self, dsn: str):
        self.dsn = dsn

    def _conn(self):
        import psycopg  # DATABASE_URL 이 없으면 psycopg 없이도 API 가 뜨도록 여기서 import
        from psycopg.rows import dict_row
        from psycopg.types.json import set_json_dumps
        conn = psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=3)
        set_json_dumps(lambda o: json.dumps(o, ensure_ascii=False), conn)
        return conn

    def ping(self) -> None:
        with self._conn() as conn:
            conn.execute("SELECT 1 FROM screening_jobs LIMIT 1")

    def create(self, tx_id, bundle_id, input_, doc_invoice_uri=None, doc_bl_uri=None, shadow_bundle_id=None) -> dict:
        from psycopg.types.json import Jsonb
        _check_bundles(bundle_id, shadow_bundle_id)
        with self._conn() as conn, conn.transaction():
            conn.execute(
                "INSERT INTO transactions (tx_id, received_at, doc_invoice_uri, doc_bl_uri, extracted_fields, "
                "extraction_source) VALUES (%s, now(), %s, %s, '{}'::jsonb, 'pending')",
                (tx_id, doc_invoice_uri, doc_bl_uri))
            conn.execute("INSERT INTO screening_jobs (tx_id, bundle_id, status, input, shadow_bundle_id) "
                         "VALUES (%s, %s, %s, %s, %s)",
                         (tx_id, bundle_id, C.SCREENING_QUEUED, Jsonb(input_), shadow_bundle_id))
        return self.get(tx_id)

    def set_status(self, tx_id, status) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE screening_jobs SET status = %s WHERE tx_id = %s", (status, tx_id))

    def start(self, tx_id) -> dict:
        """(api BackgroundTasks 용, 큐 전환 전 호환) 지정한 건을 processing 으로."""
        with self._conn() as conn:
            row = conn.execute(
                "UPDATE screening_jobs SET status = %s, started_at = now() WHERE tx_id = %s "
                "RETURNING tx_id, bundle_id, input", (C.SCREENING_PROCESSING, tx_id)).fetchone()
        return row

    def claim(self, worker_id, tx_ids=None) -> dict | None:
        """가장 오래된 queued 하나를 원자적으로 processing 으로 (attempts += 1).
        FOR UPDATE SKIP LOCKED: 다른 워커가 잡은 줄은 건너뛴다. tx_ids 를 주면 그 안에서만 (테스트·운영 점검용)."""
        where = "status = %s" + (" AND tx_id = ANY(%s)" if tx_ids is not None else "")
        params = [C.SCREENING_QUEUED] + ([list(tx_ids)] if tx_ids is not None else [])
        with self._conn() as conn:
            row = conn.execute(
                f"WITH q AS (SELECT tx_id FROM screening_jobs WHERE {where} ORDER BY submitted_at, tx_id "
                "LIMIT 1 FOR UPDATE SKIP LOCKED), "
                "u AS (UPDATE screening_jobs j SET status = %s, attempts = j.attempts + 1, started_at = now(), "
                "worker_id = %s FROM q WHERE j.tx_id = q.tx_id "
                "RETURNING j.tx_id, j.bundle_id, j.shadow_bundle_id, j.input, j.attempts) "
                "SELECT u.*, t.doc_invoice_uri, t.doc_bl_uri FROM u JOIN transactions t ON t.tx_id = u.tx_id",
                (*params, C.SCREENING_PROCESSING, worker_id)).fetchone()
        return row

    def complete(self, tx_id, extracted_fields, extraction_source, conflicts, result, *,
                 worker_id=None, attempts=None) -> bool:
        from psycopg.types.json import Jsonb
        flat = {f: (extracted_fields.get(f) or {}).get("value") for f in C.TX_FIELDS}
        flat["amount"] = amount_value(flat["amount"])
        flat["invoice_date"] = date_value(flat["invoice_date"])
        sets = ", ".join(f"{f} = %s" for f in C.TX_FIELDS)
        fence, fparams = _fence_sql(worker_id, attempts)
        with self._conn() as conn, conn.transaction():
            # 큐 행을 먼저 바꾼다 (행 잠금 + 펜싱 확인). 0건이면 아무것도 쓰지 않는다
            j = conn.execute(
                "UPDATE screening_jobs SET status = %s, conflicts = %s, error = NULL, completed_at = now() "
                f"WHERE tx_id = %s{fence} RETURNING bundle_id",
                (C.SCREENING_COMPLETED, Jsonb(conflicts), tx_id, *fparams)).fetchone()
            if j is None:
                return False
            conn.execute(f"UPDATE transactions SET {sets}, extracted_fields = %s, extraction_source = %s WHERE tx_id = %s",
                         (*[flat[f] for f in C.TX_FIELDS], Jsonb(extracted_fields), extraction_source, tx_id))
            conn.execute(_UPSERT_RESULT, (tx_id, j["bundle_id"], result["classification"], Jsonb(result["reasons"]),
                                          False))
        return True

    def add_shadow_result(self, tx_id, bundle_id, result, *, worker_id=None, attempts=None) -> bool:
        from psycopg.types.json import Jsonb
        fence, fparams = _fence_sql(worker_id, attempts, (C.SCREENING_PROCESSING, C.SCREENING_COMPLETED))
        with self._conn() as conn, conn.transaction():
            if fence and conn.execute(f"SELECT 1 FROM screening_jobs WHERE tx_id = %s{fence} FOR UPDATE",
                                      (tx_id, *fparams)).fetchone() is None:
                return False
            conn.execute(_UPSERT_RESULT, (tx_id, bundle_id, result["classification"], Jsonb(result["reasons"]), True))
        return True

    @staticmethod
    def _requeue(conn, tx_id, error, fence="", fparams=()) -> bool:
        from psycopg.types.json import Jsonb
        return conn.execute(f"UPDATE screening_jobs SET status = %s, error = %s WHERE tx_id = %s{fence} RETURNING tx_id",
                            (C.SCREENING_QUEUED, Jsonb(error), tx_id, *fparams)).fetchone() is not None

    @staticmethod
    def _give_up(conn, tx_id, error, fence="", fparams=()) -> bool:
        from psycopg.types.json import Jsonb
        j = conn.execute("UPDATE screening_jobs SET status = %s, error = %s, completed_at = now() "
                         f"WHERE tx_id = %s{fence} RETURNING bundle_id",
                         (C.SCREENING_FAILED, Jsonb(error), tx_id, *fparams)).fetchone()
        if j is None:
            return False
        cur = conn.execute("SELECT classification, reasons FROM screening_results WHERE tx_id = %s AND bundle_id = %s "
                           "FOR UPDATE", (tx_id, j["bundle_id"])).fetchone()
        reasons = list(cur["reasons"] or []) if cur else []
        conn.execute(_UPSERT_RESULT, (tx_id, j["bundle_id"], _escalate(cur["classification"] if cur else None),
                                      Jsonb(reasons + [failed_reason(error)]), False))
        return True

    def requeue(self, tx_id, error, *, worker_id=None, attempts=None) -> bool:
        fence, fparams = _fence_sql(worker_id, attempts)
        with self._conn() as conn, conn.transaction():
            return self._requeue(conn, tx_id, error, fence, fparams)

    def give_up(self, tx_id, error, *, worker_id=None, attempts=None) -> bool:
        fence, fparams = _fence_sql(worker_id, attempts)
        with self._conn() as conn, conn.transaction():
            return self._give_up(conn, tx_id, error, fence, fparams)

    def release_stale(self, older_than_seconds, tx_ids=None) -> list[tuple[str, str]]:
        """processing 인 채로 오래된 건(워커가 죽음)을 되돌린다. attempts 가 다 찼으면 give_up.
        반환: [(tx_id, 새 상태)]  새 상태 = queued / failed"""
        where = "status = %s AND started_at < now() - make_interval(secs => %s)"
        params = [C.SCREENING_PROCESSING, float(older_than_seconds)]
        if tx_ids is not None:
            where += " AND tx_id = ANY(%s)"
            params.append(list(tx_ids))
        out = []
        with self._conn() as conn, conn.transaction():
            rows = conn.execute(f"SELECT tx_id, attempts FROM screening_jobs WHERE {where} FOR UPDATE SKIP LOCKED",
                                params).fetchall()
            for r in rows:
                err = _error(r["tx_id"], f"처리 중 워커 응답 없음 ({older_than_seconds:g}초 초과)")
                if r["attempts"] >= C.MAX_ATTEMPTS:
                    self._give_up(conn, r["tx_id"], err)
                    out.append((r["tx_id"], C.SCREENING_FAILED))
                else:
                    self._requeue(conn, r["tx_id"], err)
                    out.append((r["tx_id"], C.SCREENING_QUEUED))
        return out

    def fail(self, tx_id, error) -> None:
        from psycopg.types.json import Jsonb
        with self._conn() as conn:
            conn.execute("UPDATE screening_jobs SET status = %s, error = %s, completed_at = now() WHERE tx_id = %s",
                         (C.SCREENING_FAILED, Jsonb(error), tx_id))

    def review(self, tx_id, decision, reviewer, note, corrected_fields) -> None:
        from psycopg.types.json import Jsonb
        with self._conn() as conn:
            conn.execute(
                "UPDATE screening_results r SET reviewer_decision = %s, reviewer = %s, reviewer_note = %s, "
                "corrected_fields = %s, reviewed_at = now() FROM screening_jobs j "
                "WHERE r.tx_id = j.tx_id AND r.bundle_id = j.bundle_id AND j.tx_id = %s",
                (decision, reviewer, note, Jsonb(corrected_fields) if corrected_fields is not None else None, tx_id))

    def get(self, tx_id) -> dict | None:
        with self._conn() as conn:
            r = conn.execute(
                "SELECT j.tx_id, j.bundle_id, j.status, j.input, j.conflicts, j.error, j.submitted_at, j.started_at, "
                "j.completed_at, j.attempts, j.worker_id, j.shadow_bundle_id, "
                "t.received_at, t.doc_invoice_uri, t.doc_bl_uri, t.extracted_fields, t.extraction_source, "
                "r.classification, r.reasons, r.reviewer_decision, r.reviewer, r.reviewer_note, r.corrected_fields, "
                "r.reviewed_at FROM screening_jobs j JOIN transactions t ON t.tx_id = j.tx_id "
                "LEFT JOIN screening_results r ON r.tx_id = j.tx_id AND r.bundle_id = j.bundle_id WHERE j.tx_id = %s",
                (tx_id,)).fetchone()
            if r is None:
                return None
            shadow = conn.execute(
                "SELECT bundle_id, classification, reasons FROM screening_results WHERE tx_id = %s AND is_shadow "
                "ORDER BY bundle_id", (tx_id,)).fetchall()
        return _record(r.pop("tx_id"), r.pop("bundle_id"), r.pop("status"), r.pop("input"), shadow=shadow, **r)

    def list(self, status=None, classification=None, bundle_id=None, limit=50, offset=0,
             unreviewed_first=False) -> dict:
        classes = _check_filters(status, classification, bundle_id)
        conds, params = [], []
        for col, v in (("j.status", status), ("j.bundle_id", bundle_id)):
            if v is not None:
                conds.append(f"{col} = %s")
                params.append(v)
        if classes is not None:
            conds.append("r.classification = ANY(%s)")
            params.append(classes)
        where = ("WHERE " + " AND ".join(conds)) if conds else ""
        base = ("FROM screening_jobs j JOIN transactions t ON t.tx_id = j.tx_id "
                "LEFT JOIN screening_results r ON r.tx_id = j.tx_id AND r.bundle_id = j.bundle_id AND NOT r.is_shadow "
                f"{where}")
        cols = ", ".join(f"t.{f}" for f in C.SCREENING_FIELDS)
        order = ("(r.reviewer_decision IS NOT NULL), " if unreviewed_first else "") + "j.submitted_at DESC, j.tx_id DESC"
        with self._conn() as conn:
            total = conn.execute(f"SELECT count(*) AS n {base}", params).fetchone()["n"]
            rows = conn.execute(
                f"SELECT j.tx_id, j.status, j.bundle_id, r.classification, j.submitted_at, j.completed_at, "
                f"r.reviewer_decision, {cols} {base} ORDER BY {order} LIMIT %s OFFSET %s",
                (*params, int(limit), int(offset))).fetchall()
        for r in rows:
            r["submitted_at"], r["completed_at"] = _iso(r["submitted_at"]), _iso(r["completed_at"])
        return {"items": rows, "total": total}

    def stats(self) -> dict:
        """심사 서비스가 접수한 건(screening_jobs 에 있는 것)만 센다. 시드·Replay 가 넣은 screening_results 는 제외."""
        from psycopg.types.json import Jsonb
        out = _empty_stats()
        recent = ("SELECT tx_id, bundle_id, started_at, completed_at, row_number() OVER "
                  "(PARTITION BY bundle_id ORDER BY completed_at DESC) AS rn FROM screening_jobs "
                  "WHERE status = %s AND started_at IS NOT NULL AND completed_at IS NOT NULL")
        primary = ("FROM screening_results r JOIN screening_jobs j ON j.tx_id = r.tx_id AND j.bundle_id = r.bundle_id "
                   "WHERE NOT r.is_shadow")
        with self._conn() as conn:
            out["results"] = conn.execute(
                f"SELECT r.bundle_id, r.classification, count(*) AS count {primary} "
                "GROUP BY r.bundle_id, r.classification ORDER BY r.bundle_id, r.classification").fetchall()
            for r in conn.execute("SELECT status, count(*) AS n FROM screening_jobs GROUP BY status"):
                out["jobs"][r["status"]] = r["n"]
            for r in conn.execute(
                    f"SELECT bundle_id, avg(EXTRACT(EPOCH FROM completed_at - started_at)) AS s FROM ({recent}) x "
                    "WHERE rn <= %s GROUP BY bundle_id", (C.SCREENING_COMPLETED, RECENT_N)):
                out["processing_seconds_avg"][r["bundle_id"]] = round(float(r["s"]), 3)
            for r in conn.execute(
                    f"SELECT x.bundle_id, avg((t.extracted_fields -> f ->> 'confidence')::float) AS c FROM ({recent}) x "
                    "JOIN transactions t ON t.tx_id = x.tx_id CROSS JOIN unnest(%s::text[]) AS f "
                    "WHERE x.rn <= %s AND jsonb_typeof(t.extracted_fields -> f -> 'confidence') = 'number' "
                    "GROUP BY x.bundle_id", (C.SCREENING_COMPLETED, list(C.SCREENING_FIELDS), RECENT_N)):
                out["ocr_confidence_avg"][r["bundle_id"]] = round(float(r["c"]), 2)
            for r in conn.execute("SELECT bundle_id, count(*) AS n FROM screening_jobs WHERE status = %s "
                                  "GROUP BY bundle_id", (C.SCREENING_FAILED,)):
                out["failed"][r["bundle_id"]] = r["n"]
            out["shadow_disagreements"] = conn.execute(
                "SELECT j.bundle_id, j.shadow_bundle_id, count(*) AS count FROM screening_jobs j "
                "JOIN screening_results p ON p.tx_id = j.tx_id AND p.bundle_id = j.bundle_id AND NOT p.is_shadow "
                "JOIN screening_results s ON s.tx_id = j.tx_id AND s.bundle_id = j.shadow_bundle_id AND s.is_shadow "
                "WHERE p.classification <> s.classification AND NOT (s.reasons @> %s) "
                "GROUP BY j.bundle_id, j.shadow_bundle_id ORDER BY j.bundle_id, j.shadow_bundle_id",
                (Jsonb(_FAILED_REASON_JSON),)).fetchall()
            for r in conn.execute(f"SELECT r.reviewer_decision AS d, count(*) AS n {primary} "
                                  "AND r.reviewer_decision IS NOT NULL GROUP BY r.reviewer_decision"):
                out["reviews"][r["d"]] = r["n"]
        return out


def make_store(dsn: str | None):
    return PostgresStore(dsn) if dsn else MemoryStore()
