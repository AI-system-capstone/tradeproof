"""Postgres 저장소 테스트. compose 의 postgres 가 떠 있을 때만 돈다 (없으면 skip → CI 에서는 skip).

실행 전:  docker compose up -d postgres
기본 주소: postgresql://tradeproof:tradeproof-dev@localhost:5432/tradeproof  (PLATFORM_TEST_DATABASE_URL 로 변경)
개발 DB 의 deployments 표를 그대로 쓰되, 테스트가 만든 줄(DP-test-*)만 지운다.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from d_platform.api import models as M  # noqa: E402
from d_platform.api.store import InProgressExists, PostgresStore, finish  # noqa: E402

DSN = os.environ.get("PLATFORM_TEST_DATABASE_URL",
                     "postgresql://tradeproof:tradeproof-dev@localhost:5432/tradeproof")
SCHEMA = Path(__file__).resolve().parents[2] / "common" / "schema" / "40_d_platform.sql"


def _connect():
    try:
        return psycopg.connect(DSN, connect_timeout=1)
    except psycopg.OperationalError:
        return None


@pytest.fixture
def store():
    conn = _connect()
    if conn is None:
        pytest.skip("Postgres 가 떠 있지 않음")
    with conn:
        conn.execute(SCHEMA.read_text(encoding="utf-8"))   # CREATE ... IF NOT EXISTS 라 여러 번 실행해도 안전
        busy = conn.execute(
            "SELECT 1 FROM deployments WHERE status IN ('pending','progressing') "
            "AND deployment_id NOT LIKE 'DP-test-%'").fetchone()
    conn.close()
    if busy:
        pytest.skip("개발 DB 에 진행 중인 배포가 있어 건너뜀")
    yield PostgresStore(DSN)
    with psycopg.connect(DSN) as c:
        c.execute("DELETE FROM deployments WHERE deployment_id LIKE 'DP-test-%'")


def _dep(bundle="B-2") -> M.Deployment:
    return M.Deployment(deployment_id=f"DP-test-{uuid.uuid4().hex[:8]}", bundle_id=bundle,
                        previous_bundle_id="B-1", status=M.PENDING, requested_by="pytest",
                        steps=[10, 50, 100], analysis={"result": "running", "checks": []})


def test_create_get_save_roundtrip(store):
    dep = store.create(_dep())
    got = store.get(dep.deployment_id)
    assert (got.bundle_id, got.status, got.steps, got.analysis) == ("B-2", "pending", [10, 50, 100],
                                                                    {"result": "running", "checks": []})
    assert got.created_at.utcoffset().total_seconds() == 0 or got.created_at.tzinfo is not None

    got.status, got.weight, got.step_index = M.PROGRESSING, 50, 1
    store.save(got)
    again = store.get(dep.deployment_id)
    assert (again.status, again.weight, again.step_index) == ("progressing", 50, 1)
    assert store.in_progress().deployment_id == dep.deployment_id


def test_only_one_in_progress(store):
    first = store.create(_dep("B-2"))
    with pytest.raises(InProgressExists):
        store.create(_dep("B-3"))
    finish(first, M.ROLLED_BACK)
    first.rollback_reason = M.ROLLBACK_MANUAL
    store.save(first)
    store.create(_dep("B-3"))                              # 끝난 뒤에는 새 배포 가능
    assert store.count_rollbacks().get("manual", 0) >= 1


def test_ping(store):
    store.ping()
