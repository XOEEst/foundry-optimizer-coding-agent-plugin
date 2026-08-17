from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlparse

import json

from pydantic import Field, StringConstraints, ValidationError, field_validator, model_validator

from foundry_opt.bootstrap.canonical import canonical_sha256
from foundry_opt.bootstrap.contracts import BootstrapDocument
from foundry_opt.bootstrap.errors import BootstrapConfigError
from foundry_opt.poc.config import POCConfigurationError, _validate_resource_id, load_strict_yaml_mapping, validate_repository_relative_path

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
GitCommit = Annotated[str, StringConstraints(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]
RepositoryIdentity = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")]
RepositoryUrl = Annotated[str, StringConstraints(pattern=r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?$")]
Guid = Annotated[str, StringConstraints(pattern=r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")]
RoleDefinitionId = Annotated[str, StringConstraints(pattern=r"^/subscriptions/[A-Za-z0-9-]+/providers/Microsoft\.Authorization/roleDefinitions/[0-9a-fA-F-]{36}$")]
EnvironmentName = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9._-]+$")]
VariableName = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Z][A-Z0-9_]*$")]
ManifestId = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^[a-z0-9](?:[a-z0-9._/-]*[a-z0-9])?$")]
RepoRelativePath = Annotated[str, StringConstraints(min_length=1, max_length=240)]
AgentId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")]
ProjectEndpoint = Annotated[str, StringConstraints(max_length=300, pattern=r"^https://[^\s]+/api/projects/[^\s/]+/?$")]
GenerationMode = Literal['reuse_reviewed_sources', 'replace_reviewed_sources']
GenerationSourceKind = Literal['reviewed_file']
SemanticPatchMode = Literal['none', 'apply']
OwnershipMode = Literal['owned', 'shared-template', 'adopted']
ScopeName = Literal['repository', 'agent', 'shared-runtime']
PhaseName = Literal['repository', 'github', 'azure', 'evaluations']
DefaultBranchPolicyIntent = Literal['preserve_repository_default', 'require_main', 'require_explicit']
IdentityKind = Literal['user_assigned_managed_identity', 'entra_application', 'unresolved_migration']

_MAX_RENDER_CONTEXT_VALUE_BYTES = 4096
_MANIFEST_ALLOWED_EXTENSIONS = frozenset({'.yml', '.yaml', '.md', '.json'})
def _casefold_unique(values: Sequence[str], *, field: str) -> tuple[str, ...]:
    seen: dict[str, str] = {}
    result: list[str] = []
    for value in values:
        prior = seen.get(value.casefold())
        if prior is not None:
            raise BootstrapConfigError(f"{field} contains case-fold duplicate values: {prior!r} and {value!r}")
        seen[value.casefold()] = value
        result.append(value)
    return tuple(result)


def _validate_repo_path(value: str, *, field: str, allow_dot: bool = False) -> str:
    if allow_dot and value == '.':
        return '.'
    return validate_repository_relative_path(value, field=field)


def _normalize_guid(value: str, *, field: str) -> str:
    if not isinstance(value, str):
        raise BootstrapConfigError(f"{field} must be a string")
    lowered = value.lower()
    if lowered != value:
        raise BootstrapConfigError(f"{field} must be lowercase canonical GUID text")
    return lowered


def _validate_url(value: str, *, field: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != 'https' or not parsed.netloc or parsed.username or parsed.password or parsed.fragment:
        raise BootstrapConfigError(f"{field} must be a normalized https URL without credentials or fragments")
    if any(ch.isspace() for ch in value):
        raise BootstrapConfigError(f"{field} must not contain whitespace")
    return value


def _validate_render_context_value(value: Any, *, field: str) -> str | int | float | bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float('inf'), float('-inf')):
            raise BootstrapConfigError(f"{field} must be finite")
        return value
    if isinstance(value, str):
        encoded = value.encode('utf-8')
        if not encoded:
            raise BootstrapConfigError(f"{field} must not be empty")
        if len(encoded) > _MAX_RENDER_CONTEXT_VALUE_BYTES:
            raise BootstrapConfigError(f"{field} exceeds the size limit")
        return value
    raise BootstrapConfigError(f"{field} must be a scalar render context value")


class SelectedRepositoryTarget(BootstrapDocument):
    repoAgentId: AgentId
    root: RepoRelativePath
    config_path: RepoRelativePath

    @field_validator('root', 'config_path')
    @classmethod
    def _validate_path_fields(cls, value: str, info) -> str:
        return _validate_repo_path(value, field=info.field_name)


class RepositoryIdentityInput(BootstrapDocument):
    repository_id: RepositoryIdentity
    repository_url: RepositoryUrl
    default_branch: str
    root: RepoRelativePath = '.'
    selected: SelectedRepositoryTarget

    @field_validator('root')
    @classmethod
    def _validate_root(cls, value: str) -> str:
        return _validate_repo_path(value, field='root', allow_dot=True)

    @field_validator('default_branch')
    @classmethod
    def _validate_branch(cls, value: str) -> str:
        if not isinstance(value, str) or not value or len(value) > 255 or any(ch.isspace() for ch in value):
            raise BootstrapConfigError('default_branch must be a non-empty branch name without whitespace')
        return value


class RuntimeProvenanceInput(BootstrapDocument):
    repository_url: RepositoryUrl
    commit: GitCommit
    uv_lock_sha256: Sha256


class TemplateRenderContextValue(BootstrapDocument):
    key: Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r'^[A-Za-z][A-Za-z0-9_]*$')]
    value: str | int | float | bool

    @field_validator('value')
    @classmethod
    def _validate_value(cls, value: Any) -> str | int | float | bool:
        return _validate_render_context_value(value, field='value')


class TemplateManifestEntry(BootstrapDocument):
    template_id: ManifestId
    template_version: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    source_template_path: RepoRelativePath
    destination_path: Annotated[str, StringConstraints(min_length=1, max_length=240)]
    ownership_mode: OwnershipMode
    semantic_patch_id: Annotated[str | None, StringConstraints(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9._/-]+$')] = None
    semantic_patch_mode: SemanticPatchMode = 'none'
    scope: ScopeName
    required: bool
    render_context: tuple[TemplateRenderContextValue, ...] = ()

    @field_validator('source_template_path', 'destination_path')
    @classmethod
    def _validate_paths(cls, value: str, info) -> str:
        if info.field_name == 'destination_path' and '{selected.root}' in value:
            candidate = value.replace('{selected.root}', 'agent')
            validate_repository_relative_path(candidate, field=info.field_name)
            return value
        validated = _validate_repo_path(value, field=info.field_name)
        if info.field_name == 'source_template_path' and PurePosixPath(validated).suffix not in _MANIFEST_ALLOWED_EXTENSIONS:
            raise BootstrapConfigError('source_template_path must reference a supported managed template file')
        return validated

    @model_validator(mode='after')
    def _validate_entry(self) -> Self:
        if (self.semantic_patch_mode == 'apply') != (self.semantic_patch_id is not None):
            raise BootstrapConfigError('semantic_patch_mode and semantic_patch_id must be specified together')
        _casefold_unique([item.key for item in self.render_context], field=f'template manifest {self.template_id} render_context')
        return self


class RepositoryPhaseInput(BootstrapDocument):
    trusted_manifest_id: ManifestId
    trusted_manifest_version: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    managed_payloads: tuple[TemplateManifestEntry, ...]

    @model_validator(mode='after')
    def _validate_payloads(self) -> Self:
        if not self.managed_payloads:
            raise BootstrapConfigError('managed_payloads must not be empty')
        _casefold_unique([item.template_id for item in self.managed_payloads], field='managed_payloads.template_id')
        _casefold_unique([item.destination_path for item in self.managed_payloads], field='managed_payloads.destination_path')
        return self

    @property
    def manifest_hash(self) -> str:
        return canonical_sha256(self.model_dump(mode='json'))


class GitHubPhaseInput(BootstrapDocument):
    optimizer_environment: EnvironmentName
    deployment_environment: EnvironmentName
    shared_client_id: Guid
    client_id_variable_name: VariableName
    default_branch_policy_intent: DefaultBranchPolicyIntent

    @field_validator('shared_client_id')
    @classmethod
    def _validate_client_id(cls, value: str) -> str:
        return _normalize_guid(value, field='shared_client_id')


class ApprovedRoleAssignment(BootstrapDocument):
    alias: Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r'^[a-z0-9][a-z0-9._-]*$')]
    role_definition_id: RoleDefinitionId
    scope: Annotated[str, StringConstraints(min_length=1, max_length=512)]

    @field_validator('scope')
    @classmethod
    def _validate_scope(cls, value: str) -> str:
        _validate_resource_id(value, 'scope')
        return value


