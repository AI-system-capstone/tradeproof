"""Validate before persisting imports; serialize registration and deployment changes."""
import hashlib
import uuid
from sqlalchemy import select, or_
from common import contracts as C
from b_registry.contracts import TRANSITIONS
from b_registry.db import Bundle, AIModel, SanctionsList, Import, iso, now
from b_registry.verify.validation import ValidationFailure, changes, validate

class Conflict(Exception):
    pass

class NotFound(Exception):
    pass

def record(bundle, target, actor, reason):
    result = {"from": bundle.status, "to": target, "changed_at": iso(now())}
    bundle.history = [*(bundle.history or []), {**result, "actor": actor, "reason": reason}]
    bundle.status = target
    return result

def next_bundle_id(s):
    numbers = [int(b[2:]) for b in s.scalars(select(Bundle.bundle_id))]
    first = int(C.BASELINE_BUNDLE.split("-")[1])
    return f"B-{max(numbers, default=first - 1) + 1}"


def rebuild_candidate(s, bundle, active, actor, reason):
    base = s.get(Bundle, bundle.base_bundle_id) if bundle.base_bundle_id else None
    if base is None or active is None:
        raise Conflict("Cannot rebuild a bundle without a known base and current stable bundle")
    model_changed = bundle.model_id != base.model_id
    list_changed = bundle.list_id != base.list_id
    if model_changed == list_changed:
        raise Conflict("Cannot determine the single imported asset to retain")
    replacement = Bundle(bundle_id=next_bundle_id(s), status="candidate",
                         model_id=bundle.model_id if model_changed else active.model_id,
                         list_id=bundle.list_id if list_changed else active.list_id,
                         base_bundle_id=active.bundle_id)
    s.add(replacement)
    s.flush()
    result = record(bundle, "cancelled", actor, reason)
    bundle.history = [*bundle.history[:-1], {**bundle.history[-1],
        "replacement_bundle_id": replacement.bundle_id}]
    return {**result, "replacement_bundle_id": replacement.bundle_id}


def deployment_attempt(bundle):
    return next((entry["deployment_request"] for entry in reversed(bundle.history or [])
                 if "deployment_request" in entry), {})


def transition(db, bid, target, actor, reason):
    with db.transaction() as s:
        bundle = s.get(Bundle, bid)
        if not bundle:
            raise NotFound("Bundle not found")
        if any(deployment_attempt(b).get("state") == "pending"
               for b in s.scalars(select(Bundle))):
            raise Conflict("A deployment request is awaiting confirmation")
        if target not in TRANSITIONS.get(bundle.status, set()):
            raise Conflict("Transition is not allowed")
        active = s.scalar(select(Bundle).where(Bundle.status == "active"))
        canary = s.scalar(select(Bundle).where(Bundle.status == "canary"))
        if target == "canary":
            if canary:
                raise Conflict("A canary deployment is already running")
            if bundle.base_bundle_id != (active.bundle_id if active else None):
                raise Conflict("Stable bundle changed; request deployment through B")
            bundle.previous_active_id = active.bundle_id if active else None
        if target == "active":
            restoring = bundle.status == "superseded"
            if restoring:
                if canary or not active or active.previous_active_id != bid:
                    raise Conflict("Only the previous stable bundle can be restored without a running canary")
            elif bundle.previous_active_id != (active.bundle_id if active else None):
                raise Conflict("The stable bundle changed during canary deployment")
            if active:
                record(active, "cancelled" if restoring else "superseded", actor, reason)
                s.flush()
        result = record(bundle, target, actor, reason)
        return result

