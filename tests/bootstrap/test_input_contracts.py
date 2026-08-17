from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml
from pydantic import ValidationError

from foundry_opt.bootstrap.canonical import canonical_sha256
from foundry_opt.bootstrap.errors import BootstrapConfigError
from foundry_opt.bootstrap.input_contracts import BootstrapPlanInput, TrustedTemplateManifest, load_bootstrap_plan_input

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = REPOSITORY_ROOT / 'src' / 'foundry_opt' / 'templates' / 'customer-repo' / '.foundry-opt' / 'managed-payloads.manifest.yaml'
SCHEMA_PATH = REPOSITORY_ROOT / 'schemas' / 'plan-input.schema.json'


def _manifest_hash() -> str:
    return TrustedTemplateManifest.load_pinned_manifest().manifest_hash


def _sample_payload() -> dict[str, object]:
    return {
        'schema_version': 1,
        'repository': {
            'schema_version': 1,
            'repository_id': 'XOEEst/foundry-optimizer-coding-agent-plugin',
            'repository_url': 'https://github.com/XOEEst/foundry-optimizer-coding-agent-plugin.git',
            'default_branch': 'main',
            'root': '.',
            'selected_agents': [
                {
                    'schema_version': 1,
                    'repo_agent_id': 'example-agent',
                    'root': 'agent',
                    'config_path': 'agent/.foundry/foundry-opt.yaml',
                    'editable_paths': ['agent/main.py', 'agent/prompts/**'],
                }
            ],
        },
        'runtime_provenance': {
            'schema_version': 1,
            'runtime_repository_url': 'https://github.com/XOEEst/foundry-optimizer-coding-agent-plugin-runtime.git',
            'runtime_commit': 'c899b718f3baebcfd08209ee5184d0cf61d8153d',
            'uv_lock_sha256': '74d7bb534c53e71a61ce197f3d5fa3169f2413373c2e42617280e78e83d6c681',
        },
        'repository_phase': {
            'schema_version': 1,
            'trusted_manifest_id': 'foundry-v1-managed-payloads',
            'trusted_manifest_version': '1.0.0',
            'trusted_manifest_hash': _manifest_hash(),
            'agent_render_contexts': [
                {
                    'schema_version': 1,
                    'repo_agent_id': 'example-agent',
                    'values': [
                        {'schema_version': 1, 'key': 'selectedRoot', 'value': 'agent'},
                    ],
                }
            ],
        },
        'offline_plan': False,
        'required_phases': ['evaluations', 'repository', 'azure'],
        'github_phase': {
            'schema_version': 1,
            'optimizer_environment': 'copilot',
            'deployment_environment': 'foundry-production',
            'shared_client_id': 'azure_identity_resolution_required',
            'client_id_variable_name': 'AZURE_OPTIMIZER_CLIENT_ID',
            'default_branch_policy_intent': 'preserve_repository_default',
        },
        'azure_phase': {
            'schema_version': 1,
            'tenant_id': '22222222-2222-2222-2222-222222222222',
            'subscription_id': '33333333-3333-3333-3333-333333333333',
            'identity': {
                'schema_version': 1,
                'identity_kind': 'user_assigned_managed_identity',
                'existing_resource_id': '/subscriptions/33333333-3333-3333-3333-333333333333/resourceGroups/example-rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/example-uami',
            },
            'resource_group': 'example-rg',
            'location': 'eastus2',
            'github_repository_id': 'XOEEst/foundry-optimizer-coding-agent-plugin',
            'approved_role_assignments': [
                {
                    'schema_version': 1,
                    'alias': 'foundry-user',
                    'role_definition_id': '/subscriptions/33333333-3333-3333-3333-333333333333/providers/Microsoft.Authorization/roleDefinitions/00000000-0000-0000-0000-000000000001',
                    'scope': '/subscriptions/33333333-3333-3333-3333-333333333333/resourceGroups/example-rg/providers/Microsoft.CognitiveServices/accounts/example',
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


def test_plan_input_round_trips_yaml_and_json_with_canonical_hashes(tmp_path: Path) -> None:
    payload = _sample_payload()
    yaml_path = tmp_path / 'plan-input.yaml'
    json_path = tmp_path / 'plan-input.json'
    yaml_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding='utf-8')
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding='utf-8')

    loaded_yaml = load_bootstrap_plan_input(yaml_path)
    loaded_json = load_bootstrap_plan_input(json_path)

    assert loaded_yaml == loaded_json
    assert loaded_yaml.required_phases == ('repository', 'azure', 'evaluations')
    assert loaded_yaml.plan_input_hash == canonical_sha256(loaded_yaml.model_dump(mode='json', exclude_none=True))
    assert loaded_yaml.repository_phase.trusted_manifest_hash == _manifest_hash()


def test_repository_identity_and_runtime_provenance_are_distinct_and_validated() -> None:
    payload = _sample_payload()
    payload['repository']['repository_url'] = 'https://github.com/other/repo.git'
    with pytest.raises(ValidationError, match='repository_url must canonicalize to repository_id'):
        BootstrapPlanInput.model_validate(payload)
    payload = _sample_payload()
    assert urlparse(payload['runtime_provenance']['runtime_repository_url']).path != urlparse(payload['repository']['repository_url']).path
    BootstrapPlanInput.model_validate(payload)


def test_offline_and_github_resolution_rules_fail_closed() -> None:
    payload = _sample_payload()
    payload['offline_plan'] = True
    with pytest.raises(ValidationError, match='offline_plan forbids cloud phase inputs'):
        BootstrapPlanInput.model_validate(payload)
    payload = _sample_payload()
    payload['required_phases'] = ['repository', 'github']
    with pytest.raises(ValidationError, match='github phase cannot be required until shared client id is resolved'):
        BootstrapPlanInput.model_validate(payload)


def test_selected_agents_and_evaluation_agents_must_align() -> None:
    payload = _sample_payload()
    payload['evaluations_phase']['agents'].append({
        'schema_version': 1,
        'repo_agent_id': 'other-agent',
        'sidecar_path': 'other/.foundry/foundry-opt.yaml',
        'project_endpoint': 'https://example.services.ai.azure.com/api/projects/example',
        'account_resource_id': '/subscriptions/33333333-3333-3333-3333-333333333333/resourceGroups/example-rg/providers/Microsoft.CognitiveServices/accounts/example',
        'agent_name': 'other-agent',
        'agent_version': '1.0.0',
        'generation_mode': 'reuse_reviewed_sources',
        'generation_sources': [{'schema_version': 1, 'kind': 'reviewed_file', 'path': 'other/main.py'}],
        'model_deployment': 'baseline-model',
        'trace_window': 'P14D',
        'connection_name': 'foundry-default',
        'target_sample_count': 1,
        'replacement_intent': False,
    })
    with pytest.raises(ValidationError, match='outside selected_agents'):
        BootstrapPlanInput.model_validate(payload)


def test_manifest_is_pinned_and_caller_cannot_override_payload_set() -> None:
    payload = _sample_payload()
    payload['repository_phase']['trusted_manifest_hash'] = '0' * 64
    with pytest.raises(ValidationError, match='trusted_manifest_hash must match pinned trusted manifest hash'):
        BootstrapPlanInput.model_validate(payload)
    manifest = TrustedTemplateManifest.load_pinned_manifest()
    parsed = yaml.safe_load(MANIFEST_PATH.read_text(encoding='utf-8'))
    assert manifest.manifest_id == parsed['manifest_id']
    assert [item.template_id for item in manifest.managed_payloads] == [item['template_id'] for item in parsed['managed_payloads']]


def test_project_endpoint_sources_and_roles_are_strict() -> None:
    payload = _sample_payload()
    payload['evaluations_phase']['agents'][0]['project_endpoint'] = 'https://user@example.services.ai.azure.com/api/projects/example?x=1'
    with pytest.raises(ValidationError, match='project_endpoint must be https without userinfo, query, or fragment'):
        BootstrapPlanInput.model_validate(payload)
    payload = _sample_payload()
    payload['evaluations_phase']['agents'][0]['generation_sources'][0]['path'] = 'agent/secrets/token.txt'
    with pytest.raises(ValidationError, match='prohibited secret/raw content segment'):
        BootstrapPlanInput.model_validate(payload)
    payload = _sample_payload()
    payload['azure_phase']['approved_role_assignments'][0]['role_definition_id'] = '/subscriptions/33333333-3333-3333-3333-333333333333/providers/Microsoft.Authorization/roleDefinitions/ffffffff-ffff-ffff-ffff-ffffffffffff'
    with pytest.raises(ValidationError, match='approved allow-list'):
        BootstrapPlanInput.model_validate(payload)


def test_strict_json_rejects_duplicate_keys(tmp_path: Path) -> None:
    path = tmp_path / 'plan-input.json'
    path.write_text('{"schema_version":1,"schema_version":1,"repository":{},"runtime_provenance":{},"repository_phase":{},"offline_plan":false,"required_phases":["repository"]}', encoding='utf-8')
    with pytest.raises(BootstrapConfigError, match='strict JSON'):
        load_bootstrap_plan_input(path)


def test_schema_matches_generated_artifact() -> None:
    current = json.loads(SCHEMA_PATH.read_text(encoding='utf-8'))
    generated = BootstrapPlanInput.model_json_schema()
    assert current == generated
