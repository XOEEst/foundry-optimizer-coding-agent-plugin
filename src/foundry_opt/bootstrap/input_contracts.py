from __future__ import annotations

import json
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlparse

from pydantic import Field, StrictBool, StrictInt, StringConstraints, ValidationError, field_validator, model_validator

from foundry_opt.bootstrap.canonical import canonical_sha256
from foundry_opt.bootstrap.contracts import (
    BootstrapDocument,
    BuiltInEvaluatorId,
    DatasetUri,
    EvaluationDefinitionId,
    IdentityKind,
    VersionedEvaluatorUri,
)
from foundry_opt.bootstrap.errors import BootstrapConfigError
from foundry_opt.optimize_job.safety import UnsafeCheckpointContentError, assert_safe_persisted_string
from foundry_opt.poc.config import POCConfigurationError, _validate_resource_id, load_strict_yaml_mapping, validate_repository_relative_path, validate_repository_relative_paths

Sha256 = Annotated[str, StringConstraints(pattern=r'^[0-9a-f]{64}$')]
GitCommit = Annotated[str, StringConstraints(pattern=r'^(?:[0-9a-f]{40}|[0-9a-f]{64})$')]
RepositoryIdentity = Annotated[str, StringConstraints(pattern=r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')]
RepositoryUrl = Annotated[str, StringConstraints(pattern=r'^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?$')]
Guid = Annotated[str, StringConstraints(pattern=r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')]
RoleDefinitionId = Annotated[str, StringConstraints(pattern=r'^/subscriptions/[0-9a-fA-F-]+/providers/Microsoft\.Authorization/roleDefinitions/[0-9a-fA-F-]{36}$')]
EnvironmentName = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9._-]+$')]
VariableName = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r'^[A-Z][A-Z0-9_]*$')]
ManifestId = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r'^[a-z0-9](?:[a-z0-9._/-]*[a-z0-9])?$')]
RepoRelativePath = Annotated[str, StringConstraints(min_length=1, max_length=240)]
AgentId = Annotated[str, StringConstraints(pattern=r'^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$')]
ProjectEndpoint = Annotated[str, StringConstraints(max_length=300, pattern=r'^https://[^\s]+/api/projects/[^\s/]+/?$')]
GenerationMode = Literal['reuse_reviewed_sources', 'replace_reviewed_sources']
GenerationSourceKind = Literal['reviewed_file']
SemanticPatchMode = Literal['none', 'apply']
OwnershipMode = Literal['owned', 'shared-template', 'adopted']
ScopeName = Literal['repository', 'agent', 'shared-runtime']
PhaseName = Literal['repository', 'github', 'azure', 'evaluations']
DefaultBranchPolicyIntent = Literal['preserve_repository_default', 'require_main', 'require_explicit']
ManifestHash = Sha256
EvaluationIdentifier = VersionedEvaluatorUri | BuiltInEvaluatorId

_MAX_FREEFORM_BYTES = 4096
_MAX_ITEMS = 32
_PROHIBITED_PATH_PARTS = ('env', 'credential', 'credentials', 'trace', 'traces', 'dataset', 'datasets', 'secret', 'secrets', 'token', 'tokens')
_ALLOWED_ROLE_DEFINITION_IDS = frozenset({
    '00000000-0000-0000-0000-000000000001',
    '00000000-0000-0000-0000-000000000002',
    '00000000-0000-0000-0000-000000000003',
})
_REQUIRED_MANAGED_PAYLOADS = (
    ('registry', '.foundry-opt/registry.yaml'),
    ('sidecar', '{selected.root}/.foundry/foundry-opt.yaml'),
    ('optimizer-instruction', '.github/instructions/foundry-opt.instructions.md'),
    ('optimizer-issue-form', '.github/ISSUE_TEMPLATE/foundry-optimize-agent.yml'),
    ('custom-agent', '.github/agents/foundry-optimizer.agent.md'),
    ('setup-semantic-patch', '.github/workflows/copilot-setup-steps.yml'),
    ('validation-workflow', '.github/workflows/foundry-opt-validation.yml'),
    ('deploy-workflow', '.github/workflows/foundry-opt-deploy.yml'),
    ('bootstrap-lock', '.github/foundry-opt.lock.yml'),
)
_MANIFEST_PATH = Path(__file__).resolve().parents[1] / 'templates' / 'customer-repo' / '.foundry-opt' / 'managed-payloads.manifest.yaml'


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f'duplicate key: {key}')
        result[key] = value
    return result


