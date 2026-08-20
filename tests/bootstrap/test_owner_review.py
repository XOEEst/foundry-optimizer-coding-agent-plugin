from __future__ import annotations

from pathlib import Path

from foundry_opt.bootstrap.contracts import (
    BindingAssessment,
    BootstrapPlan,
    BootstrapReceipt,
    DistributionSettings,
    ExplicitAgentEntry,
    GitHubSettings,
    IdentitySettings,
    RedactedStatusInfo,
    RootRegistry,
    TemplatePayloadSpec,
)
from foundry_opt.bootstrap.discovery import discover_repository_agents
from foundry_opt.bootstrap.evaluation.execution import ONBOARDING_STAGES
from foundry_opt.bootstrap.input_contracts import BootstrapPlanInput, TrustedTemplateManifest
from foundry_opt.bootstrap.operation_state import (
    DiscoveredAgentRecord,
    DiscoveryBlockerRecord,
    OperationStateEnvelope,
    SelectionPlan,
)
from foundry_opt.bootstrap.owner_review import (
    build_discovery_review,
    build_plan_review,
    build_resource_links,
    build_status_review,
)
from foundry_opt.bootstrap.plan_factory import build_phase_actions
from foundry_opt.bootstrap.receipts import PhaseReceipt, summarize_receipt
from foundry_opt.bootstrap.repository.engine import plan_repository
from tests.bootstrap.fakes.evaluation_contract import (
    ACCOUNT_RESOURCE_ID,
    PROJECT_ENDPOINT,
    build_contract,
    evaluation_agent_payload,
)
from tests.bootstrap.fakes.foundry_env import build_fake_adapter

SUBSCRIPTION = '33333333-3333-3333-3333-333333333333'
TENANT = '22222222-2222-2222-2222-222222222222'
CLIENT_ID = '44444444-4444-4444-4444-444444444444'
OBJECT_ID = '55555555-5555-5555-5555-555555555555'
RUNTIME_REPOSITORY = 'https://github.com/XOEEst/foundry-optimizer-coding-agent-plugin.git'
RUNTIME_COMMIT = 'a' * 40
REPOSITORY_ID = 'example-org/example-repo'
REPOSITORY_URL = 'https://github.com/example-org/example-repo.git'


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding='utf-8')


def _manifest() -> TrustedTemplateManifest:
    return TrustedTemplateManifest.load_pinned_manifest()


def _plan_input(
    contract=None,
    *,
    binding_evidence: bool = False,
    managed_identity: bool = False,
) -> BootstrapPlanInput:
    contract = contract or build_contract()
    manifest = _manifest()
    identity = (
        {
            'schema_version': 1,
            'identity_kind': 'user_assigned_managed_identity',
            'existing_resource_id': (
                f'/subscriptions/{SUBSCRIPTION}/resourceGroups/example/'
                'providers/Microsoft.ManagedIdentity/userAssignedIdentities/foundry-owner-review'
            ),
            'existing_client_id': CLIENT_ID,
            'existing_object_id': OBJECT_ID,
            'create_if_missing': False,
        }
        if managed_identity
        else {
            'schema_version': 1,
            'identity_kind': 'entra_application',
            'existing_client_id': CLIENT_ID,
            'existing_object_id': OBJECT_ID,
        }
    )
    payload: dict[str, object] = {
        'schema_version': 1,
        'repository': {
            'schema_version': 1,
            'repository_id': REPOSITORY_ID,
            'repository_url': REPOSITORY_URL,
            'default_branch': 'main',
            'root': '.',
            'selected_agents': [
                {
                    'schema_version': 1,
                    'repo_agent_id': 'app',
                    'root': 'app',
                    'config_path': 'app/.foundry/foundry-opt.yaml',
                    'editable_paths': ['app/main.py'],
                }
            ],
        },
        'runtime_provenance': {
            'schema_version': 1,
            'runtime_repository_url': RUNTIME_REPOSITORY,
            'runtime_commit': RUNTIME_COMMIT,
            'uv_lock_sha256': '0' * 64,
        },
        'repository_phase': {
            'schema_version': 1,
            'trusted_manifest_id': manifest.manifest_id,
            'trusted_manifest_version': manifest.manifest_version,
            'trusted_manifest_hash': manifest.manifest_hash,
            'agent_render_contexts': [
                {
                    'schema_version': 1,
                    'repo_agent_id': 'app',
                    'values': [],
                }
            ],
        },
        'offline_plan': False,
        'required_phases': ['repository', 'github', 'azure', 'evaluations'],
        'github_phase': {
            'schema_version': 1,
            'optimizer_environment': 'copilot',
            'deployment_environment': 'foundry-production',
            'shared_client_id': CLIENT_ID,
            'client_id_variable_name': 'AZURE_OPTIMIZER_CLIENT_ID',
            'oidc_subject_prefix': 'repo:example-org@123/example-repo@456',
            'default_branch_policy_intent': 'require_explicit',
        },
        'azure_phase': {
            'schema_version': 1,
            'tenant_id': TENANT,
            'subscription_id': SUBSCRIPTION,
            'identity': identity,
            'resource_group': 'example',
            'location': 'eastus2',
            'github_repository_id': REPOSITORY_ID,
            'approved_role_assignments': [
                {
                    'schema_version': 1,
                    'alias': 'foundry-user-project',
                    'role_definition_id': (
                        f'/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Authorization/'
                        'roleDefinitions/53ca6127-db72-4b80-b1b0-d745d6d5456d'
                    ),
                    'scope': ACCOUNT_RESOURCE_ID,
                }
            ],
        },
        'evaluations_phase': {
            'schema_version': 1,
            'agents': [evaluation_agent_payload(contract)],
        },
    }
    if binding_evidence:
        payload['binding_evidence'] = {
            'schema_version': 1,
            'repository_id': REPOSITORY_ID,
            'agents': [
                {
                    'schema_version': 1,
                    'root': 'app',
                    'repo_agent_id': 'app',
                    'project_endpoint': PROJECT_ENDPOINT,
                    'agent_name': 'example-agent',
                    'agent_version': '1',
                    'source_fingerprint': '1' * 64,
                    'package_fingerprint': '2' * 64,
                    'evidence_provenance': 'reviewed_operator_attestation',
                }
            ],
        }
    return BootstrapPlanInput.model_validate(payload)


