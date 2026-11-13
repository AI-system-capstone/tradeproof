"""OCR 추출 결과 평가: truth.json vs extracted.json.

1) 필드 정확도: 가벼운 정규화(대문자, 공백 정리, 끝 문장부호 제거) 후 완전 일치.
2) 스크리닝 일치도: screen(정답 필드) vs screen(추출 필드)의 분류 비교.
   MISS(미탐) = 정답 기준 HIGH_RISK_HOLD/MANUAL_REVIEW인데 추출 기준 PASS_CANDIDATE -> 최우선 지표.
   rules-v3 이면 정답 필드는 ocr 없이(직접 입력과 같음: v3 근거 = v2 근거), 추출 필드는 OCR 신뢰도·불일치와 같이 판정.
3) 규칙별 건수: 추출 값 판정 근거에 각 규칙(contracts.RULES + RULES_V3_EXTRA)이 나온 거래 수와 등급별 근거 수.

사용:
  python ocr/evaluate.py                                   # model-v3, data/ocr/eval_report.json
  python ocr/evaluate.py --model model-v4                  # data/ocr/model-v4/eval_report.json
  python ocr/evaluate.py --rules rules-v3                  # OCR 신뢰도·불일치를 같이 넘김, eval_report.rules-v3.json
  python ocr/evaluate.py --docs-dir /tmp/ocr_sample --ocr-dir /tmp/ocr_out \
      --lists-dir /tmp/ocr_sample_lists --list list-sample
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common import contracts as C  # noqa: E402
from a_screening.screening.match import ListIndex, load_index, screen  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from extract import DEFAULT_MODEL, norm_value, out_dir_for  # noqa: E402

_SEV = {C.PASS: 0, C.REVIEW: 1, C.HOLD: 2}


def _load_index(list_version: str, lists_dir: Path | None) -> ListIndex:
    if lists_dir is None:
        return load_index(list_version)
    with open(Path(lists_dir) / f"{list_version}.json", encoding="utf-8") as fh:
        return ListIndex(json.load(fh))


def ocr_info(ext: dict) -> dict:
    """extracted.json -> screen() 의 ocr 인자 (rules-v3 가 신뢰도·Invoice/B-L 불일치를 본다)."""
    return {"confidence": {f: (v or {}).get("confidence") for f, v in (ext.get("fields") or {}).items()},
            "conflicts": ext.get("conflicts") or []}


def evaluate(docs_dir: Path, ocr_dir: Path, index: ListIndex, tx_ids: list[str] | None = None,
             rules_version: str = C.RULES_BASE) -> dict:
    per_field = {f: {"correct": 0, "total": 0} for f in C.TX_FIELDS}
    mismatches, screening_rows, skipped = [], [], []
    confusion: Counter = Counter()
    rule_tx: Counter = Counter()        # 규칙 -> 그 규칙 근거가 있는 거래 수 (추출 값 기준)
    rule_cls: Counter = Counter()       # (규칙, 근거 등급) -> 근거 수
    truth_vs_expected = {"checked": 0, "agree": 0, "disagree": []}
    conf_ok, conf_bad = [], []   # 맞은/틀린 필드의 OCR 신뢰도 (임계값 정할 때 참고)

    tx_dirs = [docs_dir / t for t in tx_ids] if tx_ids else sorted(p for p in docs_dir.iterdir() if p.is_dir())
    for tdir in tx_dirs:
        tp, ep = tdir / "truth.json", ocr_dir / tdir.name / "extracted.json"
        if not tp.exists() or not ep.exists():
            skipped.append({"tx_id": tdir.name, "truth": tp.exists(), "extracted": ep.exists()})
            continue
        truth = json.loads(tp.read_text(encoding="utf-8"))
        ext = json.loads(ep.read_text(encoding="utf-8"))
        tf = truth["fields"]
        ev = {f: (ext["fields"].get(f) or {}).get("value") for f in C.TX_FIELDS}

        for f in C.TX_FIELDS:
            per_field[f]["total"] += 1
            ok = norm_value(tf.get(f)) == norm_value(ev[f])
            conf = (ext["fields"].get(f) or {}).get("confidence")
            if ok:
                per_field[f]["correct"] += 1
                if conf is not None:
                    conf_ok.append(conf)
            else:
                mismatches.append({"tx_id": truth["tx_id"], "field": f, "truth": tf.get(f), "extracted": ev[f],
                                   "source_doc": (ext["fields"].get(f) or {}).get("source_doc"),
                                   "confidence": conf})
                if conf is not None:
                    conf_bad.append(conf)

        if rules_version == C.RULES_BASE:
            st, se = screen(tf, index), screen(ev, index)
        else:  # 정답 필드는 OCR 부가 정보 없이, 추출 필드는 신뢰도·불일치와 같이
            st, se = screen(tf, index, rules_version), screen(ev, index, rules_version, ocr=ocr_info(ext))
        ct, ce = st["classification"], se["classification"]
        confusion[(ct, ce)] += 1
        for rule in {r["rule"] for r in se["reasons"]}:
            rule_tx[rule] += 1
        for r in se["reasons"]:
            rule_cls[(r["rule"], r["classification"])] += 1
        row = {"tx_id": truth["tx_id"], "case_type": truth.get("case_type"), "truth_cls": ct, "extracted_cls": ce,
               "agree": ct == ce, "miss": ct in (C.HOLD, C.REVIEW) and ce == C.PASS,
               "under": _SEV[ce] < _SEV[ct], "over": _SEV[ce] > _SEV[ct]}
        if not row["agree"]:
            row["truth_reasons"] = st["reasons"]
            row["extracted_reasons"] = se["reasons"]
        screening_rows.append(row)

        exp = (truth.get("expected") or {}).get(index.list_version)
        if exp:
            truth_vs_expected["checked"] += 1
            if exp == ct:
                truth_vs_expected["agree"] += 1
            else:
                truth_vs_expected["disagree"].append({"tx_id": truth["tx_id"], "expected": exp, "screened": ct})

    n_tx = len(screening_rows)
    tot = sum(v["total"] for v in per_field.values())
    cor = sum(v["correct"] for v in per_field.values())
    for v in per_field.values():
        v["accuracy"] = round(v["correct"] / v["total"], 4) if v["total"] else None
    agree = sum(r["agree"] for r in screening_rows)
    misses = [r for r in screening_rows if r["miss"]]

    def _avg(xs):
        return round(sum(xs) / len(xs), 2) if xs else None

    return {
        "n_transactions": n_tx,
        "field_accuracy": round(cor / tot, 4) if tot else None,
        "fields_correct": cor, "fields_total": tot,
        "tx_all_fields_correct": n_tx - len({m["tx_id"] for m in mismatches}),
        "per_field": per_field,
        "screening": {
            "list_version": index.list_version,
            "rules_version": rules_version,
            "agreement": round(agree / n_tx, 4) if n_tx else None,
            "agree": agree, "total": n_tx,
            "misses": len(misses),                       # 미탐: 정답 HOLD/REVIEW -> 추출 PASS
            "miss_tx": [r["tx_id"] for r in misses],
            "under_classified": sum(r["under"] for r in screening_rows),   # 미탐 + HOLD->REVIEW 강등
            "over_classified": sum(r["over"] for r in screening_rows),     # 오탐 방향
            "over_clean": sum(r["over"] and r["case_type"] == C.CASE_CLEAN for r in screening_rows),  # 정상 거래가 올라감
            "confusion": [{"truth": t, "extracted": e, "count": c} for (t, e), c in sorted(confusion.items())],
            "rule_counts": _rule_counts(rule_tx, rule_cls),
            "disagreements": [r for r in screening_rows if not r["agree"]],
            "truth_vs_expected": truth_vs_expected,
        },
        "confidence": {"mean_correct": _avg(conf_ok), "mean_wrong": _avg(conf_bad)},
        "mismatches": mismatches,
        "skipped": skipped,
    }


_KNOWN_RULES = C.RULES + C.RULES_V3_EXTRA


def _rule_counts(rule_tx: Counter, rule_cls: Counter) -> list[dict]:
    """규칙별 건수. 순서: contracts 의 규칙 순서, 그 밖의 규칙은 뒤에 이름순."""
    order = [r for r in _KNOWN_RULES if r in rule_tx] + sorted(r for r in rule_tx if r not in _KNOWN_RULES)
    return [{"rule": r, "tx": rule_tx[r], "known": r in _KNOWN_RULES,
             "by_classification": {c: rule_cls[(r, c)] for c in C.CLASSIFICATIONS if rule_cls[(r, c)]}}
            for r in order]


def print_table(rep: dict, model: str) -> None:
    print(f"\n== OCR eval [{model}]  tx={rep['n_transactions']}  "
          f"field accuracy={_pct(rep['field_accuracy'])} ({rep['fields_correct']}/{rep['fields_total']})  "
          f"tx all-correct={rep['tx_all_fields_correct']}/{rep['n_transactions']}")
    print(f"{'field':<20}{'acc':>8}{'ok/n':>10}")
    for f, v in rep["per_field"].items():
        print(f"{f:<20}{_pct(v['accuracy']):>8}{v['correct']:>5}/{v['total']:<4}")
    s = rep["screening"]
    rv = "" if s.get("rules_version", C.RULES_BASE) == C.RULES_BASE else f" {s['rules_version']}"
    print(f"screening [{s['list_version']}{rv}]: agreement={_pct(s['agreement'])} ({s['agree']}/{s['total']})  "
          f"MISSES(미탐)={s['misses']}  under={s['under_classified']}  over={s['over_classified']}")
    for c in s["confusion"]:
        print(f"   truth {c['truth']:<15} -> extracted {c['extracted']:<15} x{c['count']}")
    if s.get("rule_counts"):
        print("   extracted reasons by rule (tx / reasons by classification):")
        for rc in s["rule_counts"]:
            by = ", ".join(f"{k}={v}" for k, v in rc["by_classification"].items())
            flag = "" if rc["known"] else "  (contracts 에 없는 규칙)"
            print(f"     {rc['rule']:<24} tx={rc['tx']:<4} {by}{flag}")
    if s["truth_vs_expected"]["checked"]:
        tv = s["truth_vs_expected"]
        print(f"   (sanity) screen(truth) vs truth.expected: {tv['agree']}/{tv['checked']}")
    c = rep["confidence"]
    print(f"mean OCR confidence: correct fields={c['mean_correct']}  wrong fields={c['mean_wrong']}")
    if rep["mismatches"]:
        print(f"mismatches ({len(rep['mismatches'])}, first 15):")
        for m in rep["mismatches"][:15]:
            print(f"   {m['tx_id']} {m['field']:<18} truth={m['truth']!r}  got={m['extracted']!r}")
    if rep["skipped"]:
        print(f"skipped {len(rep['skipped'])} tx (missing truth/extracted)")


def _pct(x) -> str:
    return "-" if x is None else f"{x * 100:.1f}%"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Evaluate OCR extraction against truth.json")
    ap.add_argument("--docs-dir", type=Path, default=C.DOCS_DIR)
    ap.add_argument("--ocr-dir", type=Path, default=None,
                    help=f"base OCR dir (default {C.OCR_DIR}); non-default models read <ocr-dir>/<model>/")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--list", default=C.LIST_BASE, help="sanctions list version")
    ap.add_argument("--lists-dir", type=Path, default=None, help=f"override dir of <list>.json (default {C.SANCTIONS_DIR})")
    ap.add_argument("--rules", default=C.RULES_BASE, choices=C.RULES_VERSIONS,
                    help="screening rules version (rules-v3 passes OCR confidence/conflicts)")
    ap.add_argument("--tx", nargs="*")
    ap.add_argument("--out", type=Path, default=None, help="report path (default <model ocr dir>/eval_report.json)")
    a = ap.parse_args(argv)

    ocr_dir = out_dir_for(a.ocr_dir, a.model)
    index = _load_index(a.list, a.lists_dir)
    rep = evaluate(a.docs_dir, ocr_dir, index, a.tx, a.rules)
    rep = {"model_version": a.model, "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "docs_dir": str(a.docs_dir), "ocr_dir": str(ocr_dir), **rep}
    name = "eval_report.json" if a.rules == C.RULES_BASE else f"eval_report.{a.rules}.json"
    out = a.out or ocr_dir / name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print_table(rep, a.model)
    print(f"report -> {out}")
    return 0 if rep["n_transactions"] else 1


if __name__ == "__main__":
    sys.exit(main())