def _assert_safe_text(value: str, *, field: str, limit: int = _MAX_FREEFORM_BYTES) -> str:
    try:
        assert_safe_persisted_string(value, field=field, limit=limit)
    except UnsafeCheckpointContentError as exc:
        raise BootstrapConfigError(str(exc)) from exc
    return value


def _casefold_unique(values: Sequence[str], *, field: str) -> tuple[str, ...]:
    seen: dict[str, str] = {}
    ordered: list[str] = []
    for value in values:
        prior = seen.get(value.casefold())
        if prior is not None:
            raise BootstrapConfigError(f'{field} contains case-fold duplicate values: {prior!r} and {value!r}')
        seen[value.casefold()] = value
        ordered.append(value)
    return tuple(sorted(ordered, key=lambda item: (item.casefold(), item)))


def _validate_repo_path(value: str, *, field: str, allow_dot: bool = False) -> str:
    if allow_dot and value == '.':
        return '.'
    return validate_repository_relative_path(value, field=field)


def _normalize_guid(value: str, *, field: str) -> str:
    if not isinstance(value, str):
        raise BootstrapConfigError(f'{field} must be a string')
    lowered = value.lower()
    if lowered != value:
        raise BootstrapConfigError(f'{field} must be lowercase canonical GUID text')
    return lowered


def _path_is_within(root: str, child: str) -> bool:
    root_parts = PurePosixPath(root).parts
    child_parts = PurePosixPath(child).parts
    return len(child_parts) >= len(root_parts) and child_parts[:len(root_parts)] == root_parts