def _selection_from_discovery(result) -> SelectionPlan:
    discovered = tuple(
        DiscoveredAgentRecord(
            repo_agent_id=agent.repoAgentId,
            root=agent.root,
            config_path=agent.configPath,
            source_root=agent.sourceRoot,
            package_root=agent.packageRoot,
            source_fingerprint=agent.sourceFingerprint,
            package_fingerprint=agent.packageFingerprint,
            classification=agent.bindingAssessment.classification,
            detail=agent.bindingAssessment.detail,
            confidence=agent.confidence,
            blockers=tuple(
                DiscoveryBlockerRecord(code=blocker.code, detail=blocker.detail)
                for blocker in agent.blockers
            ),
            approved_shared_source_repo_agent_ids=agent.approvedSharedSourceRepoAgentIds,
        )
        for agent in result.agents
    )
    return SelectionPlan(
        repository_root=result.repositoryRoot,
        selected_agent_ids=(result.agents[0].repoAgentId,),
        binding_assessments=tuple(agent.bindingAssessment for agent in result.agents),
        discovery_fingerprints=(),
        blockers=tuple(sorted({blocker.detail for agent in result.agents for blocker in agent.blockers})),
        discovered_agents=discovered,
    )


def _registry_for(agent_id: str) -> RootRegistry:
    return RootRegistry(
        distribution=DistributionSettings(
            repository=RUNTIME_REPOSITORY,
            channel='pinned',
            pin=RUNTIME_COMMIT,
        ),
        github=GitHubSettings(
            optimizer_environment='copilot',
            deployment_environment='foundry-production',
            client_id_variable='AZURE_OPTIMIZER_CLIENT_ID',
        ),
        identity=IdentitySettings(kind='unresolved_migration'),
        agents=(
            ExplicitAgentEntry(
                agent_id=agent_id,
                root='agent',
                config_path='agent/.foundry/agent-metadata.yaml',
                enabled=False,
            ),
        ),
    )


def _evaluation_plan(contract, *, operation_id: str = 'op-eval') -> BootstrapPlan:
    return BootstrapPlan.create(
        operation_id=operation_id,
        runtime_repository=RUNTIME_REPOSITORY,
        runtime_commit=RUNTIME_COMMIT,
        repository_identity=REPOSITORY_ID,
        actions=contract.composite_action(),
    )


def _applied_evaluation_phase_receipt(contract) -> tuple[PhaseReceipt, BootstrapPlan]:
    adapter, _ = build_fake_adapter(
        reuse=bool(contract.dataset_plan is not None and contract.dataset_plan.reuse_candidates),
    )
    plan = _evaluation_plan(contract)
    receipt = adapter.apply_resources(plan)
    return (
        PhaseReceipt(
            phase='evaluations',
            state='applied',
            provider='evaluations',
            receipt=receipt,
            parent_plan_hash=plan.plan_hash,
            phase_plan_hash=plan.plan_hash,
            summary=summarize_receipt(receipt),
            provider_state=adapter.export_provider_state(receipt),
        ),
        plan,
    )


