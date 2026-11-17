"""Argo 드라이버 테스트. 클러스터 없이, helm·kubectl 명령을 받아 적는 가짜 실행기로 확인한다.

실제 클러스터에서의 확인 결과는 d_platform/README.md 의 "시연 환경" 절에 있다.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

import pytest

from d_platform.api import models as M
from d_platform.api.drivers import ArgoDriver, DriverError, make_driver

NOW = datetime(2026, 11, 15, 0, 0, 0, tzinfo=timezone.utc)


def rollout(bundle="B-8", phase="Paused", weight=10, stable="aaa", current="bbb", abort=False,
            generation=5, observed="5", message=None) -> dict:
    return {
        "metadata": {"generation": generation},
        "spec": {"template": {"spec": {"containers": [{"env": [{"name": "BUNDLE_ID", "value": bundle}]}]}}},
        "status": {"phase": phase, "observedGeneration": observed, "stableRS": stable, "currentPodHash": current,
                   "abort": abort, "message": message, "canary": {"weights": {"canary": {"weight": weight}}}},
    }


class Kube:
    """명령을 받아 적고, Rollout 조회에는 정해 둔 상태를 돌려준다."""

    def __init__(self, state: dict):
        self.state = state
        self.calls: list[list[str]] = []
        self.on_helm = None        # helm upgrade 뒤의 상태 (컨트롤러가 새 revision 을 만든 것 흉내)
        self.fail: str | None = None

    def __call__(self, args: list[str]) -> str:
        if self.fail and args[0] == self.fail:
            raise DriverError(f"{args[0]} 실패")
        if args[0] == "kubectl" and "get" in args:
            return json.dumps(self.state)
        self.calls.append(args)
        if args[0] == "helm" and self.on_helm is not None:
            self.state = self.on_helm
        return ""

    def verbs(self) -> list[str]:
        """helm / abort / undo / promote 순서만 뽑는다."""
        return ["helm" if c[0] == "helm" else c[5] for c in self.calls]


def dep(**kw) -> M.Deployment:
    base = dict(deployment_id="DP-1", bundle_id="B-8", previous_bundle_id="B-7", status=M.PROGRESSING,
                requested_by="b-registry", steps=[10, 50, 100], analysis={"result": "running", "checks": []},
                created_at=NOW, updated_at=NOW)
    return M.Deployment(**{**base, **kw})


@pytest.fixture(autouse=True)
def values_file(tmp_path, monkeypatch):
    """드라이버가 쓰는 번들 파일(deploy/values-dev.yaml 자리)을 임시 폴더로 돌린다."""
    path = tmp_path / "values-dev.yaml"
    monkeypatch.setattr(sys.modules[__name__], "VALUES", str(path))
    return path


VALUES = ""


def driver(kube: Kube, **kw) -> ArgoDriver:
    return ArgoDriver(chart="chart", bundle_values=VALUES, run=kube, sleep=lambda _: None, **kw)


# ---------------------------------------------------------------- 시작
def test_start_writes_values_file_and_applies_it(values_file):
    kube = Kube(rollout())
    driver(kube, pause_seconds=30, values_files=("image.yaml",)).start(dep())
    assert values_file.read_text(encoding="utf-8").endswith('bundle:\n  stable: "B-7"\n  candidate: "B-8"\n')
    assert kube.calls == [[
        "helm", "upgrade", "a-screening", "chart", "-n", "tradeproof", "-f", "image.yaml", "-f", str(values_file),
        "--set", "canary.pauseSeconds=30", "--set", "canary.weights={10,50}",
    ]]


def test_finalize_records_new_stable_in_values_file(values_file):
    kube = Kube(rollout(phase="Healthy", weight=0, stable="bbb", current="bbb"))
    driver(kube).finalize(dep(status=M.SUCCEEDED))
    assert values_file.read_text(encoding="utf-8").endswith('bundle:\n  stable: "B-8"\n  candidate: ""\n')
    assert kube.calls == []


# ---------------------------------------------------------------- 관측
@pytest.mark.parametrize("weight,phase,expect", [
    (0, "Progressing", (0, 0, 0)),      # 후보 파드가 뜨는 중
    (10, "Paused", (10, 0, 0)),
    (50, "Paused", (50, 1, 1)),
    (100, "Progressing", (100, 2, 2)),  # 마지막 단계, 아직 stable 로 굳지 않음
])
def test_observe_in_progress(weight, phase, expect):
    obs = driver(Kube(rollout(weight=weight, phase=phase))).observe(dep(), NOW)
    assert (obs.status, obs.weight, obs.step_index, len(obs.analysis["checks"])) == (M.PROGRESSING, *expect)
    assert obs.analysis["result"] == "running"


def test_observe_healthy_is_succeeded():
    obs = driver(Kube(rollout(phase="Healthy", weight=0, stable="bbb", current="bbb"))).observe(dep(), NOW)
    assert (obs.status, obs.weight, obs.step_index) == (M.SUCCEEDED, 100, 2)
    assert [c["result"] for c in obs.analysis["checks"]] == ["passed"] * 3


def test_observe_ignores_status_of_old_spec():
    """helm upgrade 직후: status 는 아직 이전 번들의 Healthy 다. 끝난 것으로 보면 안 된다."""
    stale = rollout(phase="Healthy", weight=0, stable="aaa", current="aaa", generation=6, observed="5")
    obs = driver(Kube(stale)).observe(dep(status=M.PENDING), NOW)
    assert (obs.status, obs.weight) == (M.PROGRESSING, 0)


def test_observe_aborted_is_rolled_back():
    state = rollout(phase="Degraded", weight=0, abort=True, message="RolloutAborted: update to revision 26")
    obs = driver(Kube(state)).observe(dep(), NOW)
    assert (obs.status, obs.weight, obs.rollback_reason) == (M.ROLLED_BACK, 0, M.ROLLBACK_ANALYSIS_FAILED)
    assert obs.analysis["result"] == "failed" and "RolloutAborted" in obs.analysis["message"]


def test_observe_bundle_changed_outside_is_failed():
    obs = driver(Kube(rollout(bundle="B-9"))).observe(dep(), NOW)
    assert obs.status == M.FAILED and "B-9" in obs.analysis["message"]


# ---------------------------------------------------------------- 롤백
RESTORED = dict(bundle="B-7", phase="Healthy", weight=0, stable="aaa", current="aaa", generation=6, observed="6")


def test_rollback_in_progress_is_abort_then_previous_bundle_through_values_file(values_file):
    kube = Kube(rollout(weight=50))
    kube.on_helm = rollout(**RESTORED)             # spec 이 stable 과 같아져 바로 Healthy
    driver(kube).rollback(dep())
    assert kube.verbs() == ["abort", "helm"]       # undo 는 쓰지 않는다
    assert values_file.read_text(encoding="utf-8").endswith('bundle:\n  stable: "B-7"\n  candidate: ""\n')


def test_rollback_already_aborted_skips_abort():
    kube = Kube(rollout(phase="Degraded", weight=0, abort=True))
    kube.on_helm = rollout(**RESTORED)
    driver(kube).rollback(dep())
    assert kube.verbs() == ["helm"]


def test_rollback_never_promotes_while_spec_is_still_the_candidate():
    """helm 뒤에도 spec 이 후보 그대로면(적용이 안 먹힘) 승격하지 않는다."""
    kube = Kube(rollout(weight=50))
    driver(kube).rollback(dep())
    assert kube.verbs() == ["abort", "helm"]


def test_rollback_after_full_rollout_sets_previous_bundle_and_skips_steps():
    """100% 반영 뒤에는 undo 를 쓰지 않는다: 직전 번들을 직접 지정하고 promote --full."""
    kube = Kube(rollout(phase="Healthy", weight=0, stable="bbb", current="bbb"))
    kube.on_helm = rollout(bundle="B-7", phase="Paused", weight=10, stable="bbb", current="aaa",
                           generation=6, observed="6")
    driver(kube).rollback(dep(status=M.SUCCEEDED))
    assert kube.verbs() == ["helm", "promote"]
    assert kube.calls[-1][5:7] == ["promote", "--full"]


def test_rollback_when_bundle_already_restored_only_aligns_values():
    kube = Kube(rollout(bundle="B-7", phase="Healthy", weight=0, stable="aaa", current="aaa"))
    driver(kube).rollback(dep())
    assert kube.verbs() == ["helm"]


def test_command_failure_is_driver_error():
    kube = Kube(rollout())
    kube.fail = "helm"
    with pytest.raises(DriverError):
        driver(kube).start(dep())


def test_make_driver_argo_reads_env():
    d = make_driver("argo", 5, frozenset(), {"HELM_CHART": "c", "K8S_NAMESPACE": "ns",
                                             "CANARY_PAUSE_SECONDS": "7", "HELM_VALUES_FILES": "a.yaml, b.yaml"})
    assert (d.kind, d.chart, d.namespace, d.pause_seconds, d.values_files) == ("argo", "c", "ns", 7, ("a.yaml", "b.yaml"))
    with pytest.raises(ValueError):
        make_driver("nope", 5, frozenset())


# ---------------------------------------------------------------- API 와 함께
def _client(kube: Kube):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from d_platform.api.main import create_app
    from d_platform.api.registry import FakeRegistry
    from d_platform.api.store import MemoryStore

    registry = FakeRegistry(frozenset({"B-8", "B-9"}))
    app = create_app(store=MemoryStore(), driver=driver(kube), registry=registry, env={"BUNDLE_ID": "B-7"})
    return TestClient(app), registry


def test_api_start_failure_is_503_and_does_not_block_next_deployment():
    kube = Kube(rollout())
    kube.fail = "helm"
    client, registry = _client(kube)
    r = client.post("/deployments", json={"bundle_id": "B-8"}, headers={"X-Actor": "b"})
    assert r.status_code == 503 and r.json()["code"] == "SERVICE_UNAVAILABLE"
    assert client.get("/deployments").json()[0]["status"] == M.FAILED
    assert registry.transitions == []     # B 는 이 요청의 503 응답으로 안다 (전이를 보내지 않음)

    kube.fail = None
    assert client.post("/deployments", json={"bundle_id": "B-9"}, headers={"X-Actor": "b"}).status_code == 202


def test_api_follows_rollout_to_success_and_marks_bundle_active():
    kube = Kube(rollout(weight=10))
    client, registry = _client(kube)
    dep_id = client.post("/deployments", json={"bundle_id": "B-8"}, headers={"X-Actor": "b"}).json()["deployment_id"]
    assert client.get(f"/deployments/{dep_id}").json()["weight"] == 10

    kube.state = rollout(phase="Healthy", weight=0, stable="bbb", current="bbb")
    body = client.get(f"/deployments/{dep_id}").json()
    assert (body["status"], body["weight"], body["analysis"]["registry_sync"]) == (M.SUCCEEDED, 100, "ok")
    assert (registry.transitions[-1]["bundle_id"], registry.transitions[-1]["to"]) == ("B-8", "active")


def test_api_abort_seen_in_cluster_rolls_back_and_tells_registry():
    kube = Kube(rollout(weight=50))
    client, registry = _client(kube)
    dep_id = client.post("/deployments", json={"bundle_id": "B-8"}, headers={"X-Actor": "b"}).json()["deployment_id"]

    kube.state = rollout(phase="Degraded", weight=0, abort=True, message="RolloutAborted")
    body = client.get(f"/deployments/{dep_id}").json()
    assert (body["status"], body["rollback_reason"]) == (M.ROLLED_BACK, M.ROLLBACK_ANALYSIS_FAILED)
    assert kube.verbs() == ["helm", "helm"]          # 시작, 그리고 직전 번들로 되돌림 (이미 중단돼 abort 없음)
    assert (registry.transitions[-1]["bundle_id"], registry.transitions[-1]["to"]) == ("B-8", "cancelled")


def test_api_cluster_unreachable_while_observing_is_503():
    kube = Kube(rollout(weight=10))
    client, _ = _client(kube)
    dep_id = client.post("/deployments", json={"bundle_id": "B-8"}, headers={"X-Actor": "b"}).json()["deployment_id"]
    kube.fail = "kubectl"
    r = client.get(f"/deployments/{dep_id}")
    assert r.status_code == 503 and r.json()["code"] == "SERVICE_UNAVAILABLE"
