"""Imports is the persistent verification queue; all registration writes are serialized."""
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
    bundle.status = target
    return result

def transition(db, bid, target, actor, reason):
    with db.transaction() as s:
        bundle = s.get(Bundle, bid)
        if not bundle:
            raise NotFound("Bundle not found")
        if target not in TRANSITIONS.get(bundle.status, set()):
            raise Conflict("Transition is not allowed")
        if target == "active":
            active = s.scalar(select(Bundle).where(Bundle.status == "active"))
            restoring = bundle.status == "superseded"
            if restoring and (not active or active.previous_active_id != bid):
                raise Conflict("Only the previous stable bundle can be restored")
            if not restoring:
                bundle.previous_active_id = active.bundle_id if active else None
            if active:
                record(active, "rolled_back" if restoring else "superseded", actor, reason)
                s.flush()
        result = record(bundle, target, actor, reason)
        if target == "rolled_back" and result["from"] == "active" and bundle.previous_active_id:
            previous = s.get(Bundle, bundle.previous_active_id)
            if not previous or previous.status != "superseded":
                raise Conflict("Previous stable bundle is not available")
            s.flush()
            record(previous, "active", actor, reason)
        return result

class ImportService:
    def __init__(self, db, cfg, artifacts):
        self.db, self.cfg, self.artifacts = db, cfg, artifacts

    def intake(self, raw):
        # No registry row or persistent artifact is created before validation passes.
        try:
            report = validate(raw, self.cfg)
        except OSError as error:
            from b_registry.storage import StorageUnavailable
            raise StorageUnavailable("Trust key unavailable") from error
        if report.error:
            raise report.error
        with self.db.transaction() as s:
            uri = self.artifacts.put(raw)
            existing = s.scalar(select(Import).where(Import.artifact_uri == uri))
            if existing:
                return existing
            imp = Import(import_id=str(uuid.uuid4()), artifact_uri=uri)
            s.add(imp)
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
            numbers = [int(b[2:]) for b in s.scalars(select(Bundle.bundle_id))]
            first = int(C.BASELINE_BUNDLE.split("-")[1])
            bundle = Bundle(bundle_id=f"B-{max(numbers, default=first - 1) + 1}", status="candidate", **ids)
            s.add(bundle)
            record(bundle, "validated", "registry-worker", "Package validation passed")
            s.flush()
        imp.bundle_id = bundle.bundle_id
        if not active:
            for previous in s.scalars(select(Import).where(or_(Import.model_id == model.model_id, Import.list_id == listing.list_id), Import.bundle_id.is_(None))):
                previous.bundle_id = bundle.bundle_id
        return bundle.bundle_id

    def process(self, iid):
        with self.db.transaction() as s:
            imp = s.get(Import, iid)
            if not imp:
                raise NotFound("Import not found")
            if imp.status in (C.IMPORT_APPROVED, C.IMPORT_REJECTED):
                return imp.status
            try:
                raw = self._read(imp.artifact_uri)
                report = validate(raw, self.cfg)
                if report.error:
                    raise report.error
                if report.rows is not None:
                    prior, rows = self._prior(s)
                    delta = changes(report.rows, rows)
                    if prior and delta["change_ratio"] > self.cfg.review_threshold:
                        raise ValidationFailure("change_ratio", "CHANGE_RATIO_EXCEEDED",
                            f"List change ratio {delta['change_ratio']:.0%} exceeds {self.cfg.review_threshold:.0%}")
                with s.begin_nested():
                    self._register(s, imp, report)
                imp.status, imp.reason = C.IMPORT_APPROVED, None
            except (ValidationFailure, Conflict) as error:
                imp.status, imp.reason = C.IMPORT_REJECTED, str(error)
            imp.completed_at = now()
            return imp.status

    def registered_bundle(self, iid):
        with self.db.sessions() as s:
            imp = s.get(Import, iid)
            if not imp:
                raise NotFound("Import not found")
            if imp.status != C.IMPORT_APPROVED or not imp.bundle_id:
                raise Conflict("Validation and both baseline packages are required")
            return imp.bundle_id
