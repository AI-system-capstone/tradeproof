"""Run C validation as a named Kubernetes Job or local Docker container."""
import copy
import json
import os
import subprocess
from pathlib import Path
import httpx
from common import contracts as C

SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")


class RunnerUnavailable(Exception):
    pass


class ValidationJobRunner:
    def __init__(self, cfg, template=None, client=None):
        self.callback_base_url = cfg.validation_callback_base_url.rstrip("/")
        self.kind = cfg.validation_runner
        self.template = template if template is not None else (
            json.loads(Path(cfg.validation_template).read_text(encoding="utf-8")) if cfg.validation_template else None)
        self.client = client

    def check(self):
        if self.template is None:
            raise RunnerUnavailable("VALIDATION_JOB_TEMPLATE is required")
        if self.kind not in ("kubernetes", "docker"):
            raise RunnerUnavailable("Unsupported VALIDATION_RUNNER")

    @staticmethod
    def name(run):
        return "registry-validation-" + run["validation_id"].lower()

    def env(self, run):
        return dict(zip(C.REGISTRY_JOB_ENV, (run["validation_id"], run["bundle_id"],
                                            run["baseline_bundle_id"], run["actor"],
                                            f"{self.callback_base_url}/bundles/{run['bundle_id']}/validation-results/{run['validation_id']}")))

    def ensure(self, run):
        self.check()
        if self.kind == "docker":
            return self._docker(run, start=True)
        return self._kubernetes(run, start=True)

    def state(self, run):
        return self._docker(run, start=False) if self.kind == "docker" else self._kubernetes(run, start=False)

    def _kubernetes(self, run, start):
        job = copy.deepcopy(self.template)
        if job.get("kind") != "Job" or job.get("apiVersion") != "batch/v1":
            raise RunnerUnavailable("Validation template must be a batch/v1 Job")
        namespace = os.getenv("VALIDATION_NAMESPACE") or os.getenv("REPLAY_NAMESPACE")
        if not namespace:
            namespace = (SERVICE_ACCOUNT / "namespace").read_text().strip()
        meta = job.setdefault("metadata", {})
        meta.update(name=self.name(run), namespace=namespace)
        meta.pop("generateName", None)
        meta.setdefault("labels", {}).update({"tradeproof-bundle-id": run["bundle_id"],
                                              "tradeproof-validation-id": run["validation_id"]})
        for container in job["spec"]["template"]["spec"]["containers"]:
            container["env"] = [v for v in container.get("env", []) if v["name"] not in C.REGISTRY_JOB_ENV]
            container["env"] += [{"name": k, "value": v} for k, v in self.env(run).items()]
        path = f"/apis/batch/v1/namespaces/{namespace}/jobs"
        def exchange(client):
            response = client.get(path + "/" + self.name(run))
            if response.status_code == 404 and start:
                response = client.post(path, json=job)
                if response.status_code == 409:
                    response = client.get(path + "/" + self.name(run))
            if response.status_code == 404:
                return "missing"
            response.raise_for_status()
            actual = response.json()
            if actual.get("metadata", {}).get("labels", {}) != meta["labels"]:
                # Admission controllers may add labels, so only compare owned labels.
                labels = actual.get("metadata", {}).get("labels", {})
                if any(labels.get(k) != v for k, v in meta["labels"].items()):
                    raise RunnerUnavailable("Validation Job name collision")
            containers = actual.get("spec", {}).get("template", {}).get("spec", {}).get("containers", [])
            if not containers or any(any({e["name"]:e.get("value") for e in c.get("env", [])}.get(k) != v
                                         for k,v in self.env(run).items()) for c in containers):
                raise RunnerUnavailable("Existing Job has different validation inputs")
            status = actual.get("status", {})
            for condition in status.get("conditions", []):
                if condition.get("status") == "True" and condition.get("type") == "Failed":
                    return "failed"
            return "succeeded" if status.get("succeeded", 0) else "running"
        if self.client:
            return exchange(self.client)
        with httpx.Client(base_url=f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:{os.getenv('KUBERNETES_SERVICE_PORT_HTTPS','443')}",
                          verify=str(SERVICE_ACCOUNT / "ca.crt"), timeout=httpx.Timeout(5, connect=2),
                          headers={"Authorization": "Bearer " + (SERVICE_ACCOUNT / "token").read_text().strip()}) as client:
            return exchange(client)

    def _docker(self, run, start):
        name = self.name(run)
        def command(args):
            return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=30)
        result = command(["inspect", name])
        if result.returncode:
            # A daemon failure must not be mistaken for a missing container.
            if command(["info", "--format", "{{.ServerVersion}}"]).returncode:
                raise RunnerUnavailable("Docker unavailable")
            if not start:
                return "missing"
            config = self.template
            args = ["run", "--detach", "--name", name,
                    "--label", "tradeproof-validation-id=" + run["validation_id"]]
            for key, value in {**config.get("env", {}), **self.env(run)}.items():
                args += ["--env", f"{key}={value}"]
            if config.get("network"):
                args += ["--network", config["network"]]
            for mount in config.get("mounts", []):
                args += ["--mount", mount]
            command([*args, config["image"], *config.get("command", [])])
            result = command(["inspect", name])
            if result.returncode:
                raise RunnerUnavailable("Could not confirm validation container")
        container = json.loads(result.stdout)[0]
        env = dict(e.split("=",1) for e in container["Config"].get("Env",[]) if "=" in e)
        if (container["Config"].get("Labels", {}).get("tradeproof-validation-id") != run["validation_id"]
                or any(env.get(k) != v for k,v in self.env(run).items())):
            raise RunnerUnavailable("Validation container name collision")
        state = container["State"]
        if state.get("Running") or state.get("Status") == "created":
            return "running"
        return "succeeded" if state.get("ExitCode") == 0 else "failed"
