"""B owns deployment requests; D owns traffic changes. No automatic POST retries."""
import httpx
from sqlalchemy import select
from b_registry.db import Bundle, iso, now
from b_registry.verify.workflow import Conflict, NotFound, record, rebuild_candidate, deployment_attempt


class DeploymentUnavailable(Exception):
    pass


class DeploymentRejected(Exception):
    pass


class DeploymentClient:
    def __init__(self, url):
        self.url = url.rstrip("/")

    def request(self, bid, actor):
        try:
            response = httpx.post(self.url + "/deployments", json={"bundle_id": bid},
                                  headers={"X-Actor": actor}, timeout=httpx.Timeout(5, connect=2))
            if 400 <= response.status_code < 500:
                raise DeploymentRejected(f"D rejected deployment: HTTP {response.status_code}")
            if response.status_code != 202:
                raise DeploymentUnavailable("D deployment acceptance could not be confirmed")
            return self._check(response.json(), bid)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            raise DeploymentUnavailable("D deployment acceptance could not be confirmed") from error

    def find(self, bid):
        try:
            response = httpx.get(self.url + "/deployments", params={"limit": 100},
                                 timeout=httpx.Timeout(5, connect=2))
            response.raise_for_status()
            matches = [item for item in response.json() if item.get("bundle_id") == bid]
            if len(matches) != 1:
                raise DeploymentUnavailable("Deployment outcome is unknown; POST was not retried")
            return self._check(matches[0], bid)
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as error:
            raise DeploymentUnavailable("Cannot reconcile D deployment outcome") from error

    @staticmethod
    def _check(data, bid):
        if (data["bundle_id"] != bid or not isinstance(data["deployment_id"], str)
                or not data["deployment_id"] or data["status"] not in
                ("pending", "progressing", "succeeded", "rolled_back", "failed")):
            raise DeploymentUnavailable("Invalid D deployment response")
        return data


def _attempt(bundle, actor, reason, state, deployment_id=None):
    bundle.history = [*(bundle.history or []), {
        "from": bundle.status, "to": bundle.status, "actor": actor, "reason": reason,
        "changed_at": iso(now()),
        "deployment_request": {"state": state, "deployment_id": deployment_id}}]


def request_deployment(db, bid, actor, reason, client):
    # Commit the intent before HTTP: D can read the still-approved B bundle,
    # and a timeout/restart must not cause a duplicate POST.
    with db.transaction() as s:
        bundle = s.get(Bundle, bid)
        if bundle is None:
            raise NotFound("Bundle not found")
        attempt = deployment_attempt(bundle)
        if attempt.get("state") == "accepted":
            return {"bundle_id": bid, "status": bundle.status,
                    "deployment_id": attempt["deployment_id"]}
        if bundle.status != "approved":
            raise Conflict("Deployment requires an approved bundle")
        recovering = attempt.get("state") == "pending"
        for other in s.scalars(select(Bundle)):
            if other.bundle_id != bid and (other.status == "canary" or deployment_attempt(other).get("state") == "pending"):
                raise Conflict("Another deployment is running or awaiting confirmation")
        if not recovering:
            active = s.scalar(select(Bundle).where(Bundle.status == "active"))
            if bundle.base_bundle_id != (active.bundle_id if active else None):
                rebuilt = rebuild_candidate(s, bundle, active, actor, reason)
                return {"bundle_id": bid, "status": "cancelled",
                        "replacement_bundle_id": rebuilt["replacement_bundle_id"]}
            bundle.previous_active_id = active.bundle_id if active else None
            _attempt(bundle, actor, reason, "pending")
    try:
        accepted = client.find(bid) if recovering else client.request(bid, actor)
    except DeploymentRejected as error:
        with db.transaction() as s:
            bundle = s.get(Bundle, bid)
            _attempt(bundle, actor, str(error), "rejected")
        raise Conflict(str(error)) from error
    # Unknown results deliberately remain pending; repeat requests only query D.
    with db.transaction() as s:
        bundle = s.get(Bundle, bid)
        existing = deployment_attempt(bundle)
        if existing.get("state") == "accepted":
            return {"bundle_id": bid, "status": bundle.status, "deployment_id": existing["deployment_id"]}
        if existing.get("state") != "pending" or bundle.status != "approved":
            raise Conflict("Deployment intent changed")
        if accepted["status"] in ("rolled_back", "failed"):
            record(bundle, "cancelled", actor, reason)
        else:
            record(bundle, "canary", actor, reason)
            if accepted["status"] == "succeeded":
                active = s.scalar(select(Bundle).where(Bundle.status == "active"))
                if active:
                    record(active, "superseded", actor, reason)
                    s.flush()
                record(bundle, "active", actor, reason)
        _attempt(bundle, actor, reason, "accepted", accepted["deployment_id"])
        return {"bundle_id": bid, "status": bundle.status, "deployment_id": accepted["deployment_id"]}
