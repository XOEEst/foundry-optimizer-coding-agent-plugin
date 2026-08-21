from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from foundry_opt.bootstrap.connection import ConnectionApproval, ConnectionPlan, ConnectionPhaseState, ConnectionStatus
from foundry_opt.bootstrap.drivers import AzurePhaseDriver, GitHubPhaseDriver
from foundry_opt.bootstrap.input_contracts import BootstrapPlanInput, approved_role_definition
from foundry_opt.bootstrap.providers.azure import _role_assignment_id

_DEFAULT_GITHUB_VARIABLE = 'AZURE_OPTIMIZER_CLIENT_ID'
PreviewDisposition = Literal['create', 'adopt', 'unchanged', 'update']


def _heading(title: str, *, markdown: bool, level: int = 2) -> str:
    return f'{"#" * level} {title}' if markdown else title


def _yes_no(value: bool) -> str:
    return 'yes' if value else 'no'


def _present(value: str | None) -> str:
    return value or 'available-after-apply'


@dataclass(frozen=True, slots=True)
class _ConnectionVariablePreview:
    name: str
    value: str
    disposition: PreviewDisposition


@dataclass(frozen=True, slots=True)
class _GitHubEnvironmentPreview:
    name: str
    disposition: PreviewDisposition
    variables: tuple[_ConnectionVariablePreview, ...] = ()
    branch_name: str | None = None
    branch_disposition: PreviewDisposition | None = None


@dataclass(frozen=True, slots=True)
class _AzureIdentityPreview:
    kind: str
    name: str
    disposition: PreviewDisposition
    resource_id: str | None = None
    client_id: str | None = None
    object_id: str | None = None
    principal_id: str | None = None
    tenant_id: str | None = None
    subscription_id: str | None = None
    location: str | None = None


@dataclass(frozen=True, slots=True)
class _AzureSubjectPreview:
    subject: str
    disposition: PreviewDisposition


@dataclass(frozen=True, slots=True)
class _AzureRolePreview:
    label: str
    scope: str
    disposition: PreviewDisposition


@dataclass(frozen=True, slots=True)
class ConnectionPlanPreview:
    repository_identity: str
    operation_id: str
    github_environments: tuple[_GitHubEnvironmentPreview, ...]
    azure_identity: _AzureIdentityPreview
    oidc_subjects: tuple[_AzureSubjectPreview, ...]
    role_assignments: tuple[_AzureRolePreview, ...]
    creates: tuple[str, ...]
    adopts: tuple[str, ...]
    unchanged: tuple[str, ...]
    updates: tuple[str, ...]

    def render_text(self) -> str:
        return '\n'.join(self._render_lines(markdown=False))

    def render_markdown(self) -> str:
        return '\n'.join(self._render_lines(markdown=True))

    def _render_lines(self, *, markdown: bool) -> list[str]:
        lines = [_heading('Connection plan', markdown=markdown)]
        lines.append(f'- Repository: {self.repository_identity}')
        lines.append(f'- Operation: {self.operation_id}')
        lines.append(
            '- Approval target: approve this GitHub and Azure connection once; owners never need a manual approval file'
        )
        if self.github_environments:
            lines.append('')
            lines.append(_heading('GitHub', markdown=markdown, level=3))
            for environment in self.github_environments:
                lines.append(
                    f'- {environment.name}: {_disposition_label(environment.disposition)} environment'
                )
                for variable in environment.variables:
                    lines.append(
                        f'  - {variable.name}={variable.value} ({_disposition_label(variable.disposition)})'
                    )
                if environment.branch_name:
                    assert environment.branch_disposition is not None
                    lines.append(
                        f'  - Branch policy: {environment.branch_name} ({_disposition_label(environment.branch_disposition)})'
                    )
        lines.append('')
        lines.append(_heading('Azure', markdown=markdown, level=3))
        lines.append(
            '- Identity: '
            f'{_disposition_label(self.azure_identity.disposition)} '
            f'{self.azure_identity.kind} {self.azure_identity.name}'
        )
        lines.append(f'  - Resource ID: {_present(self.azure_identity.resource_id)}')
        lines.append(f'  - Client ID: {_present(self.azure_identity.client_id)}')
        lines.append(
            f'  - Principal/Object ID: {_present(self.azure_identity.principal_id or self.azure_identity.object_id)}'
        )
        lines.append(f'  - Tenant ID: {_present(self.azure_identity.tenant_id)}')
        lines.append(f'  - Subscription ID: {_present(self.azure_identity.subscription_id)}')
        if self.azure_identity.location:
            lines.append(f'  - Location: {self.azure_identity.location}')
        if self.oidc_subjects:
            lines.append('- OIDC subjects:')
            for subject in self.oidc_subjects:
                lines.append(f'  - {_disposition_label(subject.disposition)} {subject.subject}')
        if self.role_assignments:
            lines.append('- RBAC:')
            for role in self.role_assignments:
                lines.append(
                    f'  - {_disposition_label(role.disposition)} {role.label} on {role.scope}'
                )
        lines.append('')
        lines.append(_heading('Planned changes', markdown=markdown, level=3))
        for label, values in (
            ('Create', self.creates),
            ('Adopt', self.adopts),
            ('Unchanged', self.unchanged),
            ('Update', self.updates),
        ):
            if values:
                lines.append(f'- {label}: {"; ".join(values)}')
        lines.append(
            '- Next action: run foundry-opt bootstrap connect approve --repository-id '
            f'{self.repository_identity} --operation-id {self.operation_id} --actor <owner> --summary "<reason>" '
            'or run foundry-opt bootstrap connect apply --approve with the same actor and summary'
        )
        return lines


