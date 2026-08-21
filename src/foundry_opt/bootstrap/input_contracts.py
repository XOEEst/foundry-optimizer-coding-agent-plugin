from __future__ import annotations

import json
import re
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlparse

from pydantic import Field, StrictBool, StrictInt, StringConstraints, ValidationError, field_validator, model_validator

from foundry_opt.bootstrap.canonical import canonical_sha256
from foundry_opt.bootstrap.contracts import (
    BootstrapSidecar,
    BootstrapDocument,
    BuiltInEvaluatorId,
    DatasetUri,
    EvaluationDefinitionId,
    GitHubOidcSubjectPrefix,
    IdentityKind,
    ReviewedFoundryTarget,
    SelectedAgentProfile,
    VersionedEvaluatorUri,
)
from foundry_opt.bootstrap.errors import BootstrapConfigError
from foundry_opt.bootstrap.evaluation.execution import EvaluationOnboardingRequest, SidecarPolicy
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
BindingEvidenceProvenance = Literal['foundry_agent_code_download', 'reviewed_operator_attestation']
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


@dataclass(frozen=True, slots=True)
class ApprovedRoleDefinition:
    """One reviewed Azure built-in role that bootstrap may assign."""

    slug: str
    display_name: str
    role_definition_guid: str
    scope_kind: Literal['foundry', 'telemetry']
    purpose: str


# Least-privilege matrix. Every entry is a real Azure built-in role definition GUID; the
# bootstrap contract refuses any role outside this list, and refuses the privileged
# fallbacks below outright. The retained pilot assigns only project-scoped `Foundry User`.
# Documented in docs/identity-rbac.md, which is verified against this table by tests.
APPROVED_ROLE_DEFINITIONS: tuple[ApprovedRoleDefinition, ...] = (
    ApprovedRoleDefinition(
        slug='foundry-user',
        display_name='Foundry User',
        role_definition_guid='53ca6127-db72-4b80-b1b0-d745d6d5456d',
        scope_kind='foundry',
        purpose='project read plus Cognitive Services data actions for draft agent, dataset, evaluator, definition, and run operations',
    ),
    ApprovedRoleDefinition(
        slug='foundry-project-runtime-user',
        display_name='Foundry Project Runtime User',
        role_definition_guid='142bfaed-a13f-4c2d-bed2-6db62c4a1009',
        scope_kind='foundry',
        purpose='project runtime data-plane access used by hosted agent execution during evaluation and deployment verification',
    ),
    ApprovedRoleDefinition(
        slug='foundry-agent-consumer',
        display_name='Foundry Agent Consumer',
        role_definition_guid='eed3b665-ab3a-47b6-8f48-c9382fb1dad6',
        scope_kind='foundry',
        purpose='invoke an existing agent version without publication or routing authority',
    ),
    ApprovedRoleDefinition(
        slug='monitoring-reader',
        display_name='Monitoring Reader',
        role_definition_guid='43d0d8ad-25c7-4714-9337-8ba259a9fe05',
        scope_kind='telemetry',
        purpose='read Application Insights telemetry when trace-derived dataset generation is modeled',
    ),
    ApprovedRoleDefinition(
        slug='log-analytics-reader',
        display_name='Log Analytics Reader',
        role_definition_guid='73c42c96-874c-492b-b04d-ab87d138a893',
        scope_kind='telemetry',
        purpose='query the Log Analytics workspace backing Application Insights trace availability probes',
    ),
)

# Privileged fallbacks that must never be planned, even if a reviewer supplies them.
FORBIDDEN_ROLE_DEFINITION_IDS: Mapping[str, str] = {
    '8e3af657-a8ff-443c-a75c-2fe8c4bcb635': 'Owner',
    'b24988ac-6180-42a0-ab88-20f7382dd24c': 'Contributor',
    'eadc314b-1a2d-4efa-be10-5d325db5065e': 'Azure AI Project Manager',
}

