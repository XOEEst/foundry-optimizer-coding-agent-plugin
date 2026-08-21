from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from foundry_opt.poc.checks import RepositoryCheckResult
from foundry_opt.poc.candidate import FinalizedCandidate, PreparedCandidate
from foundry_opt.poc.decision import CandidateAssessment, Decision, EvaluationSummary
from foundry_opt.poc.verification import VerificationResolution


STATE_SCHEMA_VERSION = 1
STATE_FILENAME = "optimize-job-poc-state.json"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StateError(RuntimeError):
    """Base error for the optimize-job POC state store."""


class StateNotFoundError(StateError):
    """The expected optimize-job state file does not exist."""


class StateConflictError(StateError):
    """The optimize-job state generation has changed since it was read."""


class StateValidationError(StateError):
    """The optimize-job state file is not trusted schema-valid JSON."""


class JobRuntimeDigests(_FrozenModel):
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    hosted_contracts_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    development_evaluation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    validating_evaluation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class JobIdentity(_FrozenModel):
    job_id: str = Field(min_length=1, max_length=128)
    repository: str = Field(min_length=1, max_length=512)
    issue_number: int = Field(gt=0)
    shared_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    base_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    source_root: str = Field(min_length=1, max_length=256)
    route_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    min_candidates: int = Field(ge=1)
    runtime_digests: JobRuntimeDigests | None = None


class IssueCommentReceipt(_FrozenModel):
    marker_id: str = Field(min_length=1, max_length=256)
    receipt_id: str = Field(min_length=1, max_length=256)
    body_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class CleanupReceipt(_FrozenModel):
    draft_id: str = Field(min_length=1, max_length=256)
    receipt_id: str = Field(min_length=1, max_length=256)


class ProjectionReceipt(_FrozenModel):
    candidate_id: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    receipt_id: str = Field(min_length=1, max_length=256)
    patch_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ClosureReceipt(_FrozenModel):
    receipt_id: str = Field(min_length=1, max_length=256)
    outcome: Literal["no_winner", "platform_failure"]


class BaselineState(_FrozenModel):
    evaluation: EvaluationSummary | None = None
    comment_receipt: IssueCommentReceipt | None = None

    @model_validator(mode="after")
    def validate_evaluation(self) -> "BaselineState":
        if self.evaluation is not None and self.evaluation.run_kind != "development":
            raise ValueError("baseline evaluation must use the development dataset")
        return self


class CandidateState(_FrozenModel):
    handoff: PreparedCandidate
    finalized: FinalizedCandidate | None = None
    development: EvaluationSummary | None = None
    validating: EvaluationSummary | None = None
    repository_checks: tuple[RepositoryCheckResult, ...] = ()
    assessment: CandidateAssessment | None = None
    comment_receipt: IssueCommentReceipt | None = None
    draft_id: str | None = Field(default=None, min_length=1, max_length=256)
    cleanup_receipt: CleanupReceipt | None = None

    @model_validator(mode="after")
    def validate_consistency(self) -> "CandidateState":
        if self.finalized is not None:
            if self.finalized.candidate_id != self.handoff.candidate_id:
                raise ValueError("finalized candidate must match the handoff candidate")
            if self.finalized.parent_id != self.handoff.parent_id:
                raise ValueError("candidate parent lineage is immutable")
            if self.finalized.model != self.handoff.model:
                raise ValueError("candidate model is immutable")
            if self.finalized.hypothesis != self.handoff.hypothesis:
                raise ValueError("candidate hypothesis is immutable")
            if self.finalized.workspace_path != self.handoff.workspace_path:
                raise ValueError("candidate workspace path is immutable")
        if self.development is not None and self.development.run_kind != "development":
            raise ValueError("development evidence must use the development dataset")
        if self.validating is not None and self.validating.run_kind != "validating":
            raise ValueError("validating evidence must use the validating dataset")
        if self.assessment is not None and self.assessment.candidate_id != self.handoff.candidate_id:
            raise ValueError("candidate assessment must match the handoff candidate")
        check_ids = [check.spec.casefold_key for check in self.repository_checks]
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("repository check results must be unique")
        if self.cleanup_receipt is not None:
            if self.draft_id is None or self.cleanup_receipt.draft_id != self.draft_id:
                raise ValueError("cleanup receipts must bind to the candidate draft")
        return self

    @property
    def completed(self) -> bool:
        if self.assessment is None:
            return False
        if self.assessment.outcome == "invalid":
            return False
        if self.assessment.outcome == "platform_failure":
            return self.development is not None or bool(self.repository_checks)
        return True


