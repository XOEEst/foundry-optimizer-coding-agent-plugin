from __future__ import annotations

import pytest

from foundry_opt.bootstrap.contracts import BootstrapSidecar, DistributionSettings, ExplicitAgentEntry, IdentitySettings, RootRegistry
from foundry_opt.bootstrap.errors import BootstrapConfigError


def _sidecar() -> BootstrapSidecar:
    from foundry_opt.bootstrap.contracts import EvaluatorReference, IssueEvaluatorRequestEntry, ResolvedWeightedObjective
    objective = ResolvedWeightedObjective.create((IssueEvaluatorRequestEntry(evaluator=EvaluatorReference(evaluator_id='azureai://accounts/a/projects/p/evaluators/quality/versions/1', provenance='reused_existing')),)).model_dump(mode='json')
    return BootstrapSidecar.from_document({
        'repo_agent_id': 'agent-one',
        'source_root': 'src/agent',
        'package_root': 'src',
        'editable_paths': ['src/agent/**'],
        'runtime': {'kind': 'hosted', 'entrypoint': ['python', 'main.py'], 'protocol_name': 'responses', 'protocol_version': '2.0.0'},
        'foundry_project': {
            'project_endpoint': 'https://example.services.ai.azure.com/api/projects/example',
            'account_resource_id': '/subscriptions/000/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/example',
            'agent_name': 'example-agent',
            'expected_version': '92bca79a5faea0718e32101e56b34ebf29c628e3',
        },
        'baseline_model': 'baseline-model',
        'allowed_models': ['baseline-model'],
        'max_candidates': 2,
        'decision_policy': {'minimum_aggregate_delta': 0.1, 'focused_cases_required': True, 'max_regressions': 0},
        'development_dataset': {'dataset_id': 'azureai://accounts/a/projects/p/data/dev/versions/1'},
        'validating_dataset': {'dataset_id': 'azureai://accounts/a/projects/p/data/val/versions/1'},
        'default_evaluator_bundle': {
            'objective': {
                'evaluators': [{'evaluator': {'evaluator_id': 'azureai://accounts/a/projects/p/evaluators/quality/versions/1', 'provenance': 'reused_existing'}}],
                'normalized_weights': [1.0],
                'objective_hash': objective['objective_hash'],
                'score_normalization': {'minimum': 0.0, 'maximum': 1.0, 'normalized_range': [0.0, 1.0]},
            },
            'datasets': [{'dataset_id': 'azureai://accounts/a/projects/p/data/dev/versions/1'}],
            'definitions': [{'definition_id': 'azureai://accounts/a/projects/p/evaluationDefinitions/default/versions/1'}],
        },
        'hard_guardrails': [{'evaluator_name': 'safety', 'required_pass_rate': 1.0, 'required': True}],
        'deployment': {'environment': 'foundry-production', 'enabled': True, 'eligibility': 'eligible'},
    })


def test_root_registry_rejects_casefold_duplicate_agent_ids() -> None:
    with pytest.raises(BootstrapConfigError):
        RootRegistry.from_document({
            'distribution': {
                'repository': 'org/repo',
                'channel': 'wave2',
                'optimizer_environment': 'copilot',
                'deployment_environment': 'foundry-production',
                'optimizer_client_id_variable': 'AZURE_OPTIMIZER_CLIENT_ID',
                'deployment_client_id_variable': 'AZURE_DEPLOYMENT_CLIENT_ID',
            },
            'identity': {'kind': 'azure_subscription', 'resource_id': '/subscriptions/000/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/example'},
            'agents': [
                {'agent_id': 'agent-one', 'root': 'src/one', 'config_path': '.foundry/a.yaml', 'enabled': True},
                {'agent_id': 'Agent-One', 'root': 'src/two', 'config_path': '.foundry/b.yaml', 'enabled': True},
            ],
        })


def test_sidecar_rejects_invalid_foundry_uri() -> None:
    document = _sidecar().model_dump(mode='json')
    document['development_dataset']['dataset_id'] = 'dataset@1'
    with pytest.raises(BootstrapConfigError):
        BootstrapSidecar.from_document(document)


def test_root_registry_accepts_explicit_agents() -> None:
    registry = RootRegistry(
        distribution=DistributionSettings(
            repository='org/repo',
            channel='wave2',
            optimizer_environment='copilot',
            deployment_environment='foundry-production',
            optimizer_client_id_variable='AZURE_OPTIMIZER_CLIENT_ID',
            deployment_client_id_variable='AZURE_DEPLOYMENT_CLIENT_ID',
        ),
        identity=IdentitySettings(kind='azure_subscription', resource_id='/subscriptions/000/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/example'),
        agents=(ExplicitAgentEntry(agent_id='agent-one', root='src/one', config_path='.foundry/a.yaml'),),
    )
    assert registry.agents[0].enabled is True
