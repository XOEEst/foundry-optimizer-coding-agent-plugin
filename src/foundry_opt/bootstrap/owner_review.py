from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re
from typing import Literal
from urllib.parse import quote

from foundry_opt.bootstrap.contracts import BootstrapPlan, RootRegistry
from foundry_opt.bootstrap.evaluation.execution import (
    EvaluationFinalization,
    EvaluationOnboardingRequest,
    ONBOARDING_STAGES,
)
from foundry_opt.bootstrap.input_contracts import BootstrapPlanInput, approved_role_definition
from foundry_opt.bootstrap.operation_state import (
    DiscoveredAgentRecord,
    OperationStateEnvelope,
    OperationStatus,
    SelectionPlan,
    status_from_state,
)
from foundry_opt.bootstrap.receipts import ApplyPhaseName, PhaseReceipt
from foundry_opt.models import FrozenModel

WarningCode = Literal['deployment-unverified', 'repository-conflicts-unresolved']
RepositoryIntent = Literal['add', 'update', 'preserve', 'conflict']
VerificationChoiceKind = Literal[
    'reviewed_binding_evidence',
    'reviewed_claim_only',
    'not_applicable',
    'unavailable',
]
ResourceDisposition = Literal['created', 'adopted', 'unchanged']

_PHASE_ORDER: tuple[ApplyPhaseName, ...] = (
    'repository',
    'github',
    'azure',
    'evaluations',
)
_DATASET_URI_RE = re.compile(
    r'^azureai://accounts/[^/]+/projects/[^/]+/data/(?P<name>[^/]+)/versions/(?P<version>[^/]+)$'
)
_EVALUATOR_URI_RE = re.compile(
    r'^azureai://accounts/[^/]+/projects/[^/]+/evaluators/(?P<name>[^/]+)/versions/(?P<version>[^/]+)$'
)
_DEFINITION_URI_RE = re.compile(
    r'^azureai://accounts/[^/]+/projects/[^/]+/evaluationDefinitions/(?P<name>[^/]+)/versions/(?P<version>[^/]+)$'
)


class ReviewWarning(FrozenModel):
    code: WarningCode
    summary: str


class DiscoveryAgentReview(FrozenModel):
    repo_agent_id: str
    selected: bool
    root: str
    source_root: str
    package_root: str
    readiness: Literal['ready', 'not-ready']
    binding_classification: str
    summary: str
    blockers: tuple[str, ...] = ()
    registered: bool | None = None
    enabled_intent: bool | None = None


class RepositoryFileReview(FrozenModel):
    path: str
    template_id: str | None = None
    intent: RepositoryIntent
    proposed_path: str | None = None


class GitHubEnvironmentReview(FrozenModel):
    environment: str
    variables: tuple[str, ...] = ()
    branch_policy: str | None = None


class AzureIdentityReview(FrozenModel):
    kind: Literal['managed-identity', 'entra-application']
    disposition: Literal['create', 'adopt']
    name: str
    resource_id: str | None = None
    client_id: str | None = None
    object_id: str | None = None
    principal_id: str | None = None
    tenant_id: str | None = None
    subscription_id: str | None = None
    location: str | None = None


class RoleAssignmentReview(FrozenModel):
    alias: str
    display_name: str | None = None
    scope: str
    role_definition_id: str


class VerificationChoiceReview(FrozenModel):
    kind: VerificationChoiceKind
    summary: str
    verified_agent_ids: tuple[str, ...] = ()


class DeploymentReview(FrozenModel):
    repo_agent_id: str
    environment: str | None = None
    enabled: bool
    binding_classification: str | None = None
    require_aligned_binding: bool | None = None
    warning: str | None = None


class PlannedResourceReview(FrozenModel):
    disposition: ResourceDisposition
    resource_type: str
    identifier: str
    repo_agent_id: str | None = None
    detail: str | None = None


class PhaseProgressReview(FrozenModel):
    phase: ApplyPhaseName
    state: str
    summary: str
    completed_steps: int | None = None
    total_steps: int | None = None
    current_step: str | None = None
    step_details: tuple[str, ...] = ()


class ResourceLink(FrozenModel):
    label: str
    target: str
    url: str | None = None


class _RenderableReview(FrozenModel):
    def render_markdown(self) -> str:
        return '\n'.join(self._render_lines(markdown=True))

    def render_text(self) -> str:
        return '\n'.join(self._render_lines(markdown=False))

    def _render_lines(self, *, markdown: bool) -> list[str]:
        raise NotImplementedError


class DiscoveryReview(_RenderableReview):
    repository_root: str
    agents_found: int
    selected_agent_ids: tuple[str, ...] = ()
    agents: tuple[DiscoveryAgentReview, ...] = ()

    def _render_lines(self, *, markdown: bool) -> list[str]:
        lines = [_heading('Discovery review', markdown=markdown)]
        lines.append(f'- Agents found: {self.agents_found}')
        if self.selected_agent_ids:
            lines.append(f'- Selected stable IDs: {", ".join(self.selected_agent_ids)}')
        for agent in self.agents:
            lines.append(f'- {agent.repo_agent_id}')
            lines.append(f'  - Folder: {agent.root}')
            lines.append(f'  - Source root: {agent.source_root}')
            lines.append(f'  - State: {agent.summary}')
            if agent.blockers:
                lines.append(f'  - Blockers: {", ".join(agent.blockers)}')
            else:
                lines.append('  - Blockers: none')
            if agent.registered is not None:
                if not agent.registered:
                    lines.append('  - Registry intent: not registered')
                elif agent.enabled_intent is None:
                    lines.append('  - Registry intent: registered')
                else:
                    state = 'enabled' if agent.enabled_intent else 'disabled'
                    lines.append(f'  - Registry intent: registered, {state}')
        return lines