_ALLOWED_ROLE_DEFINITION_IDS = frozenset(item.role_definition_guid for item in APPROVED_ROLE_DEFINITIONS)
_ROLE_DEFINITIONS_BY_GUID: Mapping[str, ApprovedRoleDefinition] = {
    item.role_definition_guid: item for item in APPROVED_ROLE_DEFINITIONS
}


def approved_role_definition(role_definition_guid: str) -> ApprovedRoleDefinition | None:
    """Return the reviewed role definition for a GUID, or None when it is not approved."""

    return _ROLE_DEFINITIONS_BY_GUID.get(role_definition_guid.casefold())

_REQUIRED_MANAGED_PAYLOADS = (
    ('registry', '.foundry-opt/registry.yaml'),
    ('sidecar', '{selected.root}/.foundry/foundry-opt.yaml'),
    ('optimizer-instruction', '.github/instructions/foundry-opt.instructions.md'),
    ('optimizer-issue-form', '.github/ISSUE_TEMPLATE/foundry-optimize-agent.yml'),
    ('setup-semantic-patch', '.github/workflows/copilot-setup-steps.yml'),
    ('validation-workflow', '.github/workflows/foundry-opt-validation.yml'),
    ('deploy-workflow', '.github/workflows/foundry-opt-deploy.yml'),
)
# The committed managed lock is `.foundry-opt/bootstrap.lock.json`, produced by repository
# apply from the applied plan; it is never a rendered template payload. The legacy
# `.github/foundry-opt.lock.yml` shared pin remains readable for migration only.
MANAGED_LOCK_PATH = '.foundry-opt/bootstrap.lock.json'
LEGACY_LOCK_PATH = '.github/foundry-opt.lock.yml'
_REFUSED_MANAGED_PAYLOADS = {
    'bootstrap-lock': LEGACY_LOCK_PATH,
}
_UAMI_RESOURCE_ID_RE = re.compile(
    r'^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/Microsoft\.ManagedIdentity'
    r'/userAssignedIdentities/(?P<name>[^/]+)$',
    re.IGNORECASE,
)
_IDENTITY_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{2,127}$')
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
    discovery_root: Annotated[str, StringConstraints(min_length=1, max_length=240)] | None = None
    config_path: RepoRelativePath
    editable_paths: tuple[RepoRelativePath, ...]
    enabled: StrictBool | None = None
    foundry_target: ReviewedFoundryTarget | None = None
    profile: SelectedAgentProfile | None = None

    @field_validator('root')
    @classmethod
    def _validate_root(cls, value: str) -> str:
        return _validate_repo_path(value, field='root', allow_dot=True)

    @field_validator('discovery_root')
    @classmethod
    def _validate_discovery_root(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_repo_path(value, field='discovery_root', allow_dot=True)

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
        discovery_root = self.discovery_root
        if self.root == '.':
            parts = PurePosixPath(self.config_path).parts
            if len(parts) <= 2 or parts[-2:] != ('.foundry', 'foundry-opt.yaml'):
                raise BootstrapConfigError(
                    "dot-like selected root requires config_path under a concrete agent directory"
                )
            managed_root = PurePosixPath(*parts[:-2]).as_posix()
            object.__setattr__(self, 'root', managed_root)
            discovery_root = discovery_root or '.'
        if discovery_root is None:
            discovery_root = self.root
        object.__setattr__(self, 'discovery_root', discovery_root)
        if self.root != '.' and not _path_is_within(self.root, self.config_path):
            raise BootstrapConfigError('config_path must be within selected agent root')
        return self

    @property
    def discovery_selection_root(self) -> str:
        return self.discovery_root or self.root

    @property
    def profile_document(self) -> BootstrapSidecar | None:
        if self.profile is None:
            return None
        return BootstrapSidecar.from_selected_agent_profile(
            repo_agent_id=self.repo_agent_id,
            source_root=self.root,
            editable_paths=self.editable_paths,
            profile=self.profile,
        )

    @property
    def rendered_enabled(self) -> bool:
        return bool(self.enabled)


def _profile_from_sidecar_policy(
    *,
    repo_agent_id: str,
    policy: SidecarPolicy,
    editable_paths: Sequence[str] | None = None,
) -> BootstrapSidecar:
    return BootstrapSidecar(
        repo_agent_id=repo_agent_id,
        source_root=policy.source_root,
        package_root=policy.package_root,
        editable_paths=tuple(editable_paths or policy.editable_paths),
        runtime=policy.runtime,
        foundry_project=policy.foundry_project,
        baseline_model=policy.baseline_model,
        allowed_models=policy.allowed_models,
        min_candidates=policy.min_candidates,
        max_candidates=policy.max_candidates,
        primary_metric=policy.primary_metric,
        decision_policy=policy.decision_policy,
        max_issue_evaluators=policy.max_issue_evaluators,
        hard_guardrails=policy.hard_guardrails,
        deployment=policy.deployment,
        verification=policy.verification,
    )


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
            if self.root != '.' and not _path_is_within(self.root, agent.discovery_selection_root):
                raise BootstrapConfigError('selected_agents discovery_root must be within repository root')
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
        if self.template_id in _REFUSED_MANAGED_PAYLOADS or self.destination_path in _REFUSED_MANAGED_PAYLOADS.values():
            raise BootstrapConfigError(
                f'{self.destination_path} is not a managed payload; the committed lock is '
                f'{MANAGED_LOCK_PATH}, generated by repository apply'
            )
        if self.destination_path == MANAGED_LOCK_PATH:
            raise BootstrapConfigError(
                f'{MANAGED_LOCK_PATH} is generated by repository apply and must not be a rendered template'
            )
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
    oidc_subject_prefix: GitHubOidcSubjectPrefix | None = None
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
    create_if_missing: StrictBool = False

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
            match = _UAMI_RESOURCE_ID_RE.fullmatch(self.existing_resource_id)
            if match is None:
                raise BootstrapConfigError(
                    'user_assigned_managed_identity existing_resource_id must target '
                    'Microsoft.ManagedIdentity/userAssignedIdentities/<name>'
                )
            if _IDENTITY_NAME_RE.fullmatch(match.group('name')) is None:
                raise BootstrapConfigError('managed identity name is not a valid Azure identity name')
            if self.create_if_missing and (
                self.existing_client_id is not None
                or self.existing_object_id is not None
            ):
                raise BootstrapConfigError(
                    'new managed identity cannot predeclare generated client/object ids'
                )
        elif self.identity_kind == 'entra_application':
            if self.existing_client_id is None or self.existing_object_id is None:
                raise BootstrapConfigError('entra_application requires existing_client_id and existing_object_id')
            if self.create_if_missing:
                raise BootstrapConfigError(
                    'v1 does not create Entra applications during bootstrap'
                )
        else:
            if self.create_if_missing or any(value is not None for value in (self.existing_resource_id, self.existing_client_id, self.existing_object_id)):
                raise BootstrapConfigError('unresolved_migration cannot set existing identity ids')
        return self

    @property
    def identity_name(self) -> str:
        """The exact identity this operation targets, adopted or created.

        For a user-assigned managed identity the name is always the final segment of
        `existing_resource_id` -- including when `create_if_missing` is true, where that id is
        the reviewed creation target -- so plans, receipts, and provider state name the real
        resource instead of a placeholder. Adopted Entra applications have no ARM resource id,
        so their exact client id is used as the identity label.
        """

        if self.existing_resource_id is not None:
            match = _UAMI_RESOURCE_ID_RE.fullmatch(self.existing_resource_id)
            if match is None:
                raise BootstrapConfigError('existing_resource_id does not name a user-assigned managed identity')
            return match.group('name')
        if self.identity_kind == 'entra_application' and self.existing_client_id is not None:
            return self.existing_client_id
        raise BootstrapConfigError(
            'azure identity planning requires a resolved identity resource id or client id'
        )


class ApprovedRoleAssignment(BootstrapDocument):
    alias: Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r'^[a-z0-9][a-z0-9._-]*$')]
    role_definition_id: RoleDefinitionId = Field(
        description=(
            'Azure built-in role definition id. Only the reviewed least-privilege matrix in '
            'docs/identity-rbac.md is accepted; Owner, Contributor, and Azure AI Project '
            'Manager are refused outright.'
        ),
        json_schema_extra={
            'x-approved-role-definitions': [
                {
                    'slug': item.slug,
                    'display_name': item.display_name,
                    'role_definition_guid': item.role_definition_guid,
                    'scope_kind': item.scope_kind,
                }
                for item in APPROVED_ROLE_DEFINITIONS
            ],
            'x-refused-role-definitions': [
                {'display_name': name, 'role_definition_guid': guid}
                for guid, name in sorted(FORBIDDEN_ROLE_DEFINITION_IDS.items())
            ],
        },
    )
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
        forbidden = FORBIDDEN_ROLE_DEFINITION_IDS.get(role_guid)
        if forbidden is not None:
            raise BootstrapConfigError(
                f'{forbidden} is a privileged fallback role and is never planned by bootstrap'
            )
        definition = approved_role_definition(role_guid)
        if definition is None:
            raise BootstrapConfigError('role_definition_id is not in the approved allow-list')
        if not self.alias.casefold().startswith(definition.slug):
            raise BootstrapConfigError(
                f'role alias must identify its role: expected an alias starting with {definition.slug!r}'
            )
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
    connection_name: Annotated[str, StringConstraints(min_length=1, max_length=128)] | None = None
    target_sample_count: Annotated[StrictInt, Field(ge=1, le=100000)]
    replacement_intent: StrictBool
    onboarding_contract: EvaluationOnboardingRequest | None = None

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

    @field_validator('agent_name', 'agent_version', 'model_deployment', 'trace_window')
    @classmethod
    def _validate_text_fields(cls, value: str, info) -> str:
        return _assert_safe_text(value, field=info.field_name)

    @field_validator('connection_name')
    @classmethod
    def _validate_connection_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _assert_safe_text(value, field='connection_name')

    @field_validator('existing_dataset_ids', 'existing_evaluator_ids', 'existing_definition_ids')
    @classmethod
    def _validate_identifier_sets(cls, value: Sequence[str], info) -> tuple[str, ...]:
        return _casefold_unique(tuple(value), field=info.field_name)

    @model_validator(mode='after')
    def _validate_generation(self) -> Self:
        if not self.generation_sources:
            raise BootstrapConfigError('generation_sources must not be empty')
        _casefold_unique([source.path for source in self.generation_sources], field=f'{self.repo_agent_id} generation_sources')
        return self._validate_resolved_execution()

    def _validate_resolved_execution(self) -> Self:
        contract = self.onboarding_contract
        if contract is None:
            return self
        if contract.repo_agent_id.casefold() != self.repo_agent_id.casefold():
            raise BootstrapConfigError('onboarding_contract must describe the same repo_agent_id')
        if (contract.replacement is not None) != self.replacement_intent:
            raise BootstrapConfigError('replacement_intent must match the reviewed replacement lineage')
        if contract.stopped:
            return self
        assert contract.dataset_plan is not None and contract.definition_plan is not None
        assert contract.activation_plan is not None and contract.sidecar_policy is not None
        assert contract.telemetry_probe is not None
        if contract.sidecar_policy.path != self.sidecar_path:
            raise BootstrapConfigError('onboarding sidecar path must match the evaluation sidecar_path')
        if contract.activation_plan.model_deployment != self.model_deployment:
            raise BootstrapConfigError('onboarding activation must use the reviewed model deployment')
        if contract.definition_plan.model_deployment != self.model_deployment:
            raise BootstrapConfigError('onboarding definitions must use the reviewed model deployment')
        if contract.telemetry_probe.telemetry_window != self.trace_window:
            raise BootstrapConfigError('onboarding telemetry probe must use the reviewed trace window')
        if contract.bounds.target_sample_count != self.target_sample_count:
            raise BootstrapConfigError('onboarding bounds must request the reviewed target sample count')
        if contract.dataset_plan.agent_name != self.agent_name or contract.dataset_plan.agent_version != self.agent_version:
            raise BootstrapConfigError('onboarding generation must target the reviewed agent version')
        if contract.dataset_plan.connection_name != self.connection_name:
            raise BootstrapConfigError('onboarding generation must use the reviewed connection')
        known_datasets = {item.casefold() for item in self.existing_dataset_ids}
        for candidate in (contract.dataset_plan.reuse_development_dataset_id, contract.dataset_plan.reuse_validating_dataset_id):
            if candidate is not None and candidate.casefold() not in known_datasets:
                raise BootstrapConfigError('dataset reuse candidates must come from the reviewed existing dataset inventory')
        if contract.evaluator_plan is not None and contract.evaluator_plan.reuse_evaluator_id is not None:
            known_evaluators = {item.casefold() for item in self.existing_evaluator_ids}
            if contract.evaluator_plan.reuse_evaluator_id.casefold() not in known_evaluators:
                raise BootstrapConfigError('evaluator reuse candidates must come from the reviewed existing evaluator inventory')
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


