from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import Field, field_validator, model_validator

from foundry_opt.bootstrap.canonical import canonical_sha256, safe_persisted_document
from foundry_opt.bootstrap.contracts import BootstrapDocument, BootstrapReceipt, FingerprintRecord, RedactedStatusInfo
from foundry_opt.bootstrap.errors import BootstrapApplyError, BootstrapConfigError

ApplyPhaseName = Literal["repository", "github", "azure", "evaluations"]
PhaseStateName = Literal["pending", "applying", "applied", "failed", "compensation_required", "rolled_back"]


class ApprovalRecord(BootstrapDocument):
    parent_plan_hash: str
    phase: ApplyPhaseName
    actor: str = Field(min_length=1, max_length=256)
    summary: str = Field(min_length=1, max_length=4096)
    approval_hash: str

    @classmethod
    def create(cls, *, parent_plan_hash: str, phase: ApplyPhaseName, actor: str, summary: str) -> "ApprovalRecord":
        payload = {
            "parent_plan_hash": parent_plan_hash,
            "phase": phase,
            "actor": actor,
            "summary": summary,
        }
        safe_persisted_document(payload)
        return cls.model_validate({**payload, "approval_hash": canonical_sha256(payload)})

    @property
    def plan_hash(self) -> str:
        return self.parent_plan_hash

    @model_validator(mode="after")
    def validate_hash(self) -> "ApprovalRecord":
        payload = {
            "parent_plan_hash": self.parent_plan_hash,
            "phase": self.phase,
            "actor": self.actor,
            "summary": self.summary,
        }
        if self.approval_hash != canonical_sha256(payload):
            raise BootstrapApplyError("approval_hash does not match approval payload")
        return self


class PhaseReceipt(BootstrapDocument):
    phase: ApplyPhaseName
    state: PhaseStateName
    provider: str = Field(min_length=1, max_length=128)
    receipt: BootstrapReceipt
    parent_plan_hash: str
    phase_plan_hash: str
    approval_hash: str | None = None
    summary: str = Field(min_length=1, max_length=4096)
    provider_state: Mapping[str, object] = Field(default_factory=dict)
    recorded_fingerprints: tuple[FingerprintRecord, ...] = ()
    rollback_summary: str | None = Field(default=None, max_length=4096)

    @field_validator("provider_state")
    @classmethod
    def validate_provider_state(cls, value: Mapping[str, object]) -> Mapping[str, object]:
        safe_persisted_document(value)
        return value

    @model_validator(mode="after")
    def validate_receipt_hashes(self) -> "PhaseReceipt":
        if self.state in {"applied", "compensation_required", "failed"} and (
            self.receipt.plan_hash != self.phase_plan_hash
        ):
            raise BootstrapApplyError("phase receipt plan hash does not match provider receipt")
        return self


class PhaseResult(BootstrapDocument):
    receipt: BootstrapReceipt
    verified: bool
    compensation_required: bool
    summary: str = Field(min_length=1, max_length=4096)
    provider_state: Mapping[str, object] = Field(default_factory=dict)
    recorded_fingerprints: tuple[FingerprintRecord, ...] = ()

    @field_validator("provider_state")
    @classmethod
    def validate_provider_state(cls, value: Mapping[str, object]) -> Mapping[str, object]:
        safe_persisted_document(value)
        return value


class EvaluationReplacementRecord(BootstrapDocument):
    active_bundle_id: str = Field(min_length=1, max_length=256)
    candidate_bundle_id: str = Field(min_length=1, max_length=256)
    preserved_bundle_id: str = Field(min_length=1, max_length=256)
    lineage_hash: str = Field(min_length=1, max_length=128)
    status: Literal["planned", "activated", "failed"]
    detail: str | None = Field(default=None, max_length=4096)


def summarize_receipt(receipt: BootstrapReceipt) -> str:
    summary = {
        "created": len(receipt.created_actions),
        "adopted": len(receipt.adopted_actions),
        "changed": len(receipt.changed_actions),
        "skipped": len(receipt.skipped_actions),
        "compensation_required": len(receipt.compensation_required_actions),
        "error": receipt.error_info.code if receipt.error_info else None,
        "resume": receipt.resume_info.code if receipt.resume_info else None,
    }
    safe_persisted_document(summary)
    return str(summary)


def failure_receipt(
    *,
    phase: ApplyPhaseName,
    provider: str,
    operation_id: str,
    runtime_repository: str,
    runtime_commit: str,
    repository_identity: str,
    parent_plan_hash: str,
    phase_plan_hash: str,
    before_fingerprints: Sequence[FingerprintRecord],
    code: str,
    summary: str,
    compensation_required_actions: Sequence[str] = (),
    resume_code: str = "resume-required",
    resume_summary: str = "resume the failed phase using the same repository revision",
) -> PhaseResult:
    if len(code) > 64 or len(summary) > 256:
        raise BootstrapConfigError("sanitized failure details exceed bounds")
    receipt = BootstrapReceipt.create(
        operation_id=operation_id,
        runtime_repository=runtime_repository,
        runtime_commit=runtime_commit,
        repository_identity=repository_identity,
        plan_hash=phase_plan_hash,
        before_fingerprints=tuple(before_fingerprints),
        after_fingerprints=tuple(before_fingerprints),
        compensation_required_actions=tuple(compensation_required_actions),
        error_info=RedactedStatusInfo(code=code, summary=summary),
        resume_info=RedactedStatusInfo(code=resume_code, summary=resume_summary),
    )
    return PhaseResult(
        receipt=receipt,
        verified=False,
        compensation_required=bool(compensation_required_actions),
        summary=f"{phase} via {provider} failed: {summary}",
        provider_state={"phase_plan_hash": phase_plan_hash},
        recorded_fingerprints=tuple(before_fingerprints),
    )


def merge_phase_receipts(receipts: Mapping[str, PhaseReceipt]) -> tuple[PhaseReceipt, ...]:
    return tuple(receipts[key] for key in sorted(receipts))