class PlanReview(_RenderableReview):
    required_phases: tuple[str, ...]
    repository_files: tuple[RepositoryFileReview, ...] = ()
    github_environments: tuple[GitHubEnvironmentReview, ...] = ()
    azure_identity: AzureIdentityReview | None = None
    oidc_subjects: tuple[str, ...] = ()
    role_assignments: tuple[RoleAssignmentReview, ...] = ()
    verification: VerificationChoiceReview
    deployments: tuple[DeploymentReview, ...] = ()
    resource_dispositions: tuple[PlannedResourceReview, ...] = ()
    warnings: tuple[ReviewWarning, ...] = ()

    def _render_lines(self, *, markdown: bool) -> list[str]:
        lines = [_heading('Plan review', markdown=markdown)]
        lines.append(f'- Phases: {", ".join(self.required_phases) if self.required_phases else "none"}')
        if self.repository_files:
            counts = defaultdict(int)
            for item in self.repository_files:
                counts[item.intent] += 1
            lines.append(
                '- Repository files: '
                f'{counts["add"]} add, {counts["update"]} update, '
                f'{counts["preserve"]} preserve, {counts["conflict"]} conflict'
            )
            for item in self.repository_files:
                detail = item.path
                if item.intent == 'conflict' and item.proposed_path:
                    detail = f'{item.path} -> {item.proposed_path}'
                lines.append(f'  - {item.intent}: {detail}')
        if self.github_environments:
            lines.append('- GitHub:')
            for env in self.github_environments:
                parts = []
                if env.variables:
                    parts.append(', '.join(env.variables))
                if env.branch_policy:
                    parts.append(env.branch_policy)
                suffix = f' ({"; ".join(parts)})' if parts else ''
                lines.append(f'  - {env.environment}{suffix}')
        if self.azure_identity is not None:
            lines.append(
                '- Azure identity: '
                f'{self.azure_identity.disposition} {self.azure_identity.kind} {self.azure_identity.name}'
            )
        if self.oidc_subjects:
            lines.append('- OIDC subjects:')
            for subject in self.oidc_subjects:
                lines.append(f'  - {subject}')
        if self.role_assignments:
            lines.append('- RBAC:')
            for role in self.role_assignments:
                label = role.display_name or role.alias
                lines.append(f'  - {label} on {role.scope}')
        lines.append(f'- Verification: {self.verification.summary}')
        if self.deployments:
            lines.append('- Deployment:')
            for deployment in self.deployments:
                env = deployment.environment or 'not planned'
                state = 'enabled' if deployment.enabled else 'blocked'
                detail = f'{deployment.repo_agent_id} -> {env} ({state})'
                if deployment.warning:
                    detail = f'{detail} — {deployment.warning}'
                lines.append(f'  - {detail}')
        if self.resource_dispositions:
            grouped = _group_resource_dispositions(self.resource_dispositions)
            lines.append('- Resource intent:')
            for bucket in ('created', 'adopted', 'unchanged'):
                if not grouped[bucket]:
                    continue
                rendered = '; '.join(grouped[bucket])
                lines.append(f'  - {bucket.capitalize()}: {rendered}')
        if self.warnings:
            lines.append('- Warnings:')
            for warning in self.warnings:
                lines.append(f'  - {warning.code}: {warning.summary}')
        return lines


class StatusReview(_RenderableReview):
    phases: tuple[PhaseProgressReview, ...]
    failures: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    next_action: str
    verification_eligible: bool
    deployment_eligible: bool

    def _render_lines(self, *, markdown: bool) -> list[str]:
        lines = [_heading('Status review', markdown=markdown)]
        for phase in self.phases:
            detail = f'{phase.phase}: {phase.state}'
            if phase.summary:
                detail = f'{detail} — {phase.summary}'
            if phase.completed_steps is not None and phase.total_steps is not None:
                detail = f'{detail} ({phase.completed_steps}/{phase.total_steps} steps)'
            lines.append(f'- {detail}')
            for step in phase.step_details:
                lines.append(f'  - {step}')
        if self.failures:
            lines.append('- Failures:')
            for failure in self.failures:
                lines.append(f'  - {failure}')
        if self.blockers:
            lines.append('- Blockers:')
            for blocker in self.blockers:
                lines.append(f'  - {blocker}')
        lines.append(f'- Next action: {self.next_action}')
        lines.append(f'- Verification eligible: {_yes_no(self.verification_eligible)}')
        lines.append(f'- Deployment eligible: {_yes_no(self.deployment_eligible)}')
        return lines


class ResourceLinksReview(_RenderableReview):
    github: tuple[ResourceLink, ...] = ()
    azure: tuple[ResourceLink, ...] = ()
    foundry: tuple[ResourceLink, ...] = ()

    def _render_lines(self, *, markdown: bool) -> list[str]:
        lines = [_heading('Resource links', markdown=markdown)]
        for title, values in (
            ('GitHub', self.github),
            ('Azure', self.azure),
            ('Foundry', self.foundry),
        ):
            if not values:
                continue
            lines.append(_heading(title, markdown=markdown, level=3))
            for item in values:
                lines.append(f'- {item.label}: {_render_target(item, markdown=markdown)}')
        return lines


class OwnerReviewBundle(_RenderableReview):
    discovery: DiscoveryReview
    plan: PlanReview
    status: StatusReview
    resource_links: ResourceLinksReview

    def _render_lines(self, *, markdown: bool) -> list[str]:
        lines: list[str] = []
        for index, section in enumerate(
            (self.discovery, self.plan, self.status, self.resource_links)
        ):
            if index:
                lines.append('')
            lines.extend(section._render_lines(markdown=markdown))
        return lines


@dataclass(frozen=True, slots=True)
class _EvaluationContext:
    repo_agent_id: str
    project_endpoint: str | None
    agent_name: str | None
    agent_version: str | None
    contract: EvaluationOnboardingRequest | None


def build_discovery_review(
    source: SelectionPlan | OperationStateEnvelope,
    *,
    registry: RootRegistry | None = None,
) -> DiscoveryReview:
    selection = source.selection_plan if isinstance(source, OperationStateEnvelope) else source
    selected_ids = tuple(
        sorted(
            {item for item in selection.selected_agent_ids if item},
            key=str.casefold,
        )
    )
    registry_by_id = (
        {item.agent_id.casefold(): item for item in registry.agents}
        if registry is not None
        else {}
    )
    records = selection.discovered_agents or _fallback_discovered_agents(selection)
    agents = []
    for record in sorted(records, key=lambda item: (item.repo_agent_id.casefold(), item.root, item.source_root)):
        registry_entry = registry_by_id.get(record.repo_agent_id.casefold())
        blockers = tuple(
            sorted({item.detail for item in getattr(record, 'blockers', ()) if item.detail})
        )
        if not blockers and record.detail and record.classification == 'not-ready':
            blockers = (record.detail,)
        agents.append(
            DiscoveryAgentReview(
                repo_agent_id=record.repo_agent_id,
                selected=record.repo_agent_id.casefold() in {item.casefold() for item in selected_ids},
                root=record.root,
                source_root=record.source_root,
                package_root=record.package_root,
                readiness='not-ready' if record.classification == 'not-ready' else 'ready',
                binding_classification=record.classification,
                summary=_binding_summary(record.classification, record.detail),
                blockers=blockers,
                registered=None if registry is None else registry_entry is not None,
                enabled_intent=None if registry_entry is None else registry_entry.enabled,
            )
        )
    return DiscoveryReview(
        repository_root=selection.repository_root,
        agents_found=len(agents),
        selected_agent_ids=selected_ids,
        agents=tuple(agents),
    )


