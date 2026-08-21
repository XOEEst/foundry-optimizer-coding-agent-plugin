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
    protected_editable_patterns_for_repository,
    resolve_registry_selection,
    verify_issue_check_authority,
    verify_issue_dataset_authority,
    verify_issue_evaluator_authority,
)
from foundry_opt.poc.config import IssueEvaluatorEntry
from foundry_opt.verification import VerificationCheckSpec, VerificationDatasetInput


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


def _write_quick_repo(
    tmp_path: Path,
    *,
    enabled: bool = True,
    profile_exists: bool = True,
    evaluation_gate_policy: str = "allow_no_evidence",
    repository_checks: tuple[str, ...] = (),
    verification_mode: str | None = None,
) -> Path:
    root = tmp_path / "quick-repo"
    (root / ".foundry-opt").mkdir(parents=True)
    (root / "agent" / ".foundry").mkdir(parents=True)
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
                f"    enabled: {'true' if enabled else 'false'}",
            )
        ),
        encoding="utf-8",
    )
    if profile_exists:
        mode = verification_mode or ("optional" if repository_checks else "off")
        verification_lines = [
            "verification:",
            "  schema_version: 1",
            f"  mode: '{mode}'",
            f"  evaluation_gate_policy: '{evaluation_gate_policy}'",
            "  bundle: null",
            "  lineage: null",
        ]
        if repository_checks:
            verification_lines.append("  repository_checks:")
            verification_lines.extend(
                f'    - "{check}"' for check in repository_checks
            )
        else:
            verification_lines.append("  repository_checks: []")
        (root / "agent" / ".foundry" / "foundry-opt.yaml").write_text(
            "\n".join(
                (
                    "schema_version: 2",
                    "repo_agent_id: example-agent",
                    "source_root: agent",
                    "package_root: agent",
                    "editable_paths:",
                    "  - agent/main.py",
                    "shared_source_relations: []",
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
                    "foundry_project:",
                    "  schema_version: 1",
                    "  project_endpoint: https://example.services.ai.azure.com/api/projects/example",
                    "  account_resource_id: /subscriptions/1/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/a",
                    "  agent_name: example-agent",
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
                    *verification_lines,
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
    with pytest.raises(BootstrapConfigError, match="trusted issue author permission"):
        verify_issue_evaluator_authority(None, evaluators)
    with pytest.raises(BootstrapConfigError, match="write, maintain, or admin"):
        verify_issue_evaluator_authority("read", evaluators)
    with pytest.raises(BootstrapConfigError, match="unknown issue author permission"):
        verify_issue_evaluator_authority("owner", evaluators)


def test_issue_dataset_and_checks_authority_fail_closed(tmp_path: Path) -> None:
    _write_repo(tmp_path)
    dataset = VerificationDatasetInput(
        dataset_id_or_uri="azureai://accounts/a/projects/p/data/dev/versions/1"
    )
    checks = (
        VerificationCheckSpec(kind="command", value="python -m pytest tests/agent -q"),
    )
    with pytest.raises(
        BootstrapConfigError,
        match="trusted issue author permission",
    ):
        verify_issue_dataset_authority(None, dataset)
    with pytest.raises(
        BootstrapConfigError,
        match="trusted issue author permission",
    ):
        verify_issue_check_authority(None, checks)
    with pytest.raises(BootstrapConfigError, match="write, maintain, or admin"):
        verify_issue_dataset_authority("read", dataset)
    with pytest.raises(BootstrapConfigError, match="write, maintain, or admin"):
        verify_issue_check_authority("triage", checks)


def test_changed_path_matrix_supports_shared_roots_noop_and_manual(tmp_path: Path) -> None:
    root = _write_repo(tmp_path, second_enabled=True)
    manual = build_changed_path_matrix(root, changed_paths=(), manual_repo_agent_id="shared-agent")
    assert manual[0].repo_agent_id == "shared-agent"
    noop = build_changed_path_matrix(root, changed_paths=("docs/readme.md",))
    assert noop == ()
    shared = build_changed_path_matrix(root, changed_paths=("shared/main.py",))
    assert {entry.repo_agent_id for entry in shared} == {"example-agent", "shared-agent"}


def test_changed_path_matrix_rejects_unknown_shared_relation(tmp_path: Path) -> None:
    root = _write_repo(tmp_path)
    sidecar = root / "agent" / ".foundry" / "foundry-opt.yaml"
    sidecar.write_text(sidecar.read_text(encoding="utf-8").replace("shared-agent", "missing-agent", 1), encoding="utf-8")
    with pytest.raises(BootstrapConfigError, match="unknown agent_id"):
        build_changed_path_matrix(root, changed_paths=("agent/main.py",))


def test_repository_protected_patterns_include_all_registry_config_paths(tmp_path: Path) -> None:
    root = _write_repo(tmp_path, second_enabled=True)
    protected = protected_editable_patterns_for_repository(root)
    assert "agent/.foundry/foundry-opt.yaml" in protected
    assert "shared/.foundry/foundry-opt.yaml" in protected


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
    assert plan.verification.mode == "foundry_evaluation"
    assert plan.verification.status == "planned"
    assert plan.registry_hash != plan.sidecar_hash


def test_registry_selection_supports_enabled_quick_profile(tmp_path: Path) -> None:
    root = _write_quick_repo(tmp_path)
    selection = resolve_registry_selection(root)

    assert selection.sidecar.schema_version == 2
    assert selection.sidecar.verification.mode == "off"
    plan = build_registered_deployment_plan(
        selection,
        changed_root="agent",
        exact_source="c" * 40,
        use_repository_default_evaluators=True,
    )
    assert plan.objective_hash is None
    assert plan.default_evaluator_ids == ()
    assert plan.verification.mode == "none"
    assert plan.verification.unverified_deployment is True
    assert plan.verification.warning is not None
    assert plan.verification.warning.code == "deployment-unverified"


def test_registered_deployment_plan_falls_back_to_repository_checks(
    tmp_path: Path,
) -> None:
    root = _write_quick_repo(
        tmp_path,
        evaluation_gate_policy="allow_repository_checks",
        repository_checks=("check: CI / unit-tests",),
    )
    selection = resolve_registry_selection(root)

    plan = build_registered_deployment_plan(
        selection,
        changed_root="agent",
        exact_source="c" * 40,
        use_repository_default_evaluators=True,
    )

    assert plan.objective_hash is None
    assert plan.default_evaluator_ids == ()
    assert plan.verification.mode == "repository_checks"
    assert plan.verification.check_results[0].status == "planned"
    assert plan.verification.check_results[0].value == "CI / unit-tests"
    assert plan.verification.evaluator_ids == ()


def test_registered_deployment_plan_uses_repository_checks_when_allow_no_evidence(
    tmp_path: Path,
) -> None:
    root = _write_quick_repo(
        tmp_path,
        evaluation_gate_policy="allow_no_evidence",
        repository_checks=("check: CI / unit-tests",),
    )
    selection = resolve_registry_selection(root)

    plan = build_registered_deployment_plan(
        selection,
        changed_root="agent",
        exact_source="c" * 40,
        use_repository_default_evaluators=True,
    )

    assert plan.verification.mode == "repository_checks"
    assert plan.verification.evaluation_gate_policy == "allow_no_evidence"
    assert plan.verification.check_results[0].kind == "check"
    assert plan.verification.check_results[0].value == "CI / unit-tests"


def test_registered_deployment_plan_requires_repository_checks_when_bundle_missing(
    tmp_path: Path,
) -> None:
    root = _write_quick_repo(
        tmp_path,
        evaluation_gate_policy="allow_repository_checks",
    )
    selection = resolve_registry_selection(root)

    with pytest.raises(
        BootstrapConfigError,
        match="trusted repository checks",
    ):
        build_registered_deployment_plan(
            selection,
            changed_root="agent",
            exact_source="c" * 40,
            use_repository_default_evaluators=True,
        )


def test_enabled_registry_entry_without_profile_fails_closed(tmp_path: Path) -> None:
    root = _write_quick_repo(tmp_path, profile_exists=False)

    with pytest.raises(BootstrapConfigError, match="requires a profile"):
        resolve_registry_selection(root)
