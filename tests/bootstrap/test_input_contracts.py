from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
import yaml

from foundry_opt.bootstrap.canonical import canonical_sha256
from foundry_opt.bootstrap.errors import BootstrapConfigError
from foundry_opt.bootstrap.input_contracts import BootstrapPlanInput, load_bootstrap_plan_input

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPOSITORY_ROOT / 'src' / 'foundry_opt' / 'templates' / 'customer-repo' / '.foundry-opt' / 'managed-payloads.manifest.yaml'
SCHEMA_PATH = REPOSITORY_ROOT / 'schemas' / 'plan-input.schema.json'


def _sample_payload() -> dict[str, object]:
    return {
        'schema_version': 1,
        'repository': {
            'schema_version': 1,
            'repository_id': 'XOEEst/foundry-optimizer-coding-agent-plugin',
            'repository_url': 'https://github.com/XOEEst/foundry-optimizer-coding-agent-plugin.git',
            'default_branch': 'main',
            'root': '.',
            'selected': {
                'schema_version': 1,
                'repoAgentId': 'example-agent',
                'root': 'agent',
                'config_path': 'agent/.foundry/foundry-opt.yaml',
            },
        },
        'runtime_provenance': {
            'schema_version': 1,
            'repository_url': 'https://github.com/XOEEst/foundry-optimizer-coding-agent-plugin.git',
            'commit': 'c899b718f3baebcfd08209ee5184d0cf61d8153d',
            'uv_lock_sha256': '74d7bb534c53e71a61ce197f3d5fa3169f2413373c2e42617280e78e83d6c681',
        },
        'repository_phase': {
            'schema_version': 1,
            'trusted_manifest_id': 'foundry-v1-managed-payloads',
            'trusted_manifest_version': '1.0.0',
            'managed_payloads': yaml.safe_load(MANIFEST_PATH.read_text(encoding='utf-8'))['managed_payloads'],
        },
        'offline_plan': False,
        'required_phases': ['repository', 'github', 'azure', 'evaluations'],
        'github_phase': {
            'schema_version': 1,
            'optimizer_environment': 'copilot',
            'deployment_environment': 'foundry-production',
            'shared_client_id': '11111111-1111-1111-1111-111111111111',
            'client_id_variable_name': 'AZURE_OPTIMIZER_CLIENT_ID',
            'default_branch_policy_intent': 'preserve_repository_default',
        },
        'azure_phase': {
            'schema_version': 1,
            'tenant_id': '22222222-2222-2222-2222-222222222222',
            'subscription_id': '33333333-3333-3333-3333-333333333333',
            'identity_kind': 'unresolved_migration',
            'resource_group': 'example-rg',
            'location': 'eastus2',
            'github_repository_id': 'XOEEst/foundry-optimizer-coding-agent-plugin',
            'approved_role_assignments': [
                {
                    'schema_version': 1,
                    'alias': 'foundry-user',
                    'role_definition_id': '/subscriptions/33333333-3333-3333-3333-333333333333/providers/Microsoft.Authorization/roleDefinitions/44444444-4444-4444-4444-444444444444',
                    'scope': '/subscriptions/33333333-3333-3333-3333-333333333333/resourceGroups/example-rg',
                }
            ],
        },
        'evaluations_phase': {
            'schema_version': 1,
            'agents': [
                {
                    'schema_version': 1,
                    'repo_agent_id': 'example-agent',
                    'sidecar_path': 'agent/.foundry/foundry-opt.yaml',
                    'project_endpoint': 'https://example.services.ai.azure.com/api/projects/example',
                    'account_resource_id': '/subscriptions/33333333-3333-3333-3333-333333333333/resourceGroups/example-rg/providers/Microsoft.CognitiveServices/accounts/example',
                    'agent_name': 'example-agent',
                    'agent_version': '1.2.3',
                    'existing_dataset_ids': ['azureai://accounts/example/projects/example/data/development/versions/1'],
                    'existing_evaluator_ids': ['azureai://accounts/example/projects/example/evaluators/safety/versions/1'],
                    'existing_definition_ids': ['eval_development'],
                    'generation_mode': 'reuse_reviewed_sources',
                    'generation_sources': [
                        {'schema_version': 1, 'kind': 'reviewed_file', 'path': 'agent/main.py'},
                        {'schema_version': 1, 'kind': 'reviewed_file', 'path': 'agent/prompts/system.txt'},
                    ],
                    'model_deployment': 'baseline-model',
                    'trace_window': 'P14D',
                    'connection_name': 'foundry-default',
                    'target_sample_count': 25,
                    'replacement_intent': False,
                }
            ],
        },
    }


