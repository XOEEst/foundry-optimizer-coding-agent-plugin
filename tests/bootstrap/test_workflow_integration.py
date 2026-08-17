from __future__ import annotations

from pathlib import Path

import pytest

from foundry_opt.bootstrap.errors import BootstrapConfigError
from foundry_opt.bootstrap.contracts import (
    EvaluatorNormalization,
    EvaluatorReference,
    ResolvedEvaluator,
    ResolvedWeightedObjective,
)
from foundry_opt.bootstrap.workflow_integration import (
    build_changed_path_matrix,
    build_registered_deployment_plan,
    protected_editable_patterns,
    resolve_registry_selection,
    verify_issue_evaluator_authority,
)
from foundry_opt.poc.config import IssueEvaluatorEntry


def _write_repo(tmp_path: Path, *, second_enabled: bool = False) -> Path:
    root = tmp_path / "repo"
    (root / ".foundry-opt").mkdir(parents=True)
    (root / "agent" / ".foundry").mkdir(parents=True)
    (root / "shared" / ".foundry").mkdir(parents=True)
    (root / ".foundry-opt" / "registry.yaml").write_text(
        "\n".join(
            (
                "schema_version: 1",
                "distribution:",
                "  schema_version: 1",
                "  repository: https://github.com/example/shared.git",
                "  channel: wave4",
                "  pin: " + ("a" * 40),
                "github:",
                "  schema_version: 1",
                "  optimizer_environment: copilot",
                "  deployment_environment: foundry-production",
                "  client_id_variable: AZURE_OPTIMIZER_CLIENT_ID",
                "identity:",
                "  schema_version: 1",
                "  kind: unresolved_migration",
                "agents:",
                "  - schema_version: 1",
                "    agent_id: example-agent",
                "    root: agent",
                "    config_path: agent/.foundry/foundry-opt.yaml",
                "    enabled: true",
                "  - schema_version: 1",
                "    agent_id: shared-agent",
                "    root: shared",
                "    config_path: shared/.foundry/foundry-opt.yaml",
                f"    enabled: {'true' if second_enabled else 'false'}",
            )
        ),
        encoding="utf-8",
    )
    objective_hash = ResolvedWeightedObjective.create(
        (
            ResolvedEvaluator(
                reference=EvaluatorReference(
                    evaluator_id="azureai://built-in/evaluators/safety",
                    provenance="reused_existing",
                ),
                normalization=EvaluatorNormalization(kind="pass_fail"),
                weight=1.0,
            ),
        )
    ).objective_hash
    for agent_id, sidecar_path, source_root, relation in (
        ("example-agent", root / "agent" / ".foundry" / "foundry-opt.yaml", "agent", "shared-agent"),
        ("shared-agent", root / "shared" / ".foundry" / "foundry-opt.yaml", "shared", "example-agent"),
    ):
        sidecar_path.write_text(
            "\n".join(
                (
                    "schema_version: 1",
                    f"repo_agent_id: {agent_id}",
                    f"source_root: {source_root}",
                    f"package_root: {source_root}",
                    "editable_paths:",
                    f"  - {source_root}/main.py",
                    "shared_source_relations:",
                    "  - schema_version: 1",
                    f"    agent_id: {relation}",
                    "    relation: shared-source",
                    "runtime:",
                    "  schema_version: 1",
                    "  kind: hosted",
                    "  runtime: python_3_13",
                    "  entrypoint:",
                    "    - python",
                    "    - main.py",
                    "  dependency_resolution: remote_build",
                    "  protocol_name: responses",
                    "  protocol_version: '2.0.0'",
                    "  cpu: '0.5'",
                    "  memory: 1Gi",
                    "  model_environment_variable: MODEL",
                    "foundry_project:",
                    "  schema_version: 1",
                    "  project_endpoint: https://example.services.ai.azure.com/api/projects/example",
                    "  account_resource_id: /subscriptions/1/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/a",
                    f"  agent_name: {agent_id}",
                    "  model_deployment_aliases: [baseline]",
                    "baseline_model: baseline",
                    "allowed_models: [baseline]",
                    "min_candidates: 1",
                    "max_candidates: 1",
                    "primary_metric: quality",
                    "decision_policy:",
                    "  schema_version: 1",
                    "  minimum_aggregate_delta: 0.01",
                    "  focused_cases_required: true",
                    "  max_regressions: 0",
                    "development_dataset:",
                    "  schema_version: 1",
                    "  dataset_id: azureai://accounts/a/projects/p/data/dev/versions/1",
                    "validating_dataset:",
                    "  schema_version: 1",
                    "  dataset_id: azureai://accounts/a/projects/p/data/val/versions/1",
                    "development_definition:",
                    "  schema_version: 1",
                    "  definition_id: eval_development",
                    "validating_definition:",
                    "  schema_version: 1",
                    "  definition_id: eval_validating",
                    "default_evaluator_bundle:",
                    "  schema_version: 1",
                    "  objective:",
                    "    schema_version: 1",
                    "    evaluators:",
                    "      - schema_version: 1",
                    "        reference:",
                    "          schema_version: 1",
                    "          evaluator_id: azureai://built-in/evaluators/safety",
                    "          provenance: reused_existing",
                    "        normalization:",
                    "          schema_version: 1",
                    "          kind: pass_fail",
                    "        weight: 1.0",
                    "    objective_hash: " + objective_hash,
                    "  datasets:",
                    "    - schema_version: 1",
                    "      dataset_id: azureai://accounts/a/projects/p/data/dev/versions/1",
                    "    - schema_version: 1",
                    "      dataset_id: azureai://accounts/a/projects/p/data/val/versions/1",
                    "  definitions:",
                    "    - schema_version: 1",
                    "      definition_id: eval_development",
                    "    - schema_version: 1",
                    "      definition_id: eval_validating",
                    "max_issue_evaluators: 8",
                    "hard_guardrails:",
                    "  - schema_version: 1",
                    "    evaluator_name: safety",
                    "    required_pass_rate: 1.0",
                    "    required: true",
                    "deployment:",
                    "  schema_version: 1",
                    "  environment: foundry-production",
                    "  enabled: true",
                    "  require_aligned_binding: true",
                )
            ),
            encoding="utf-8",
        )
    return root


