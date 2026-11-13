"""배포 드라이버: 실제로 트래픽 비율을 바꾸는 쪽.

지금은 가짜 드라이버(FakeDriver)만 있다. k3s 단계에서 Argo Rollouts 를 조작하는 실제 드라이버로 바꾼다.
API 는 드라이버에게 "지금 어디까지 왔는지"만 묻고(observe), 그 결과를 deployments 에 적는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from d_platform.api import models as M


@dataclass
class Observation:
    status: str                       # models 의 상태값
    weight: int
    step_index: int
    analysis: dict = field(default_factory=dict)
    rollback_reason: str | None = None


class FakeDriver:
    """시간이 지나면 단계가 올라가는 가짜 canary. 상태를 따로 들고 있지 않아 재시작해도 결과가 같다.

    - 배포 생성 시각부터 step_seconds 마다 다음 단계로: 10 → 50 → 100
    - 마지막 단계(100)에서 step_seconds 를 더 버티면 succeeded
    - fail_bundles 에 든 번들은 fail_at_step 단계의 분석이 실패 → 자동 롤백 (MON-02 흉내)
    """

    kind = "fake"

    def __init__(self, step_seconds: float = 5.0, fail_bundles: frozenset[str] = frozenset(),
                 fail_at_step: int = 1):
        self.step_seconds = step_seconds
        self.fail_bundles = fail_bundles
        self.fail_at_step = fail_at_step

    def start(self, dep: M.Deployment) -> None:
        return None  # 실제 드라이버에서는 Rollout 의 BUNDLE_ID 를 바꿔 canary 를 시작한다

    def rollback(self, dep: M.Deployment) -> None:
        return None  # 실제 드라이버에서는 abort 후 undo

    def observe(self, dep: M.Deployment, now: datetime) -> Observation:
        steps = dep.steps
        elapsed = max(0.0, (now - dep.created_at).total_seconds())
        passed = len(steps) if self.step_seconds <= 0 else int(elapsed // self.step_seconds)  # 끝난 단계 수
        will_fail = dep.bundle_id in self.fail_bundles and self.fail_at_step < len(steps)

        checks = []
        for i in range(min(passed, len(steps))):
            failed = will_fail and i == self.fail_at_step
            checks.append({"step_index": i, "weight": steps[i], "result": "failed" if failed else "passed",
                           "source": "fake"})
            if failed:
                return Observation(M.ROLLED_BACK, 0, i, {"result": "failed", "checks": checks},
                                   M.ROLLBACK_ANALYSIS_FAILED)

        if passed >= len(steps):
            return Observation(M.SUCCEEDED, 100, len(steps) - 1, {"result": "passed", "checks": checks})
        return Observation(M.PROGRESSING, steps[passed], passed, {"result": "running", "checks": checks})


def make_driver(kind: str, step_seconds: float, fail_bundles: frozenset[str]):
    if kind == "fake":
        return FakeDriver(step_seconds=step_seconds, fail_bundles=fail_bundles)
    raise ValueError(f"알 수 없는 DEPLOY_DRIVER: {kind} (지금은 fake 만 지원)")
