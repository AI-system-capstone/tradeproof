"""배포 드라이버: 실제로 트래픽 비율을 바꾸는 쪽.

- FakeDriver: 시간이 지나면 단계가 올라가는 가짜 (compose·테스트)
- ArgoDriver: k3s 의 Argo Rollout(a-screening)을 helm·kubectl 로 조작 (시연 환경)

API 는 드라이버에게 "지금 어디까지 왔는지"만 묻고(observe), 그 결과를 deployments 에 적는다.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Mapping

from d_platform.api import models as M


class DriverError(Exception):
    """드라이버가 클러스터를 조작·조회하지 못함 (helm·kubectl 실패). API 는 503 으로 돌려준다."""


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
        return None

    def rollback(self, dep: M.Deployment) -> None:
        return None

    def finalize(self, dep: M.Deployment) -> None:
        return None  # 100% 반영이 끝났을 때 한 번 불린다

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


def _run(args: list[str]) -> str:
    try:
        done = subprocess.run(args, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise DriverError(f"{args[0]} 실행 실패: {e}") from e
    if done.returncode != 0:
        raise DriverError(f"{' '.join(args[:4])} 실패: {(done.stderr or done.stdout).strip()[-300:]}")
    return done.stdout


def _bundle_of(rollout: dict) -> str | None:
    """Rollout 파드 템플릿의 BUNDLE_ID 환경변수."""
    for c in rollout.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
        for e in c.get("env", []):
            if e.get("name") == "BUNDLE_ID":
                return e.get("value")
    return None


def _observed(rollout: dict) -> bool:
    """컨트롤러가 최신 spec 을 봤는지. 아니면 status 는 이전 spec 의 것이다."""
    return str(rollout.get("metadata", {}).get("generation")) == str(
        rollout.get("status", {}).get("observedGeneration"))


def _aborted(rollout: dict) -> bool:
    return bool(rollout.get("status", {}).get("abort"))


def _updating(rollout: dict) -> bool:
    st = rollout.get("status", {})
    return _aborted(rollout) or st.get("stableRS") != st.get("currentPodHash")


VALUES_HEADER = """\
# 시연 환경(k3s) 번들 설정. 배포 API(D)가 쓰고 Helm 으로 적용한다 — 손으로 고치지 않는다.
# stable: 현재 운영 번들, candidate: canary 진행 중인 후보 (없으면 "")
"""


def write_bundle_values(path: str, stable: str, candidate: str) -> None:
    """deploy/values-dev.yaml 을 다시 쓴다. 쓰는 도중 끊겨도 반쪽 파일이 남지 않게 바꿔치기한다."""
    text = f"{VALUES_HEADER}bundle:\n  stable: {json.dumps(stable)}\n  candidate: {json.dumps(candidate)}\n"
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except OSError as e:
        raise DriverError(f"{path} 를 쓸 수 없음: {e}") from e


class ArgoDriver:
    """Argo Rollout(a-screening)의 canary 를 조작한다.

    번들 교체 = deploy/values-dev.yaml → Helm → BUNDLE_ID (docs/interfaces.md 4절). 파일은 D 만 쓴다(11/15 팀 합의).

    - 시작: 파일에 stable=직전, candidate=새 번들을 적고 helm upgrade → 파드 템플릿이 바뀌어 canary 시작.
      단계는 Argo 가 pause_seconds 마다 스스로 올린다 (0 이면 사람이 promote 할 때까지 멈춤).
    - 롤백 (진행 중이든 100% 반영 뒤든 같다):
      abort(진행 중일 때만, 트래픽을 바로 stable 로) → 파일을 직전 번들로 → helm upgrade
      → 직전 번들로 가는 canary 가 생겼으면 promote --full 로 단계를 건너뜀.
      undo 는 쓰지 않는다: 100% 반영 뒤에는 canary 단계를 다시 밟고, 되돌아갈 곳이 revision 순서에 달려 있다
      (2026-11-15 k3s 에서 확인). 직전 번들은 D 가 배포 시작 때 적은 deployments.previous_bundle_id 다.
    - 100% 반영 완료: 파일을 stable=새 번들, candidate="" 로 (BUNDLE_ID 는 그대로라 적용할 것은 없다).
    - 관측: kubectl get rollout 의 status (비율은 status.canary.weights.canary.weight).
      중단됨(abort)·Degraded 는 롤백으로 본다. 감시(AnalysisRun)가 중단시킨 경우도 여기로 온다.
    """

    kind = "argo"

    def __init__(self, chart: str, bundle_values: str = "deploy/values-dev.yaml", namespace: str = "tradeproof",
                 release: str = "a-screening", rollout: str = "a-screening", pause_seconds: int = 30,
                 values_files: tuple[str, ...] = (),
                 run: Callable[[list[str]], str] = _run, sleep: Callable[[float], None] = time.sleep):
        self.chart, self.bundle_values = chart, bundle_values
        self.namespace, self.release, self.rollout = namespace, release, rollout
        self.pause_seconds, self.values_files = pause_seconds, tuple(values_files)
        self._run, self._sleep = run, sleep

    # ---- 명령
    def _apply(self, stable: str, candidate: str, steps: list[int]) -> None:
        """파일을 고치고 그 파일로 helm upgrade."""
        write_bundle_values(self.bundle_values, stable, candidate)
        args = ["helm", "upgrade", self.release, self.chart, "-n", self.namespace]
        for f in (*self.values_files, self.bundle_values):
            args += ["-f", f]
        args += ["--set", f"canary.pauseSeconds={self.pause_seconds}"]
        weights = [w for w in steps if w < 100]
        if weights:
            args += ["--set", "canary.weights={" + ",".join(str(w) for w in weights) + "}"]
        self._run(args)

    def _argo(self, *verb: str) -> None:
        self._run(["kubectl", "argo", "rollouts", "-n", self.namespace, *verb, self.rollout])

    def _get(self) -> dict:
        out = self._run(["kubectl", "-n", self.namespace, "get", "rollouts.argoproj.io", self.rollout, "-o", "json"])
        try:
            return json.loads(out)
        except ValueError as e:
            raise DriverError(f"Rollout 조회 결과를 읽을 수 없음: {e}") from e

    def _wait(self, ok: Callable[[dict], bool], tries: int = 20) -> dict:
        """조건이 될 때까지 최대 tries × 0.5초 기다린다. 안 되면 마지막 상태를 그대로 돌려준다."""
        ro = self._get()
        for _ in range(tries):
            if ok(ro):
                break
            self._sleep(0.5)
            ro = self._get()
        return ro

    # ---- 드라이버 인터페이스
    def start(self, dep: M.Deployment) -> None:
        self._apply(dep.previous_bundle_id, dep.bundle_id, dep.steps)

    def finalize(self, dep: M.Deployment) -> None:
        write_bundle_values(self.bundle_values, dep.bundle_id, "")

    def rollback(self, dep: M.Deployment) -> None:
        previous = dep.previous_bundle_id
        ro = self._wait(_observed)
        if _bundle_of(ro) == dep.bundle_id and _updating(ro) and not _aborted(ro):
            self._argo("abort")                      # 진행 중: 트래픽부터 stable 로
        self._apply(previous, "", dep.steps)
        # 반영이 끝난 배포였다면 직전 번들로 가는 canary 가 새로 생긴다 → 단계를 건너뛴다.
        # spec 이 직전 번들인 것을 확인한 뒤에만 승격한다 (후보를 승격하는 일이 없도록).
        ro = self._wait(lambda r: _observed(r) and _bundle_of(r) == previous and not _aborted(r))
        if _observed(ro) and _bundle_of(ro) == previous and not _aborted(ro) and _updating(ro):
            self._argo("promote", "--full")

    def observe(self, dep: M.Deployment, now: datetime) -> Observation:
        ro = self._get()
        st = ro.get("status", {})
        steps = dep.steps
        if not _observed(ro):                       # 컨트롤러가 아직 새 spec 을 못 봄
            return Observation(M.PROGRESSING, dep.weight, dep.step_index,
                               dep.analysis or {"result": "running", "checks": []})

        weight = int((((st.get("canary") or {}).get("weights") or {}).get("canary") or {}).get("weight") or 0)
        step_index = max([i for i, w in enumerate(steps) if w <= weight], default=0)
        passed = [{"step_index": i, "weight": w, "result": "passed", "source": "argo"} for i, w in enumerate(steps)]
        checks = passed[:step_index]                # 지금 단계보다 앞 단계는 통과한 것
        phase, message = st.get("phase"), st.get("message") or None

        current = _bundle_of(ro)
        if current != dep.bundle_id:
            return Observation(M.FAILED, 0, step_index, {
                "result": "failed", "checks": checks,
                "message": f"Rollout 의 BUNDLE_ID 가 배포 밖에서 {current} 로 바뀜"})
        if _aborted(ro) or phase == "Degraded":
            return Observation(M.ROLLED_BACK, 0, step_index,
                               {"result": "failed", "checks": checks, "message": message},
                               M.ROLLBACK_ANALYSIS_FAILED)
        if phase == "Healthy" and not _updating(ro):
            return Observation(M.SUCCEEDED, 100, len(steps) - 1, {"result": "passed", "checks": passed})
        return Observation(M.PROGRESSING, weight, step_index, {"result": "running", "checks": checks})


def make_driver(kind: str, step_seconds: float, fail_bundles: frozenset[str], env: Mapping[str, str] | None = None):
    env = env or {}
    if kind == "fake":
        return FakeDriver(step_seconds=step_seconds, fail_bundles=fail_bundles)
    if kind == "argo":
        files = tuple(f.strip() for f in env.get("HELM_VALUES_FILES", "").split(",") if f.strip())
        return ArgoDriver(chart=env.get("HELM_CHART", "d_platform/helm/a-screening"),
                          bundle_values=env.get("HELM_BUNDLE_VALUES", "deploy/values-dev.yaml"),
                          namespace=env.get("K8S_NAMESPACE", "tradeproof"),
                          release=env.get("HELM_RELEASE", "a-screening"),
                          pause_seconds=int(env.get("CANARY_PAUSE_SECONDS", "30")), values_files=files)
    raise ValueError(f"알 수 없는 DEPLOY_DRIVER: {kind} (fake | argo)")
