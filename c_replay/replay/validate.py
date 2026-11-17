"""M3 검증: 골든셋 검증(VAL-01) + Replay(VAL-02·03). 명세: docs/openapi/c_replay.json 의 GoldenResult·ReplayResult·Diff

계산(golden_result, replay_result)은 "거래 하나 + 번들 -> 판정"을 하는 process(tx, bundle_id) 를 주입받는 순수 함수다.
  process 반환: {"classification", "reasons", "fields": {TX_FIELDS: str | None}}
실제 처리(ocr_process)는 A 의 공개 처리 경로(a_screening/pipeline: OCR -> 판정)를 그대로 부른다 (SVC-04).
A 의 평시 심사(ocr-worker)도 같은 함수를 쓴다.

데이터 (A·데이터 담당 테이블, 읽기 전용 트랜잭션으로만 읽는다):
  골든셋   golden_labels(tx_id, expected)          expected = {list_id: 분류} (a_screening/db/golden_labels_tmp.sql)
                                                  또는 {list_id: {classification, ...}} (dataset/spec/label.schema.json)
  번들     B 의 GET /bundles/{id} -> {model_id, list_id} (fetch_bundles). 규칙은 번들에 없고 A 가 rules-v3 로 고정한다
           서류는 data/docs/<tx_id>/ (A 의 sample 입력)
  과거 거래 transactions + screening_results(is_shadow=false) + screening_jobs(있으면 input·원문 위치)
           before = A 가 기록한 판정. 거래마다 한 줄: Job 이 있으면 그 Job 의 번들로 처리한 기록,
           없으면 가장 최근(screened_at, result_id) 기록. 처리 중(queued·processing)인 건은 뺀다
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request

from common import contracts as C

_SEVERITY = {C.PASS: 0, C.REVIEW: 1, C.HOLD: 2}


class EmptyGolden(LookupError):
    """골든셋이 비어 있다 (검증할 수 없음)."""


class GoldenFailed(RuntimeError):
    """골든셋 거래 한 건을 처리하지 못했다 (어느 거래인지 메시지에 들어 있다)."""


_FROM_B: dict[str, dict] = {}   # fetch_bundles 가 B 에서 읽어 온 번들 구성


def fetch_bundles(b_url: str, bundle_ids, timeout: float = 5) -> None:
    """B 의 GET /bundles/{id} 로 번들 구성(model_id, list_id)을 읽어 둔다. 없는 번들은 urllib.error.HTTPError(404)."""
    for bid in bundle_ids:
        url = f"{b_url.rstrip('/')}/bundles/{urllib.parse.quote(bid, safe='')}"
        with urllib.request.urlopen(url, timeout=timeout) as r:
            b = json.loads(r.read())
        _FROM_B[bid] = {"model_id": b["model_id"], "list_id": b["list_id"]}


def bundle(bundle_id: str) -> dict:
    """번들 -> {model_id, list_id}. B 에서 읽어 둔 것을 먼저 쓰고, 없으면 contracts.BUNDLES (B 없이 손으로 돌릴 때).
    A 의 pipeline 도 같은 키를 받는다. 규칙 버전은 번들에 없다 (A 가 contracts.RULES_FIXED 로 고정)."""
    if bundle_id in _FROM_B:
        return _FROM_B[bundle_id]
    if bundle_id not in C.BUNDLES:
        raise LookupError(bundle_id)
    return {k: C.BUNDLES[bundle_id][k] for k in ("model_id", "list_id")}


def _expected(item: dict, list_id: str) -> str:
    e = (item.get("expected") or {}).get(list_id)
    e = e.get("classification") if isinstance(e, dict) else e
    if e not in C.CLASSIFICATIONS:
        raise ValueError(f"골든셋 {item['tx_id']} 에 {list_id} 기대 분류가 없음")
    return e


def _metric(baseline, candidate, ndigits=None) -> dict:
    d = candidate - baseline
    return {"baseline": baseline, "candidate": candidate, "diff": round(d, ndigits) if ndigits else d}


def golden_result(items: list[dict], process, baseline_id: str, candidate_id: str) -> dict:
    """GoldenResult. 기대 분류는 정답 라벨에서, 각 번들의 list_id 기준으로 고른다.
    미탐 = 기대보다 낮은 위험도로 분류 (보류->검토, 보류->통과, 검토->통과),
    오탐 = 기대보다 높은 위험도로 분류 (통과->검토, 통과->보류, 검토->보류). 위험도 순서는 _SEVERITY.
    보류->검토도 미탐으로 센다: rules-v3 는 OCR 이 불확실하면 통과가 아니라 검토로 보내므로,
    저하 모델은 '보류->통과'가 아니라 '보류->검토'로 나타난다 (S5 에서 게이트가 막아야 하는 경우).
    한 건이라도 처리에 실패하면 GoldenFailed 로 전체가 실패한다. 일부로 센 정확도는 전체 정확도로 오해되기 때문이다.
    (replay_result 는 건수 일치 여부(count_match)를 같이 보여 주므로 실패한 거래를 건너뛴다.)"""
    if not items:
        raise EmptyGolden()
    items = sorted(items, key=lambda i: i["tx_id"])
    stats = {}
    for bid in (baseline_id, candidate_id):
        lv = bundle(bid)["list_id"]
        ok = fn = fp = 0
        for item in items:
            exp = _expected(item, lv)
            try:
                got = process(item, bid)["classification"]
            except Exception as e:  # noqa: BLE001
                raise GoldenFailed(f"골든셋 {item['tx_id']} 처리 실패 ({bid}): {type(e).__name__}: {e}") from e
            ok += got == exp
            fn += _SEVERITY[got] < _SEVERITY[exp]
            fp += _SEVERITY[got] > _SEVERITY[exp]
        stats[bid] = (round(ok / len(items), 4), fn, fp)
    (ab, nb, pb), (ac, nc, pc) = stats[baseline_id], stats[candidate_id]
    lists = sorted({bundle(b)["list_id"] for b in (baseline_id, candidate_id)})
    return {"golden_version": ",".join(f"golden-{lv}" for lv in lists), "total": len(items),
            "accuracy": _metric(ab, ac, 4), "false_negative": _metric(nb, nc), "false_positive": _metric(pb, pc)}


def _str(v):
    return None if v is None else str(v)


def replay_result(past: list[dict], process, candidate_id: str, log=lambda **kw: None) -> tuple[dict, list[dict]]:
    """(ReplayResult, Diff 목록). past 한 건 = {tx_id, bundle_id, classification, reasons, fields} (A 의 기록).
    처리 중 예외가 난 거래는 processed 에서 빠진다 (count_match=false). 분류가 같으면 필드가 달라도 diff 가 아니다."""
    ids = [t["tx_id"] for t in past]
    if len(set(ids)) != len(ids):
        dup = sorted({i for i in ids if ids.count(i) > 1})
        raise ValueError(f"과거 기록에 같은 tx_id 가 두 번 이상 있음: {dup[:5]}")   # 결과(tx_id 기준)가 섞인다
    processed, diffs = 0, []
    for tx in sorted(past, key=lambda t: t["tx_id"]):
        try:
            after = process(tx, candidate_id)
        except Exception as e:  # noqa: BLE001  한 건 실패가 전체를 멈추지 않게 (건수 불일치로 드러난다)
            log(event="replay_tx_failed", tx_id=tx["tx_id"], error=f"{type(e).__name__}: {e}")
            continue
        processed += 1
        b, a = tx["classification"], after["classification"]
        if b == a:
            continue
        before_f, after_f = tx.get("fields") or {}, after.get("fields") or {}
        diffs.append({
            "tx_id": tx["tx_id"],
            "direction": "up" if _SEVERITY[a] > _SEVERITY[b] else "down",
            "before": {"bundle_id": tx["bundle_id"], "classification": b, "reasons": tx.get("reasons") or []},
            "after": {"bundle_id": candidate_id, "classification": a, "reasons": after.get("reasons") or []},
            "fields_changed": [{"field": f, "before": _str(before_f.get(f)), "after": _str(after_f.get(f))}
                               for f in C.TX_FIELDS if _str(before_f.get(f)) != _str(after_f.get(f))],
        })
    diffs.sort(key=lambda d: (d["direction"] != "down", d["tx_id"]))
    return ({"total": len(past), "processed": processed, "count_match": processed == len(past),
             "changed": len(diffs), "changed_down": sum(d["direction"] == "down" for d in diffs)}, diffs)


# ---------------------------------------------------------------- 실제 데이터·처리 (A 의 테이블과 코드)
def _flat(extracted) -> dict:
    """extracted_fields -> {field: value}. OCR 이면 {field: {value, ...}}, 정답 시드면 {field: value}."""
    return {f: (v.get("value") if isinstance(v, dict) else v) for f, v in (extracted or {}).items()}


def read_golden(dsn: str) -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=3) as c:
        c.read_only = True
        return c.execute("SELECT tx_id, expected FROM golden_labels ORDER BY tx_id").fetchall()


def read_past(dsn: str) -> list[dict]:
    import psycopg
    from psycopg.rows import dict_row
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=3) as c:
        c.read_only = True
        rows = c.execute(
            "SELECT DISTINCT ON (t.tx_id) t.tx_id, t.extracted_fields, t.doc_invoice_uri, t.doc_bl_uri, j.input, "
            "r.bundle_id, r.classification, r.reasons "
            "FROM transactions t JOIN screening_results r ON r.tx_id = t.tx_id AND NOT r.is_shadow "
            "LEFT JOIN screening_jobs j ON j.tx_id = t.tx_id "
            "WHERE j.tx_id IS NULL OR (j.status IN ('completed', 'failed') AND r.bundle_id = j.bundle_id) "
            "ORDER BY t.tx_id, r.screened_at DESC, r.result_id DESC").fetchall()
    return [{**r, "fields": _flat(r.pop("extracted_fields"))} for r in rows]


class _ReadOnlyBlob:
    """A 의 원문 저장소를 읽기만 한다. A 의 prepare_upload 가 PDF 렌더링 PNG 를 put 하지만 A 가 처음 처리할 때
    이미 저장했으므로 쓰지 않고 키만 돌려준다."""

    def __init__(self, blob):
        self._blob = blob

    def get(self, uri: str) -> bytes:
        return self._blob.get(uri)

    def put(self, key: str, data: bytes, content_type: str = "") -> str:
        return key


def ocr_process(blob, workdir):
    """process(tx, bundle_id): A 의 pipeline 으로 OCR -> 판정 (extract_documents + screen_extracted = screen_documents).
    같은 거래·같은 모델의 OCR 은 한 번만 한다 (골든셋·Replay·두 번들 공유).
    서류 폴더: 입력이 없는 거래(시드)와 sample 입력은 data/docs/<tx_id>, upload 입력은 원문 저장소에서 workdir 로 받는다
    (PDF 렌더링이 필요해서 이 준비만 A 의 worker.prepare_upload 를 쓴다). fields 입력은 OCR 없이 판정만 한다."""
    from a_screening import pipeline as P   # tesseract·numpy 는 실제 처리할 때만 필요
    blob, dirs, ocr = _ReadOnlyBlob(blob), {}, {}

    def tx_dir(tx: dict, inp: dict, source: str):
        if source == C.INPUT_UPLOAD:
            from a_screening.ocr.worker import prepare_upload
            return prepare_upload(tx, blob, workdir)
        d = C.sample_dir(inp.get("sample_tx_id"))
        if d is None:
            raise LookupError(f"샘플 서류 없음: {inp.get('sample_tx_id')!r} ({C.DOCS_DIR})")
        return d

    def process(tx: dict, bundle_id: str) -> dict:
        tx_id, spec = tx["tx_id"], {"bundle_id": bundle_id, **bundle(bundle_id)}
        inp = tx.get("input") or {"source": C.INPUT_SAMPLE, "sample_tx_id": tx_id}
        source = inp.get("source") or (C.INPUT_SAMPLE if inp.get("sample_tx_id") else C.INPUT_FIELDS)
        if source == C.INPUT_FIELDS:
            extracted, conflicts = {f: {"value": (inp.get("fields") or {}).get(f)} for f in C.TX_FIELDS}, []
        else:
            key = (tx_id, spec["model_id"])
            if key not in ocr:
                if tx_id not in dirs:
                    dirs[tx_id] = tx_dir(tx, inp, source)
                ocr[key] = P.extract_documents(dirs[tx_id], spec["model_id"])
            extracted, conflicts = ocr[key]
        res = P.screen_extracted(extracted, conflicts, spec, from_ocr=source != C.INPUT_FIELDS)
        return {"classification": res["classification"], "reasons": res["reasons"], "fields": _flat(extracted)}

    return process


def log(**kw) -> None:
    print(json.dumps(kw, ensure_ascii=False, default=str), flush=True)