def test_registry_selection_requires_single_enabled_agent_when_implicit(tmp_path: Path) -> None:
    root = _write_repo(tmp_path, second_enabled=True)
    with pytest.raises(BootstrapConfigError, match="exactly one enabled agent"):
        resolve_registry_selection(root)


def test_registry_selection_and_protected_paths(tmp_path: Path) -> None:
    root = _write_repo(tmp_path)
    selection = resolve_registry_selection(root)
    protected = protected_editable_patterns(selection)
    assert selection.repo_agent_id == "example-agent"
    assert ".foundry-opt/**" in protected
    assert "agent/.foundry/foundry-opt.yaml" in protected


def test_issue_evaluator_authority_fails_closed(tmp_path: Path) -> None:
    _write_repo(tmp_path)
    evaluators = (IssueEvaluatorEntry(evaluator_id="azureai://built-in/evaluators/safety", weight=1.0),)
    with pytest.raises(BootstrapConfigError, match="write-authority resolver"):
        verify_issue_evaluator_authority("octocat", evaluators, resolver=None)
    with pytest.raises(BootstrapConfigError, match="not authorized"):
        verify_issue_evaluator_authority("octocat", evaluators, resolver=lambda *_: False)


def test_changed_path_matrix_supports_shared_roots_noop_and_manual(tmp_path: Path) -> None:
    root = _write_repo(tmp_path, second_enabled=True)
    manual = build_changed_path_matrix(root, changed_paths=(), manual_repo_agent_id="shared-agent")
    assert manual[0].repo_agent_id == "shared-agent"
    noop = build_changed_path_matrix(root, changed_paths=("docs/readme.md",))
    assert noop == ()
    shared = build_changed_path_matrix(root, changed_paths=("shared/main.py",))
    assert {entry.repo_agent_id for entry in shared} == {"example-agent", "shared-agent"}


def test_registered_deployment_plan_uses_repository_defaults(tmp_path: Path) -> None:
    root = _write_repo(tmp_path)
    selection = resolve_registry_selection(root)
    plan = build_registered_deployment_plan(
        selection,
        changed_root="agent",
        exact_source="c" * 40,
        use_repository_default_evaluators=True,
    )
    assert plan.repo_agent_id == "example-agent"
    assert plan.default_evaluator_ids == ("azureai://built-in/evaluators/safety",)
    assert plan.registry_hash != plan.sidecar_hash