def _selection(binding: str = 'bound-aligned') -> SelectionPlan:
    return SelectionPlan(
        repository_root='.',
        selected_agent_ids=('app',),
        binding_assessments=(
            BindingAssessment(agent_id='app', classification=binding),
        ),
        discovery_fingerprints=(),
    )


def test_discovery_review_summarizes_luffy_shape(tmp_path: Path) -> None:
    repo = tmp_path / 'repo'
    _write(
        repo / '.foundry' / 'agent-metadata.yaml',
        '\n'.join(
            (
                'schema_version: 1',
                'project_endpoint: https://example',
                'agent_name: luffy',
            )
        )
        + '\n',
    )
    _write(
        repo / '.github' / 'foundry-optimizer.yaml',
        '\n'.join(
            (
                'schema_version: 1',
                'source_root: agent',
                'editable_paths: [agent/**]',
                'metadata_path: .foundry/agent-metadata.yaml',
            )
        )
        + '\n',
    )
    _write(
        repo / 'agent' / 'main.py',
        '\n'.join(
            (
                'from agent_framework import Agent',
                'from agent_framework_foundry_hosting import ResponsesHostServer',
                'def create_responses_host():',
                '    return ResponsesHostServer(Agent())',
            )
        )
        + '\n',
    )

    result = discover_repository_agents(repo)
    selection = _selection_from_discovery(result)
    review = build_discovery_review(selection, registry=_registry_for(result.agents[0].repoAgentId))

    assert review.agents_found == 1
    assert review.selected_agent_ids == (result.agents[0].repoAgentId,)
    assert review.agents[0].root == '.'
    assert review.agents[0].source_root == 'agent'
    assert review.agents[0].binding_classification == 'bound-unknown'
    assert review.agents[0].registered is True
    assert review.agents[0].enabled_intent is False
    assert 'Selected stable IDs' in review.render_markdown()


def test_plan_review_surfaces_repository_conflicts(tmp_path: Path) -> None:
    repo = tmp_path / 'repo'
    _write(repo / '.foundry-opt' / 'registry.yaml', 'current: true\n')
    payload = TemplatePayloadSpec(
        template_id='registry',
        destination_path='.foundry-opt/registry.yaml',
        rendered_template='current: false\n',
    )
    plan = plan_repository(
        repo,
        operation_id='op-conflict',
        runtime_repository=RUNTIME_REPOSITORY,
        runtime_commit=RUNTIME_COMMIT,
        repository_identity=REPOSITORY_ID,
        payloads=(payload,),
    )

    review = build_plan_review(plan)

    assert review.repository_files[0].intent == 'conflict'
    assert review.repository_files[0].proposed_path == '.foundry-opt/registry.yaml.foundry-proposed'
    assert {warning.code for warning in review.warnings} == {'repository-conflicts-unresolved'}
    assert '.foundry-opt/registry.yaml.foundry-proposed' in review.render_text()


def test_plan_review_reports_immutable_oidc_and_optional_verification() -> None:
    contract = build_contract()
    plan_input = _plan_input(contract, binding_evidence=False)
    plan = BootstrapPlan.create(
        operation_id='op-plan',
        runtime_repository=RUNTIME_REPOSITORY,
        runtime_commit=RUNTIME_COMMIT,
        repository_identity=REPOSITORY_ID,
        actions=build_phase_actions(plan_input),
    )

    review = build_plan_review(plan, plan_input=plan_input)

    assert review.azure_identity is not None
    assert review.azure_identity.kind == 'entra-application'
    assert review.azure_identity.disposition == 'adopt'
    assert review.oidc_subjects == (
        'repo:example-org@123/example-repo@456:environment:copilot',
        'repo:example-org@123/example-repo@456:environment:foundry-production',
    )
    assert review.role_assignments[0].scope == ACCOUNT_RESOURCE_ID
    assert review.verification.kind == 'reviewed_claim_only'
    assert review.deployments[0].enabled is True
    assert {warning.code for warning in review.warnings} == {'deployment-unverified'}
    assert 'repo:example-org@123/example-repo@456:environment:copilot' in review.render_markdown()


