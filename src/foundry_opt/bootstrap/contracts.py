from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Annotated, Any, ClassVar, Literal, Protocol, Self

from pydantic import Field, StringConstraints, ValidationError, field_validator, model_validator

from foundry_opt.bootstrap.canonical import canonical_sha256, safe_persisted_document
from foundry_opt.bootstrap.errors import BootstrapConfigError, BootstrapPlanError
from foundry_opt.models import FrozenModel
from foundry_opt.poc.config import (
    POCConfigurationError,
    load_strict_yaml_mapping,
    validate_repository_relative_path,
)

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
SemVer = Annotated[str, StringConstraints(pattern=r"^v[0-9]+(?:\.[0-9]+){0,2}$")]
VersionedId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9](?:[a-z0-9._-]*[a-z0-9])?@[A-Za-z0-9._-]+$")]
AgentId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")]
PhaseName = Literal["discovery", "repository", "providers", "evaluation", "apply", "verify", "rollback"]
BindingStateName = Literal["unbound", "planned", "applied", "verified", "failed", "rolled_back"]
EvaluatorProvenance = Literal["reused_existing", "auto_generated_unreviewed", "issue_supplied_existing"]
SemanticPatchOperation = Literal["replace", "insert_before", "insert_after", "delete"]

PROHIBITED_CONTENT_KEYS: frozenset[str] = frozenset({
    "prompt", "prompts", "response", "responses", "trace", "traces", "dataset_rows",
    "token", "tokens", "raw_prompt", "raw_response", "transcript", "content",
})
MAX_ISSUE_EVALUATORS = 8


