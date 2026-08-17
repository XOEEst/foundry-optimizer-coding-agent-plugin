from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, ValidationError, field_validator, model_validator

from foundry_opt.bootstrap.canonical import canonical_sha256, safe_persisted_document
from foundry_opt.bootstrap.errors import BootstrapConfigError, BootstrapPlanError
from foundry_opt.models import FrozenModel
from foundry_opt.poc.config import (
    POCConfigurationError,
    _validate_resource_id,
    load_strict_yaml_mapping,
    validate_repository_relative_path,
    validate_repository_relative_paths,
)

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
AgentId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")]
AzureAIResourceUri = Annotated[
    str,
    StringConstraints(
        pattern=(
            r"^azureai://accounts/[^/]+/projects/[^/]+/"
            r"(?:evaluators|data|evaluationDefinitions)/[^/]+/versions/[^/]+$"
        )
    ),
]
ApplyPhase = Literal["repository", "github", "azure", "evaluations"]
OperationStage = Literal["planned", "applying", "verifying", "completed", "failed", "compensation_required"]
BindingClassification = Literal["bound-aligned", "bound-diverged", "bound-unknown", "ready-unbound", "not-ready"]
ResourceApplyState = Literal["planned", "created", "adopted", "updated", "verified", "failed", "skipped"]
EvaluatorProvenance = Literal["reused_existing", "auto_generated_unreviewed", "issue_supplied_existing"]
RuntimeKind = Literal["hosted"]
DeploymentEligibility = Literal["eligible", "ineligible", "unknown"]
IdentityKind = Literal["azure_subscription"]
OwnershipMode = Literal["owned", "shared-template", "adopted"]
ActivationOutcome = Literal["succeeded", "failed", "compensation_required", "unknown"]
SemanticPatchOperation = Literal["replace", "insert_before", "insert_after", "delete"]

PROHIBITED_CONTENT_KEYS: frozenset[str] = frozenset({
    "prompt", "prompts", "response", "responses", "trace", "traces", "dataset_rows",
    "token", "tokens", "raw_prompt", "raw_response", "transcript", "content",
})
MAX_ISSUE_EVALUATORS = 8
_PROJECT_ENDPOINT_RE = re.compile(r"^https://[^\s]+/api/projects/[^\s/]+/?$")


def _jsonable(value: object) -> object:
    if isinstance(value, FrozenModel):
        return value.model_dump(mode="json")
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, Mapping):
        return {key: _jsonable(item) for key, item in value.items()}
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


def _validate_project_endpoint(value: str, *, field: str) -> str:
    if _PROJECT_ENDPOINT_RE.fullmatch(value) is None:
        raise BootstrapConfigError(f"{field} must be an HTTPS Foundry project endpoint")
    return value


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


def _normalize_weight_inputs(entries: Sequence["IssueEvaluatorRequestEntry"]) -> tuple[float, ...]:
    explicit = [entry.weight is not None for entry in entries]
    if not entries:
        return ()
    raw = [entry.weight if entry.weight is not None else 1.0 for entry in entries] if any(explicit) else [1.0 for _ in entries]
    total = sum(raw)
    return tuple(weight / total for weight in raw)


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


class DistributionSettings(BootstrapDocument):
    repository: str
    channel: str
    pin: str | None = None
    optimizer_environment: str
    deployment_environment: str
    optimizer_client_id_variable: str
    deployment_client_id_variable: str


class IdentitySettings(BootstrapDocument):
    kind: IdentityKind
    resource_id: str

    @field_validator("resource_id")
    @classmethod
    def validate_resource_id(cls, value: str) -> str:
        return _validate_resource_id(value, "resource_id")


class ExplicitAgentEntry(BootstrapDocument):
    agent_id: AgentId
    root: str
    config_path: str
    enabled: bool = True

    @field_validator("root", "config_path")
    @classmethod
    def validate_paths(cls, value: str, info) -> str:
        return _validate_safe_path(value, field=info.field_name)


class RootRegistry(BootstrapDocument):
    distribution: DistributionSettings
    identity: IdentitySettings
    agents: tuple[ExplicitAgentEntry, ...]

    @model_validator(mode="after")
    def validate_agents(self) -> Self:
        _casefold_unique([agent.agent_id for agent in self.agents], field="agents")
        _casefold_unique([agent.root for agent in self.agents], field="agent roots")
        return self