def _disposition_label(value: PreviewDisposition) -> str:
    return {
        'create': 'create',
        'adopt': 'adopt',
        'unchanged': 'unchanged',
        'update': 'update',
    }[value]


def _bucket(
    creates: list[str],
    adopts: list[str],
    unchanged: list[str],
    updates: list[str],
    *,
    disposition: PreviewDisposition,
    label: str,
) -> None:
    if disposition == 'create':
        creates.append(label)
    elif disposition == 'adopt':
        adopts.append(label)
    elif disposition == 'unchanged':
        unchanged.append(label)
    else:
        updates.append(label)


def _github_variable_request(action: object) -> tuple[str, str, str]:
    diagnostics = tuple(getattr(action, 'diagnostics', ()))
    if len(diagnostics) >= 3:
        return str(diagnostics[0]), str(diagnostics[1]), str(diagnostics[2])
    if len(diagnostics) == 2:
        return str(diagnostics[0]), _DEFAULT_GITHUB_VARIABLE, str(diagnostics[1])
    raise ValueError('github-variable action diagnostics are invalid')


def _github_environment_previews(
    plan: ConnectionPlan,
    driver: GitHubPhaseDriver,
    *,
    creates: list[str],
    adopts: list[str],
    unchanged: list[str],
    updates: list[str],
) -> tuple[_GitHubEnvironmentPreview, ...]:
    provider = driver._client()
    repository = provider.read_repository_settings(plan.repository_identity)
    owner, repo = plan.repository_identity.split('/', 1)
    default_branch = str(repository['default_branch'])
    actions = tuple(plan.phase_plan('github').plan.actions)
    environments: list[str] = []
    seen: set[str] = set()
    branches: dict[str, str] = {}
    variable_requests: dict[str, list[tuple[str, str]]] = {}
    for action in actions:
        diagnostics = tuple(getattr(action, 'diagnostics', ()))
        if not diagnostics:
            continue
        environment = str(diagnostics[0])
        if environment not in seen:
            environments.append(environment)
            seen.add(environment)
        if getattr(action, 'kind', None) == 'github-branch-policy' and len(diagnostics) >= 2:
            branches[environment] = str(diagnostics[1])
        elif getattr(action, 'kind', None) == 'github-variable':
            env_name, variable_name, value = _github_variable_request(action)
            variable_requests.setdefault(env_name, []).append((variable_name, value))
    previews: list[_GitHubEnvironmentPreview] = []
    for environment in environments:
        branch = branches.get(environment, default_branch)
        state = provider._inventory_environment(owner, repo, environment, branch)
        env_disposition: PreviewDisposition = 'unchanged' if state.exists else 'create'
        _bucket(
            creates,
            adopts,
            unchanged,
            updates,
            disposition=env_disposition,
            label=f'GitHub environment {environment}',
        )
        variables: list[_ConnectionVariablePreview] = []
        for variable_name, value in sorted(variable_requests.get(environment, ()), key=lambda item: item[0].casefold()):
            current = provider._read_environment_variable(owner, repo, environment, variable_name)
            if not current.exists:
                disposition: PreviewDisposition = 'create'
            elif current.value == value:
                disposition = 'unchanged'
            else:
                disposition = 'update'
            _bucket(
                creates,
                adopts,
                unchanged,
                updates,
                disposition=disposition,
                label=f'GitHub variable {variable_name} in {environment}',
            )
            variables.append(
                _ConnectionVariablePreview(
                    name=variable_name,
                    value=value,
                    disposition=disposition,
                )
            )
        branch_disposition: PreviewDisposition | None = None
        if environment in branches:
            branch_disposition = 'unchanged' if state.requested_branch_policy.exists else 'update'
            _bucket(
                creates,
                adopts,
                unchanged,
                updates,
                disposition=branch_disposition,
                label=f'GitHub branch policy {branch} for {environment}',
            )
        previews.append(
            _GitHubEnvironmentPreview(
                name=environment,
                disposition=env_disposition,
                variables=tuple(variables),
                branch_name=branches.get(environment),
                branch_disposition=branch_disposition,
            )
        )
    return tuple(previews)