class JobState(_FrozenModel):
    schema_version: Literal[1] = STATE_SCHEMA_VERSION
    generation: int = Field(default=0, ge=0)
    identity: JobIdentity
    verification: VerificationResolution | None = None
    baseline: BaselineState | None = None
    candidates: tuple[CandidateState, ...] = ()
    decision: Decision | None = None
    provisional_winner_id: str | None = Field(default=None, pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    final_winner_id: str | None = Field(default=None, pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    selected_candidate_id: str | None = Field(default=None, pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    final_comment_receipt: IssueCommentReceipt | None = None
    projection_receipt: ProjectionReceipt | None = None
    no_winner_receipt: ClosureReceipt | None = None
    terminal_outcome: Literal[
        "winner",
        "recommended",
        "proposed_unverified",
        "no_winner",
        "platform_failure",
    ] | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_selected_candidate(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        if payload.get("selected_candidate_id") is None:
            if isinstance(payload.get("final_winner_id"), str):
                payload["selected_candidate_id"] = payload["final_winner_id"]
            elif isinstance(payload.get("projection_receipt"), dict):
                candidate_id = payload["projection_receipt"].get("candidate_id")
                if isinstance(candidate_id, str):
                    payload["selected_candidate_id"] = candidate_id
            elif isinstance(payload.get("decision"), dict):
                selected = payload["decision"].get("selected_candidate_id")
                if isinstance(selected, str):
                    payload["selected_candidate_id"] = selected
                elif isinstance(payload["decision"].get("winner_id"), str):
                    payload["selected_candidate_id"] = payload["decision"]["winner_id"]
        return payload

    @model_validator(mode="after")
    def validate_state(self) -> "JobState":
        candidate_ids = [candidate.handoff.candidate_id for candidate in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate handoffs must be unique")
        by_id = {candidate.handoff.candidate_id: candidate for candidate in self.candidates}
        for name, value in {
            "provisional_winner_id": self.provisional_winner_id,
            "final_winner_id": self.final_winner_id,
            "selected_candidate_id": self.selected_candidate_id,
        }.items():
            if value is not None and value not in by_id:
                raise ValueError(f"{name} must reference a known candidate")
        if self.final_winner_id is not None:
            if self.terminal_outcome != "winner":
                raise ValueError("winner projections require terminal_outcome='winner'")
            if self.selected_candidate_id != self.final_winner_id:
                raise ValueError("winner jobs must select the winning candidate")
        if self.projection_receipt is not None:
            if self.selected_candidate_id is None:
                raise ValueError("projection receipts require a selected candidate")
            if self.projection_receipt.candidate_id != self.selected_candidate_id:
                raise ValueError("projection receipt must match the selected candidate")
        if self.no_winner_receipt is not None and self.terminal_outcome in {
            "winner",
            "recommended",
            "proposed_unverified",
        }:
            raise ValueError("positive recommendation jobs cannot carry a no-winner closure receipt")
        if self.terminal_outcome in {"recommended", "proposed_unverified"}:
            if self.selected_candidate_id is None:
                raise ValueError("positive recommendation jobs require a selected candidate")
            if self.final_winner_id is not None:
                raise ValueError("non-quantitative recommendations cannot declare a winner")
        if self.decision is not None:
            if self.decision.provisional_winner_id != self.provisional_winner_id:
                raise ValueError("decision provisional winner must match state")
            if self.decision.winner_id != self.final_winner_id:
                raise ValueError("decision winner must match state")
            if self.decision.selected_candidate_id != self.selected_candidate_id:
                raise ValueError("decision selected candidate must match state")
        return self

    @property
    def digest_sha256(self) -> str:
        payload = self.model_dump(mode="json", exclude={"generation"})
        return _sha256_bytes(_canonical_json_bytes(payload))

    @property
    def completed_candidate_count(self) -> int:
        return sum(1 for candidate in self.candidates if candidate.completed)

    @property
    def verification_mode(self) -> str:
        if self.verification is not None:
            return self.verification.mode
        return "foundry_evaluation"

    def candidate(self, candidate_id: str) -> CandidateState | None:
        for candidate in self.candidates:
            if candidate.handoff.candidate_id == candidate_id:
                return candidate
        return None

    def with_baseline(self, baseline: BaselineState) -> "JobState":
        return self.model_copy(update={"baseline": baseline})

    def with_verification(self, verification: VerificationResolution) -> "JobState":
        return self.model_copy(update={"verification": verification})

    def with_candidate(self, candidate: CandidateState) -> "JobState":
        records = {item.handoff.candidate_id: item for item in self.candidates}
        records[candidate.handoff.candidate_id] = candidate
        ordered = tuple(records[key] for key in sorted(records))
        return self.model_copy(update={"candidates": ordered})

    def with_decision(self, decision: Decision | None) -> "JobState":
        return self.model_copy(
            update={
                "decision": decision,
                "provisional_winner_id": None if decision is None else decision.provisional_winner_id,
                "final_winner_id": None if decision is None else decision.winner_id,
                "selected_candidate_id": None if decision is None else decision.selected_candidate_id,
            }
        )


def _identity_without_runtime_digests(identity: JobIdentity) -> JobIdentity:
    return identity if identity.runtime_digests is None else identity.model_copy(update={"runtime_digests": None})


def _identities_compatible(left: JobIdentity, right: JobIdentity) -> bool:
    if left == right:
        return True
    if _identity_without_runtime_digests(left) != _identity_without_runtime_digests(right):
        return False
    return left.runtime_digests is None or right.runtime_digests is None


class JobStateStore:
    """Atomic compare-and-swap JSON state for the optimize-job POC."""

    def __init__(self, path: Path) -> None:
        resolved = Path(path)
        self.path = (
            resolved
            if resolved.suffix.lower() == ".json"
            else resolved / STATE_FILENAME
        )

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    def initialize(self, identity: JobIdentity) -> JobState:
        if self.exists:
            current = self.load()
            if not _identities_compatible(current.identity, identity):
                raise StateConflictError(
                    "state already exists for a different optimize-job identity"
                )
            if current.identity != identity and current.identity.runtime_digests is None:
                return self.save(
                    current.model_copy(update={"identity": identity}),
                    expected_generation=current.generation,
                )
            return current
        return self._write_new(JobState(identity=identity))

    def load(self) -> JobState:
        if not self.path.is_file():
            raise StateNotFoundError("optimize-job state does not exist")
        try:
            envelope = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as error:
            raise StateError("optimize-job state could not be read") from error
        except json.JSONDecodeError as error:
            raise StateValidationError("optimize-job state is not valid JSON") from error
        if not isinstance(envelope, dict) or set(envelope) != {"content_sha256", "state"}:
            raise StateValidationError("optimize-job state envelope is invalid")
        payload = envelope["state"]
        digest = envelope["content_sha256"]
        if not isinstance(payload, dict) or not isinstance(digest, str):
            raise StateValidationError("optimize-job state envelope types are invalid")
        computed = _sha256_bytes(_canonical_json_bytes(payload))
        if computed != digest:
            raise StateValidationError("optimize-job state digest does not match content")
        try:
            return JobState.model_validate(payload)
        except ValidationError as error:
            raise StateValidationError("optimize-job state does not match the schema") from error

    def save(
        self,
        state: JobState,
        *,
        expected_generation: int | None = None,
    ) -> JobState:
        if state.generation < 0:
            raise StateValidationError("generation must be non-negative")
        current = self.load() if self.exists else None
        if current is None:
            if expected_generation not in {None, 0}:
                raise StateConflictError("optimize-job state does not exist yet")
            return self._write_new(state)
        expected = state.generation if expected_generation is None else expected_generation
        if current.generation != expected:
            raise StateConflictError("optimize-job state generation changed")
        if not _identities_compatible(state.identity, current.identity):
            raise StateConflictError("optimize-job identity is immutable")
        if state.generation != current.generation:
            raise StateConflictError("stale state generation was supplied")
        if state == current:
            return current
        return self._write_existing(state)

    def update(self, mutate: Callable[[JobState], JobState]) -> JobState:
        current = self.load()
        updated = mutate(current)
        if updated == current:
            return current
        if not _identities_compatible(updated.identity, current.identity):
            raise StateConflictError("optimize-job identity is immutable")
        if updated.generation != current.generation:
            raise StateConflictError("state generation was mutated out of band")
        return self.save(updated, expected_generation=current.generation)

    def _write_new(self, state: JobState) -> JobState:
        persisted = JobState.model_validate(
            {
                **state.model_dump(mode="python"),
                "generation": state.generation + 1,
            }
        )
        self._write_envelope(persisted)
        return persisted

    def _write_existing(self, state: JobState) -> JobState:
        persisted = JobState.model_validate(
            {
                **state.model_dump(mode="python"),
                "generation": state.generation + 1,
            }
        )
        self._write_envelope(persisted)
        return persisted

    def _write_envelope(self, state: JobState) -> None:
        payload = state.model_dump(mode="json")
        content = _canonical_json_bytes(payload)
        envelope = {
            "content_sha256": _sha256_bytes(content),
            "state": payload,
        }
        encoded = _canonical_json_bytes(envelope)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as error:
            raise StateError("optimize-job state write did not complete") from error


def _canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
