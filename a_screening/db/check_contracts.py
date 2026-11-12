"""common/schema/10_a_screening.sql 이 common/contracts.py 와 맞는지 확인한다 (DB 없이 파일만 읽음).

  python a_screening/db/check_contracts.py        # 어긋나면 종료 코드 1

확인 항목:
  - transactions 의 추출 필드 컬럼 == contracts.TX_FIELDS (이름·순서)
  - screening_results.classification CHECK 값 == contracts.CLASSIFICATIONS
  - screening_results.reviewer_decision CHECK 값 == contracts.REVIEW_DECISIONS
  - screening_jobs.status CHECK 값 == contracts.SCREENING_STATUSES
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common import contracts as C  # noqa: E402

SCHEMA = Path(__file__).resolve().parents[2] / "common" / "schema" / "10_a_screening.sql"
# transactions 에서 추출 필드가 아닌 컬럼 (메타데이터)
TX_META_COLUMNS = ("tx_id", "received_at", "doc_invoice_uri", "doc_bl_uri",
                   "extracted_fields", "extraction_source", "created_at")


def tables(sql: str) -> dict[str, str]:
    sql = re.sub(r"--[^\n]*", "", sql)
    return {m.group(1): m.group(2) for m in
            re.finditer(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\);", sql, re.S)}


def columns(body: str) -> list[str]:
    cols = []
    for line in body.splitlines():
        m = re.match(r"\s*(\w+)\s+[A-Z]", line)
        if m and m.group(1).upper() not in ("UNIQUE", "PRIMARY", "CHECK", "FOREIGN", "CONSTRAINT"):
            cols.append(m.group(1))
    return cols


def check_values(body: str, col: str) -> tuple[str, ...]:
    m = re.search(rf"\b{col}\b[^,]*?CHECK\s*\(\s*{col}\s+IN\s*\((.*?)\)\s*\)", body, re.S)
    if not m:
        raise SystemExit(f"{col}: CHECK (... IN (...)) 를 스키마에서 찾지 못함")
    return tuple(re.findall(r"'([^']*)'", m.group(1)))


def main() -> int:
    t = tables(SCHEMA.read_text(encoding="utf-8"))
    problems = []

    def same(label, got, want, ordered=True):
        ok = tuple(got) == tuple(want) if ordered else set(got) == set(want)
        print(f"{'OK  ' if ok else 'DIFF'} {label}")
        if not ok:
            problems.append(label)
            print(f"       schema     : {list(got)}")
            print(f"       contracts  : {list(want)}")

    tx_fields = [c for c in columns(t["transactions"]) if c not in TX_META_COLUMNS]
    same("transactions 추출 필드 == TX_FIELDS", tx_fields, C.TX_FIELDS)
    same("screening_results.classification == CLASSIFICATIONS",
         check_values(t["screening_results"], "classification"), C.CLASSIFICATIONS, ordered=False)
    same("screening_results.reviewer_decision == REVIEW_DECISIONS",
         check_values(t["screening_results"], "reviewer_decision"), C.REVIEW_DECISIONS, ordered=False)
    same("screening_jobs.status == SCREENING_STATUSES",
         check_values(t["screening_jobs"], "status"), C.SCREENING_STATUSES, ordered=False)

    print("결과:", "모두 일치" if not problems else f"{len(problems)}건 불일치")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