def build_plan_review(
    source: BootstrapPlan | OperationStateEnvelope,
    *,
    plan_input: BootstrapPlanInput | None = None,
    verified_binding_classifications: Mapping[str, str] | None = None,
) -> PlanReview:
    plan = source.bootstrap_plan if isinstance(source, OperationStateEnvelope) else source
    required_phases = (
        tuple(source.required_phases)
        if isinstance(source, OperationStateEnvelope) and source.required_phases
        else tuple(plan_input.required_phases)
        if plan_input is not None
        else _required_phases_from_plan(plan)
    )
    repository_files = tuple(sorted(_repository_file_reviews(plan), key=lambda item: (item.path, item.intent)))
    github_environments = tuple(sorted(_github_environment_reviews(plan, plan_input), key=lambda item: item.environment.casefold()))
    azure_identity = _azure_identity_review(plan)
    oidc_subjects = tuple(sorted(_oidc_subjects(plan), key=str.casefold))
    role_assignments = tuple(
        sorted(_role_assignment_reviews(plan), key=lambda item: (item.alias.casefold(), item.scope.casefold()))
    )
    verification = _verification_choice(plan_input, verified_binding_classifications)
    evaluation_contexts = _evaluation_contexts(plan, plan_input)
    deployments = tuple(
        sorted(_deployment_reviews(evaluation_contexts), key=lambda item: item.repo_agent_id.casefold())
    )
    resource_dispositions = tuple(
        sorted(
            _planned_resource_reviews(evaluation_contexts, azure_identity),
            key=lambda item: (
                ('created', 'adopted', 'unchanged').index(item.disposition),
                item.repo_agent_id or '',
                item.resource_type,
                item.identifier,
            ),
        )
    )
    warnings = _plan_warnings(repository_files, verification, deployments)
    return PlanReview(
        required_phases=required_phases,
        repository_files=repository_files,
        github_environments=github_environments,
        azure_identity=azure_identity,
        oidc_subjects=oidc_subjects,
        role_assignments=role_assignments,
        verification=verification,
        deployments=deployments,
        resource_dispositions=resource_dispositions,
        warnings=warnings,
    )


def build_status_review(
    source: OperationStatus | OperationStateEnvelope,
    *,
    required_phases: Sequence[str] | None = None,
    selected_agent_ids: Sequence[str] | None = None,
) -> StatusReview:
    if isinstance(source, OperationStateEnvelope):
        status = status_from_state(source)
        phase_receipts = status.phase_states
        selected_ids = tuple(source.selection_plan.selected_agent_ids)
        required = tuple(source.required_phases or required_phases or ())
    else:
        status = source
        phase_receipts = status.phase_states
        selected_ids = tuple(selected_agent_ids or ())
        required = tuple(required_phases or ())
    if not required:
        required = _required_phases_from_receipts(phase_receipts)
    receipt_by_phase = {item.phase: item for item in phase_receipts}
    phases = []
    for phase in _ordered_phases(required or receipt_by_phase):
        receipt = receipt_by_phase.get(phase)
        if receipt is None:
            phases.append(
                PhaseProgressReview(
                    phase=phase,
                    state='pending',
                    summary='awaiting approval and apply',
                )
            )
            continue
        completed_steps = None
        total_steps = None
        current_step = None
        step_details: tuple[str, ...] = ()
        if phase == 'evaluations':
            step_details, completed_steps, total_steps, current_step = _evaluation_step_progress(receipt.provider_state)
        elif receipt.state == 'applied':
            completed_steps, total_steps = 1, 1
        elif receipt.state == 'rolled_back':
            completed_steps, total_steps = 0, 1
        elif receipt.state == 'applying':
            completed_steps, total_steps = 0, 1
            current_step = 'apply'
        phases.append(
            PhaseProgressReview(
                phase=phase,
                state=receipt.state,
                summary=_phase_summary(receipt),
                completed_steps=completed_steps,
                total_steps=total_steps,
                current_step=current_step,
                step_details=step_details,
            )
        )
    relevant_ids = {item.casefold() for item in selected_ids if item}
    if not relevant_ids:
        relevant_ids = {item.agent_id.casefold() for item in status.binding_assessments}
    aligned = {
        item.agent_id.casefold()
        for item in status.binding_assessments
        if item.classification == 'bound-aligned'
    }
    verification_eligible = not relevant_ids or relevant_ids <= aligned
    blockers = _status_blockers(status, relevant_ids)
    failures = _status_failures(phase_receipts)
    next_action = _next_action(
        phases=tuple(phases),
        binding_assessments=status.binding_assessments,
        relevant_ids=relevant_ids,
        verification_eligible=verification_eligible,
        deployment_eligible=status.deployment_eligible,
    )
    return StatusReview(
        phases=tuple(phases),
        failures=failures,
        blockers=blockers,
        next_action=next_action,
        verification_eligible=verification_eligible,
        deployment_eligible=status.deployment_eligible,
    )


def build_resource_links(
    source: OperationStateEnvelope | None = None,
    *,
    repository_id: str | None = None,
    bootstrap_plan: BootstrapPlan | None = None,
    plan_input: BootstrapPlanInput | None = None,
    phase_receipts: Sequence[PhaseReceipt] | None = None,
) -> ResourceLinksReview:
    if source is not None:
        repository_id = source.repository_id
        bootstrap_plan = source.bootstrap_plan
        phase_receipts = source.phase_receipts
    receipts = tuple(phase_receipts or ())
    repo_id = (
        repository_id
        or (plan_input.repository.repository_id if plan_input is not None else None)
        or (bootstrap_plan.repository_identity if bootstrap_plan is not None else None)
    )
    github_links = _github_links(repo_id, bootstrap_plan, plan_input)
    azure_links = _azure_links(bootstrap_plan, plan_input)
    foundry_links = _foundry_links(bootstrap_plan, plan_input, receipts)
    return ResourceLinksReview(
        github=tuple(github_links),
        azure=tuple(azure_links),
        foundry=tuple(foundry_links),
    )


def build_owner_review(
    source: OperationStateEnvelope,
    *,
    plan_input: BootstrapPlanInput | None = None,
    registry: RootRegistry | None = None,
    verified_binding_classifications: Mapping[str, str] | None = None,
) -> OwnerReviewBundle:
    return OwnerReviewBundle(
        discovery=build_discovery_review(source, registry=registry),
        plan=build_plan_review(
            source,
            plan_input=plan_input,
            verified_binding_classifications=verified_binding_classifications,
        ),
        status=build_status_review(source),
        resource_links=build_resource_links(source, plan_input=plan_input),
    )


def _fallback_discovered_agents(selection: SelectionPlan) -> tuple[DiscoveredAgentRecord, ...]:
    return tuple(
        DiscoveredAgentRecord(
            repo_agent_id=item.agent_id,
            root=item.agent_id,
            source_root=item.agent_id,
            package_root=item.agent_id,
            source_fingerprint='0' * 64,
            package_fingerprint='0' * 64,
            classification=item.classification,
            detail=item.detail,
            blockers=(),
        )
        for item in selection.binding_assessments
    )


def _required_phases_from_plan(plan: BootstrapPlan) -> tuple[str, ...]:
    phases = {action.phase for action in plan.actions}
    return _ordered_phases(phases)


def _required_phases_from_receipts(receipts: Sequence[PhaseReceipt]) -> tuple[str, ...]:
    return _ordered_phases({item.phase for item in receipts})


def _ordered_phases(values: Sequence[str] | set[str]) -> tuple[str, ...]:
    seen = {str(value) for value in values}
    return tuple(phase for phase in _PHASE_ORDER if phase in seen)


