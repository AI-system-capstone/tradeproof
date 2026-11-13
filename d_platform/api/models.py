"""배포 한 건의 모양과 상태값.

TODO(전원 리뷰): 상태값·ID 접두사는 common/contracts.py 가 main 에 들어오면 그쪽으로 옮긴다.
"""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

PENDING = "pending"            # 접수됨, 아직 canary 시작 전
PROGRESSING = "progressing"    # canary 진행 중 (weight 가 단계별로 올라감)
SUCCEEDED = "succeeded"        # 100% 반영 완료 → 이 번들이 stable
ROLLED_BACK = "rolled_back"    # 직전 번들로 되돌림 (수동 또는 분석 실패로 자동)
FAILED = "failed"              # 배포 자체가 실패 (드라이버 오류 등)
STATUSES = (PENDING, PROGRESSING, SUCCEEDED, ROLLED_BACK, FAILED)
IN_PROGRESS = (PENDING, PROGRESSING)

ROLLBACK_MANUAL = "manual"
ROLLBACK_ANALYSIS_FAILED = "analysis_failed"

DEFAULT_STEPS = (10, 50, 100)  # MTG-05 제안 (팀 확정 전)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def new_deployment_id() -> str:
    # TODO: common/ids.py 의 new_id("DP") 가 생기면 교체
    return f"DP-{uuid.uuid4().hex[:12]}"


def iso(dt: datetime | None) -> str | None:
    """UTC ISO8601, 끝은 +00:00 (공통 규약)."""
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds") if dt else None


@dataclass
class Deployment:
    deployment_id: str
    bundle_id: str
    previous_bundle_id: str
    status: str
    requested_by: str
    steps: list[int]
    weight: int = 0
    step_index: int = 0
    analysis: dict = field(default_factory=dict)
    rollback_reason: str | None = None
    created_at: datetime = field(default_factory=now_utc)
    updated_at: datetime = field(default_factory=now_utc)
    finished_at: datetime | None = None

    @property
    def in_progress(self) -> bool:
        return self.status in IN_PROGRESS

    def to_json(self) -> dict:
        d = asdict(self)
        for k in ("created_at", "updated_at", "finished_at"):
            d[k] = iso(d[k])
        return d
