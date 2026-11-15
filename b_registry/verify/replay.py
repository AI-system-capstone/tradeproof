"""Reconcile candidate bundles with Kubernetes Jobs; no submission state in DB."""
import copy
import json
import logging
import os
from pathlib import Path
import httpx
from sqlalchemy import select
from b_registry.db import Bundle

SERVICE_ACCOUNT = Path("/var/run/secrets/kubernetes.io/serviceaccount")

class ReplayDispatcher:
    def __init__(self, db, template=None, client=None):
        self.db = db
        path = os.getenv("REPLAY_JOB_TEMPLATE")
        self.template = template if template is not None else (json.loads(Path(path).read_text()) if path else None)
        self.client = client

    def run_once(self):
        if self.template is None:
            return
        with self.db.sessions() as s:
            ids = list(s.scalars(select(Bundle.bundle_id).where(Bundle.status == "candidate")))
        for bid in ids:
            try:
                self.dispatch(bid)
            except Exception:
                logging.getLogger(__name__).warning("Replay Job creation failed for %s; retry pending", bid)

    def dispatch(self, bid):
        job = copy.deepcopy(self.template)
        if job.get("kind") != "Job" or job.get("apiVersion") != "batch/v1":
            raise ValueError("Replay template must be a batch/v1 Job")
        metadata = job.setdefault("metadata", {})
        metadata["name"] = "registry-replay-" + bid.lower()
        metadata.pop("generateName", None)
        metadata.setdefault("labels", {})["tradeproof-bundle-id"] = bid
        for container in job["spec"]["template"]["spec"]["containers"]:
            container["env"] = [e for e in container.get("env", []) if e["name"] != "BUNDLE_ID"]
            container["env"].append({"name": "BUNDLE_ID", "value": bid})
        namespace = os.getenv("REPLAY_NAMESPACE") or (SERVICE_ACCOUNT / "namespace").read_text().strip()
        metadata["namespace"] = namespace
        path = f"/apis/batch/v1/namespaces/{namespace}/jobs"
        def send(client):
            response = client.post(path, json=job)
            if response.status_code == 409:
                existing = client.get(path + "/" + metadata["name"])
                existing.raise_for_status()
                if existing.json()["metadata"].get("labels", {}).get("tradeproof-bundle-id") != bid:
                    raise ValueError("Replay Job name collision")
            else:
                response.raise_for_status()
        if self.client:
            send(self.client)
        else:
            host = os.environ["KUBERNETES_SERVICE_HOST"]
            port = os.getenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
            with httpx.Client(base_url=f"https://{host}:{port}", verify=str(SERVICE_ACCOUNT / "ca.crt"),
                headers={"Authorization": "Bearer " + (SERVICE_ACCOUNT / "token").read_text().strip()}, timeout=10) as client:
                send(client)