def test_plan_input_round_trips_yaml_and_json(tmp_path: Path) -> None:
    payload = _sample_payload()
    yaml_path = tmp_path / 'plan-input.yaml'
    json_path = tmp_path / 'plan-input.json'
    yaml_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding='utf-8')
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding='utf-8')

    loaded_yaml = load_bootstrap_plan_input(yaml_path)
    loaded_json = load_bootstrap_plan_input(json_path)

    assert loaded_yaml == loaded_json
    assert loaded_yaml.plan_input_hash == canonical_sha256(loaded_yaml.model_dump(mode='json', exclude_none=True))
    assert loaded_yaml.repository_phase.manifest_hash == canonical_sha256(loaded_yaml.repository_phase.model_dump(mode='json'))


def test_offline_plan_rejects_cloud_inputs_and_cloud_required_phases() -> None:
    payload = _sample_payload()
    payload['offline_plan'] = True
    with pytest.raises(ValidationError, match='offline_plan forbids cloud phase inputs'):
        BootstrapPlanInput.model_validate(payload)
    payload['github_phase'] = None
    payload['azure_phase'] = None
    payload['evaluations_phase'] = None
    with pytest.raises(ValidationError, match='offline_plan cannot require cloud phases'):
        BootstrapPlanInput.model_validate(payload)


def test_required_phase_without_inputs_fails_closed() -> None:
    payload = _sample_payload()
    payload['github_phase'] = None
    with pytest.raises(ValidationError, match='github_phase inputs are required'):
        BootstrapPlanInput.model_validate(payload)


def test_casefold_duplicate_and_secretish_content_rejected() -> None:
    payload = _sample_payload()
    payload['azure_phase']['approved_role_assignments'].append({
        'schema_version': 1,
        'alias': 'foundry-user',
        'role_definition_id': '/subscriptions/33333333-3333-3333-3333-333333333333/providers/Microsoft.Authorization/roleDefinitions/55555555-5555-5555-5555-555555555555',
        'scope': '/subscriptions/33333333-3333-3333-3333-333333333333/resourceGroups/example-rg/providers/Microsoft.CognitiveServices/accounts/example-two',
    })
    with pytest.raises(ValidationError, match='case-fold duplicate'):
        BootstrapPlanInput.model_validate(payload)


def test_schema_matches_generated_artifact() -> None:
    current = json.loads(SCHEMA_PATH.read_text(encoding='utf-8'))
    generated = BootstrapPlanInput.model_json_schema()
    assert current == generated


def test_manifest_covers_current_v1_managed_payloads() -> None:
    payloads = yaml.safe_load(MANIFEST_PATH.read_text(encoding='utf-8'))['managed_payloads']
    destinations = {item['destination_path'] for item in payloads}
    assert '.foundry-opt/registry.yaml' in destinations
    assert '.github/foundry-opt.lock.yml' in destinations
    assert '.github/instructions/foundry-opt.instructions.md' in destinations
    assert '.github/ISSUE_TEMPLATE/foundry-optimize-agent.yml' in destinations
    assert '.github/agents/foundry-optimizer.agent.md' in destinations
    assert '.github/workflows/copilot-setup-steps.yml' in destinations
    assert '.github/workflows/foundry-opt-validation.yml' in destinations
    assert '.github/workflows/foundry-opt-deploy.yml' in destinations
    assert '{selected.root}/.foundry/foundry-opt.yaml' in destinations
