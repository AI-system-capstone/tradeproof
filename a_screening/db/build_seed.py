"""과거 거래 DB 초기 데이터 생성.

data/docs/<tx_id>/truth.json 을 읽어 거래 레코드를 만들고,
기준 번들(B-7, list-v14)로 판정한 결과와 정답 라벨까지 seed.sql 로 쓴다.

  python db/build_seed.py                       # 정답 필드 기준 (OCR 없이 빠르게)
  python db/build_seed.py --source ocr          # data/ocr/<tx_id>/extracted.json 기준
  python db/build_seed.py --load --dsn postgresql://tradeproof:tradeproof@localhost:5432/tradeproof
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common import contracts as C  # noqa: E402
from a_screening.db_values import amount_value, date_value  # noqa: E402
from a_screening.screening.match import load_index, screen  # noqa: E402

SCHEMA = Path(__file__).resolve().parents[2] / "common" / "schema" / "10_a_screening.sql"
# golden_labels 는 데이터 담당 소유(15_dataset.sql, 확정 전). 확정될 때까지 A 가 임시 정의로 만든다
GOLDEN_TMP = Path(__file__).resolve().parent / "golden_labels_tmp.sql"
COLUMNS = ("tx_id", "received_at", "doc_invoice_uri", "doc_bl_uri", *C.TX_FIELDS,
           "extracted_fields", "extraction_source")


def lit(v) -> str:
    """SQL 리터럴. None -> NULL, dict/list -> jsonb."""
    if v is None:
        return "NULL"
    if isinstance(v, (dict, list)):
        return "'" + json.dumps(v, ensure_ascii=False).replace("'", "''") + "'::jsonb"
    return "'" + str(v).replace("'", "''") + "'"


def load_fields(tx_dir: Path, truth: dict, source: str, ocr_dir: Path):
    if source == "truth":
        return dict(truth["fields"]), dict(truth["fields"]), "ground_truth"
    ext_path = ocr_dir / truth["tx_id"] / "extracted.json"
    if not ext_path.exists():
        raise FileNotFoundError(f"{ext_path} 없음: 먼저 ocr/extract.py 를 실행하세요")
    ext = json.loads(ext_path.read_text(encoding="utf-8"))
    flat = {k: (v or {}).get("value") for k, v in ext["fields"].items()}
    return flat, ext["fields"], f"ocr:{ext.get('model_version', 'unknown')}"


def build(docs_dir: Path, source: str, ocr_dir: Path, bundle_id: str) -> tuple[str, dict]:
    bundle = C.BUNDLES[bundle_id]
    index = load_index(bundle["list_version"])
    truths = sorted(docs_dir.glob("TX-*/truth.json"))
    if not truths:
        raise SystemExit(f"{docs_dir} 에 truth.json 이 없습니다. 먼저 docs/gen_docs.py 를 실행하세요.")

    now = datetime.now(timezone.utc).isoformat()
    out = ["-- 자동 생성: db/build_seed.py", "BEGIN;",
           "TRUNCATE screening_results, golden_labels, transactions RESTART IDENTITY CASCADE;"]

    stats = {"transactions": 0, "classification": {c: 0 for c in C.CLASSIFICATIONS}, "mismatch_vs_expected": []}
    for tpath in truths:
        truth = json.loads(tpath.read_text(encoding="utf-8"))
        flat, full, src = load_fields(tpath.parent, truth, source, ocr_dir)
        row = {
            "tx_id": truth["tx_id"],
            "received_at": truth["received_at"],
            "doc_invoice_uri": truth["documents"]["invoice"]["pdf"],
            "doc_bl_uri": truth["documents"]["bl"]["pdf"],
            **{f: flat.get(f) for f in C.TX_FIELDS},
            "extracted_fields": full,
            "extraction_source": src,
        }
        row["amount"] = amount_value(row["amount"])
        row["invoice_date"] = date_value(row["invoice_date"])
        out.append(f"INSERT INTO transactions ({', '.join(COLUMNS)}) VALUES "
                   f"({', '.join(lit(row[c]) for c in COLUMNS)});")

        res = screen(flat, index)
        out.append("INSERT INTO screening_results (tx_id, bundle_id, classification, reasons, screened_at) VALUES "
                   f"({lit(truth['tx_id'])}, {lit(bundle_id)}, {lit(res['classification'])}, {lit(res['reasons'])}, {lit(now)});")
        out.append("INSERT INTO golden_labels (tx_id, case_type, matched_entry_id, expected) VALUES "
                   f"({lit(truth['tx_id'])}, {lit(truth['case_type'])}, {lit(truth.get('matched_entry_id'))}, {lit(truth['expected'])});")

        stats["transactions"] += 1
        stats["classification"][res["classification"]] += 1
        exp = truth["expected"].get(bundle["list_version"])
        if exp != res["classification"]:
            stats["mismatch_vs_expected"].append({"tx_id": truth["tx_id"], "expected": exp, "got": res["classification"]})
    out.append("COMMIT;")
    return "\n".join(out) + "\n", stats


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--docs-dir", type=Path, default=C.DOCS_DIR)
    ap.add_argument("--ocr-dir", type=Path, default=C.OCR_DIR)
    ap.add_argument("--source", choices=("truth", "ocr"), default="truth")
    ap.add_argument("--bundle", default=C.BASELINE_BUNDLE, choices=list(C.BUNDLES))
    ap.add_argument("--out", type=Path, default=C.DB_OUT_DIR / "seed.sql")
    ap.add_argument("--load", action="store_true", help="psql 로 A 스키마 + golden_labels 임시 정의 + seed.sql 적재")
    ap.add_argument("--dsn", default="postgresql://tradeproof:tradeproof@localhost:5432/tradeproof")
    a = ap.parse_args()

    sql, stats = build(a.docs_dir, a.source, a.ocr_dir, a.bundle)
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(sql, encoding="utf-8")
    print(f"seed 작성: {a.out}  거래 {stats['transactions']}건  분류 {stats['classification']}")
    if stats["mismatch_vs_expected"]:
        print(f"기대 분류와 다른 거래 {len(stats['mismatch_vs_expected'])}건: {stats['mismatch_vs_expected'][:10]}")

    if a.load:
        env = {**os.environ, "PGOPTIONS": "-c client_min_messages=warning"}
        for f in (SCHEMA, GOLDEN_TMP, a.out):
            subprocess.run(["psql", a.dsn, "-v", "ON_ERROR_STOP=1", "-q", "-f", str(f)], check=True, env=env)
        print("적재 완료")


if __name__ == "__main__":
    main()