def _azure_identity_preview(
    plan: ConnectionPlan,
    plan_input: BootstrapPlanInput,
    driver: AzurePhaseDriver,
) -> tuple[_AzureIdentityPreview, object, object]:
    provider = driver._client(plan_input)
    planned = provider._planned_bindings(plan.phase_plan('azure').plan)
    identity = planned.identity
    if identity.kind == 'user_assigned_managed_identity':
        existing = provider._get_uami_if_exists(identity.resource_id or '')
        if existing is None:
            if identity.adopted:
                raise ValueError('approved managed identity is missing')
            live = identity
            disposition: PreviewDisposition = 'create'
        else:
            provider._assert_expected_identity(identity, existing, allow_fill_missing=True)
            live = existing
            disposition = 'adopt'
    else:
        live = provider._resolve_identity(identity)
        disposition = 'adopt'
    preview = _AzureIdentityPreview(
        kind='managed identity' if live.kind == 'user_assigned_managed_identity' else 'Entra application',
        name=live.name,
        disposition=disposition,
        resource_id=live.resource_id,
        client_id=live.client_id,
        object_id=live.object_id,
        principal_id=live.principal_id,
        tenant_id=live.tenant_id,
        subscription_id=live.subscription_id,
        location=live.location,
    )
    return preview, planned, live


def _azure_subject_previews(
    planned: object,
    identity: object,
    driver: AzurePhaseDriver,
    *,
    creates: list[str],
    adopts: list[str],
    unchanged: list[str],
    updates: list[str],
) -> tuple[_AzureSubjectPreview, ...]:
    provider = driver._client(None)
    previews: list[_AzureSubjectPreview] = []
    for subject in getattr(planned, 'subjects', ()):
        existing = provider._get_fic(identity, str(subject))
        if existing is None:
            disposition: PreviewDisposition = 'create'
        else:
            props = existing.get('properties', existing)
            if (
                isinstance(props, Mapping)
                and props.get('issuer') == 'https://token.actions.githubusercontent.com'
                and props.get('subject') == subject
                and props.get('audiences') == ['api://AzureADTokenExchange']
            ):
                disposition = 'unchanged'
            else:
                disposition = 'update'
        _bucket(
            creates,
            adopts,
            unchanged,
            updates,
            disposition=disposition,
            label=f'Azure OIDC subject {subject}',
        )
        previews.append(_AzureSubjectPreview(subject=str(subject), disposition=disposition))
    return tuple(previews)


def _azure_role_previews(
    planned: object,
    identity: object,
    driver: AzurePhaseDriver,
    *,
    creates: list[str],
    adopts: list[str],
    unchanged: list[str],
    updates: list[str],
) -> tuple[_AzureRolePreview, ...]:
    provider = driver._client(None)
    principal_id = getattr(identity, 'principal_id', None)
    if not isinstance(principal_id, str) or not principal_id:
        principal_id = getattr(getattr(planned, 'identity', None), 'principal_id', None)
    if not isinstance(principal_id, str) or not principal_id:
        raise ValueError('approved identity principal_id is unavailable')
    subscription_id = getattr(identity, 'subscription_id', None)
    previews: list[_AzureRolePreview] = []
    for role in getattr(planned, 'roles', ()):
        assignment_id = _role_assignment_id(str(role.scope), principal_id, str(role.role_definition_id))
        existing = provider._get_role(str(role.scope), assignment_id)
        if existing is None:
            disposition: PreviewDisposition = 'create'
        else:
            provider._verify_role_properties(
                existing,
                principal_id,
                str(role.role_definition_id),
                str(role.scope),
                str(subscription_id or str(role.scope).split('/')[2]),
                require_defaults=False,
            )
            props = existing['properties']
            assert isinstance(props, Mapping)
            if any(props.get(key) not in (None, '') for key in ('condition', 'conditionVersion', 'delegatedManagedIdentityResourceId')):
                disposition = 'update'
            else:
                disposition = 'unchanged'
        guid = str(role.role_definition_id).rsplit('/', 1)[-1].lower()
        approved = approved_role_definition(guid)
        label = approved.display_name if approved is not None else str(role.role_key)
        _bucket(
            creates,
            adopts,
            unchanged,
            updates,
            disposition=disposition,
            label=f'Azure role {label} on {role.scope}',
        )
        previews.append(_AzureRolePreview(label=label, scope=str(role.scope), disposition=disposition))
    return tuple(previews)


