"""M2 automatic validation dispatch, completion callbacks, and release approvals."""
import logging
import uuid
from datetime import datetime
from sqlalchemy import select
from common import contracts as C
from b_registry.db import Bundle, iso, now
from b_registry.verify.gate import evaluate, policy, has_passed_validation
from b_registry.verify.workflow import Conflict, NotFound, record, deployment_attempt


class ReleaseService:
    def __init__(self, db, cfg, runner):
        self.db, self.cfg, self.runner = db, cfg, runner

    def start(self, bid, actor, reason, baseline=None):
        with self.db.transaction() as s:
            bundle = s.get(Bundle, bid)
            if bundle is None:
                raise NotFound("Bundle not found")
            current = bundle.validation or {}
            if current.get("status") in C.REGISTRY_VALIDATION_PENDING:
                if baseline is not None and baseline != current["baseline_bundle_id"]:
                    raise Conflict("A different validation is already running")
                return current
            if current.get("status") == C.REGISTRY_VALIDATION_FAILED and self.runner.state(current) == "running":
                raise Conflict("Previous validation Job is still running; finish it before retrying")
            if bundle.status not in (C.REGISTRY_CANDIDATE, C.REGISTRY_VALIDATION_FAILED_BUNDLE):
                raise Conflict("Only candidates or failed validation bundles can be validated")
            if deployment_attempt(bundle).get("state") in ("pending", "accepted"):
                raise Conflict("Bundle has a deployment request")
            base = bundle.base_bundle_id
            if base is None:
                raise Conflict("Initial bundle has no comparison target; use the deployment API")
            if baseline is not None and baseline != base:
                raise Conflict("Validation baseline must match the bundle creation baseline")
            if bundle.status == C.REGISTRY_VALIDATION_FAILED_BUNDLE:
                record(bundle, C.REGISTRY_CANDIDATE, actor, reason)
            vid = "RP-" + uuid.uuid4().hex
            run = {"validation_id": vid, "bundle_id": bid, "baseline_bundle_id": base,
                   "model_id": bundle.model_id, "list_id": bundle.list_id,
                   "actor": actor, "reason": reason, "status": C.REGISTRY_VALIDATION_QUEUED,
                   "created_at": iso(now()), "policy": policy(), "gate": None,
                   "result": None, "error": None}
            bundle.validation_id, bundle.validation = vid, run
            bundle.history = [*(bundle.history or []), {"from":bundle.status, "to":bundle.status,
                "actor":actor, "reason":reason, "changed_at":iso(now()), "validation_id":vid}]
            return run

    def get(self, bid):
        with self.db.sessions() as s:
            bundle = s.get(Bundle, bid)
            if bundle is None:
                raise NotFound("Bundle not found")
            if bundle.validation is None:
                raise NotFound("Validation has not been requested")
            return bundle.validation

    def _save(self, bid, vid, **updates):
        with self.db.transaction() as s:
            bundle = s.get(Bundle, bid)
            if (bundle is None or bundle.validation_id != vid or bundle.status != C.REGISTRY_CANDIDATE
                    or (bundle.validation or {}).get("status") not in C.REGISTRY_VALIDATION_PENDING):
                return
            bundle.validation = {**bundle.validation, **updates}

    def _finish(self, run, result=None, error=None, callback=False):
        gate = None
        if error is None:
            try:
                gate = evaluate(result, run["policy"])
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                error = str(exc)
        with self.db.transaction() as s:
            bundle = s.get(Bundle, run["bundle_id"])
            if (bundle is None or bundle.validation_id != run["validation_id"] or bundle.status != C.REGISTRY_CANDIDATE
                    or bundle.validation.get("status") not in C.REGISTRY_VALIDATION_PENDING):
                if callback and not (bundle and bundle.validation_id == run["validation_id"]
                                     and bundle.validation.get("result") == result):
                    raise Conflict("Validation is no longer accepting results")
                return
            bundle.validation = {**bundle.validation,
                "status": C.REGISTRY_VALIDATION_FAILED if error else C.REGISTRY_VALIDATION_COMPLETED,
                "gate":gate, "result":result, "error":error, "finished_at":iso(now())}
            if error:
                bundle.history = [*(bundle.history or []), {"from":C.REGISTRY_CANDIDATE, "to":C.REGISTRY_CANDIDATE,
                    "actor":"b-registry", "reason":error, "changed_at":iso(now()),
                    "validation_id":run["validation_id"]}]
            else:
                passed = gate["decision"] == C.REGISTRY_GATE_PASSED
                record(bundle, C.REGISTRY_VALIDATED if passed else C.REGISTRY_VALIDATION_FAILED_BUNDLE, "b-registry",
                       "Validation gate passed" if passed else "; ".join(gate["reasons"]))
                bundle.history = [*bundle.history[:-1], {**bundle.history[-1],
                    "validation_id":run["validation_id"], "gate":gate}]

    def receive(self, bid, vid, result):
        run = self.get(bid)
        if any(result.get(k) != run[k] for k in ("validation_id", "bundle_id", "baseline_bundle_id")) or vid != run["validation_id"]:
            raise Conflict("Validation result identity mismatch or obsolete run")
        if run["status"] not in C.REGISTRY_VALIDATION_PENDING:
            if run.get("result") == result:
                return run
            raise Conflict("Validation already finished with a different result")
        if result["status"] == C.REGISTRY_VALIDATION_COMPLETED:
            # Reject malformed callbacks without consuming the pending run.
            evaluate(result, run["policy"])
            self._finish(run, result=result, callback=True)
        else:
            self._finish(run, result=result, error=result.get("error") or "C validation execution failed", callback=True)
        return self.get(bid)

    def run_once(self):
        # The persisted candidate itself is the durable dispatch intent. This also
        # recovers imports/rebuilt candidates committed immediately before a crash.
        with self.db.sessions() as s:
            candidates = [b.bundle_id for b in s.scalars(select(Bundle).where(
                Bundle.status == C.REGISTRY_CANDIDATE, Bundle.validation_id.is_(None),
                Bundle.base_bundle_id.is_not(None)))]
        for bid in candidates:
            try:
                self.start(bid, "b-registry", "Automatic validation after bundle registration")
            except Conflict:
                pass
        with self.db.sessions() as s:
            runs = [dict(b.validation) for b in s.scalars(select(Bundle).where(Bundle.status == C.REGISTRY_CANDIDATE))
                    if b.validation and b.validation.get("status") in C.REGISTRY_VALIDATION_PENDING]
        for run in runs:
            bid, vid = run["bundle_id"], run["validation_id"]
            try:
                if (now() - datetime.fromisoformat(run["created_at"])).total_seconds() > self.cfg.validation_timeout_seconds:
                    self._finish(run, error="Validation callback deadline exceeded")
                    continue
                # Once launched, wait for C's callback; never poll C results.
                if run["status"] == C.REGISTRY_VALIDATION_RUNNING:
                    continue
                state = self.runner.ensure(run)
                if state == "failed":
                    self._finish(run, error="Validation Job failed")
                else:
                    self._save(bid, vid, status=C.REGISTRY_VALIDATION_RUNNING, error=None)
            except Exception as error:
                self._save(bid, vid, error=type(error).__name__)
                logging.getLogger(__name__).warning("Validation dispatch retry for %s: %s", vid, type(error).__name__)

    def approve(self, bid, decision, actor, reason):
        with self.db.transaction() as s:
            bundle = s.get(Bundle, bid)
            if bundle is None:
                raise NotFound("Bundle not found")
            if decision == C.REGISTRY_APPROVE and bundle.status == C.REGISTRY_APPROVED and has_passed_validation(bundle):
                return None
            if bundle.status != C.REGISTRY_VALIDATED or not has_passed_validation(bundle):
                raise Conflict("Approval requires a completed passing validation for this bundle")
            target = C.REGISTRY_APPROVED if decision == C.REGISTRY_APPROVE else C.REGISTRY_REJECTED
            if decision not in (C.REGISTRY_APPROVE, C.REGISTRY_REJECT):
                raise Conflict("Unknown approval decision")
            return record(bundle, target, actor, reason)