class AzurePhaseInput(BootstrapDocument):
    tenant_id: Guid
    subscription_id: Guid
    identity_kind: IdentityKind
    existing_resource_id: str | None = None
    existing_client_id: Guid | None = None
    existing_object_id: Guid | None = None
    resource_group: Annotated[str, StringConstraints(min_length=1, max_length=90, pattern=r'^[A-Za-z0-9._()/-]+$')]
    location: Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9]+$')]
    github_repository_id: RepositoryIdentity
    approved_role_assignments: tuple[ApprovedRoleAssignment, ...]

    @field_validator('tenant_id', 'subscription_id', 'existing_client_id', 'existing_object_id')
    @classmethod
    def _validate_guids(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _normalize_guid(value, field=info.field_name)

    @field_validator('existing_resource_id')
    @classmethod
    def _validate_existing_resource_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        _validate_resource_id(value, 'existing_resource_id')
        return value

    @model_validator(mode='after')
    def _validate_roles(self) -> Self:
        _casefold_unique([item.alias for item in self.approved_role_assignments], field='approved_role_assignments.alias')
        pairs = [f'{item.role_definition_id}@{item.scope}' for item in self.approved_role_assignments]
        _casefold_unique(pairs, field='approved_role_assignments')
        return self


class EvaluationGenerationSource(BootstrapDocument):
    kind: GenerationSourceKind
    path: RepoRelativePath

    @field_validator('path')
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return _validate_repo_path(value, field='path')


class EvaluationAgentInput(BootstrapDocument):
    repo_agent_id: AgentId
    sidecar_path: RepoRelativePath
    project_endpoint: ProjectEndpoint
    account_resource_id: str
    agent_name: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    agent_version: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    existing_dataset_ids: tuple[Annotated[str, StringConstraints(min_length=1, max_length=240)], ...] = ()
    existing_evaluator_ids: tuple[Annotated[str, StringConstraints(min_length=1, max_length=240)], ...] = ()
    existing_definition_ids: tuple[Annotated[str, StringConstraints(min_length=1, max_length=240)], ...] = ()
    generation_mode: GenerationMode
    generation_sources: tuple[EvaluationGenerationSource, ...]
    model_deployment: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    trace_window: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    connection_name: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    target_sample_count: Annotated[int, Field(ge=1, le=100000)]
    replacement_intent: bool

    @field_validator('sidecar_path')
    @classmethod
    def _validate_sidecar_path(cls, value: str) -> str:
        return _validate_repo_path(value, field='sidecar_path')

    @field_validator('account_resource_id')
    @classmethod
    def _validate_account(cls, value: str) -> str:
        return _validate_resource_id(value, 'account_resource_id')

    @field_validator('existing_dataset_ids', 'existing_evaluator_ids', 'existing_definition_ids')
    @classmethod
    def _validate_existing_ids(cls, value: Sequence[str], info) -> tuple[str, ...]:
        return _casefold_unique(tuple(value), field=info.field_name)

    @model_validator(mode='after')
    def _validate_generation(self) -> Self:
        if not self.generation_sources:
            raise BootstrapConfigError('generation_sources must not be empty')
        _casefold_unique([source.path for source in self.generation_sources], field=f'{self.repo_agent_id} generation_sources')
        return self


class EvaluationsPhaseInput(BootstrapDocument):
    agents: tuple[EvaluationAgentInput, ...]

    @model_validator(mode='after')
    def _validate_agents(self) -> Self:
        if not self.agents:
            raise BootstrapConfigError('evaluations.agents must not be empty')
        _casefold_unique([agent.repo_agent_id for agent in self.agents], field='evaluations.agents.repo_agent_id')
        return self


class BootstrapPlanInput(BootstrapDocument):
    repository: RepositoryIdentityInput
    runtime_provenance: RuntimeProvenanceInput
    repository_phase: RepositoryPhaseInput
    offline_plan: bool = False
    required_phases: tuple[PhaseName, ...] = ('repository',)
    github_phase: GitHubPhaseInput | None = None
    azure_phase: AzurePhaseInput | None = None
    evaluations_phase: EvaluationsPhaseInput | None = None

    @field_validator('required_phases')
    @classmethod
    def _validate_required_phases(cls, value: Sequence[str]) -> tuple[str, ...]:
        phases = tuple(value)
        if not phases:
            raise BootstrapConfigError('required_phases must not be empty')
        return _casefold_unique(phases, field='required_phases')

    @model_validator(mode='after')
    def _validate_phases(self) -> Self:
        selected = self.repository.selected
        if selected.root != '.' and not self.repository.root == '.':
            root_path = PurePosixPath(self.repository.root)
            if not str(PurePosixPath(selected.root)).startswith(str(root_path)):
                raise BootstrapConfigError('selected.root must be within repository.root')
        if self.runtime_provenance.repository_url != self.repository.repository_url:
            raise BootstrapConfigError('runtime_provenance.repository_url must match repository.repository_url')
        if self.azure_phase and self.azure_phase.github_repository_id != self.repository.repository_id:
            raise BootstrapConfigError('azure_phase.github_repository_id must match repository.repository_id')
        if self.offline_plan:
            forbidden = {'github': self.github_phase, 'azure': self.azure_phase, 'evaluations': self.evaluations_phase}
            present = [name for name, value in forbidden.items() if value is not None]
            if present:
                raise BootstrapConfigError(f'offline_plan forbids cloud phase inputs: {present!r}')
            cloud_required = [phase for phase in self.required_phases if phase != 'repository']
            if cloud_required:
                raise BootstrapConfigError(f'offline_plan cannot require cloud phases: {cloud_required!r}')
        phase_inputs = {
            'github': self.github_phase,
            'azure': self.azure_phase,
            'evaluations': self.evaluations_phase,
        }
        for phase in self.required_phases:
            if phase == 'repository':
                continue
            if phase_inputs[phase] is None:
                raise BootstrapConfigError(f'{phase}_phase inputs are required when phase {phase!r} is requested')
        if self.evaluations_phase is not None:
            selected_ids = {self.repository.selected.repoAgentId.casefold()}
            available_ids = {agent.repo_agent_id.casefold() for agent in self.evaluations_phase.agents}
            if not selected_ids <= available_ids:
                raise BootstrapConfigError('evaluations_phase must include the selected repoAgentId')
        return self

    @property
    def plan_input_hash(self) -> str:
        payload = self.model_dump(mode='json', exclude_none=True)
        return canonical_sha256(payload)

    @classmethod
    def from_document(cls, document: str | bytes | Mapping[str, object]) -> Self:
        try:
            return cls.model_validate(load_strict_yaml_mapping(document, subject='BootstrapPlanInput'))
        except POCConfigurationError as exc:
            raise BootstrapConfigError(str(exc)) from exc
        except ValidationError as exc:
            raise BootstrapConfigError(str(exc)) from exc


def load_bootstrap_plan_input(path: Path | str) -> BootstrapPlanInput:
    target = Path(path)
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise BootstrapConfigError(f'BootstrapPlanInput could not be read: {target}') from exc
    if target.suffix.casefold() == '.json':
        try:
            payload = json.loads(data.decode('utf-8'))
        except UnicodeDecodeError as exc:
            raise BootstrapConfigError('BootstrapPlanInput is not UTF-8 JSON') from exc
        except json.JSONDecodeError as exc:
            raise BootstrapConfigError('BootstrapPlanInput is not valid JSON') from exc
        if not isinstance(payload, Mapping):
            raise BootstrapConfigError('BootstrapPlanInput JSON must be a mapping')
        return BootstrapPlanInput.model_validate(dict(payload))
    return BootstrapPlanInput.from_document(data)


__all__ = ['BootstrapPlanInput', 'EvaluationAgentInput', 'EvaluationsPhaseInput', 'GitHubPhaseInput', 'AzurePhaseInput', 'RepositoryPhaseInput', 'TemplateManifestEntry', 'SelectedRepositoryTarget', 'RuntimeProvenanceInput', 'load_bootstrap_plan_input']