def _ensure_mapping(value: object, *, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise BootstrapConfigError(f"{field} must be a mapping")
    if not all(isinstance(key, str) for key in value):
        raise BootstrapConfigError(f"{field} keys must be strings")
    return value


def _casefold_unique(values: Sequence[str], *, field: str) -> tuple[str, ...]:
    seen: dict[str, str] = {}
    ordered: list[str] = []
    for value in values:
        key = value.casefold()
        previous = seen.get(key)
        if previous is not None:
            raise BootstrapConfigError(f"{field} contains case-fold duplicate values: {previous!r} and {value!r}")
        seen[key] = value
        ordered.append(value)
    return tuple(ordered)


def _validate_safe_path(value: str, *, field: str) -> str:
    return validate_repository_relative_path(value, field=field)


def _normalize_weight_inputs(entries: Sequence["IssueEvaluatorRequestEntry"]) -> tuple[float, ...]:
    explicit = [entry.weight is not None for entry in entries]
    if not entries:
        return ()
    if any(explicit) and not all(explicit):
        raw = [entry.weight if entry.weight is not None else 1.0 for entry in entries]
    elif all(explicit):
        raw = [entry.weight if entry.weight is not None else 1.0 for entry in entries]
    else:
        raw = [1.0 for _entry in entries]
    total = sum(raw)
    return tuple(weight / total for weight in raw)


def _validate_weight(value: float | None, *, field: str) -> float | None:
    if value is None:
        return None
    if not math.isfinite(value) or value <= 0:
        raise BootstrapConfigError(f"{field} must be finite and positive")
    return value


def _reject_prohibited_mapping(value: Mapping[str, object], *, field: str) -> None:
    lowered = {key.casefold() for key in value}
    overlap = lowered & PROHIBITED_CONTENT_KEYS
    if overlap:
        raise BootstrapPlanError(f"{field} includes prohibited persisted content: {sorted(overlap)!r}")


def _jsonable(value: object) -> object:
    if isinstance(value, FrozenModel):
        return value.model_dump(mode='json')
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


class BootstrapDocument(FrozenModel):
    schema_version: Literal[1] = 1

    @classmethod
    def from_document(cls, document: str | bytes | Mapping[str, object]) -> Self:
        try:
            payload = load_strict_yaml_mapping(document, subject=cls.__name__)
            return cls.model_validate(payload)
        except POCConfigurationError as exc:
            raise BootstrapConfigError(str(exc)) from exc
        except ValidationError as exc:
            raise BootstrapConfigError(str(exc)) from exc
        except Exception as exc:
            if isinstance(exc, BootstrapConfigError):
                raise
            raise BootstrapConfigError(str(exc)) from exc


class GitHubSettings(BootstrapDocument):
    repository: str
    default_branch: str
    issue_label: str


class SharedIdentity(BootstrapDocument):
    tenant_id: str
    subscription_id: str
    project_id: str


class ManagedFileLockEntry(BootstrapDocument):
    path: str
    sha256: Sha256
    owner_agent_id: AgentId

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _validate_safe_path(value, field="path")


class CloudResourceOwnership(BootstrapDocument):
    resource_kind: str
    resource_id: str
    owner_agent_id: AgentId
    binding_state: BindingStateName


class BindingState(BootstrapDocument):
    agent_id: AgentId
    state: BindingStateName
    resource_ids: tuple[str, ...] = ()


class AgentSidecarGroup(BootstrapDocument):
    roots: tuple[str, ...]
    shared_source_with: tuple[AgentId, ...] = ()
    managed_files: tuple[ManagedFileLockEntry, ...] = ()
    bindings: tuple[BindingState, ...] = ()
    cloud_resources: tuple[CloudResourceOwnership, ...] = ()

    @field_validator("roots")
    @classmethod
    def validate_roots(cls, value: Sequence[str]) -> tuple[str, ...]:
        roots = tuple(_validate_safe_path(root, field="roots") for root in value)
        if not roots:
            raise BootstrapConfigError("roots must not be empty")
        return _casefold_unique(roots, field="roots")

    @model_validator(mode="after")
    def validate_overlaps(self) -> Self:
        for left in self.roots:
            for right in self.roots:
                if left == right:
                    continue
                if left.startswith(right + "/") or right.startswith(left + "/"):
                    if not self.shared_source_with:
                        raise BootstrapConfigError("overlapping roots require shared_source_with")
        return self


class ExplicitAgentEntry(BootstrapDocument):
    agent_id: AgentId
    role: str
    provider: str
    sidecar: AgentSidecarGroup


class DistributionSettings(BootstrapDocument):
    mode: Literal["frozen_v1"] = "frozen_v1"
    repository_defaults_ref: str
    github: GitHubSettings
    identity: SharedIdentity
    agents: tuple[ExplicitAgentEntry, ...]

    @model_validator(mode="after")
    def validate_agent_ids(self) -> Self:
        _casefold_unique([entry.agent_id for entry in self.agents], field="agents")
        return self


class RootRegistry(BootstrapDocument):
    distribution: DistributionSettings


class ImmutableDatasetReference(BootstrapDocument):
    dataset_id: VersionedId


class ImmutableDefinitionReference(BootstrapDocument):
    definition_id: VersionedId


class EvaluatorReference(BootstrapDocument):
    evaluator_id: VersionedId
    provenance: EvaluatorProvenance


class ScoreNormalizationContract(BootstrapDocument):
    minimum: float = 0.0
    maximum: float = 1.0
    normalized_range: tuple[float, float] = (0.0, 1.0)

    @model_validator(mode="after")
    def validate_range(self) -> Self:
        if (self.minimum, self.maximum) != (0.0, 1.0) or self.normalized_range != (0.0, 1.0):
            raise BootstrapConfigError("score normalization contract must remain 0-1")
        return self


class IssueEvaluatorRequestEntry(BootstrapDocument):
    evaluator: EvaluatorReference
    weight: float | None = None

    @field_validator("weight")
    @classmethod
    def validate_weight(cls, value: float | None) -> float | None:
        return _validate_weight(value, field="weight")


class IssueEvaluatorRequest(BootstrapDocument):
    primary_objective_only: Literal[True] = True
    evaluators: tuple[IssueEvaluatorRequestEntry, ...]

    @model_validator(mode="after")
    def validate_entries(self) -> Self:
        if not self.evaluators:
            raise BootstrapConfigError("evaluators must not be empty")
        if len(self.evaluators) > MAX_ISSUE_EVALUATORS:
            raise BootstrapConfigError("default issue evaluator bundle cannot exceed 8 evaluators")
        return self


class ResolvedWeightedObjective(BootstrapDocument):
    evaluators: tuple[IssueEvaluatorRequestEntry, ...]
    normalized_weights: tuple[float, ...]
    objective_hash: Sha256
    score_normalization: ScoreNormalizationContract = Field(default_factory=ScoreNormalizationContract)

    @classmethod
    def create(cls, entries: Sequence[IssueEvaluatorRequestEntry]) -> "ResolvedWeightedObjective":
        normalized = _normalize_weight_inputs(entries)
        payload = {
            "evaluators": [entry.model_dump(mode="json") for entry in entries],
            "normalized_weights": list(normalized),
            "score_normalization": ScoreNormalizationContract().model_dump(mode="json"),
        }
        return cls(
            evaluators=tuple(entries),
            normalized_weights=normalized,
            objective_hash=canonical_sha256(payload),
        )

    @model_validator(mode="after")
    def validate_hash(self) -> Self:
        payload = {
            "evaluators": [entry.model_dump(mode="json") for entry in self.evaluators],
            "normalized_weights": list(self.normalized_weights),
            "score_normalization": self.score_normalization.model_dump(mode="json"),
        }
        if self.objective_hash != canonical_sha256(payload):
            raise BootstrapConfigError("objective_hash does not match normalized objective payload")
        return self


class DefaultEvaluatorBundle(BootstrapDocument):
    objective: ResolvedWeightedObjective
    datasets: tuple[ImmutableDatasetReference, ...] = ()
    definitions: tuple[ImmutableDefinitionReference, ...] = ()


class SemanticPatchSpec(BootstrapDocument):
    target_path: str
    operation: SemanticPatchOperation
    match_text: str
    replacement_text: str | None = None

    @field_validator("target_path")
    @classmethod
    def validate_target_path(cls, value: str) -> str:
        return _validate_safe_path(value, field="target_path")


class TemplatePayloadSpec(BootstrapDocument):
    template_id: VersionedId
    destination_path: str
    payload: Mapping[str, object]
    semantic_patches: tuple[SemanticPatchSpec, ...] = ()

    @field_validator("destination_path")
    @classmethod
    def validate_destination_path(cls, value: str) -> str:
        return _validate_safe_path(value, field="destination_path")

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        _reject_prohibited_mapping(self.payload, field="payload")
        safe_persisted_document(self.model_dump(mode="json"))
        return self


class BootstrapAction(BootstrapDocument):
    action_id: str
    phase: PhaseName
    kind: str
    template_payload: TemplatePayloadSpec | None = None


class BootstrapPlan(BootstrapDocument):
    plan_id: str
    phases: tuple[PhaseName, ...]
    actions: tuple[BootstrapAction, ...]
    managed_files: tuple[ManagedFileLockEntry, ...] = ()
    plan_hash: Sha256

    @model_validator(mode="after")
    def validate_hash(self) -> Self:
        payload = self._hash_payload()
        expected = canonical_sha256(payload)
        if self.plan_hash != expected:
            raise BootstrapPlanError("plan_hash does not match canonical plan payload")
        return self

    def _hash_payload(self) -> dict[str, object]:
        payload = _jsonable(self.model_dump(mode="json", exclude={"schema_version", "plan_hash"}))
        _reject_prohibited_mapping(payload, field="plan")
        return payload

    @classmethod
    def create(cls, **values: object) -> "BootstrapPlan":
        payload = _jsonable(dict(values))
        _reject_prohibited_mapping(payload, field="plan")
        materialized = cls.model_construct(schema_version=1, plan_hash="0" * 64, **payload)
        hash_payload = materialized._hash_payload()
        payload["plan_hash"] = canonical_sha256(hash_payload)
        return cls.model_validate(payload)


class BootstrapReceipt(BootstrapDocument):
    plan_hash: Sha256 = '0' * 64
    applied_actions: tuple[str, ...]
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_hash(self) -> Self:
        payload = _jsonable(self.model_dump(mode="json", exclude={"schema_version", "receipt_hash"}))
        _reject_prohibited_mapping(payload, field="receipt")
        if self.receipt_hash != canonical_sha256(payload):
            raise BootstrapPlanError("receipt_hash does not match canonical receipt payload")
        return self

    @classmethod
    def create(cls, *, plan_hash: str, applied_actions: Sequence[str]) -> "BootstrapReceipt":
        payload = _jsonable({"plan_hash": plan_hash, "applied_actions": list(applied_actions)})
        payload["receipt_hash"] = canonical_sha256(payload)
        return cls.model_validate(payload)


__all__ = [name for name in globals() if not name.startswith("_")]
