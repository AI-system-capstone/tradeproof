"""`pytest c_replay/tests` 로 실행해도 `import c_replay`, `import common` 이 되게 저장소 루트를 경로에 넣는다.

store 픽스처: 메모리 저장소와 Postgres 저장소로 같은 테스트를 돌린다.
Postgres 는 TEST_DATABASE_URL 이 있을 때만 (없으면 skip). 테스트마다 validations 를 비우므로 **테스트 전용 DB** 를 줄 것.
"""
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from c_replay.store import MemoryStore, PostgresStore  # noqa: E402


@pytest.fixture(params=["memory", "postgres"])
def store(request):
    if request.param == "memory":
        return MemoryStore()
    dsn = os.environ.get("TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("TEST_DATABASE_URL 없음")
    import psycopg
    with psycopg.connect(dsn) as c:
        c.execute((ROOT / "common" / "schema" / "30_c_replay.sql").read_text(encoding="utf-8"))
        c.execute("TRUNCATE validations CASCADE")
    return PostgresStore(dsn)