class ImportService:
    def __init__(self, db, cfg, artifacts):
        self.db, self.cfg, self.artifacts = db, cfg, artifacts

    def intake(self, raw, filename=None):
        filename = self._filename(filename)
        try:
            return self._intake(raw, filename)
        except (ValidationFailure, Conflict) as error:
            # The registration transaction has rolled back before recording metadata.
            error.import_id = self.reject(filename, str(error))
            raise

    @staticmethod
    def _filename(filename):
        if filename is None:
            return None
        return "".join(c for c in filename.replace("\\", "/").rsplit("/", 1)[-1]
                       if ord(c) >= 32 and ord(c) != 127)[:255]

    def reject(self, filename, reason):
        with self.db.transaction() as s:
            imp = Import(import_id=str(uuid.uuid4()), filename=self._filename(filename),
                         artifact_uri=None, status=C.IMPORT_REJECTED,
                         reason=reason[:500], completed_at=now())
            s.add(imp)
            s.flush()
            return imp.import_id

    def _intake(self, raw, filename):
        # Never persist rejected package bytes or register rejected assets.
        try:
            report = validate(raw, self.cfg)
        except OSError as error:
            from b_registry.storage import StorageUnavailable
            raise StorageUnavailable("Trust key unavailable") from error
        if report.error:
            raise report.error
        with self.db.transaction() as s:
            # All policy checks precede any asset/import INSERT or artifact write.
            manifest = report.manifest
            is_model = manifest.package_type == "ocr_model"
            asset_type = AIModel if is_model else SanctionsList
            asset = s.get(asset_type, manifest.item_id)
            digest = hashlib.sha256(raw).hexdigest()
            if asset and asset.artifact_uri.rsplit("/", 1)[-1] != digest:
                raise ValidationFailure("content", "VERSION_CONFLICT", "Another package already uses this version")
            if asset:
                existing = s.scalar(select(Import).where(Import.artifact_uri == asset.artifact_uri,
                                                       Import.status == C.IMPORT_APPROVED))
                if existing:
                    self.artifacts.put(raw)  # Also verify the existing immutable object.
                    return existing
            active = s.scalar(select(Bundle).where(Bundle.status == "active"))
            if not active:
                first = s.scalar(select(asset_type).order_by(asset_type.created_at).limit(1))
                if first and first != asset:
                    raise Conflict("Activate the initial bundle before importing an update")
            if report.rows is not None:
                prior, rows = self._prior(s)
                delta = changes(report.rows, rows)
                if prior and delta["change_ratio"] > self.cfg.review_threshold:
                    raise ValidationFailure("change_ratio", "CHANGE_RATIO_EXCEEDED",
                        f"List change ratio {delta['change_ratio']:.0%} exceeds {self.cfg.review_threshold:.0%}")
            uri = self.artifacts.put(raw)
            existing = s.scalar(select(Import).where(Import.artifact_uri == uri))
            imp = existing or Import(import_id=str(uuid.uuid4()), artifact_uri=uri, filename=filename)
            imp.status, imp.reason = C.IMPORT_APPROVED, None
            s.add(imp)
            self._register(s, imp, report)
            imp.completed_at = now()
            s.flush()
            return imp

    def _read(self, uri):
        raw = self.artifacts.get(uri)
        # Storage keys are content-addressed; no duplicate hash column is needed.
        if hashlib.sha256(raw).hexdigest() != uri.rsplit("/", 1)[-1]:
            raise ValidationFailure("intake", "STORAGE_HASH_MISMATCH", "Stored package was modified")
        return raw

    def _prior(self, s):
        active = s.scalar(select(Bundle).where(Bundle.status == "active"))
        prior = s.get(SanctionsList, active.list_id) if active else s.scalar(
            select(SanctionsList).order_by(SanctionsList.created_at).limit(1))
        if not prior:
            return None, {}
        report = validate(self._read(prior.artifact_uri), self.cfg)
        if report.error or report.rows is None:
            raise ValidationFailure("content", "BASELINE_INVALID", "Cannot validate the baseline list")
        return prior, report.rows

    def _register(self, s, imp, report):
        manifest = report.manifest
        is_model = manifest.package_type == "ocr_model"
        asset_type = AIModel if is_model else SanctionsList
        id_field = "model_id" if is_model else "list_id"
        asset = s.get(asset_type, manifest.item_id)
        if asset and asset.artifact_uri != imp.artifact_uri:
            raise ValidationFailure("content", "VERSION_CONFLICT", "Another package already uses this version")
        active = s.scalar(select(Bundle).where(Bundle.status == "active"))
        if not asset:
            asset = asset_type(**{id_field: manifest.item_id}, artifact_uri=imp.artifact_uri)
            s.add(asset)
            s.flush()
        setattr(imp, id_field, getattr(asset, id_field))
        if active:
            model = asset if is_model else s.get(AIModel, active.model_id)
            listing = s.get(SanctionsList, active.list_id) if is_model else asset
        else:
            model = s.scalar(select(AIModel).order_by(AIModel.created_at).limit(1))
            listing = s.scalar(select(SanctionsList).order_by(SanctionsList.created_at).limit(1))
        if not model or not listing:
            return None
        ids = dict(model_id=model.model_id, list_id=listing.list_id)
        if not active and getattr(asset, id_field) != (model.model_id if is_model else listing.list_id):
            raise Conflict("Activate the initial bundle before importing an update")
        bundle = s.scalar(select(Bundle).filter_by(**ids).order_by(Bundle.created_at.desc()).limit(1))
        if not bundle:
            bundle = Bundle(bundle_id=next_bundle_id(s), status="candidate",
                            base_bundle_id=active.bundle_id if active else None, **ids)
            s.add(bundle)
            s.flush()
        imp.bundle_id = bundle.bundle_id
        if not active:
            for previous in s.scalars(select(Import).where(or_(Import.model_id == model.model_id, Import.list_id == listing.list_id), Import.bundle_id.is_(None))):
                previous.bundle_id = bundle.bundle_id
        return bundle.bundle_id

    def registered_bundle(self, iid):
        with self.db.sessions() as s:
            imp = s.get(Import, iid)
            if not imp:
                raise NotFound("Import not found")
            if imp.status != C.IMPORT_APPROVED or not imp.bundle_id:
                raise Conflict("Validation and both baseline packages are required")
            return imp.bundle_id
