from __future__ import annotations

from foundry_opt.bootstrap.legacy import import_legacy_single_agent_documents


def test_legacy_import_builds_read_only_registry() -> None:
    registry = import_legacy_single_agent_documents(
        lock_document="""schema_version: 1
repository_url: https://example.invalid/repo.git
commit: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
package_path: .
skill_path: skills/demo
uv_lock_sha256: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
""",
        policy_document="""schema_version: 1
source_root: agent
editable_paths:
  - agent/**
min_candidates: 1
max_candidates: 1
baseline_model: baseline
allowed_models:
  - baseline
primary_metric: quality
decision_rules:
  minimum_aggregate_delta: 0.1
  focused_cases_required: true
  max_regressions: 0
hard_guardrails:
  safety:
    required_pass_rate: 1.0
metadata_path: .foundry/agent-metadata.yaml
""",
        metadata_document="""schema_version: 1
repository_identity: org/repo
repository_id: 1
default_branch: main
project_endpoint: https://example.services.ai.azure.com/api/projects/example
foundry_account_resource_id: /subscriptions/sub/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/example
agent_name: Example Agent
authentication_method: oidc
static_credentials_allowed: false
hosted_runtime:
  kind: hosted
  runtime: python_3_13
  entry_point: [python, main.py]
  dependency_resolution: remote_build
  protocol_name: responses
  protocol_version: '2.0.0'
  cpu: '0.5'
  memory: 1Gi
  model_environment_variable: MODEL
oidc:
  issuer: https://token.actions.githubusercontent.com
  audience: api://AzureADTokenExchange
  tenant_id: tenant
  subscription_id: subscription
  repository_id_claim: '1'
  workflow_variables: []
  principals: []
model_deployments: []
development_evaluation:
  name: development
  split: development
  resolved_evaluation_id: eval
  dataset_id: dataset@1
  custom_evaluator_ids: []
validating_evaluation:
  name: validating
  split: validating
  resolved_evaluation_id: eval2
  dataset_id: dataset@2
  custom_evaluator_ids: []
""",
    )
    assert registry.distribution.github.repository == 'org/repo'
    assert registry.distribution.agents[0].agent_id == 'example-agent'
    assert registry.distribution.agents[0].sidecar.managed_files[0].path == '.github/foundry-opt.lock.yml'