def build_connection_plan_preview(
    plan: ConnectionPlan,
    *,
    plan_input: BootstrapPlanInput,
    github_driver: GitHubPhaseDriver,
    azure_driver: AzurePhaseDriver,
) -> ConnectionPlanPreview:
    creates: list[str] = []
    adopts: list[str] = []
    unchanged: list[str] = []
    updates: list[str] = []
    github_environments = _github_environment_previews(
        plan,
        github_driver,
        creates=creates,
        adopts=adopts,
        unchanged=unchanged,
        updates=updates,
    )
    azure_identity, planned, identity = _azure_identity_preview(plan, plan_input, azure_driver)
    _bucket(
        creates,
        adopts,
        unchanged,
        updates,
        disposition=azure_identity.disposition,
        label=f'Azure identity {azure_identity.name}',
    )
    oidc_subjects = _azure_subject_previews(
        planned,
        identity,
        azure_driver,
        creates=creates,
        adopts=adopts,
        unchanged=unchanged,
        updates=updates,
    )
    role_assignments = _azure_role_previews(
        planned,
        identity,
        azure_driver,
        creates=creates,
        adopts=adopts,
        unchanged=unchanged,
        updates=updates,
    )
    return ConnectionPlanPreview(
        repository_identity=plan.repository_identity,
        operation_id=plan.operation_id,
        github_environments=github_environments,
        azure_identity=azure_identity,
        oidc_subjects=oidc_subjects,
        role_assignments=role_assignments,
        creates=tuple(creates),
        adopts=tuple(adopts),
        unchanged=tuple(unchanged),
        updates=tuple(updates),
    )


def _phase_summary(phase: ConnectionPhaseState) -> str:
    parts = []
    if phase.created_actions:
        parts.append(f'{len(phase.created_actions)} created')
    if phase.adopted_actions:
        parts.append(f'{len(phase.adopted_actions)} adopted')
    if phase.changed_actions:
        parts.append(f'{len(phase.changed_actions)} changed')
    if phase.compensation_required_actions:
        parts.append(f'{len(phase.compensation_required_actions)} compensation-required')
    return ', '.join(parts) if parts else phase.summary


def _next_action_text(status: ConnectionStatus) -> str:
    command = status.next_action
    if command == 'bind-approval':
        return (
            'run foundry-opt bootstrap connect approve --repository-id '
            f'{status.repository_identity} --operation-id {status.operation_id} --actor <owner> --summary "<reason>" '
            'or rerun apply with --approve'
        )
    if command == 'apply':
        suffix = ' --approve --actor <owner> --summary "<reason>"' if status.approval_hash is None else ''
        return (
            'run foundry-opt bootstrap connect apply --repository-id '
            f'{status.repository_identity} --operation-id {status.operation_id}{suffix}'
        )
    if command == 'rollback':
        return (
            'run foundry-opt bootstrap connect rollback --repository-id '
            f'{status.repository_identity} --operation-id {status.operation_id}'
        )
    if command == 'rebuild-plan':
        return 'rerun foundry-opt bootstrap connect plan with the reviewed inputs and exact runtime commit'
    if command == 'inspect-interrupted-state':
        return 'inspect the recorded connection state before retrying this step'
    return 'none'


def render_connection_status(
    status: ConnectionStatus,
    *,
    markdown: bool = False,
    title: str = 'Connection status',
) -> str:
    lines = [_heading(title, markdown=markdown)]
    lines.append(f'- Repository: {status.repository_identity}')
    lines.append(f'- Operation: {status.operation_id}')
    lines.append(f'- State: {status.overall_state}')
    for phase in status.phase_states:
        lines.append(f'- {phase.phase}: {phase.state} - {_phase_summary(phase)}')
    lines.append(f'- Approval recorded: {_yes_no(status.approval_hash is not None)}')
    lines.append(f'- Rollback ready: {_yes_no(status.rollback_ready)}')
    if status.next_action is not None:
        lines.append(f'- Next action: {_next_action_text(status)}')
    return '\n'.join(lines)


def render_connection_approval(
    approval: ConnectionApproval,
    status: ConnectionStatus,
    *,
    markdown: bool = False,
) -> str:
    lines = [_heading('Connection approval', markdown=markdown)]
    lines.append(f'- Repository: {approval.repository_identity}')
    lines.append(f'- Operation: {approval.operation_id}')
    lines.append(f'- Actor: {approval.actor}')
    lines.append(f'- Summary: {approval.summary}')
    lines.append(f'- Next action: {_next_action_text(status)}')
    return '\n'.join(lines)


__all__ = [
    'ConnectionPlanPreview',
    'build_connection_plan_preview',
    'render_connection_approval',
    'render_connection_status',
]