def _validate_public_github_url(value: str, *, field: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise BootstrapConfigError(f'{field} must be a canonical public https URL without userinfo, query, or fragment')
    if parsed.netloc != 'github.com':
        raise BootstrapConfigError(f'{field} must target github.com')
    if any(ch.isspace() for ch in value):
        raise BootstrapConfigError(f'{field} must not contain whitespace')
    return value


def _github_identity_from_url(value: str) -> str:
    parsed = urlparse(value)
    path = parsed.path.removesuffix('.git').strip('/')
    parts = path.split('/')
    if len(parts) != 2 or not all(parts):
        raise BootstrapConfigError('repository URL must contain exactly owner/repo')
    return f'{parts[0]}/{parts[1]}'


def _validate_freeform_scalar(value: object, *, field: str) -> str | int | float | bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if value != value or value in (float('inf'), float('-inf')):
            raise BootstrapConfigError(f'{field} must be finite')
        return value
    if isinstance(value, str):
        return _assert_safe_text(value, field=field)
    raise BootstrapConfigError(f'{field} must be a strict scalar value')


def _canonical_phase_order(phases: Sequence[str]) -> tuple[str, ...]:
    normalized = _casefold_unique(tuple(phases), field='required_phases')
    ordering = {'repository': 0, 'github': 1, 'azure': 2, 'evaluations': 3}
    return tuple(sorted(normalized, key=lambda item: ordering[item]))


class SelectedAgent(BootstrapDocument):
    repo_agent_id: AgentId
    root: Annotated[str, StringConstraints(min_length=1, max_length=240)]
    config_path: RepoRelativePath
    editable_paths: tuple[RepoRelativePath, ...]

    @field_validator('root')
    @classmethod
    def _validate_root(cls, value: str) -> str:
        return _validate_repo_path(value, field='root', allow_dot=True)

    @field_validator('config_path')
    @classmethod
    def _validate_config_path(cls, value: str) -> str:
        return _validate_repo_path(value, field='config_path')

    @field_validator('editable_paths')
    @classmethod
    def _validate_editable_paths(cls, value: Sequence[str]) -> tuple[str, ...]:
        validated = validate_repository_relative_paths(value, field='editable_paths', allow_glob=True)
        return _casefold_unique(validated, field='editable_paths')

    @model_validator(mode='after')
    def _validate_paths(self) -> Self:
        if self.root != '.' and not _path_is_within(self.root, self.config_path):
            raise BootstrapConfigError('config_path must be within selected agent root')
        return self


class RepositoryIdentityInput(BootstrapDocument):
    repository_id: RepositoryIdentity
    repository_url: RepositoryUrl
    default_branch: Annotated[str, StringConstraints(min_length=1, max_length=255)]
    root: Annotated[str, StringConstraints(min_length=1, max_length=240)] = '.'
    selected_agents: tuple[SelectedAgent, ...]

    @field_validator('repository_url')
    @classmethod
    def _validate_repository_url(cls, value: str) -> str:
        return _validate_public_github_url(value, field='repository_url')

    @field_validator('default_branch')
    @classmethod
    def _validate_branch(cls, value: str) -> str:
        if any(ch.isspace() for ch in value):
            raise BootstrapConfigError('default_branch must not contain whitespace')
        return _assert_safe_text(value, field='default_branch', limit=255)

    @field_validator('root')
    @classmethod
    def _validate_root(cls, value: str) -> str:
        return _validate_repo_path(value, field='root', allow_dot=True)

    @model_validator(mode='after')
    def _validate_identity(self) -> Self:
        if _github_identity_from_url(self.repository_url).casefold() != self.repository_id.casefold():
            raise BootstrapConfigError('repository_url must canonicalize to repository_id')
        if not self.selected_agents:
            raise BootstrapConfigError('selected_agents must not be empty')
        ordered_ids = _casefold_unique([agent.repo_agent_id for agent in self.selected_agents], field='selected_agents.repo_agent_id')
        if tuple(agent.repo_agent_id for agent in self.selected_agents) != ordered_ids:
            object.__setattr__(self, 'selected_agents', tuple(sorted(self.selected_agents, key=lambda item: (item.repo_agent_id.casefold(), item.repo_agent_id))))
        for agent in self.selected_agents:
            if self.root != '.' and not _path_is_within(self.root, agent.root):
                raise BootstrapConfigError('selected_agents root must be within repository root')
        return self


class RuntimeProvenanceInput(BootstrapDocument):
    runtime_repository_url: RepositoryUrl
    runtime_commit: GitCommit
    uv_lock_sha256: Sha256

    @field_validator('runtime_repository_url')
    @classmethod
    def _validate_runtime_url(cls, value: str) -> str:
        return _validate_public_github_url(value, field='runtime_repository_url')


class TemplateRenderValue(BootstrapDocument):
    key: Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r'^[A-Za-z][A-Za-z0-9_]*$')]
    value: str | StrictInt | float | StrictBool

    @field_validator('key')
    @classmethod
    def _validate_key(cls, value: str) -> str:
        return _assert_safe_text(value, field='render key', limit=128)

    @field_validator('value')
    @classmethod
    def _validate_value(cls, value: object) -> str | int | float | bool:
        return _validate_freeform_scalar(value, field='render value')


