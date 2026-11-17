"""검증(validation) 저장소. DATABASE_URL 이 있으면 Postgres, 없으면 메모리(로컬 테스트용, 프로세스 하나만).

Postgres 에 쓰는 곳 (common/schema/30_c_replay.sql): validations, validation_diffs

  API       get() / list() / diffs() / stats() / ping()          (조회만)
  검증 Job   create() -> start() -> complete() 또는 fail()     (조건이 안 맞으면 아무것도 쓰지 않고 False)
            create(validation_id=...) 는 B 가 만든 검증 ID 를 그대로 쓴다 (없으면 직접 만든다)
            fail_stale()  Job 이 죽어서 진행 중으로 남은 오래된 검증을 failed 로 (create 전에 부른다)

두 저장소는 같은 모양의 dict 를 돌려준다 (API 응답 그대로, docs/openapi/c_replay.json 의 Validation·Diff).
"같은 번들에 진행 중(queued·running) 검증은 하나"는 저장소가 막는다 → InProgressExists.
"""
from __future__ import annotations

import copy
import threading
import uuid
from datetime import datetime, timedelta, timezone

from common import contracts as C

QUEUED, RUNNING, COMPLETED, FAILED = "queued", "running", "completed", "failed"
STATUSES = (QUEUED, RUNNING, COMPLETED, FAILED)
IN_PROGRESS = (QUEUED, RUNNING)
UP, DOWN = "up", "down"
DIRECTIONS = (UP, DOWN)

_TIMES = ("created_at", "started_at", "finished_at")


class InProgressExists(Exception):
    """같은 번들에 이미 진행 중인 검증이 있다."""


class StoreUnavailable(Exception):
    """저장소(DB)에 연결할 수 없다."""


def new_validation_id() -> str:
    return f"RP-{uuid.uuid4().hex[:12]}"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _out(row: dict) -> dict:
    """시각을 ISO 8601 UTC 문자열로 바꾼 사본."""
    d = copy.deepcopy(row)
    for k in _TIMES:
        if d[k] is not None:
            d[k] = d[k].astimezone(timezone.utc).isoformat(timespec="milliseconds")
    return d


def _check_diffs(diffs: list[dict]) -> None:
    for d in diffs:
        if d["direction"] not in DIRECTIONS:
            raise ValueError(f"direction 은 {DIRECTIONS} 중에서 ({d['direction']!r})")
        for side in ("before", "after"):
            if d[side]["classification"] not in C.CLASSIFICATIONS:
                raise ValueError(f"classification 은 {C.CLASSIFICATIONS} 중에서 ({d[side]['classification']!r})")


def _diff_key(d: dict):
    return (d["direction"] != DOWN, d["tx_id"])   # 위험도가 낮아진 거래 먼저, 그다음 거래 ID 순


