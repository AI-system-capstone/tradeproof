"""B owns deployment requests; D owns traffic changes. No automatic POST retries."""
import httpx
from sqlalchemy import select
from b_registry.db import Bundle, iso, now
from b_registry.verify.gate import has_passed_validation
from b_registry.verify.workflow import Conflict, NotFound, record, rebuild_candidate, deployment_attempt


class DeploymentUnavailable(Exception):
    pass


class DeploymentRejected(Exception):
    pass


class DeploymentNotSent(DeploymentUnavailable):
    """The deployment POST was not sent; retrying cannot duplicate it."""


class DeploymentNotFound(DeploymentUnavailable):
    """No matching deployment in a complete D listing (not proof of no in-flight POST)."""


class DeploymentClient:
    def __init__(self, url):
        self.url = url.rstrip("/")

    def request(self, bid, actor):
        # D currently accepts the actor only in a header. Do not silently encode
        # or replace the name: D would persist the wrong actor without a contract change.
        try:
            httpx.Headers({"X-Actor": actor})
        except (UnicodeError, ValueError) as error:
            raise DeploymentNotSent("D currently requires an ASCII X-Actor; request was not sent. "
                                    "Unicode actor support requires a D API change") from error
        try:
            response = httpx.post(self.url + "/deployments", json={"bundle_id": bid},
                                  headers={"X-Actor": actor}, timeout=httpx.Timeout(5, connect=2))
            if 400 <= response.status_code < 500:
                raise DeploymentRejected(f"D rejected deployment: HTTP {response.status_code}")
            if response.status_code != 202:
                raise DeploymentUnavailable("D deployment acceptance could not be confirmed")
            return self._check(response.json(), bid)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout,
                httpx.UnsupportedProtocol, httpx.InvalidURL) as error:
            raise DeploymentNotSent("Could not connect to D; deployment request was not sent") from error
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as error:
            raise DeploymentUnavailable("D deployment acceptance could not be confirmed") from error

    def find(self, bid):
        try:
            response = httpx.get(self.url + "/deployments", params={"limit": 100},
                                 timeout=httpx.Timeout(5, connect=2))
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list) or any(not isinstance(item, dict) for item in rows):
                raise DeploymentUnavailable("Invalid D deployment listing")
            for item in rows:
                if not isinstance(item.get("bundle_id"), str) or not item["bundle_id"]:
                    raise DeploymentUnavailable("Invalid D deployment listing")
                self._check(item, item["bundle_id"])
            matches = [item for item in rows if item.get("bundle_id") == bid]
            if not matches and len(rows) < 100:
                raise DeploymentNotFound("No deployment found; operator confirmation is required before recovery")
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


def _attempt_time(bundle):
    return next((entry["changed_at"] for entry in reversed(bundle.history or [])
                 if "deployment_request" in entry), None)


def recover_deployment(db, bid, actor, reason, expected_changed_at, client):
    """Release only the exact pending request the operator checked on D.

    The API additionally requires explicit confirmation that D has no accepted
    or in-flight request. A short listing alone cannot establish that fact.
    """
    def check(bundle):
        if bundle is None:
            raise NotFound("Bundle not found")
        if (bundle.status != "approved" or deployment_attempt(bundle).get("state") != "pending"
                or _attempt_time(bundle) != expected_changed_at):
            raise Conflict("Pending deployment changed; reload the bundle before recovery")

    with db.sessions() as s:
        check(s.get(Bundle, bid))
    try:
        client.find(bid)
    except DeploymentNotFound:
        pass
    else:
        raise Conflict("D already has this deployment; reconcile through the deployment API")
    with db.transaction() as s:
        bundle = s.get(Bundle, bid)
        check(bundle)
        _attempt(bundle, actor, reason, "failed")
        bundle.history = [*bundle.history[:-1], {**bundle.history[-1],
            "recovery": {"pending_changed_at": expected_changed_at, "confirmed_not_received": True}}]
        return {"bundle_id": bid, "status": bundle.status, "request_state": "failed"}


def request_deployment(db, bid, actor, reason, client):
    # Commit the intent before HTTP: D can read the still-approved B bundle,
    # and a timeout/restart must not cause a duplicate POST.
    with db.transaction() as s:
        bundle = s.get(Bundle, bid)
        if bundle is None:
            raise NotFound("Bundle not found")
        attempt = deployment_attempt(bundle)
        if attempt.get("state") == "accepted":
            if bundle.status not in ("canary", "active"):
                raise Conflict("This deployment ended; the bundle cannot be approved again")
            return {"bundle_id": bid, "status": bundle.status,
                    "deployment_id": attempt["deployment_id"]}
        all_bundles = list(s.scalars(select(Bundle)))
        initial = (bundle.base_bundle_id is None and not any(
            b.status in ("active", "canary", "superseded")
            or any(h.get("to") in ("active", "canary") for h in (b.history or []))
            or deployment_attempt(b).get("state") == "accepted" for b in all_bundles))
        if bundle.status == "candidate" and initial:
            record(bundle, "approved", actor, "Initial deployment without a comparison target: " + reason)
        if not initial and not has_passed_validation(bundle):
            raise Conflict("Deployment requires a passing M2 gate and approval")
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
        expected_changed_at = _attempt_time(bundle)
    try:
        accepted = client.find(bid) if recovering else client.request(bid, actor)
    except (DeploymentRejected, DeploymentNotSent) as error:
        with db.transaction() as s:
            bundle = s.get(Bundle, bid)
            if (deployment_attempt(bundle).get("state") == "pending"
                    and _attempt_time(bundle) == expected_changed_at):
                _attempt(bundle, actor, str(error), "failed" if isinstance(error, DeploymentNotSent) else "rejected")
        if isinstance(error, DeploymentNotSent):
            raise
        raise Conflict(str(error)) from error
    # Unknown results deliberately remain pending; repeat requests only query D.
    with db.transaction() as s:
        bundle = s.get(Bundle, bid)
        existing = deployment_attempt(bundle)
        if existing.get("state") == "accepted":
            if bundle.status not in ("canary", "active"):
                raise Conflict("This deployment ended; the bundle cannot be approved again")
            return {"bundle_id": bid, "status": bundle.status, "deployment_id": existing["deployment_id"]}
        if (existing.get("state") != "pending" or bundle.status != "approved"
                or _attempt_time(bundle) != expected_changed_at):
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