class TrustedManifestPayload(BootstrapDocument):
    template_id: ManifestId
    template_version: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    source_template_path: RepoRelativePath
    destination_path: Annotated[str, StringConstraints(min_length=1, max_length=240)]
    ownership_mode: OwnershipMode
    semantic_patch_id: Annotated[str | None, StringConstraints(min_length=1, max_length=128, pattern=r'^[A-Za-z0-9._/-]+$')] = None
    semantic_patch_mode: SemanticPatchMode = 'none'
    scope: ScopeName
    required: StrictBool

    @field_validator('source_template_path')
    @classmethod
    def _validate_source_path(cls, value: str) -> str:
        return _validate_repo_path(value, field='source_template_path')

    @field_validator('destination_path')
    @classmethod
    def _validate_destination_path(cls, value: str) -> str:
        if '{selected.root}' in value:
            validate_repository_relative_path(value.replace('{selected.root}', 'agent'), field='destination_path')
            return value
        return _validate_repo_path(value, field='destination_path')

    @model_validator(mode='after')
    def _validate_payload(self) -> Self:
        if 'legacy' in self.template_id or 'legacy' in self.destination_path:
            raise BootstrapConfigError('legacy payloads are migration-only and not trusted manifest entries')
        if (self.semantic_patch_mode == 'apply') != (self.semantic_patch_id is not None):
            raise BootstrapConfigError('semantic patch metadata must be paired')
        return self


class TrustedTemplateManifest(BootstrapDocument):
    manifest_id: ManifestId
    manifest_version: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    managed_payloads: tuple[TrustedManifestPayload, ...]

    @model_validator(mode='after')
    def _validate_manifest(self) -> Self:
        expected = OrderedDict(_REQUIRED_MANAGED_PAYLOADS)
        seen = OrderedDict((item.template_id, item.destination_path) for item in self.managed_payloads)
        if seen != expected:
            raise BootstrapConfigError('trusted manifest must contain the exact required v1 managed payload set')
        _casefold_unique([item.template_id for item in self.managed_payloads], field='managed_payloads.template_id')
        _casefold_unique([item.destination_path for item in self.managed_payloads], field='managed_payloads.destination_path')
        return self

    @property
    def manifest_hash(self) -> str:
        return canonical_sha256(self.model_dump(mode='json'))

    @classmethod
    def load_pinned_manifest(cls) -> 'TrustedTemplateManifest':
        return cls.from_document(_MANIFEST_PATH.read_text(encoding='utf-8'))


class AgentRenderContext(BootstrapDocument):
    repo_agent_id: AgentId
    values: tuple[TemplateRenderValue, ...]

    @model_validator(mode='after')
    def _validate_values(self) -> Self:
        _casefold_unique([item.key for item in self.values], field=f'{self.repo_agent_id} render values')
        return self


class RepositoryPhaseInput(BootstrapDocument):
    trusted_manifest_id: ManifestId
    trusted_manifest_version: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    trusted_manifest_hash: ManifestHash
    agent_render_contexts: tuple[AgentRenderContext, ...]

    @model_validator(mode='after')
    def _validate_manifest(self) -> Self:
        manifest = TrustedTemplateManifest.load_pinned_manifest()
        if self.trusted_manifest_id != manifest.manifest_id:
            raise BootstrapConfigError('trusted_manifest_id must match pinned trusted manifest')
        if self.trusted_manifest_version != manifest.manifest_version:
            raise BootstrapConfigError('trusted_manifest_version must match pinned trusted manifest')
        if self.trusted_manifest_hash != manifest.manifest_hash:
            raise BootstrapConfigError('trusted_manifest_hash must match pinned trusted manifest hash')
        _casefold_unique([item.repo_agent_id for item in self.agent_render_contexts], field='agent_render_contexts.repo_agent_id')
        return self


class GitHubPhaseInput(BootstrapDocument):
    optimizer_environment: EnvironmentName
    deployment_environment: EnvironmentName
    shared_client_id: Guid | Literal['azure_identity_resolution_required']
    client_id_variable_name: VariableName
    default_branch_policy_intent: DefaultBranchPolicyIntent

    @field_validator('shared_client_id')
    @classmethod
    def _validate_client_id(cls, value: str) -> str:
        if value == 'azure_identity_resolution_required':
            return value
        return _normalize_guid(value, field='shared_client_id')


