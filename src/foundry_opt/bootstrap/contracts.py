from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, StringConstraints, ValidationError, field_validator, model_validator

from foundry_opt.bootstrap.canonical import canonical_sha256, safe_persisted_document
from foundry_opt.bootstrap.errors import BootstrapConfigError, BootstrapPlanError
from foundry_opt.models import FrozenModel
from foundry_opt.poc.config import POCConfigurationError, _validate_resource_id, load_strict_yaml_mapping, validate_repository_relative_path, validate_repository_relative_paths

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
GitCommit = Annotated[str, StringConstraints(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
RepositoryIdentity = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
RepositoryUrl = Annotated[str, StringConstraints(pattern=r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?$")]
AgentId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")]
DatasetUri = Annotated[str, StringConstraints(pattern=r"^azureai://accounts/[^/]+/projects/[^/]+/data/[^/]+/versions/[^/]+$")]
VersionedEvaluatorUri = Annotated[str, StringConstraints(pattern=r"^azureai://accounts/[^/]+/projects/[^/]+/evaluators/[^/]+/versions/[^/]+$")]
BuiltInEvaluatorId = Annotated[str, StringConstraints(pattern=r"^azureai://built-in/evaluators/[^/]+$")]
EvaluationDefinitionId = Annotated[str, StringConstraints(pattern=r"^(?:[A-Za-z0-9][A-Za-z0-9._-]*|azureai://accounts/[^/]+/projects/[^/]+/evaluationDefinitions/[^/]+/versions/[^/]+)$")]
ApplyPhase = Literal["repository", "github", "azure", "evaluations"]
OperationStage = Literal["planned", "applying", "verifying", "completed", "failed", "compensation_required"]
BindingClassification = Literal["bound-aligned", "bound-diverged", "bound-unknown", "ready-unbound", "not-ready"]
EvaluatorProvenance = Literal["reused_existing", "auto_generated_unreviewed", "issue_supplied_existing"]
IdentityKind = Literal["user_assigned_managed_identity", "entra_application", "unresolved_migration"]
RuntimeKind = Literal["hosted"]
OwnershipMode = Literal["owned", "shared-template", "adopted"]
OwnerScope = Literal["repository", "agent", "shared-runtime"]
ActivationOutcome = Literal["succeeded", "failed", "compensation_required", "unknown"]
SemanticPatchOperation = Literal["replace", "insert_before", "insert_after", "delete"]
NormalizationKind = Literal["pass_fail", "scalar"]

PROHIBITED_CONTENT_KEYS = frozenset({"prompt", "prompts", "response", "responses", "trace", "traces", "dataset_rows", "token", "tokens", "raw_prompt", "raw_response", "transcript", "content"})
MAX_ISSUE_EVALUATORS = 8
_PROJECT_ENDPOINT_RE = re.compile(r"^https://[^\s]+/api/projects/[^\s/]+/?$")


def _jsonable(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode='json')
    if isinstance(value, tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, list):
        return [_jsonable(v) for v in value]
    if isinstance(value, Mapping):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def _casefold_unique(values: Sequence[str], *, field: str) -> tuple[str, ...]:
    seen: dict[str, str] = {}
    out: list[str] = []
    for value in values:
        key = value.casefold()
        if key in seen:
            raise BootstrapConfigError(f"{field} contains case-fold duplicate values: {seen[key]!r} and {value!r}")
        seen[key] = value
        out.append(value)
    return tuple(out)


def _validate_safe_path(value: str, *, field: str, allow_dot: bool = False) -> str:
    if allow_dot and value == '.':
        return value
    return validate_repository_relative_path(value, field=field)


def _validate_project_endpoint(value: str, *, field: str) -> str:
    if _PROJECT_ENDPOINT_RE.fullmatch(value) is None:
        raise BootstrapConfigError(f"{field} must be an HTTPS Foundry project endpoint")
    return value


def _reject_prohibited_mapping(value: Mapping[str, object], *, field: str) -> None:
    overlap = {k.casefold() for k in value} & PROHIBITED_CONTENT_KEYS
    if overlap:
        raise BootstrapPlanError(f"{field} includes prohibited persisted content: {sorted(overlap)!r}")


def _validate_weight(value: float | None, *, field: str) -> float | None:
    if value is None:
        return None
    if not math.isfinite(value) or value <= 0:
        raise BootstrapConfigError(f"{field} must be finite and positive")
    return value


class BootstrapDocument(FrozenModel):
    schema_version: Literal[1] = 1

    @classmethod
    def from_document(cls, document: str | bytes | Mapping[str, object]) -> Self:
        try:
            return cls.model_validate(load_strict_yaml_mapping(document, subject=cls.__name__))
        except POCConfigurationError as exc:
            raise BootstrapConfigError(str(exc)) from exc
        except ValidationError as exc:
            raise BootstrapConfigError(str(exc)) from exc


class DistributionSettings(BootstrapDocument):
    repository: RepositoryUrl
    channel: str
    pin: GitCommit | None = None


class GitHubSettings(BootstrapDocument):
    optimizer_environment: str
    deployment_environment: str
    client_id_variable: str


class IdentitySettings(BootstrapDocument):
    kind: IdentityKind
    resource_id: str | None = None
    client_id: str | None = None

    @model_validator(mode='after')
    def validate_identity(self) -> Self:
        if self.kind == 'user_assigned_managed_identity':
            if self.resource_id is None:
                raise BootstrapConfigError('user_assigned_managed_identity requires resource_id')
            _validate_resource_id(self.resource_id, 'resource_id')
        elif self.kind == 'entra_application':
            if not self.client_id:
                raise BootstrapConfigError('entra_application requires client_id')
        elif self.kind == 'unresolved_migration':
            if self.resource_id is not None or self.client_id is not None:
                raise BootstrapConfigError('unresolved_migration cannot set resource_id or client_id')
        return self


class ExplicitAgentEntry(BootstrapDocument):
    agent_id: AgentId
    root: str
    config_path: str
    enabled: bool = True

    @field_validator('root', 'config_path')
    @classmethod
    def validate_paths(cls, value: str, info) -> str:
        return _validate_safe_path(value, field=info.field_name)


class RootRegistry(BootstrapDocument):
    distribution: DistributionSettings
    github: GitHubSettings
    identity: IdentitySettings
    agents: tuple[ExplicitAgentEntry, ...]

    @model_validator(mode='after')
    def validate_agents(self) -> Self:
        _casefold_unique([a.agent_id for a in self.agents], field='agents')
        return self


class BindingAssessment(BootstrapDocument):
    agent_id: AgentId
    classification: BindingClassification
    detail: str | None = None


class SharedSourceRelation(BootstrapDocument):
    agent_id: AgentId
    relation: Literal['shared-source']


class RuntimeProtocolSettings(BootstrapDocument):
    kind: RuntimeKind
    runtime: str
    entrypoint: tuple[str, ...]
    dependency_resolution: str
    protocol_name: str
    protocol_version: str
    cpu: str | None = None
    memory: str | None = None
    model_environment_variable: str | None = None


class FoundryProjectSettings(BootstrapDocument):
    project_endpoint: str
    account_resource_id: str
    agent_name: str
    expected_version: str | None = None
    model_deployment_aliases: tuple[str, ...] = ()

    @field_validator('project_endpoint')
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        return _validate_project_endpoint(value, field='project_endpoint')

    @field_validator('account_resource_id')
    @classmethod
    def validate_arm_id(cls, value: str) -> str:
        return _validate_resource_id(value, 'account_resource_id')


class DecisionPolicy(BootstrapDocument):
    minimum_aggregate_delta: float
    focused_cases_required: bool
    max_regressions: int


class EvaluatorNormalization(BootstrapDocument):
    kind: NormalizationKind
    source_min: float | None = None
    source_max: float | None = None

    @model_validator(mode='after')
    def validate_bounds(self) -> Self:
        if self.kind == 'pass_fail':
            if self.source_min is not None or self.source_max is not None:
                raise BootstrapConfigError('pass_fail normalization cannot carry scalar bounds')
        else:
            if self.source_min is None or self.source_max is None or self.source_max <= self.source_min:
                raise BootstrapConfigError('scalar normalization requires increasing source_min/source_max')
        return self


class ImmutableDatasetReference(BootstrapDocument):
    dataset_id: DatasetUri


class ImmutableDefinitionReference(BootstrapDocument):
    definition_id: EvaluationDefinitionId


class EvaluatorReference(BootstrapDocument):
    evaluator_id: VersionedEvaluatorUri | BuiltInEvaluatorId
    provenance: EvaluatorProvenance


class ResolvedEvaluator(BootstrapDocument):
    reference: EvaluatorReference
    normalization: EvaluatorNormalization
    weight: float

    @field_validator('weight')
    @classmethod
    def validate_weight(cls, value: float) -> float:
        checked = _validate_weight(value, field='weight')
        assert checked is not None
        return checked


class IssueEvaluatorRequestEntry(BootstrapDocument):
    evaluator_id: VersionedEvaluatorUri | BuiltInEvaluatorId
    weight: float | None = None

    @field_validator('weight')
    @classmethod
    def validate_weight_field(cls, value: float | None) -> float | None:
        return _validate_weight(value, field='weight')


class IssueEvaluatorRequest(BootstrapDocument):
    primary_objective_only: Literal[True] = True
    evaluators: tuple[IssueEvaluatorRequestEntry, ...]

    @model_validator(mode='after')
    def validate_entries(self) -> Self:
        if not self.evaluators:
            raise BootstrapConfigError('evaluators must not be empty')
        if len(self.evaluators) > MAX_ISSUE_EVALUATORS:
            raise BootstrapConfigError('default issue evaluator bundle cannot exceed 8 evaluators')
        return self


class ResolvedWeightedObjective(BootstrapDocument):
    evaluators: tuple[ResolvedEvaluator, ...]
    objective_hash: Sha256

    @classmethod
    def create(cls, evaluators: Sequence[ResolvedEvaluator]) -> 'ResolvedWeightedObjective':
        if not evaluators:
            raise BootstrapConfigError('evaluators must not be empty')
        weights = [e.weight for e in evaluators]
        total = sum(weights)
        normalized = tuple(e.model_copy(update={'weight': e.weight / total}) for e in evaluators)
        payload = {'evaluators': [entry.model_dump(mode='json') for entry in normalized]}
        return cls(evaluators=normalized, objective_hash=canonical_sha256(payload))

    @model_validator(mode='after')
    def validate_hash(self) -> Self:
        payload = {'evaluators': [entry.model_dump(mode='json') for entry in self.evaluators]}
        if self.objective_hash != canonical_sha256(payload):
            raise BootstrapConfigError('objective_hash does not match normalized objective payload')
        return self


class DefaultEvaluatorBundle(BootstrapDocument):
    objective: ResolvedWeightedObjective
    datasets: tuple[ImmutableDatasetReference, ...]
    definitions: tuple[ImmutableDefinitionReference, ...]


class HardGuardrail(BootstrapDocument):
    evaluator_name: str
    required_pass_rate: float
    required: bool = True


class DeploymentSettings(BootstrapDocument):
    environment: str
    enabled: bool
    require_aligned_binding: bool


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
    min_candidates: int
    max_candidates: int
    primary_metric: str
    decision_policy: DecisionPolicy
    development_dataset: ImmutableDatasetReference
    validating_dataset: ImmutableDatasetReference
    development_definition: ImmutableDefinitionReference
    validating_definition: ImmutableDefinitionReference
    default_evaluator_bundle: DefaultEvaluatorBundle
    max_issue_evaluators: int = MAX_ISSUE_EVALUATORS
    hard_guardrails: tuple[HardGuardrail, ...]
    deployment: DeploymentSettings

    @field_validator('source_root')
    @classmethod
    def validate_source_root(cls, value: str) -> str:
        return _validate_safe_path(value, field='source_root')

    @field_validator('package_root')
    @classmethod
    def validate_package_root(cls, value: str) -> str:
        return _validate_safe_path(value, field='package_root', allow_dot=True)

    @field_validator('editable_paths')
    @classmethod
    def validate_editable_paths(cls, value: Sequence[str]) -> tuple[str, ...]:
        return validate_repository_relative_paths(value, field='editable_paths', allow_glob=True)

    @model_validator(mode='after')
    def validate_sidecar(self) -> Self:
        if self.min_candidates <= 0 or self.max_candidates <= 0 or self.min_candidates > self.max_candidates:
            raise BootstrapConfigError('candidate bounds must be positive and ordered')
        if not 1 <= self.max_issue_evaluators <= MAX_ISSUE_EVALUATORS:
            raise BootstrapConfigError('max_issue_evaluators must be between 1 and 8')
        bundle_dataset_ids = {item.dataset_id for item in self.default_evaluator_bundle.datasets}
        explicit_dataset_ids = {self.development_dataset.dataset_id, self.validating_dataset.dataset_id}
        if bundle_dataset_ids != explicit_dataset_ids:
            raise BootstrapConfigError('default bundle datasets must match explicit development/validating datasets')
        bundle_definition_ids = {item.definition_id for item in self.default_evaluator_bundle.definitions}
        explicit_definition_ids = {self.development_definition.definition_id, self.validating_definition.definition_id}
        if bundle_definition_ids != explicit_definition_ids:
            raise BootstrapConfigError('default bundle definitions must match explicit development/validating definitions')
        if not self.hard_guardrails:
            raise BootstrapConfigError('hard_guardrails must not be empty')
        return self


class CloudResourceLedgerEntry(BootstrapDocument):
    provider: str
    kind: str
    resource_id: str
    ownership: Literal['created', 'adopted']
    action_id: str


class ManagedFileEntry(BootstrapDocument):
    path: str
    ownership_mode: OwnershipMode
    owner_scope: OwnerScope
    template_id: str
    template_base_sha256: Sha256
    applied_sha256: Sha256
    semantic_patch_id: str | None = None

    @field_validator('path')
    @classmethod
    def validate_path(cls, value: str) -> str:
        return _validate_safe_path(value, field='path')


class GitHubEnvironmentLedgerEntry(BootstrapDocument):
    environment: str
    variable_names: tuple[str, ...]


class ActivationOutcomeRecord(BootstrapDocument):
    outcome: ActivationOutcome
    detail: str | None = None


class BootstrapLock(BootstrapDocument):
    engine: str
    runtime_repository: RepositoryUrl
    channel: str
    runtime_commit: GitCommit
    managed_files: tuple[ManagedFileEntry, ...]
    github_environments: tuple[GitHubEnvironmentLedgerEntry, ...]
    cloud_resources: tuple[CloudResourceLedgerEntry, ...]
    sidecar_paths: tuple[str, ...]
    last_activation: ActivationOutcomeRecord

    @field_validator('sidecar_paths')
    @classmethod
    def validate_sidecar_paths(cls, value: Sequence[str]) -> tuple[str, ...]:
        return tuple(_validate_safe_path(v, field='sidecar_paths') for v in value)


class SemanticPatchSpec(BootstrapDocument):
    target_path: str
    operation: SemanticPatchOperation
    match_text: str
    replacement_text: str | None = None

    @field_validator('target_path')
    @classmethod
    def validate_target(cls, value: str) -> str:
        return _validate_safe_path(value, field='target_path')


class TemplatePayloadSpec(BootstrapDocument):
    template_id: str
    destination_path: str
    rendered_template: str
    semantic_patches: tuple[SemanticPatchSpec, ...] = ()

    @field_validator('destination_path')
    @classmethod
    def validate_destination(cls, value: str) -> str:
        return _validate_safe_path(value, field='destination_path')

    @field_validator('rendered_template')
    @classmethod
    def validate_rendered_template(cls, value: str) -> str:
        if not value:
            raise BootstrapConfigError('rendered_template must not be empty')
        if len(value.encode('utf-8')) > 1024 * 1024:
            raise BootstrapConfigError('rendered_template exceeds the size limit')
        if any(ord(character) < 32 and character not in '\t\n\r' for character in value):
            raise BootstrapConfigError('rendered_template contains control characters')
        if '\x7f' in value:
            raise BootstrapConfigError('rendered_template contains control characters')
        return value

    @model_validator(mode='after')
    def validate_payload(self) -> Self:
        for patch in self.semantic_patches:
            if patch.target_path != self.destination_path:
                raise BootstrapConfigError('semantic patch target must match destination_path')
        return self


class BootstrapAction(BootstrapDocument):
    action_id: str
    phase: ApplyPhase
    stage: OperationStage
    kind: str
    target_agent_id: AgentId | None = None
    template_payload: TemplatePayloadSpec | None = None
    diagnostics: tuple[str, ...] = ()


class FingerprintRecord(BootstrapDocument):
    label: str
    sha256: Sha256


class RedactedStatusInfo(BootstrapDocument):
    code: str
    summary: str


class BootstrapPlanPayload(BootstrapDocument):
    operation_id: str
    runtime_repository: RepositoryUrl
    runtime_commit: GitCommit
    repository_identity: RepositoryIdentity
    actions: tuple[BootstrapAction, ...]


class BootstrapPlan(BootstrapPlanPayload):
    plan_hash: Sha256

    @model_validator(mode='after')
    def validate_hash(self) -> Self:
        payload = BootstrapPlanPayload.model_validate(self.model_dump(mode='python', exclude={'plan_hash'}))
        if self.plan_hash != canonical_sha256(payload.model_dump(mode='json')):
            raise BootstrapPlanError('plan_hash does not match canonical plan payload')
        return self

    @classmethod
    def create(cls, **values: object) -> 'BootstrapPlan':
        payload = _jsonable(dict(values))
        _reject_prohibited_mapping(payload, field='plan')
        validated = BootstrapPlanPayload.model_validate(payload)
        return cls.model_validate({**validated.model_dump(mode='json'), 'plan_hash': canonical_sha256(validated.model_dump(mode='json'))})


class BootstrapReceiptPayload(BootstrapDocument):
    operation_id: str
    runtime_repository: RepositoryUrl
    runtime_commit: GitCommit
    repository_identity: RepositoryIdentity
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


class BootstrapReceipt(BootstrapReceiptPayload):
    receipt_hash: Sha256

    @model_validator(mode='after')
    def validate_hash(self) -> Self:
        payload = BootstrapReceiptPayload.model_validate(self.model_dump(mode='python', exclude={'receipt_hash'}))
        if self.receipt_hash != canonical_sha256(payload.model_dump(mode='json')):
            raise BootstrapPlanError('receipt_hash does not match canonical receipt payload')
        return self

    @classmethod
    def create(cls, **values: object) -> 'BootstrapReceipt':
        payload = _jsonable(dict(values))
        _reject_prohibited_mapping(payload, field='receipt')
        validated = BootstrapReceiptPayload.model_validate(payload)
        return cls.model_validate({**validated.model_dump(mode='json'), 'receipt_hash': canonical_sha256(validated.model_dump(mode='json'))})


class LegacyMigrationProposal(BootstrapDocument):
    registry: RootRegistry
    sidecars: tuple[BootstrapSidecar, ...]
    actions: tuple[BootstrapAction, ...] = ()


__all__ = [name for name in globals() if not name.startswith('_')]
