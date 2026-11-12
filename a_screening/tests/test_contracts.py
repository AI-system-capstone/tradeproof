"""A 스키마(common/schema/10_a_screening.sql) 가 common/contracts.py 와 맞는지 (DB 없이 파일만 읽음)."""
from a_screening.db import check_contracts


def test_schema_matches_contracts():
    assert check_contracts.main() == 0