class AzureIdentityInput(BootstrapDocument):
    identity_kind: IdentityKind
    existing_resource_id: str | None = None
    existing_client_id: Guid | None = None
    existing_object_id: Guid | None = None

    @field_validator('existing_client_id', 'existing_object_id')
    @classmethod
    def _validate_guid_fields(cls, value: str | None, info) -> str | None:
        if value is None:
            return None
        return _normalize_guid(value, field=info.field_name)

    @field_validator('existing_resource_id')
    @classmethod
    def _validate_resource(cls, value: str | None) -> str | None:
        if value is None:
            return None
        _validate_resource_id(value, 'existing_resource_id')
        return value

    @model_validator(mode='after')
    def _validate_combo(self) -> Self:
        if self.identity_kind == 'user_assigned_managed_identity':
            if self.existing_resource_id is None:
                raise BootstrapConfigError('user_assigned_managed_identity requires existing_resource_id')
        elif self.identity_kind == 'entra_application':
            if self.existing_client_id is None or self.existing_object_id is None:
                raise BootstrapConfigError('entra_application requires existing_client_id and existing_object_id')
        else:
            if any(value is not None for value in (self.existing_resource_id, self.existing_client_id, self.existing_object_id)):
                raise BootstrapConfigError('unresolved_migration cannot set existing identity ids')
        return self


class ApprovedRoleAssignment(BootstrapDocument):
    alias: Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r'^[a-z0-9][a-z0-9._-]*$')]
    role_definition_id: RoleDefinitionId
    scope: Annotated[str, StringConstraints(min_length=1, max_length=512)]

    @field_validator('alias')
    @classmethod
    def _validate_alias(cls, value: str) -> str:
        return _assert_safe_text(value, field='role alias', limit=64)

    @field_validator('scope')
    @classmethod
    def _validate_scope(cls, value: str) -> str:
        _validate_resource_id(value, 'scope')
        if '/subscriptions/' == value[:15] and value.count('/') <= 2:
            raise BootstrapConfigError('subscription-scope role assignments are not allowed')
        return value

    @model_validator(mode='after')
    def _validate_role(self) -> Self:
        role_guid = self.role_definition_id.rsplit('/', 1)[-1].lower()
        if role_guid not in _ALLOWED_ROLE_DEFINITION_IDS:
            raise BootstrapConfigError('role_definition_id is not in the approved allow-list')
        return self


class AzurePhaseInput(BootstrapDocument):
    tenant_id: Guid
    subscription_id: Guid
    identity: AzureIdentityInput
    resource_group: Annotated[str, StringConstraints(min_length=1, max_length=90, pattern=r'^[A-Za-z0-9._()/-]+$')]
    location: Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9]+$')]
    github_repository_id: RepositoryIdentity
    approved_role_assignments: tuple[ApprovedRoleAssignment, ...]

    @field_validator('tenant_id', 'subscription_id')
    @classmethod
    def _validate_core_guids(cls, value: str, info) -> str:
        return _normalize_guid(value, field=info.field_name)

    @model_validator(mode='after')
    def _validate_resources(self) -> Self:
        _casefold_unique([item.alias for item in self.approved_role_assignments], field='approved_role_assignments.alias')
        for assignment in self.approved_role_assignments:
            if f'/subscriptions/{self.subscription_id}' not in assignment.scope.casefold():
                raise BootstrapConfigError('approved role assignment scope must stay within azure subscription')
            if f'/resourcegroups/{self.resource_group.casefold()}' not in assignment.scope.casefold():
                raise BootstrapConfigError('approved role assignment scope must stay within azure resource_group')
        if self.identity.existing_resource_id is not None:
            lowered = self.identity.existing_resource_id.casefold()
            if f'/subscriptions/{self.subscription_id}' not in lowered or f'/resourcegroups/{self.resource_group.casefold()}' not in lowered:
                raise BootstrapConfigError('existing_resource_id must match azure subscription/resource_group')
        return self


