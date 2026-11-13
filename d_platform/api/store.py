"""deployments 저장소. DATABASE_URL 이 있으면 Postgres, 없으면 메모리(테스트·로컬 실행용).

두 구현 모두 "진행 중인 배포는 하나만"을 저장소 수준에서 보장한다 → 동시에 온 요청도 한쪽은 InProgressExists.
"""
from __future__ import annotations

import copy
import json
import threading
from datetime import datetime

from d_platform.api import models as M


class InProgressExists(Exception):
    """이미 진행 중인 배포가 있어서 새 배포를 만들 수 없다."""


class MemoryStore:
    kind = "memory"

    def __init__(self):
        self._rows: dict[str, M.Deployment] = {}
        self._lock = threading.Lock()

    def ping(self) -> None:
        return None

    def create(self, dep: M.Deployment) -> M.Deployment:
        with self._lock:
            if any(d.in_progress for d in self._rows.values()):
                raise InProgressExists()
            self._rows[dep.deployment_id] = copy.deepcopy(dep)
        return dep

    def get(self, deployment_id: str) -> M.Deployment | None:
        with self._lock:
            d = self._rows.get(deployment_id)
            return copy.deepcopy(d) if d else None

    def save(self, dep: M.Deployment) -> None:
        dep.updated_at = M.now_utc()
        with self._lock:
            self._rows[dep.deployment_id] = copy.deepcopy(dep)

    def in_progress(self) -> M.Deployment | None:
        with self._lock:
            for d in self._rows.values():
                if d.in_progress:
                    return copy.deepcopy(d)
        return None

    def latest(self, limit: int = 20) -> list[M.Deployment]:
        with self._lock:
            rows = sorted(self._rows.values(), key=lambda d: d.created_at, reverse=True)
            return [copy.deepcopy(d) for d in rows[:limit]]

    def stable_bundle_id(self) -> str | None:
        with self._lock:
            done = [d for d in self._rows.values() if d.status == M.SUCCEEDED and d.finished_at]
            return max(done, key=lambda d: d.finished_at).bundle_id if done else None

    def count_by_status(self) -> dict[str, int]:
        with self._lock:
            out: dict[str, int] = {}
            for d in self._rows.values():
                out[d.status] = out.get(d.status, 0) + 1
            return out

    def count_rollbacks(self) -> dict[str, int]:
        with self._lock:
            out: dict[str, int] = {}
            for d in self._rows.values():
                if d.status == M.ROLLED_BACK:
                    key = d.rollback_reason or "unknown"
                    out[key] = out.get(key, 0) + 1
            return out


_COLS = ("deployment_id, bundle_id, previous_bundle_id, status, weight, step_index, steps, analysis, "
         "rollback_reason, requested_by, created_at, updated_at, finished_at")


def _row(r) -> M.Deployment:
    return M.Deployment(
        deployment_id=r[0], bundle_id=r[1], previous_bundle_id=r[2], status=r[3], weight=r[4],
        step_index=r[5], steps=list(r[6]), analysis=dict(r[7]), rollback_reason=r[8], requested_by=r[9],
        created_at=r[10], updated_at=r[11], finished_at=r[12],
    )


class PostgresStore:
    """요청마다 짧은 연결을 연다 (배포 API 는 호출이 드물어 풀을 두지 않는다)."""

    kind = "postgres"

    def __init__(self, dsn: str):
        import psycopg  # 메모리 저장소만 쓸 때는 psycopg 없이도 동작
        self._psycopg = psycopg
        self._dsn = dsn

    def _conn(self):
        return self._psycopg.connect(self._dsn, connect_timeout=2)

    def ping(self) -> None:
        with self._conn() as c:
            c.execute("SELECT 1 FROM deployments LIMIT 1")

    def create(self, dep: M.Deployment) -> M.Deployment:
        try:
            with self._conn() as c:
                c.execute(
                    f"INSERT INTO deployments ({_COLS}) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s)",
                    (dep.deployment_id, dep.bundle_id, dep.previous_bundle_id, dep.status, dep.weight,
                     dep.step_index, json.dumps(dep.steps), json.dumps(dep.analysis), dep.rollback_reason,
                     dep.requested_by, dep.created_at, dep.updated_at, dep.finished_at),
                )
        except self._psycopg.errors.UniqueViolation as e:
            # deployments_one_in_progress 부분 유일 인덱스
            raise InProgressExists() from e
        return dep

    def get(self, deployment_id: str) -> M.Deployment | None:
        with self._conn() as c:
            r = c.execute(f"SELECT {_COLS} FROM deployments WHERE deployment_id = %s", (deployment_id,)).fetchone()
        return _row(r) if r else None

    def save(self, dep: M.Deployment) -> None:
        dep.updated_at = M.now_utc()
        with self._conn() as c:
            c.execute(
                "UPDATE deployments SET status=%s, weight=%s, step_index=%s, analysis=%s::jsonb, "
                "rollback_reason=%s, updated_at=%s, finished_at=%s WHERE deployment_id=%s",
                (dep.status, dep.weight, dep.step_index, json.dumps(dep.analysis), dep.rollback_reason,
                 dep.updated_at, dep.finished_at, dep.deployment_id),
            )

    def in_progress(self) -> M.Deployment | None:
        with self._conn() as c:
            r = c.execute(
                f"SELECT {_COLS} FROM deployments WHERE status IN ('pending','progressing') LIMIT 1"
            ).fetchone()
        return _row(r) if r else None

    def latest(self, limit: int = 20) -> list[M.Deployment]:
        with self._conn() as c:
            rows = c.execute(
                f"SELECT {_COLS} FROM deployments ORDER BY created_at DESC LIMIT %s", (limit,)
            ).fetchall()
        return [_row(r) for r in rows]

    def stable_bundle_id(self) -> str | None:
        with self._conn() as c:
            r = c.execute(
                "SELECT bundle_id FROM deployments WHERE status='succeeded' ORDER BY finished_at DESC LIMIT 1"
            ).fetchone()
        return r[0] if r else None

    def count_by_status(self) -> dict[str, int]:
        with self._conn() as c:
            return dict(c.execute("SELECT status, count(*) FROM deployments GROUP BY status").fetchall())

    def count_rollbacks(self) -> dict[str, int]:
        with self._conn() as c:
            return dict(c.execute(
                "SELECT coalesce(rollback_reason,'unknown'), count(*) FROM deployments "
                "WHERE status='rolled_back' GROUP BY 1"
            ).fetchall())


def make_store(dsn: str | None):
    return PostgresStore(dsn) if dsn else MemoryStore()


def finish(dep: M.Deployment, status: str, at: datetime | None = None) -> None:
    dep.status = status
    dep.finished_at = at or M.now_utc()
