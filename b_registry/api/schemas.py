from typing import Literal
from common import contracts as C
from pydantic import BaseModel, ConfigDict, Field, field_validator
from b_registry.contracts import BUNDLE_STATUSES

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

class ImportAccepted(StrictModel):
    """Package validation and registration have completed synchronously."""
    import_id: str
    bundle_id: str | None

class VerificationResult(StrictModel):
    """Registration result or rejection metadata; rejected files are never stored."""
    filename: str | None
    status: Literal["APPROVED", "REJECTED"]
    reason: str | None
    bundle_id: str | None

class BundleRegistered(StrictModel):
    bundle_id: str

class ErrorResponse(StrictModel):
    code: str
    message: str
    target_id: str | None

class Transition(StrictModel):
    to: Literal[*BUNDLE_STATUSES]
    actor: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("to")
    @classmethod
    def status(cls, value):
        if value not in BUNDLE_STATUSES:
            raise ValueError("Unknown bundle status")
        return value

    @field_validator("actor", "reason")
    @classmethod
    def printable(cls, value):
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("Nonempty printable text required")
        return value

class TransitionResult(StrictModel):
    from_: str = Field(alias="from")
    to: str
    changed_at: str

class AIModelResult(StrictModel):
    model_id: str
    uri: str

class SanctionsListResult(StrictModel):
    list_id: str
    uri: str

class BundleResult(StrictModel):
    base_bundle_id: str | None
    history: list[dict]
    bundle_id: str
    status: Literal[*BUNDLE_STATUSES]
    model_id: str
    list_id: str
    created_at: str
    model: AIModelResult
    list: SanctionsListResult

class ActiveBundles(StrictModel):
    active: BundleResult | None
    canary: BundleResult | None


class DeploymentRequest(StrictModel):
    actor: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("actor", "reason")
    @classmethod
    def printable(cls, value):
        return Transition.printable(value)


class DeploymentResult(StrictModel):
    bundle_id: str
    status: str
    deployment_id: str | None = None
    replacement_bundle_id: str | None = None


class ValidationRetryRequest(DeploymentRequest):
    actor: str = Field(min_length=1, max_length=64)


class DeploymentRecoveryRequest(DeploymentRequest):
    expected_changed_at: str = Field(min_length=1, max_length=80)
    confirmed_not_received: Literal[True]

    @field_validator("confirmed_not_received", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("Explicit true confirmation is required")
        return value


class DeploymentRecoveryResult(StrictModel):
    bundle_id: str
    status: str
    request_state: Literal["failed"]


class ValidationCallback(StrictModel):
    validation_id: str
    bundle_id: str
    baseline_bundle_id: str
    status: Literal[C.REGISTRY_VALIDATION_COMPLETED, C.REGISTRY_VALIDATION_FAILED]
    golden: dict | None = None
    replay: dict | None = None
    error: str | None = None


class ApprovalRequest(DeploymentRequest):
    bundle_id: str
    decision: Literal[C.REGISTRY_APPROVE, C.REGISTRY_REJECT]


class ValidationRunResult(StrictModel):
    validation_id: str
    bundle_id: str
    baseline_bundle_id: str
    model_id: str
    list_id: str
    actor: str
    reason: str
    status: Literal[*C.REGISTRY_VALIDATION_STATES]
    created_at: str
    finished_at: str | None = None
    policy: dict
    gate: dict | None
    result: dict | None
    error: str | None