class SharedSourceRelation(BootstrapDocument):
    agent_id: AgentId
    relation: Literal["shared-source"]


class RuntimeProtocolSettings(BootstrapDocument):
    kind: RuntimeKind
    entrypoint: tuple[str, ...]
    protocol_name: str
    protocol_version: str


class FoundryProjectSettings(BootstrapDocument):
    project_endpoint: str
    account_resource_id: str
    agent_name: str
    expected_version: str

    @field_validator("project_endpoint")
    @classmethod
    def validate_project_endpoint_field(cls, value: str) -> str:
        return _validate_project_endpoint(value, field="project_endpoint")

    @field_validator("account_resource_id")
    @classmethod
    def validate_account_resource_id(cls, value: str) -> str:
        return _validate_resource_id(value, "account_resource_id")


class DecisionPolicy(BootstrapDocument):
    minimum_aggregate_delta: float
    focused_cases_required: bool
    max_regressions: int


class ImmutableDatasetReference(BootstrapDocument):
    dataset_id: AzureAIResourceUri


class ImmutableDefinitionReference(BootstrapDocument):
    definition_id: AzureAIResourceUri


class EvaluatorReference(BootstrapDocument):
    evaluator_id: AzureAIResourceUri
    provenance: EvaluatorProvenance


class EvaluatorLineageEntry(BootstrapDocument):
    evaluator: EvaluatorReference
    source: Literal["default_bundle", "issue_request", "legacy_metadata"]


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
        return cls(evaluators=tuple(entries), normalized_weights=normalized, objective_hash=canonical_sha256(payload))

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
    datasets: tuple[ImmutableDatasetReference, ...]
    definitions: tuple[ImmutableDefinitionReference, ...]
    evaluator_lineage: tuple[EvaluatorLineageEntry, ...] = ()


class HardGuardrail(BootstrapDocument):
    evaluator_name: str
    required_pass_rate: float
    required: bool = True


class DeploymentSettings(BootstrapDocument):
    environment: str
    enabled: bool
    eligibility: DeploymentEligibility


class BootstrapSidecar(BootstrapDocument):
    repo_agent_id: AgentId
    source_root: str
    package_root: str
    editable_paths: tuple[str, ...]
    shared_source_relations: tuple[SharedSourceRelation, ...] = ()
    runtime: RuntimeProtocolSettings
    foundry_project: FoundryProjectSettings
    baseline_model: str
    allowed_models: tuple[str, ...]
    max_candidates: int
    decision_policy: DecisionPolicy
    development_dataset: ImmutableDatasetReference
    validating_dataset: ImmutableDatasetReference
    default_evaluator_bundle: DefaultEvaluatorBundle
    max_issue_evaluators: int = MAX_ISSUE_EVALUATORS
    hard_guardrails: tuple[HardGuardrail, ...]
    deployment: DeploymentSettings

    @field_validator("source_root", "package_root")
    @classmethod
    def validate_root_paths(cls, value: str, info) -> str:
        if info.field_name == 'package_root' and value == '.':
            return value
        return _validate_safe_path(value, field=info.field_name)

    @field_validator("editable_paths")
    @classmethod
    def validate_editable_paths(cls, value: Sequence[str]) -> tuple[str, ...]:
        return validate_repository_relative_paths(value, field="editable_paths", allow_glob=True)

    @model_validator(mode="after")
    def validate_source_relationships(self) -> Self:
        editable_roots = tuple(path.split('/')[0] for path in self.editable_paths)
        _casefold_unique(editable_roots, field="editable path roots")
        if self.max_issue_evaluators > MAX_ISSUE_EVALUATORS:
            raise BootstrapConfigError("max_issue_evaluators cannot exceed default ceiling of 8")
        return self


class ManagedFileEntry(BootstrapDocument):
    path: str
    ownership_mode: OwnershipMode
    template_base_sha256: Sha256
    applied_sha256: Sha256
    semantic_patch_id: str | None = None

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _validate_safe_path(value, field="path")


class GitHubEnvironmentLedgerEntry(BootstrapDocument):
    environment: str
    variable_names: tuple[str, ...]