def _binding_summary(classification: str, detail: str | None) -> str:
    base = {
        'bound-aligned': 'ready and aligned to the reviewed deployed baseline',
        'bound-diverged': 'ready, but the deployed binding diverges',
        'bound-unknown': 'ready, but the deployed binding is not yet proven',
        'ready-unbound': 'ready, but no deployed binding was found',
        'not-ready': 'not ready for binding or deployment',
    }.get(classification, classification)
    return f'{base} — {detail}' if detail else base


def _heading(title: str, *, markdown: bool, level: int = 2) -> str:
    return f'{"#" * level} {title}' if markdown else title


def _yes_no(value: bool) -> str:
    return 'yes' if value else 'no'


def _render_target(link: ResourceLink, *, markdown: bool) -> str:
    if link.url and markdown:
        return f'[{link.target}]({link.url})'
    if link.url:
        return f'{link.target} ({link.url})'
    return link.target


def _group_resource_dispositions(items: Sequence[PlannedResourceReview]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {'created': [], 'adopted': [], 'unchanged': []}
    for item in items:
        label = item.identifier
        if item.repo_agent_id:
            label = f'{item.repo_agent_id} {label}'
        if item.detail:
            label = f'{label} ({item.detail})'
        grouped[item.disposition].append(label)
    return grouped


def _repository_file_reviews(plan: BootstrapPlan) -> list[RepositoryFileReview]:
    reviews: list[RepositoryFileReview] = []
    for action in plan.actions:
        if action.phase != 'repository' or action.template_payload is None:
            continue
        mode = _repository_mode(action)
        target_missing = _repository_target_missing(action)
        proposed = _repository_conflict_path(action)
        if mode == 'conflict':
            intent: RepositoryIntent = 'conflict'
        elif mode == 'write' and target_missing:
            intent = 'add'
        elif mode == 'write':
            intent = 'update'
        else:
            intent = 'preserve'
        reviews.append(
            RepositoryFileReview(
                path=action.template_payload.destination_path,
                template_id=action.template_payload.template_id,
                intent=intent,
                proposed_path=proposed,
            )
        )
    return reviews


def _repository_mode(action: object) -> str:
    for item in getattr(action, 'diagnostics', ()):
        if isinstance(item, str) and item.startswith('mode:'):
            return item.split(':', 1)[1]
    return 'skip'


def _repository_target_missing(action: object) -> bool:
    for item in getattr(action, 'diagnostics', ()):
        if isinstance(item, str) and item.startswith('target:'):
            return item.endswith('missing')
    return False


def _repository_conflict_path(action: object) -> str | None:
    for item in getattr(action, 'diagnostics', ()):
        if isinstance(item, str) and item.startswith('conflict:'):
            value = item.split(':', 1)[1]
            return value or None
    return None


def _github_environment_reviews(
    plan: BootstrapPlan,
    plan_input: BootstrapPlanInput | None,
) -> list[GitHubEnvironmentReview]:
    environments: dict[str, dict[str, object]] = {}
    for action in plan.actions:
        if action.phase != 'github':
            continue
        if action.kind == 'github-environment' and action.diagnostics:
            environment = action.diagnostics[0]
            environments.setdefault(environment, {'variables': set(), 'branch_policy': None})
        elif action.kind == 'github-variable' and len(action.diagnostics) >= 2:
            environment = action.diagnostics[0]
            variable = action.diagnostics[1]
            state = environments.setdefault(environment, {'variables': set(), 'branch_policy': None})
            state['variables'].add(variable)
        elif action.kind == 'github-branch-policy' and len(action.diagnostics) >= 2:
            environment, branch = action.diagnostics[0], action.diagnostics[1]
            state = environments.setdefault(environment, {'variables': set(), 'branch_policy': None})
            state['branch_policy'] = _branch_policy_summary(plan_input, branch)
    return [
        GitHubEnvironmentReview(
            environment=name,
            variables=tuple(sorted(state['variables'], key=str.casefold)),
            branch_policy=state['branch_policy'] if isinstance(state['branch_policy'], str) else None,
        )
        for name, state in environments.items()
    ]


def _branch_policy_summary(
    plan_input: BootstrapPlanInput | None,
    branch: str,
) -> str:
    if plan_input is None or plan_input.github_phase is None:
        return f'branch policy requires {branch}'
    intent = plan_input.github_phase.default_branch_policy_intent
    if intent == 'require_main':
        return 'branch policy requires main'
    return f'branch policy requires {branch}'


def _azure_identity_review(plan: BootstrapPlan) -> AzureIdentityReview | None:
    for action in plan.actions:
        if action.phase != 'azure' or action.kind not in {'managed-identity', 'entra-application'}:
            continue
        fields = _diagnostics_map(action.diagnostics)
        return AzureIdentityReview(
            kind=action.kind,
            disposition='adopt' if fields.get('adopted', 'false') == 'true' else 'create',
            name=fields.get('name', ''),
            resource_id=fields.get('resource_id'),
            client_id=fields.get('client_id'),
            object_id=fields.get('object_id'),
            principal_id=fields.get('principal_id'),
            tenant_id=fields.get('tenant_id'),
            subscription_id=fields.get('subscription_id'),
            location=fields.get('location'),
        )
    return None


def _oidc_subjects(plan: BootstrapPlan) -> list[str]:
    subjects: list[str] = []
    for action in plan.actions:
        if action.phase != 'azure' or action.kind != 'federated-credential':
            continue
        fields = _diagnostics_map(action.diagnostics)
        subject = fields.get('subject')
        if subject:
            subjects.append(subject)
    return subjects


def _role_assignment_reviews(plan: BootstrapPlan) -> list[RoleAssignmentReview]:
    reviews: list[RoleAssignmentReview] = []
    for action in plan.actions:
        if action.phase != 'azure' or action.kind != 'role-assignment':
            continue
        fields = _diagnostics_map(action.diagnostics)
        role_definition_id = fields.get('role_definition_id', '')
        guid = role_definition_id.rsplit('/', 1)[-1].lower() if role_definition_id else ''
        approved = approved_role_definition(guid) if guid else None
        reviews.append(
            RoleAssignmentReview(
                alias=fields.get('role', ''),
                display_name=None if approved is None else approved.display_name,
                scope=fields.get('scope', ''),
                role_definition_id=role_definition_id,
            )
        )
    return reviews


def _verification_choice(
    plan_input: BootstrapPlanInput | None,
    verified_binding_classifications: Mapping[str, str] | None,
) -> VerificationChoiceReview:
    if plan_input is None:
        return VerificationChoiceReview(
            kind='unavailable',
            summary='verification choice is unavailable from the plan alone',
        )
    if plan_input.evaluations_phase is None:
        return VerificationChoiceReview(
            kind='not_applicable',
            summary='no evaluation deployment is planned',
        )
    if plan_input.binding_evidence is None:
        return VerificationChoiceReview(
            kind='reviewed_claim_only',
            summary='binding evidence was not supplied, so reviewed deployment claims remain unverified',
        )
    verified_source = (
        verified_binding_classifications
        if verified_binding_classifications is not None
        else {item.repo_agent_id: item.repo_agent_id for item in plan_input.binding_evidence.agents}
    )
    verified = tuple(sorted({str(key) for key in verified_source}, key=str.casefold))
    return VerificationChoiceReview(
        kind='reviewed_binding_evidence',
        summary='binding evidence was supplied and deployment claims can be re-verified',
        verified_agent_ids=verified,
    )


def _evaluation_contexts(
    plan: BootstrapPlan | None,
    plan_input: BootstrapPlanInput | None,
) -> tuple[_EvaluationContext, ...]:
    contexts: dict[str, _EvaluationContext] = {}
    if plan_input is not None and plan_input.evaluations_phase is not None:
        for agent in plan_input.evaluations_phase.agents:
            contexts[agent.repo_agent_id.casefold()] = _EvaluationContext(
                repo_agent_id=agent.repo_agent_id,
                project_endpoint=agent.project_endpoint,
                agent_name=agent.agent_name,
                agent_version=agent.agent_version,
                contract=agent.onboarding_contract,
            )
    if plan is not None:
        for action in plan.actions:
            if action.kind != 'evaluation_onboarding':
                continue
            contract = _contract_from_action(action)
            if contract is None:
                continue
            project_endpoint = (
                contract.sidecar_policy.foundry_project.project_endpoint
                if contract.sidecar_policy is not None
                else None
            )
            agent_name = (
                contract.dataset_plan.agent_name
                if contract.dataset_plan is not None
                else None
            )
            agent_version = (
                contract.dataset_plan.agent_version
                if contract.dataset_plan is not None
                else None
            )
            contexts.setdefault(
                contract.repo_agent_id.casefold(),
                _EvaluationContext(
                    repo_agent_id=contract.repo_agent_id,
                    project_endpoint=project_endpoint,
                    agent_name=agent_name,
                    agent_version=agent_version,
                    contract=contract,
                ),
            )
    return tuple(sorted(contexts.values(), key=lambda item: item.repo_agent_id.casefold()))


def _contract_from_action(action: object) -> EvaluationOnboardingRequest | None:
    diagnostics = getattr(action, 'diagnostics', ())
    if getattr(action, 'kind', None) != 'evaluation_onboarding' or len(diagnostics) < 3:
        return None
    try:
        return EvaluationOnboardingRequest.model_validate_json(diagnostics[2])
    except Exception:
        return None


def _deployment_reviews(
    contexts: Sequence[_EvaluationContext],
) -> list[DeploymentReview]:
    reviews: list[DeploymentReview] = []
    for context in contexts:
        contract = context.contract
        if contract is None:
            continue
        if contract.sidecar_policy is None:
            reviews.append(
                DeploymentReview(
                    repo_agent_id=context.repo_agent_id,
                    enabled=False,
                    binding_classification=contract.binding_classification,
                    warning=contract.stop_reason,
                )
            )
            continue
        deployment = contract.sidecar_policy.deployment
        warning = None
        if not deployment.enabled:
            warning = (
                'deployment stays blocked until binding is aligned'
                if contract.binding_classification != 'bound-aligned'
                else 'deployment is disabled in the reviewed sidecar policy'
            )
        reviews.append(
            DeploymentReview(
                repo_agent_id=context.repo_agent_id,
                environment=deployment.environment,
                enabled=deployment.enabled,
                binding_classification=contract.binding_classification,
                require_aligned_binding=deployment.require_aligned_binding,
                warning=warning,
            )
        )
    return reviews


def _planned_resource_reviews(
    contexts: Sequence[_EvaluationContext],
    azure_identity: AzureIdentityReview | None,
) -> list[PlannedResourceReview]:
    reviews: list[PlannedResourceReview] = []
    if azure_identity is not None:
        reviews.append(
            PlannedResourceReview(
                disposition='created' if azure_identity.disposition == 'create' else 'adopted',
                resource_type='azure identity',
                identifier=azure_identity.name,
            )
        )
    for context in contexts:
        contract = context.contract
        if context.project_endpoint:
            reviews.append(
                PlannedResourceReview(
                    disposition='unchanged',
                    resource_type='foundry project',
                    identifier=context.project_endpoint,
                    repo_agent_id=context.repo_agent_id,
                )
            )
        if context.agent_name and context.agent_version:
            reviews.append(
                PlannedResourceReview(
                    disposition='unchanged',
                    resource_type='agent version',
                    identifier=f'{context.agent_name}:{context.agent_version}',
                    repo_agent_id=context.repo_agent_id,
                )
            )
        if contract is None or contract.stopped:
            continue
        assert contract.dataset_plan is not None
        assert contract.evaluator_plan is not None
        assert contract.definition_plan is not None
        assert contract.activation_plan is not None
        if contract.dataset_plan.reuse_candidates is not None:
            development_id, validating_id = contract.dataset_plan.reuse_candidates
            reviews.extend(
                (
                    PlannedResourceReview(
                        disposition='adopted',
                        resource_type='dataset',
                        identifier=development_id,
                        repo_agent_id=context.repo_agent_id,
                        detail='development',
                    ),
                    PlannedResourceReview(
                        disposition='adopted',
                        resource_type='dataset',
                        identifier=validating_id,
                        repo_agent_id=context.repo_agent_id,
                        detail='validating',
                    ),
                )
            )
        else:
            for role, name in (
                ('development', contract.dataset_plan.requested_development_name),
                ('validating', contract.dataset_plan.requested_validating_name),
            ):
                reviews.append(
                    PlannedResourceReview(
                        disposition='created',
                        resource_type='dataset',
                        identifier=f'{name}:{contract.dataset_plan.requested_version}',
                        repo_agent_id=context.repo_agent_id,
                        detail=role,
                    )
                )
        if contract.evaluator_plan.reuse_evaluator_id is not None:
            reviews.append(
                PlannedResourceReview(
                    disposition='adopted',
                    resource_type='evaluator',
                    identifier=contract.evaluator_plan.reuse_evaluator_id,
                    repo_agent_id=context.repo_agent_id,
                    detail='objective',
                )
            )
        else:
            reviews.append(
                PlannedResourceReview(
                    disposition='created',
                    resource_type='evaluator',
                    identifier=(
                        f'{contract.evaluator_plan.requested_name}:'
                        f'{contract.evaluator_plan.requested_version}'
                    ),
                    repo_agent_id=context.repo_agent_id,
                    detail='objective',
                )
            )
        reviews.append(
            PlannedResourceReview(
                disposition='unchanged',
                resource_type='evaluator bundle',
                identifier=', '.join(contract.evaluator_plan.required_safety_evaluators),
                repo_agent_id=context.repo_agent_id,
                detail='built-in safety evaluators',
            )
        )
        for role, name in (
            ('development', contract.definition_plan.requested_development_name),
            ('validating', contract.definition_plan.requested_validating_name),
        ):
            reviews.append(
                PlannedResourceReview(
                    disposition='created',
                    resource_type='evaluation definition',
                    identifier=name,
                    repo_agent_id=context.repo_agent_id,
                    detail=role,
                )
            )
        reviews.append(
            PlannedResourceReview(
                disposition='created',
                resource_type='draft agent',
                identifier=(
                    f'{contract.activation_plan.draft_agent_name}:'
                    f'{contract.activation_plan.draft_agent_version}'
                ),
                repo_agent_id=context.repo_agent_id,
            )
        )
        reviews.extend(
            (
                PlannedResourceReview(
                    disposition='created',
                    resource_type='activation run',
                    identifier='development',
                    repo_agent_id=context.repo_agent_id,
                ),
                PlannedResourceReview(
                    disposition='created',
                    resource_type='activation run',
                    identifier='validating',
                    repo_agent_id=context.repo_agent_id,
                ),
            )
        )
    return reviews


def _plan_warnings(
    repository_files: Sequence[RepositoryFileReview],
    verification: VerificationChoiceReview,
    deployments: Sequence[DeploymentReview],
) -> tuple[ReviewWarning, ...]:
    warnings: list[ReviewWarning] = []
    conflicts = [item for item in repository_files if item.intent == 'conflict']
    if conflicts:
        warnings.append(
            ReviewWarning(
                code='repository-conflicts-unresolved',
                summary=f'{len(conflicts)} repository file conflict(s) still need review',
            )
        )
    if deployments:
        blocked = [item for item in deployments if not item.enabled]
        if blocked:
            warnings.append(
                ReviewWarning(
                    code='deployment-unverified',
                    summary='deployment remains blocked until binding is aligned for the reviewed agent set',
                )
            )
        elif verification.kind != 'reviewed_binding_evidence':
            warnings.append(
                ReviewWarning(
                    code='deployment-unverified',
                    summary='binding evidence was not supplied, so deployment is not independently verified',
                )
            )
    return tuple(warnings)


def _phase_summary(receipt: PhaseReceipt) -> str:
    if receipt.state == 'applying':
        return 'apply in progress'
    if receipt.state == 'rolled_back':
        return receipt.rollback_summary or 'phase rolled back'
    if receipt.receipt.error_info is not None:
        return receipt.receipt.error_info.summary
    counts = []
    if receipt.receipt.created_actions:
        counts.append(f'{len(receipt.receipt.created_actions)} created')
    if receipt.receipt.adopted_actions:
        counts.append(f'{len(receipt.receipt.adopted_actions)} adopted')
    if receipt.receipt.changed_actions:
        counts.append(f'{len(receipt.receipt.changed_actions)} changed')
    if receipt.receipt.skipped_actions:
        counts.append(f'{len(receipt.receipt.skipped_actions)} unchanged')
    if not counts:
        return receipt.summary
    return ', '.join(counts)


def _evaluation_step_progress(
    provider_state: Mapping[str, object],
) -> tuple[tuple[str, ...], int | None, int | None, str | None]:
    ledgers = _foundry_onboarding_ledgers(provider_state)
    if not ledgers:
        return (), None, None, None
    details: list[str] = []
    completed_total = 0
    total_total = 0
    current: str | None = None
    for repo_agent_id, ledger in sorted(ledgers.items(), key=lambda item: item[0].casefold()):
        finalization = ledger.get('finalization')
        if isinstance(finalization, Mapping):
            completed = len(ONBOARDING_STAGES)
            current_step = None
        else:
            completed = 0
            current_step = None
            stages = ledger.get('stages')
            stage_map = stages if isinstance(stages, Mapping) else {}
            for stage in ONBOARDING_STAGES:
                entry = stage_map.get(stage)
                status = entry.get('status') if isinstance(entry, Mapping) else None
                if status == 'completed':
                    completed += 1
                elif status == 'in_flight' and current_step is None:
                    current_step = stage
            if current_step is None and completed < len(ONBOARDING_STAGES):
                for stage in ONBOARDING_STAGES:
                    entry = stage_map.get(stage)
                    status = entry.get('status') if isinstance(entry, Mapping) else None
                    if status != 'completed':
                        current_step = stage
                        break
        if current is None and current_step:
            current = f'{repo_agent_id}:{current_step}'
        completed_total += completed
        total_total += len(ONBOARDING_STAGES)
        detail = f'{repo_agent_id}: {completed}/{len(ONBOARDING_STAGES)} complete'
        if current_step:
            detail = f'{detail}; current step {current_step}'
        details.append(detail)
    return tuple(details), completed_total, total_total, current


def _foundry_onboarding_ledgers(
    provider_state: Mapping[str, object],
) -> dict[str, Mapping[str, object]]:
    ledgers: dict[str, Mapping[str, object]] = {}
    for state in _iter_foundry_provider_states(provider_state):
        onboarding = state.get('onboarding')
        if not isinstance(onboarding, Mapping):
            continue
        for action_id, ledger in onboarding.items():
            if not isinstance(ledger, Mapping):
                continue
            repo_agent_id = _repo_agent_id_from_action_id(str(action_id))
            if repo_agent_id:
                ledgers[repo_agent_id] = ledger
    return ledgers


def _iter_foundry_provider_states(
    provider_state: Mapping[str, object],
) -> tuple[Mapping[str, object], ...]:
    if not provider_state:
        return ()
    if provider_state.get('multi_project'):
        projects = provider_state.get('projects')
        if not isinstance(projects, Mapping):
            return ()
        return tuple(
            payload.get('provider_state')
            for _, payload in sorted(projects.items())
            if isinstance(payload, Mapping) and isinstance(payload.get('provider_state'), Mapping)
        )
    if provider_state.get('checkpoint'):
        projects = provider_state.get('projects')
        if not isinstance(projects, Mapping):
            return ()
        return tuple(
            snapshot
            for _, snapshot in sorted(projects.items())
            if isinstance(snapshot, Mapping)
        )
    return (provider_state,)


def _repo_agent_id_from_action_id(action_id: str) -> str:
    parts = action_id.split(':')
    if len(parts) >= 3 and parts[0] == 'evaluations':
        return parts[1]
    return ''


def _status_blockers(
    status: OperationStatus,
    relevant_ids: set[str],
) -> tuple[str, ...]:
    blockers = {_humanize_blocker(item) for item in status.blockers}
    for assessment in status.binding_assessments:
        if relevant_ids and assessment.agent_id.casefold() not in relevant_ids:
            continue
        if assessment.classification != 'bound-aligned':
            blockers.add(f'{assessment.agent_id} is {assessment.classification}')
    return tuple(sorted(blockers, key=str.casefold))


def _status_failures(receipts: Sequence[PhaseReceipt]) -> tuple[str, ...]:
    failures: list[str] = []
    for receipt in receipts:
        if receipt.state not in {'failed', 'compensation_required'}:
            continue
        summary = (
            receipt.receipt.error_info.summary
            if receipt.receipt.error_info is not None
            else receipt.summary
        )
        failures.append(f'{receipt.phase} failed: {summary}')
    return tuple(failures)


def _humanize_blocker(blocker: str) -> str:
    if blocker.startswith('phase:'):
        _, phase, state = blocker.split(':', 2)
        return f'{phase} is {state.replace("_", " ")}'
    if blocker.startswith('error:'):
        _, phase, code = blocker.split(':', 2)
        return f'{phase} reported {code}'
    return blocker


def _next_action(
    *,
    phases: Sequence[PhaseProgressReview],
    binding_assessments: Sequence[object],
    relevant_ids: set[str],
    verification_eligible: bool,
    deployment_eligible: bool,
) -> str:
    for phase in phases:
        if phase.state == 'compensation_required':
            return f'roll back or resume the {phase.phase} phase'
        if phase.state == 'failed':
            return f'fix the {phase.phase} phase and rerun bootstrap apply --phase {phase.phase}'
    for phase in phases:
        if phase.state == 'applying':
            if phase.current_step:
                return f'wait for {phase.phase} to finish {phase.current_step}'
            return f'wait for {phase.phase} apply to finish'
    for phase in phases:
        if phase.state == 'pending':
            return f'approve and apply the {phase.phase} phase'
    if not verification_eligible:
        blocked: dict[str, list[str]] = defaultdict(list)
        for assessment in binding_assessments:
            agent_id = getattr(assessment, 'agent_id', '')
            classification = getattr(assessment, 'classification', '')
            if relevant_ids and str(agent_id).casefold() not in relevant_ids:
                continue
            if classification != 'bound-aligned':
                blocked[classification].append(str(agent_id))
        if blocked.get('not-ready'):
            return f'fix runtime readiness for {", ".join(sorted(blocked["not-ready"], key=str.casefold))}'
        if blocked.get('ready-unbound'):
            return f'bind or disable deployment for {", ".join(sorted(blocked["ready-unbound"], key=str.casefold))}'
        return 'collect reviewed binding evidence or resolve deployed drift before enabling deployment'
    if not deployment_eligible:
        return 'resolve the remaining deployment blockers and rerun status'
    return 'deployment can proceed'


def _github_links(
    repository_id: str | None,
    bootstrap_plan: BootstrapPlan | None,
    plan_input: BootstrapPlanInput | None,
) -> list[ResourceLink]:
    if not repository_id:
        return []
    plan = bootstrap_plan
    environment_names: set[str] = set()
    if plan_input is not None and plan_input.github_phase is not None:
        environment_names.update(
            (
                plan_input.github_phase.optimizer_environment,
                plan_input.github_phase.deployment_environment,
            )
        )
    if plan is not None:
        for action in plan.actions:
            if action.kind == 'github-environment' and action.diagnostics:
                environment_names.add(action.diagnostics[0])
            elif action.kind == 'github-variable' and action.diagnostics:
                environment_names.add(action.diagnostics[0])
            elif action.kind == 'github-branch-policy' and action.diagnostics:
                environment_names.add(action.diagnostics[0])
    repo_url = _github_repo_url(repository_id)
    links = [ResourceLink(label='Actions', target=repo_url + '/actions', url=repo_url + '/actions')]
    for environment in sorted(environment_names, key=str.casefold):
        env_url = f'{repo_url}/settings/environments/{quote(environment, safe="")}'
        links.append(
            ResourceLink(
                label=f'Environment {environment}',
                target=environment,
                url=env_url,
            )
        )
    return links


def _azure_links(
    bootstrap_plan: BootstrapPlan | None,
    plan_input: BootstrapPlanInput | None,
) -> list[ResourceLink]:
    identity = None
    if plan_input is not None and plan_input.azure_phase is not None:
        azure = plan_input.azure_phase
        if azure.identity.identity_kind == 'user_assigned_managed_identity':
            identity = ResourceLink(
                label='Managed identity',
                target=azure.identity.existing_resource_id or 'available after apply',
                url=_arm_resource_url(azure.identity.existing_resource_id),
            )
        elif azure.identity.existing_object_id:
            identity = ResourceLink(
                label='Entra application',
                target=azure.identity.existing_client_id or azure.identity.existing_object_id,
                url=_graph_application_url(azure.identity.existing_object_id),
            )
    elif bootstrap_plan is not None:
        parsed = _azure_identity_review(bootstrap_plan)
        if parsed is not None:
            if parsed.resource_id:
                identity = ResourceLink(
                    label='Managed identity',
                    target=parsed.resource_id,
                    url=_arm_resource_url(parsed.resource_id),
                )
            elif parsed.object_id:
                identity = ResourceLink(
                    label='Entra application',
                    target=parsed.client_id or parsed.object_id,
                    url=_graph_application_url(parsed.object_id),
                )
    links = [identity] if identity is not None else []
    role_scopes: set[tuple[str, str]] = set()
    if plan_input is not None and plan_input.azure_phase is not None:
        role_scopes.update(
            (item.alias, item.scope) for item in plan_input.azure_phase.approved_role_assignments
        )
    elif bootstrap_plan is not None:
        for role in _role_assignment_reviews(bootstrap_plan):
            role_scopes.add((role.alias, role.scope))
    for alias, scope in sorted(role_scopes, key=lambda item: (item[0].casefold(), item[1].casefold())):
        links.append(
            ResourceLink(
                label=f'Role scope {alias}',
                target=scope,
                url=_arm_resource_url(scope),
            )
        )
    return links


def _foundry_links(
    bootstrap_plan: BootstrapPlan | None,
    plan_input: BootstrapPlanInput | None,
    phase_receipts: Sequence[PhaseReceipt],
) -> list[ResourceLink]:
    contexts = _evaluation_contexts(bootstrap_plan, plan_input)
    if not contexts:
        return []
    finalizations = _evaluation_finalizations(phase_receipts)
    links: list[ResourceLink] = []
    for context in contexts:
        if context.project_endpoint:
            links.append(
                ResourceLink(
                    label=f'{context.repo_agent_id} project',
                    target=context.project_endpoint,
                    url=context.project_endpoint,
                )
            )
        if context.project_endpoint and context.agent_name and context.agent_version:
            links.append(
                ResourceLink(
                    label=f'{context.repo_agent_id} agent version',
                    target=f'{context.agent_name}:{context.agent_version}',
                    url=_foundry_agent_url(
                        context.project_endpoint,
                        context.agent_name,
                        context.agent_version,
                    ),
                )
            )
        finalization = finalizations.get(context.repo_agent_id.casefold())
        if finalization is not None:
            links.extend(_finalized_foundry_links(context, finalization))
            continue
        if context.contract is not None and not context.contract.stopped:
            links.extend(_planned_foundry_links(context))
    return sorted(links, key=lambda item: (item.label.casefold(), item.target.casefold()))


def _evaluation_finalizations(
    phase_receipts: Sequence[PhaseReceipt],
) -> dict[str, EvaluationFinalization]:
    finalizations: dict[str, EvaluationFinalization] = {}
    for receipt in phase_receipts:
        if receipt.phase != 'evaluations':
            continue
        for state in _iter_foundry_provider_states(receipt.provider_state):
            onboarding = state.get('onboarding')
            if not isinstance(onboarding, Mapping):
                continue
            for ledger in onboarding.values():
                if not isinstance(ledger, Mapping):
                    continue
                payload = ledger.get('finalization')
                if not isinstance(payload, Mapping):
                    continue
                try:
                    finalization = EvaluationFinalization.model_validate(dict(payload))
                except Exception:
                    continue
                finalizations[finalization.repo_agent_id.casefold()] = finalization
    return finalizations


def _finalized_foundry_links(
    context: _EvaluationContext,
    finalization: EvaluationFinalization,
) -> list[ResourceLink]:
    links: list[ResourceLink] = []
    for dataset in finalization.datasets:
        links.append(
            ResourceLink(
                label=f'{context.repo_agent_id} {dataset.role} dataset',
                target=dataset.dataset_id,
                url=None if context.project_endpoint is None else _foundry_dataset_url(context.project_endpoint, dataset.dataset_id),
            )
        )
    for evaluator in finalization.evaluators:
        links.append(
            ResourceLink(
                label=f'{context.repo_agent_id} {evaluator.role} evaluator',
                target=evaluator.evaluator_id,
                url=None if context.project_endpoint is None else _foundry_evaluator_url(context.project_endpoint, evaluator.evaluator_id),
            )
        )
    for definition in finalization.definitions:
        links.append(
            ResourceLink(
                label=f'{context.repo_agent_id} {definition.role} definition',
                target=definition.definition_id,
                url=None if context.project_endpoint is None else _foundry_definition_url(context.project_endpoint, definition.definition_id),
            )
        )
    links.extend(
        (
            ResourceLink(
                label=f'{context.repo_agent_id} development run',
                target=finalization.activation.development_run_id,
            ),
            ResourceLink(
                label=f'{context.repo_agent_id} validating run',
                target=finalization.activation.validating_run_id,
            ),
        )
    )
    return links


def _planned_foundry_links(context: _EvaluationContext) -> list[ResourceLink]:
    contract = context.contract
    assert contract is not None
    assert contract.dataset_plan is not None
    assert contract.evaluator_plan is not None
    assert contract.definition_plan is not None
    links: list[ResourceLink] = []
    dataset_candidates = contract.dataset_plan.reuse_candidates
    for role, target in (
        ('development', dataset_candidates[0] if dataset_candidates is not None else 'available after apply'),
        ('validating', dataset_candidates[1] if dataset_candidates is not None else 'available after apply'),
    ):
        label = f'{context.repo_agent_id} {role} dataset'
        if target != 'available after apply':
            links.append(
                ResourceLink(
                    label=label,
                    target=target,
                    url=None if context.project_endpoint is None else _foundry_dataset_url(context.project_endpoint, target),
                )
            )
        else:
            name = (
                contract.dataset_plan.requested_development_name
                if role == 'development'
                else contract.dataset_plan.requested_validating_name
            )
            links.append(ResourceLink(label=f'{label} ({name}:{contract.dataset_plan.requested_version})', target='available after apply'))
    if contract.evaluator_plan.reuse_evaluator_id is not None:
        links.append(
            ResourceLink(
                label=f'{context.repo_agent_id} objective evaluator',
                target=contract.evaluator_plan.reuse_evaluator_id,
                url=None if context.project_endpoint is None else _foundry_evaluator_url(context.project_endpoint, contract.evaluator_plan.reuse_evaluator_id),
            )
        )
    else:
        links.append(
            ResourceLink(
                label=(
                    f'{context.repo_agent_id} objective evaluator '
                    f'({contract.evaluator_plan.requested_name}:{contract.evaluator_plan.requested_version})'
                ),
                target='available after apply',
            )
        )
    for role, name in (
        ('development', contract.definition_plan.requested_development_name),
        ('validating', contract.definition_plan.requested_validating_name),
    ):
        links.append(
            ResourceLink(
                label=f'{context.repo_agent_id} {role} definition ({name})',
                target='available after apply',
            )
        )
    links.extend(
        (
            ResourceLink(
                label=f'{context.repo_agent_id} development run',
                target='available after apply',
            ),
            ResourceLink(
                label=f'{context.repo_agent_id} validating run',
                target='available after apply',
            ),
        )
    )
    return links


def _foundry_agent_url(project_endpoint: str, agent_name: str, agent_version: str) -> str:
    return (
        f'{project_endpoint.rstrip("/")}/agents/'
        f'{quote(agent_name, safe="")}/versions/{quote(agent_version, safe="")}'
    )


def _foundry_dataset_url(project_endpoint: str, dataset_id: str) -> str | None:
    match = _DATASET_URI_RE.fullmatch(dataset_id)
    if match is None:
        return None
    return (
        f'{project_endpoint.rstrip("/")}/data/'
        f'{quote(match.group("name"), safe="")}/versions/{quote(match.group("version"), safe="")}'
    )


def _foundry_evaluator_url(project_endpoint: str, evaluator_id: str) -> str | None:
    match = _EVALUATOR_URI_RE.fullmatch(evaluator_id)
    if match is None:
        return None
    return (
        f'{project_endpoint.rstrip("/")}/evaluators/'
        f'{quote(match.group("name"), safe="")}/versions/{quote(match.group("version"), safe="")}'
    )


def _foundry_definition_url(project_endpoint: str, definition_id: str) -> str | None:
    match = _DEFINITION_URI_RE.fullmatch(definition_id)
    if match is None:
        return None
    return (
        f'{project_endpoint.rstrip("/")}/evaluationDefinitions/'
        f'{quote(match.group("name"), safe="")}/versions/{quote(match.group("version"), safe="")}'
    )


def _github_repo_url(repository_id: str) -> str:
    return f'https://github.com/{repository_id}'


def _arm_resource_url(resource_id: str | None) -> str | None:
    if not resource_id or not resource_id.startswith('/subscriptions/'):
        return None
    return f'https://resources.azure.com{resource_id}'


def _graph_application_url(object_id: str) -> str:
    return f'https://graph.microsoft.com/v1.0/applications/{quote(object_id, safe="")}'


def _diagnostics_map(values: Sequence[str]) -> dict[str, str]:
    fields: dict[str, str] = {}
    for item in values:
        if '=' not in item:
            continue
        key, value = item.split('=', 1)
        fields[key] = value
    return fields


__all__ = [
    'AzureIdentityReview',
    'DeploymentReview',
    'DiscoveryAgentReview',
    'DiscoveryReview',
    'GitHubEnvironmentReview',
    'OwnerReviewBundle',
    'PhaseProgressReview',
    'PlanReview',
    'PlannedResourceReview',
    'RepositoryFileReview',
    'ResourceLink',
    'ResourceLinksReview',
    'ReviewWarning',
    'RoleAssignmentReview',
    'StatusReview',
    'VerificationChoiceReview',
    'build_discovery_review',
    'build_owner_review',
    'build_plan_review',
    'build_resource_links',
    'build_status_review',
]