class ObservedAgentBinding(BootstrapDocument):
    """One reviewed, non-secret observation of a deployed immutable agent version.

    Both content fingerprints are required: metadata alone (endpoint/name/version) can never
    prove that the deployed version runs the repository's current source, so an evidence
    record without content digests is refused rather than silently downgraded.
    """

    root: Annotated[str, StringConstraints(min_length=1, max_length=240)]
    repo_agent_id: AgentId
    project_endpoint: ProjectEndpoint
    agent_name: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    agent_version: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    source_fingerprint: Sha256
    package_fingerprint: Sha256
    evidence_provenance: BindingEvidenceProvenance
    code_content_hash: Sha256 | None = None
    code_content_hash_verified: StrictBool = False
    observed_at: Annotated[str, StringConstraints(min_length=20, max_length=32, pattern=r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$')] | None = None

    @field_validator('root')
    @classmethod
    def _validate_root(cls, value: str) -> str:
        return _validate_repo_path(value, field='binding_evidence.root', allow_dot=True)

    @model_validator(mode='after')
    def _validate_provenance(self) -> Self:
        if self.evidence_provenance == 'foundry_agent_code_download':
            if self.code_content_hash is None:
                raise BootstrapConfigError('downloaded binding evidence must record the immutable code content hash')
            if not self.code_content_hash_verified:
                raise BootstrapConfigError('downloaded binding evidence must confirm the code content hash against the observed bytes')
        elif self.code_content_hash_verified:
            raise BootstrapConfigError('only downloaded binding evidence can claim a verified code content hash')
        return self

    def to_discovery_payload(self) -> dict[str, str]:
        return {
            'project_endpoint': self.project_endpoint,
            'agent_name': self.agent_name,
            'agent_version': self.agent_version,
            'source_fingerprint': self.source_fingerprint,
            'package_fingerprint': self.package_fingerprint,
        }


class BindingEvidenceInput(BootstrapDocument):
    """Strict, non-secret binding evidence document consumed by discovery."""

    evidence_version: Literal[1] = 1
    repository_id: RepositoryIdentity
    agents: tuple[ObservedAgentBinding, ...]

    @model_validator(mode='after')
    def _validate_agents(self) -> Self:
        if not self.agents:
            raise BootstrapConfigError('binding_evidence.agents must not be empty')
        if len(self.agents) > _MAX_ITEMS:
            raise BootstrapConfigError('binding_evidence.agents exceeds the supported item count')
        object.__setattr__(self, 'agents', tuple(sorted(self.agents, key=lambda item: (item.repo_agent_id.casefold(), item.repo_agent_id))))
        _casefold_unique([agent.repo_agent_id for agent in self.agents], field='binding_evidence.agents.repo_agent_id')
        _casefold_unique([agent.root for agent in self.agents], field='binding_evidence.agents.root')
        return self

    @property
    def evidence_hash(self) -> str:
        return canonical_sha256(self.model_dump(mode='json', exclude_none=True))

    def by_root(self) -> dict[str, dict[str, str]]:
        return {agent.root: agent.to_discovery_payload() for agent in self.agents}

    @classmethod
    def from_document(cls, document: str | bytes | Mapping[str, object]) -> Self:
        try:
            return cls.model_validate(load_strict_yaml_mapping(document, subject='BindingEvidenceInput'))
        except POCConfigurationError as exc:
            raise BootstrapConfigError(str(exc)) from exc
        except ValidationError as exc:
            raise BootstrapConfigError(str(exc)) from exc


class BootstrapPlanInput(BootstrapDocument):
    repository: RepositoryIdentityInput
    runtime_provenance: RuntimeProvenanceInput
    repository_phase: RepositoryPhaseInput
    offline_plan: StrictBool = False
    required_phases: tuple[PhaseName, ...] = ('repository',)
    github_phase: GitHubPhaseInput | None = None
    azure_phase: AzurePhaseInput | None = None
    evaluations_phase: EvaluationsPhaseInput | None = None
    binding_evidence: BindingEvidenceInput | None = None

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
        for selected in self.repository.selected_agents:
            if selected.profile is not None and selected.profile_document is None:
                raise BootstrapConfigError('selected agent profile could not be rendered')
            if (
                selected.profile is not None
                and selected.foundry_target is not None
                and selected.foundry_target.state != 'blocked'
            ):
                profile_project = selected.profile.foundry_project
                if profile_project.project_endpoint != selected.foundry_target.project_endpoint:
                    raise BootstrapConfigError('selected foundry_target project_endpoint must match selected profile foundry_project')
                if profile_project.agent_name != selected.foundry_target.agent_name:
                    raise BootstrapConfigError('selected foundry_target agent_name must match selected profile foundry_project')
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
                if 'evaluations' in self.required_phases and agent.onboarding_contract is None:
                    raise BootstrapConfigError(
                        'the evaluations phase requires an approved onboarding contract for every evaluation agent'
                    )
                if agent.onboarding_contract is not None:
                    policy = agent.onboarding_contract.sidecar_policy
                    if policy is not None and policy.source_root != selected.root:
                        raise BootstrapConfigError('onboarding sidecar source_root must match the selected agent root')
                    if policy is not None and policy.path != selected.config_path:
                        raise BootstrapConfigError('onboarding sidecar path must match selected agent config_path')
                    if policy is not None:
                        for path in policy.editable_paths:
                            if not any(
                                path == editable
                                or path.startswith(editable[:-2])
                                for editable in selected.editable_paths
                                if editable.endswith('/**')
                            ) and path not in selected.editable_paths:
                                raise BootstrapConfigError(
                                    'onboarding editable_paths must stay within selected agent editable_paths'
                                )
                    if selected.profile is not None and policy is not None:
                        selected_profile = selected.profile_document
                        assert selected_profile is not None
                        contract_profile = _profile_from_sidecar_policy(
                            repo_agent_id=agent.repo_agent_id,
                            policy=policy,
                            editable_paths=selected.editable_paths,
                        )
                        if selected_profile.static_fingerprint() != contract_profile.static_fingerprint():
                            raise BootstrapConfigError('selected profile must match the reviewed onboarding sidecar policy')
                if agent.sidecar_path != selected.config_path:
                    raise BootstrapConfigError('evaluation sidecar_path must match selected agent config_path')
                if selected.foundry_target is not None and selected.foundry_target.state != 'blocked':
                    if selected.foundry_target.project_endpoint != agent.project_endpoint:
                        raise BootstrapConfigError('evaluation project_endpoint must match selected foundry_target')
                    if selected.foundry_target.agent_name != agent.agent_name:
                        raise BootstrapConfigError('evaluation agent_name must match selected foundry_target')
                    if (
                        selected.foundry_target.account_resource_id is not None
                        and selected.foundry_target.account_resource_id != agent.account_resource_id
                    ):
                        raise BootstrapConfigError('evaluation account_resource_id must match selected foundry_target')
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
        if any(selected.enabled for selected in self.repository.selected_agents):
            approved_policies = {
                agent.repo_agent_id.casefold(): agent.onboarding_contract.sidecar_policy
                for agent in (self.evaluations_phase.agents if self.evaluations_phase is not None else ())
                if agent.onboarding_contract is not None and agent.onboarding_contract.sidecar_policy is not None
            }
            for selected in self.repository.selected_agents:
                if not selected.enabled:
                    continue
                if selected.profile is None and approved_policies.get(selected.repo_agent_id.casefold()) is None:
                    raise BootstrapConfigError(
                        'enabled selected agents require a reviewed profile or onboarding sidecar policy'
                    )
        if self.binding_evidence is not None:
            if self.binding_evidence.repository_id.casefold() != self.repository.repository_id.casefold():
                raise BootstrapConfigError('binding_evidence repository_id must match repository_id')
            selected_by_root = {
                agent.discovery_selection_root.casefold(): agent
                for agent in self.repository.selected_agents
            }
            evaluation_by_id = {agent.repo_agent_id.casefold(): agent for agent in (self.evaluations_phase.agents if self.evaluations_phase is not None else ())}
            for observation in self.binding_evidence.agents:
                selected = selected_by_root.get(observation.root.casefold())
                if selected is None:
                    raise BootstrapConfigError('binding_evidence root must match a selected agent root')
                if selected.repo_agent_id.casefold() != observation.repo_agent_id.casefold():
                    raise BootstrapConfigError('binding_evidence repo_agent_id must match the selected agent for that root')
                evaluation = evaluation_by_id.get(observation.repo_agent_id.casefold())
                if evaluation is None:
                    continue
                if evaluation.project_endpoint != observation.project_endpoint:
                    raise BootstrapConfigError('binding_evidence project_endpoint must match the reviewed evaluation project')
                if evaluation.agent_name != observation.agent_name or evaluation.agent_version != observation.agent_version:
                    raise BootstrapConfigError('binding_evidence must observe the reviewed agent name and version')
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


def load_binding_evidence_input(path: Path | str) -> BindingEvidenceInput:
    target = Path(path)
    try:
        data = target.read_bytes()
    except OSError as exc:
        raise BootstrapConfigError(f'BindingEvidenceInput could not be read: {target}') from exc
    if len(data) > _MAX_FREEFORM_BYTES * _MAX_ITEMS:
        raise BootstrapConfigError('BindingEvidenceInput exceeds the supported document size')
    if target.suffix.casefold() == '.json':
        try:
            payload = json.loads(data.decode('utf-8'), object_pairs_hook=_strict_json_object)
        except UnicodeDecodeError as exc:
            raise BootstrapConfigError('BindingEvidenceInput is not UTF-8 JSON') from exc
        except (json.JSONDecodeError, ValueError) as exc:
            raise BootstrapConfigError('BindingEvidenceInput is not valid strict JSON') from exc
        if not isinstance(payload, Mapping):
            raise BootstrapConfigError('BindingEvidenceInput JSON must be a mapping')
        try:
            return BindingEvidenceInput.model_validate(dict(payload))
        except ValidationError as exc:
            raise BootstrapConfigError(str(exc)) from exc
    return BindingEvidenceInput.from_document(data)


__all__ = [
    'APPROVED_ROLE_DEFINITIONS',
    'AgentRenderContext',
    'ApprovedRoleAssignment',
    'ApprovedRoleDefinition',
    'AzureIdentityInput',
    'AzurePhaseInput',
    'BindingEvidenceInput',
    'BootstrapPlanInput',
    'EvaluationAgentInput',
    'EvaluationOnboardingRequest',
    'EvaluationsPhaseInput',
    'FORBIDDEN_ROLE_DEFINITION_IDS',
    'GitHubPhaseInput',
    'ObservedAgentBinding',
    'RepositoryIdentityInput',
    'RepositoryPhaseInput',
    'RuntimeProvenanceInput',
    'SelectedAgent',
    'TemplateRenderValue',
    'TrustedTemplateManifest',
    'approved_role_definition',
    'load_binding_evidence_input',
    'load_bootstrap_plan_input',
]