class IdentityOwnershipLedgerEntry(BootstrapDocument):
    fic_resource_id: str | None = None
    rbac_resource_id: str | None = None
    foundry_resource_id: str | None = None

    @field_validator("fic_resource_id", "rbac_resource_id", "foundry_resource_id")
    @classmethod
    def validate_optional_resource_id(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _validate_resource_id(value, info.field_name)


class ActivationOutcomeRecord(BootstrapDocument):
    outcome: ActivationOutcome
    detail: str | None = None


class BootstrapLock(BootstrapDocument):
    engine: str
    runtime_repository: str
    channel: str
    exact_revision: str
    managed_files: tuple[ManagedFileEntry, ...]
    github_environments: tuple[GitHubEnvironmentLedgerEntry, ...]
    identity_ownership: tuple[IdentityOwnershipLedgerEntry, ...]
    sidecar_paths: tuple[str, ...]
    last_activation: ActivationOutcomeRecord

    @field_validator("sidecar_paths")
    @classmethod
    def validate_sidecar_paths(cls, value: Sequence[str]) -> tuple[str, ...]:
        return tuple(_validate_safe_path(item, field="sidecar_paths") for item in value)


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
    template_id: str
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
    phase: ApplyPhase
    stage: OperationStage
    kind: str
    target_agent_id: AgentId | None = None
    template_payload: TemplatePayloadSpec | None = None


class FingerprintRecord(BootstrapDocument):
    label: str
    sha256: Sha256


class RedactedStatusInfo(BootstrapDocument):
    code: str
    summary: str


class BootstrapPlan(BootstrapDocument):
    operation_id: str
    runtime_sha256: Sha256
    repository_identity: str
    actions: tuple[BootstrapAction, ...]
    plan_hash: Sha256

    @model_validator(mode="after")
    def validate_hash(self) -> Self:
        payload = self._hash_payload()
        if self.plan_hash != canonical_sha256(payload):
            raise BootstrapPlanError("plan_hash does not match canonical plan payload")
        return self

    def _hash_payload(self) -> dict[str, object]:
        payload = _jsonable(self.model_dump(mode="json", exclude={"plan_hash"}))
        _reject_prohibited_mapping(payload, field="plan")
        return payload

    @classmethod
    def create(cls, **values: object) -> "BootstrapPlan":
        payload = _jsonable(dict(values))
        _reject_prohibited_mapping(payload, field='plan')
        candidate = cls.model_construct(schema_version=1, plan_hash='0' * 64, **payload)
        hashed = canonical_sha256(candidate._hash_payload())
        return cls.model_validate({**payload, 'plan_hash': hashed})


class BootstrapReceipt(BootstrapDocument):
    operation_id: str
    runtime_sha256: Sha256
    repository_identity: str
    plan_hash: Sha256
    before_fingerprints: tuple[FingerprintRecord, ...] = ()
    after_fingerprints: tuple[FingerprintRecord, ...] = ()
    created_actions: tuple[str, ...] = ()
    adopted_actions: tuple[str, ...] = ()
    changed_actions: tuple[str, ...] = ()
    skipped_actions: tuple[str, ...] = ()
    compensation_required_actions: tuple[str, ...] = ()
    error_info: RedactedStatusInfo | None = None
    resume_info: RedactedStatusInfo | None = None
    receipt_hash: Sha256

    @model_validator(mode="after")
    def validate_hash(self) -> Self:
        payload = _jsonable(self.model_dump(mode="json", exclude={"receipt_hash"}))
        _reject_prohibited_mapping(payload, field="receipt")
        if self.receipt_hash != canonical_sha256(payload):
            raise BootstrapPlanError("receipt_hash does not match canonical receipt payload")
        return self

    @classmethod
    def create(cls, **values: object) -> "BootstrapReceipt":
        payload = _jsonable(dict(values))
        _reject_prohibited_mapping(payload, field='receipt')
        candidate = cls.model_construct(schema_version=1, receipt_hash='0' * 64, **payload)
        hash_payload = _jsonable(candidate.model_dump(mode='json', exclude={'receipt_hash'}))
        return cls.model_validate({**payload, 'receipt_hash': canonical_sha256(hash_payload)})


class LegacyMigrationProposal(BootstrapDocument):
    registry: RootRegistry
    sidecars: tuple[BootstrapSidecar, ...]
    actions: tuple[BootstrapAction, ...] = ()


__all__ = [name for name in globals() if not name.startswith("_")]