class MemoryStore:
    kind = "memory"

    def __init__(self):
        self._rows: dict[str, dict] = {}          # 등록 순서 유지
        self._diffs: dict[str, list[dict]] = {}
        self._lock = threading.Lock()

    def ping(self) -> None:
        return None

    def create(self, bundle_id: str, baseline_bundle_id: str, requested_by: str,
               validation_id: str | None = None) -> dict:
        with self._lock:
            if any(r["bundle_id"] == bundle_id and r["status"] in IN_PROGRESS for r in self._rows.values()):
                raise InProgressExists()
            if validation_id in self._rows:
                raise ValueError(f"이미 있는 검증 ID: {validation_id}")
            row = {"validation_id": validation_id or new_validation_id(), "bundle_id": bundle_id,
                   "baseline_bundle_id": baseline_bundle_id, "status": QUEUED,
                   "golden": None, "replay": None, "error": None, "requested_by": requested_by,
                   "created_at": _now(), "started_at": None, "finished_at": None}
            self._rows[row["validation_id"]] = row
            return _out(row)

    def get(self, validation_id: str) -> dict | None:
        with self._lock:
            row = self._rows.get(validation_id)
            return _out(row) if row else None

    def list(self, bundle_id: str | None = None, status: str | None = None,
             limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
        with self._lock:
            rows = [r for r in reversed(self._rows.values())
                    if (bundle_id is None or r["bundle_id"] == bundle_id)
                    and (status is None or r["status"] == status)]
            return [_out(r) for r in rows[offset:offset + limit]], len(rows)

    def start(self, validation_id: str) -> bool:
        with self._lock:
            row = self._rows.get(validation_id)
            if row is None or row["status"] != QUEUED:
                return False
            row.update(status=RUNNING, started_at=_now())
            return True

    def complete(self, validation_id: str, golden: dict, replay: dict, diffs: list[dict]) -> bool:
        _check_diffs(diffs)
        with self._lock:
            row = self._rows.get(validation_id)
            if row is None or row["status"] != RUNNING:
                return False
            row.update(status=COMPLETED, golden=copy.deepcopy(golden), replay=copy.deepcopy(replay),
                       finished_at=_now())
            self._diffs[validation_id] = sorted(copy.deepcopy(diffs), key=_diff_key)
            return True

    def fail(self, validation_id: str, error: dict) -> bool:
        with self._lock:
            row = self._rows.get(validation_id)
            if row is None or row["status"] not in IN_PROGRESS:
                return False
            row.update(status=FAILED, error=copy.deepcopy(error), finished_at=_now())
            return True

    def fail_stale(self, bundle_id: str, older_than: timedelta, error: dict) -> int:
        """같은 번들의 진행 중 검증 중 만든 지 older_than 이 지난 것을 failed 로. 바꾼 건수."""
        limit, n = _now() - older_than, 0
        with self._lock:
            for row in self._rows.values():
                if row["bundle_id"] == bundle_id and row["status"] in IN_PROGRESS and row["created_at"] < limit:
                    row.update(status=FAILED, error=copy.deepcopy(error), finished_at=_now())
                    n += 1
        return n

    def diffs(self, validation_id: str, direction: str | None = None,
              limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
        with self._lock:
            rows = [d for d in self._diffs.get(validation_id, [])
                    if direction is None or d["direction"] == direction]
            return copy.deepcopy(rows[offset:offset + limit]), len(rows)

    def stats(self) -> dict:
        """지표용: 상태별 건수, 번들별 최근 완료 건의 소요 시간(초)."""
        with self._lock:
            by_status: dict[str, int] = {}
            last: dict[str, dict] = {}
            for r in self._rows.values():
                by_status[r["status"]] = by_status.get(r["status"], 0) + 1
                if r["status"] == COMPLETED and (r["bundle_id"] not in last
                                                 or r["finished_at"] >= last[r["bundle_id"]]["finished_at"]):
                    last[r["bundle_id"]] = r
            return {"by_status": by_status,
                    "duration_seconds": {b: (r["finished_at"] - r["started_at"]).total_seconds()
                                         for b, r in last.items()}}


_COLS = ("validation_id", "bundle_id", "baseline_bundle_id", "status", "golden", "replay", "error",
         "requested_by", "created_at", "started_at", "finished_at")
_SELECT = f"SELECT {', '.join(_COLS)} FROM validations"
_DIFF_COLS = ("tx_id", "direction", "before", "after", "fields_changed")


class PostgresStore:
    """호출마다 짧은 연결을 연다 (검증 API 는 호출이 드물어 풀을 두지 않는다)."""

    kind = "postgres"

    def __init__(self, dsn: str):
        import psycopg  # 메모리 저장소만 쓸 때는 psycopg 없이도 동작
        from psycopg.types.json import Jsonb
        self._psycopg, self._jsonb, self._dsn = psycopg, Jsonb, dsn

    def _conn(self):
        try:
            return self._psycopg.connect(self._dsn, connect_timeout=2)
        except self._psycopg.OperationalError as e:
            raise StoreUnavailable(type(e).__name__) from e

    def ping(self) -> None:
        with self._conn() as c:
            c.execute("SELECT 1 FROM validations LIMIT 1")

    def create(self, bundle_id: str, baseline_bundle_id: str, requested_by: str,
               validation_id: str | None = None) -> dict:
        row = (validation_id or new_validation_id(), bundle_id, baseline_bundle_id, QUEUED, None, None, None,
               requested_by, _now(), None, None)
        try:
            with self._conn() as c:
                c.execute(f"INSERT INTO validations ({', '.join(_COLS)}) VALUES ({', '.join(['%s'] * len(_COLS))})", row)
        except self._psycopg.errors.UniqueViolation as e:   # validations_one_in_progress 부분 유일 인덱스
            raise InProgressExists() from e
        return _out(dict(zip(_COLS, row)))

    def get(self, validation_id: str) -> dict | None:
        with self._conn() as c:
            r = c.execute(f"{_SELECT} WHERE validation_id = %s", (validation_id,)).fetchone()
        return _out(dict(zip(_COLS, r))) if r else None

    def list(self, bundle_id: str | None = None, status: str | None = None,
             limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
        where = "WHERE (%s::text IS NULL OR bundle_id = %s) AND (%s::text IS NULL OR status = %s)"
        args = (bundle_id, bundle_id, status, status)
        with self._conn() as c:
            total = c.execute(f"SELECT count(*) FROM validations {where}", args).fetchone()[0]
            rows = c.execute(f"{_SELECT} {where} ORDER BY created_at DESC, validation_id LIMIT %s OFFSET %s",
                             args + (limit, offset)).fetchall()
        return [_out(dict(zip(_COLS, r))) for r in rows], total

    def start(self, validation_id: str) -> bool:
        with self._conn() as c:
            return c.execute("UPDATE validations SET status = 'running', started_at = %s "
                             "WHERE validation_id = %s AND status = 'queued'",
                             (_now(), validation_id)).rowcount == 1

    def complete(self, validation_id: str, golden: dict, replay: dict, diffs: list[dict]) -> bool:
        _check_diffs(diffs)
        J = self._jsonb
        with self._conn() as c:   # 한 트랜잭션: 결과와 달라진 거래가 함께 들어간다
            done = c.execute("UPDATE validations SET status = 'completed', golden = %s, replay = %s, finished_at = %s "
                             "WHERE validation_id = %s AND status = 'running'",
                             (J(golden), J(replay), _now(), validation_id)).rowcount == 1
            if done and diffs:
                c.cursor().executemany(
                    "INSERT INTO validation_diffs (validation_id, tx_id, direction, before, after, fields_changed) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    [(validation_id, d["tx_id"], d["direction"], J(d["before"]), J(d["after"]),
                      J(d["fields_changed"])) for d in diffs])
        return done

    def fail(self, validation_id: str, error: dict) -> bool:
        with self._conn() as c:
            return c.execute("UPDATE validations SET status = 'failed', error = %s, finished_at = %s "
                             "WHERE validation_id = %s AND status IN ('queued', 'running')",
                             (self._jsonb(error), _now(), validation_id)).rowcount == 1

    def fail_stale(self, bundle_id: str, older_than: timedelta, error: dict) -> int:
        with self._conn() as c:
            return c.execute("UPDATE validations SET status = 'failed', error = %s, finished_at = %s "
                             "WHERE bundle_id = %s AND status IN ('queued', 'running') AND created_at < %s",
                             (self._jsonb(error), _now(), bundle_id, _now() - older_than)).rowcount

    def diffs(self, validation_id: str, direction: str | None = None,
              limit: int = 50, offset: int = 0) -> tuple[list[dict], int]:
        where = "WHERE validation_id = %s AND (%s::text IS NULL OR direction = %s)"
        args = (validation_id, direction, direction)
        with self._conn() as c:
            total = c.execute(f"SELECT count(*) FROM validation_diffs {where}", args).fetchone()[0]
            rows = c.execute(f"SELECT {', '.join(_DIFF_COLS)} FROM validation_diffs {where} "
                             "ORDER BY (direction <> 'down'), tx_id LIMIT %s OFFSET %s",
                             args + (limit, offset)).fetchall()
        return [dict(zip(_DIFF_COLS, r)) for r in rows], total

    def stats(self) -> dict:
        with self._conn() as c:
            by_status = dict(c.execute("SELECT status, count(*) FROM validations GROUP BY status").fetchall())
            durations = c.execute(
                "SELECT DISTINCT ON (bundle_id) bundle_id, EXTRACT(EPOCH FROM finished_at - started_at)::float8 "
                "FROM validations WHERE status = 'completed' ORDER BY bundle_id, finished_at DESC").fetchall()
        return {"by_status": by_status, "duration_seconds": dict(durations)}


def make_store(dsn: str | None):
    return PostgresStore(dsn) if dsn else MemoryStore()