class EvaluationGenerationSource(BootstrapDocument):
    kind: GenerationSourceKind
    path: RepoRelativePath

    @field_validator('path')
    @classmethod
    def _validate_path(cls, value: str) -> str:
        validated = _validate_repo_path(value, field='path')
        lowered_parts = {part.casefold() for part in PurePosixPath(validated).parts}
        if lowered_parts & set(_PROHIBITED_PATH_PARTS):
            raise BootstrapConfigError('generation source path contains prohibited secret/raw content segment')
        return validated


class EvaluationAgentInput(BootstrapDocument):
    repo_agent_id: AgentId
    sidecar_path: RepoRelativePath
    project_endpoint: ProjectEndpoint
    account_resource_id: str
    agent_name: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    agent_version: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    existing_dataset_ids: tuple[DatasetUri, ...] = ()
    existing_evaluator_ids: tuple[EvaluationIdentifier, ...] = ()
    existing_definition_ids: tuple[EvaluationDefinitionId, ...] = ()
    generation_mode: GenerationMode
    generation_sources: tuple[EvaluationGenerationSource, ...]
    model_deployment: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    trace_window: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    connection_name: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    target_sample_count: Annotated[StrictInt, Field(ge=1, le=100000)]
    replacement_intent: StrictBool

    @field_validator('sidecar_path')
    @classmethod
    def _validate_sidecar_path(cls, value: str) -> str:
        return _validate_repo_path(value, field='sidecar_path')

    @field_validator('project_endpoint')
    @classmethod
    def _validate_project_endpoint(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise BootstrapConfigError('project_endpoint must be https without userinfo, query, or fragment')
        return value

    @field_validator('account_resource_id')
    @classmethod
    def _validate_account(cls, value: str) -> str:
        return _validate_resource_id(value, 'account_resource_id')

    @field_validator('agent_name', 'agent_version', 'model_deployment', 'trace_window', 'connection_name')
    @classmethod
    def _validate_text_fields(cls, value: str, info) -> str:
        return _assert_safe_text(value, field=info.field_name)

    @field_validator('existing_dataset_ids', 'existing_evaluator_ids', 'existing_definition_ids')
    @classmethod
    def _validate_identifier_sets(cls, value: Sequence[str], info) -> tuple[str, ...]:
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
        object.__setattr__(self, 'agents', tuple(sorted(self.agents, key=lambda item: (item.repo_agent_id.casefold(), item.repo_agent_id))))
        _casefold_unique([agent.repo_agent_id for agent in self.agents], field='evaluations.agents.repo_agent_id')
        return self


class BootstrapPlanInput(BootstrapDocument):
    repository: RepositoryIdentityInput
    runtime_provenance: RuntimeProvenanceInput
    repository_phase: RepositoryPhaseInput
    offline_plan: StrictBool = False
    required_phases: tuple[PhaseName, ...] = ('repository',)
    github_phase: GitHubPhaseInput | None = None
    azure_phase: AzurePhaseInput | None = None
    evaluations_phase: EvaluationsPhaseInput | None = None

    @field_validator('required_phases')
    @classmethod
    def _validate_required_phases(cls, value: Sequence[str]) -> tuple[str, ...]:
        if not value:
            raise BootstrapConfigError('required_phases must not be empty')
        return _canonical_phase_order(tuple(value))

    @model_validator(mode='after')
    def _validate_cross_field_rules(self) -> Self:
        selected_by_id = {agent.repo_agent_id.casefold(): agent for agent in self.repository.selected_agents}
        render_ids = {item.repo_agent_id.casefold() for item in self.repository_phase.agent_render_contexts}
        if render_ids != set(selected_by_id):
            raise BootstrapConfigError('repository_phase agent render contexts must match the selected agent set exactly')
        if self.azure_phase is not None and self.azure_phase.github_repository_id.casefold() != self.repository.repository_id.casefold():
            raise BootstrapConfigError('azure github_repository_id must match repository_id')
        if self.offline_plan:
            if any(value is not None for value in (self.github_phase, self.azure_phase, self.evaluations_phase)):
                raise BootstrapConfigError('offline_plan forbids cloud phase inputs')
            if any(phase != 'repository' for phase in self.required_phases):
                raise BootstrapConfigError('offline_plan cannot require cloud phases')
        for phase, value in (('github', self.github_phase), ('azure', self.azure_phase), ('evaluations', self.evaluations_phase)):
            if phase in self.required_phases and value is None:
                raise BootstrapConfigError(f'{phase}_phase inputs are required when phase {phase!r} is requested')
        if self.github_phase is not None:
            identity = self.azure_phase.identity if self.azure_phase is not None else None
            if identity is not None and identity.identity_kind == 'entra_application':
                if self.github_phase.shared_client_id != identity.existing_client_id:
                    raise BootstrapConfigError('github shared_client_id must equal identity existing_client_id for adopted identity')
            if self.github_phase.shared_client_id == 'azure_identity_resolution_required':
                if 'github' in self.required_phases:
                    raise BootstrapConfigError('github phase cannot be required until shared client id is resolved')
                if identity is None or identity.identity_kind != 'user_assigned_managed_identity':
                    raise BootstrapConfigError('azure_identity_resolution_required is only valid for new managed identity planning')
        if self.evaluations_phase is not None:
            evaluation_ids = {agent.repo_agent_id.casefold() for agent in self.evaluations_phase.agents}
            if not evaluation_ids <= set(selected_by_id):
                raise BootstrapConfigError('evaluations_phase contains repo_agent_id outside selected_agents')
            for agent in self.evaluations_phase.agents:
                selected = selected_by_id[agent.repo_agent_id.casefold()]
                if agent.sidecar_path != selected.config_path:
                    raise BootstrapConfigError('evaluation sidecar_path must match selected agent config_path')
                if not _path_is_within(selected.root, agent.sidecar_path):
                    raise BootstrapConfigError('evaluation sidecar_path must stay within selected agent root')
                for source in agent.generation_sources:
                    if not _path_is_within(selected.root, source.path):
                        raise BootstrapConfigError('generation source must stay within selected agent root')
                    if not any(source.path == path or source.path.startswith(path[:-2]) for path in selected.editable_paths if path.endswith('/**')) and source.path not in selected.editable_paths:
                        raise BootstrapConfigError('generation source must stay within selected agent editable paths')
                account_lower = agent.account_resource_id.casefold()
                endpoint_account = urlparse(agent.project_endpoint).hostname.split('.')[0] if urlparse(agent.project_endpoint).hostname else ''
                if endpoint_account and f'/accounts/{endpoint_account}'.casefold() not in account_lower:
                    raise BootstrapConfigError('project_endpoint account and account_resource_id must match')
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
            payload = json.loads(data.decode('utf-8'), object_pairs_hook=_strict_json_object)
        except UnicodeDecodeError as exc:
            raise BootstrapConfigError('BootstrapPlanInput is not UTF-8 JSON') from exc
        except (json.JSONDecodeError, ValueError) as exc:
            raise BootstrapConfigError('BootstrapPlanInput is not valid strict JSON') from exc
        if not isinstance(payload, Mapping):
            raise BootstrapConfigError('BootstrapPlanInput JSON must be a mapping')
        return BootstrapPlanInput.model_validate(dict(payload))
    return BootstrapPlanInput.from_document(data)


__all__ = [
    'AgentRenderContext',
    'AzureIdentityInput',
    'AzurePhaseInput',
    'BootstrapPlanInput',
    'EvaluationAgentInput',
    'EvaluationsPhaseInput',
    'GitHubPhaseInput',
    'RepositoryIdentityInput',
    'RepositoryPhaseInput',
    'RuntimeProvenanceInput',
    'SelectedAgent',
    'TemplateRenderValue',
    'TrustedTemplateManifest',
    'load_bootstrap_plan_input',
]