def test_status_review_reports_failures_and_next_action() -> None:
    plan = BootstrapPlan.create(
        operation_id='op-status',
        runtime_repository=RUNTIME_REPOSITORY,
        runtime_commit=RUNTIME_COMMIT,
        repository_identity=REPOSITORY_ID,
        actions=(),
    )
    repository_receipt = BootstrapReceipt.create(
        operation_id=plan.operation_id,
        runtime_repository=plan.runtime_repository,
        runtime_commit=plan.runtime_commit,
        repository_identity=plan.repository_identity,
        plan_hash=plan.plan_hash,
        created_actions=('repository:registry',),
    )
    github_receipt = BootstrapReceipt.create(
        operation_id=plan.operation_id,
        runtime_repository=plan.runtime_repository,
        runtime_commit=plan.runtime_commit,
        repository_identity=plan.repository_identity,
        plan_hash=plan.plan_hash,
        error_info=RedactedStatusInfo(code='apply_failed', summary='github apply failed'),
    )
    envelope = OperationStateEnvelope.create(
        generation=2,
        repository_id=REPOSITORY_ID,
        operation_id=plan.operation_id,
        runtime_repository=plan.runtime_repository,
        runtime_commit=plan.runtime_commit,
        selection_plan=_selection(),
        bootstrap_plan=plan,
        discovery_fingerprints=(),
        required_phases=('repository', 'github', 'azure'),
        phase_receipts=(
            PhaseReceipt(
                phase='repository',
                state='applied',
                provider='repository',
                receipt=repository_receipt,
                parent_plan_hash=plan.plan_hash,
                phase_plan_hash=plan.plan_hash,
                summary=summarize_receipt(repository_receipt),
            ),
            PhaseReceipt(
                phase='github',
                state='failed',
                provider='github',
                receipt=github_receipt,
                parent_plan_hash=plan.plan_hash,
                phase_plan_hash=plan.plan_hash,
                summary='github via github failed: github apply failed',
            ),
        ),
    )

    review = build_status_review(envelope)

    assert [phase.phase for phase in review.phases] == ['repository', 'github', 'azure']
    assert [phase.state for phase in review.phases] == ['applied', 'failed', 'pending']
    assert review.failures == ('github failed: github apply failed',)
    assert review.next_action == 'fix the github phase and rerun bootstrap apply --phase github'
    assert review.deployment_eligible is False


def test_status_review_reports_evaluation_step_progress() -> None:
    contract = build_contract()
    phase_receipt, plan = _applied_evaluation_phase_receipt(contract)
    envelope = OperationStateEnvelope.create(
        generation=1,
        repository_id=REPOSITORY_ID,
        operation_id=plan.operation_id,
        runtime_repository=plan.runtime_repository,
        runtime_commit=plan.runtime_commit,
        selection_plan=_selection(),
        bootstrap_plan=plan,
        discovery_fingerprints=(),
        required_phases=('evaluations',),
        phase_receipts=(phase_receipt,),
    )

    review = build_status_review(envelope)

    assert review.phases[0].phase == 'evaluations'
    assert review.phases[0].completed_steps == len(ONBOARDING_STAGES)
    assert review.phases[0].total_steps == len(ONBOARDING_STAGES)
    assert any('7/7 complete' in step for step in review.phases[0].step_details)


def test_resource_links_render_actual_and_placeholder_foundry_resources() -> None:
    contract = build_contract()
    plan_input = _plan_input(contract, binding_evidence=True, managed_identity=True)
    phase_receipt, _ = _applied_evaluation_phase_receipt(contract)

    linked = build_resource_links(
        repository_id=REPOSITORY_ID,
        plan_input=plan_input,
        phase_receipts=(phase_receipt,),
    )
    pending = build_resource_links(
        repository_id=REPOSITORY_ID,
        plan_input=plan_input,
        phase_receipts=(),
    )

    assert linked.github[0].url == 'https://github.com/example-org/example-repo/actions'
    assert linked.azure[0].url is not None and linked.azure[0].url.startswith('https://resources.azure.com/subscriptions/')
    assert any(link.label == 'app project' and link.url == PROJECT_ENDPOINT for link in linked.foundry)
    assert any(link.label == 'app agent version' and link.url and '/agents/example-agent/versions/1' in link.url for link in linked.foundry)
    assert any(link.label == 'app development dataset' and link.target.startswith('azureai://accounts/example/projects/example/data/') for link in linked.foundry)
    assert any(link.label == 'app development run' and link.target != 'available after apply' for link in linked.foundry)
    assert any(link.label.startswith('app development definition') and link.target == 'available after apply' for link in pending.foundry)
    assert 'available after apply' in pending.render_text()
