from __future__ import annotations

from pathlib import Path

import pytest

from foundry_opt.bootstrap.contracts import (
    BootstrapSidecar,
    DistributionSettings,
    EvaluatorNormalization,
    EvaluatorReference,
    ExplicitAgentEntry,
    GitHubSettings,
    IdentitySettings,
    ResolvedEvaluator,
    ResolvedWeightedObjective,
    RootRegistry,
    TemplatePayloadSpec,
)
from foundry_opt.bootstrap.errors import BootstrapConfigError
from foundry_opt.bootstrap.legacy import import_legacy_single_agent_documents


def _sidecar() -> BootstrapSidecar:
    objective = ResolvedWeightedObjective.create((
        ResolvedEvaluator(reference=EvaluatorReference(evaluator_id='azureai://accounts/a/projects/p/evaluators/quality/versions/1', provenance='reused_existing'), normalization=EvaluatorNormalization(kind='pass_fail'), weight=1.0),
    )).model_dump(mode='json')
    return BootstrapSidecar.from_document({
        'repo_agent_id': 'agent-one',
        'source_root': 'agent',
        'package_root': 'agent',
        'editable_paths': ['agent/**'],
        'runtime': {'kind': 'hosted', 'runtime': 'python_3_13', 'entrypoint': ['python', 'main.py'], 'dependency_resolution': 'remote_build', 'protocol_name': 'responses', 'protocol_version': '2.0.0', 'cpu': '0.5', 'memory': '1Gi', 'model_environment_variable': 'AZURE_AI_MODEL_DEPLOYMENT_NAME'},
        'foundry_project': {'project_endpoint': 'https://example.services.ai.azure.com/api/projects/example', 'account_resource_id': '/subscriptions/000/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/example', 'agent_name': 'example-agent', 'model_deployment_aliases': ['baseline-model']},
        'baseline_model': 'baseline-model',
        'allowed_models': ['baseline-model'],
        'min_candidates': 2,
        'max_candidates': 2,
        'primary_metric': 'primary_metric',
        'decision_policy': {'minimum_aggregate_delta': 0.1, 'focused_cases_required': True, 'max_regressions': 0},
        'development_dataset': {'dataset_id': 'azureai://accounts/a/projects/p/data/dev/versions/1'},
        'validating_dataset': {'dataset_id': 'azureai://accounts/a/projects/p/data/val/versions/1'},
        'development_definition': {'definition_id': 'eval_development'},
        'validating_definition': {'definition_id': 'eval_validating'},
        'default_evaluator_bundle': {'objective': objective, 'datasets': [{'dataset_id': 'azureai://accounts/a/projects/p/data/dev/versions/1'}, {'dataset_id': 'azureai://accounts/a/projects/p/data/val/versions/1'}], 'definitions': [{'definition_id': 'eval_development'}, {'definition_id': 'eval_validating'}]},
        'hard_guardrails': [{'evaluator_name': 'safety', 'required_pass_rate': 1.0, 'required': True}],
        'deployment': {'environment': 'foundry-production', 'enabled': True, 'require_aligned_binding': True},
    })


def test_root_registry_rejects_casefold_duplicate_agent_ids() -> None:
    with pytest.raises(BootstrapConfigError):
        RootRegistry.from_document({
            'distribution': {'repository': 'https://github.com/org/repo.git', 'channel': 'wave2'},
            'github': {'optimizer_environment': 'copilot', 'deployment_environment': 'foundry-production', 'client_id_variable': 'AZURE_OPTIMIZER_CLIENT_ID'},
            'identity': {'kind': 'unresolved_migration'},
            'agents': [
                {'agent_id': 'agent-one', 'root': 'src/one', 'config_path': 'src/one/.foundry/foundry-opt.yaml', 'enabled': True},
                {'agent_id': 'Agent-One', 'root': 'src/two', 'config_path': 'src/two/.foundry/foundry-opt.yaml', 'enabled': True},
            ],
        })


def test_sidecar_rejects_invalid_foundry_uri() -> None:
    document = _sidecar().model_dump(mode='json')
    document['development_dataset']['dataset_id'] = 'dataset@1'
    with pytest.raises(BootstrapConfigError):
        BootstrapSidecar.from_document(document)


def test_actual_template_legacy_import_succeeds() -> None:
    root = Path(__file__).resolve().parents[2] / 'src' / 'foundry_opt' / 'templates' / 'customer-repo'
    proposal = import_legacy_single_agent_documents(
        lock_document=(root / '.github' / 'foundry-opt.lock.yml').read_text(encoding='utf-8'),
        policy_document=(root / '.github' / 'foundry-optimizer.yaml').read_text(encoding='utf-8'),
        metadata_document=(root / '.foundry' / 'agent-metadata.yaml').read_text(encoding='utf-8'),
    )
    assert proposal.registry.distribution.repository.endswith('foundry-optimizer-coding-agent-plugin.git')
    assert proposal.sidecars[0].development_definition.definition_id == 'eval_development'
    assert proposal.actions[0].kind == 'unresolved-shared-identity'


def test_root_registry_accepts_explicit_agents() -> None:
    registry = RootRegistry(
        distribution=DistributionSettings(repository='https://github.com/org/repo.git', channel='wave2'),
        github=GitHubSettings(optimizer_environment='copilot', deployment_environment='foundry-production', client_id_variable='AZURE_OPTIMIZER_CLIENT_ID'),
        identity=IdentitySettings(kind='unresolved_migration', resource_id=None),
        agents=(ExplicitAgentEntry(agent_id='agent-one', root='src/one', config_path='src/one/.foundry/foundry-opt.yaml'),),
    )
    assert registry.agents[0].enabled is True


def test_template_payload_accepts_reviewed_rendered_text() -> None:
    payload = TemplatePayloadSpec(
        template_id="foundry-opt-instructions",
        destination_path=".github/instructions/foundry-opt.instructions.md",
        rendered_template="---\napplyTo: \"**\"\n---\n\nUse OIDC only.\n",
    )

    assert payload.rendered_template.endswith("Use OIDC only.\n")
