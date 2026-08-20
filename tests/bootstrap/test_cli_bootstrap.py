from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

from typer.testing import CliRunner
import yaml

from foundry_opt.bootstrap.contracts import (
    EvaluatorNormalization,
    EvaluatorReference,
    ResolvedEvaluator,
    ResolvedWeightedObjective,
)
from foundry_opt.cli import app
from foundry_opt.bootstrap import drivers
from foundry_opt.bootstrap.input_contracts import TrustedTemplateManifest
from foundry_opt.bootstrap.receipts import ApprovalRecord
from tests.bootstrap.fakes.evaluation_contract import build_contract, evaluation_agent_payload
from tests.bootstrap.fakes.foundry_env import build_fake_adapter, fake_credential

runner = CliRunner()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app" / ".foundry").mkdir(parents=True)
    (repo / "app" / ".foundry" / "agent-metadata.yaml").write_text(
        "agent_name: app\nsource_root: app\npackage_root: app\n",
        encoding="utf-8",
    )
    (repo / "app" / "main.py").write_text("import fastapi\napp = fastapi.FastAPI()\n", encoding="utf-8")
    return repo


def _repository_root_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "root-repo"
    (repo / ".foundry").mkdir(parents=True)
    (repo / ".foundry" / "agent-metadata.yaml").write_text(
        "agent_name: app\nsource_root: agent\npackage_root: agent\n",
        encoding="utf-8",
    )
    (repo / "agent").mkdir()
    (repo / "agent" / "main.py").write_text(
        "import fastapi\napp = fastapi.FastAPI()\n",
        encoding="utf-8",
    )
    return repo


def _offline_plan_input(tmp_path: Path, sha: str) -> Path:
    manifest = TrustedTemplateManifest.load_pinned_manifest()
    path = tmp_path / "plan-input.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "repository": {
                    "schema_version": 1,
                    "repository_id": "org/repo",
                    "repository_url": "https://github.com/org/repo.git",
                    "default_branch": "main",
                    "root": ".",
                    "selected_agents": [
                        {
                            "schema_version": 1,
                            "repo_agent_id": "app",
                            "root": "app",
                            "config_path": "app/.foundry/foundry-opt.yaml",
                            "editable_paths": ["app/main.py"],
                        }
                    ],
                },
                "runtime_provenance": {
                    "schema_version": 1,
                    "runtime_repository_url": "https://github.com/XOEEst/foundry-optimizer-coding-agent-plugin.git",
                    "runtime_commit": sha,
                    "uv_lock_sha256": "0" * 64,
                },
                "repository_phase": {
                    "schema_version": 1,
                    "trusted_manifest_id": manifest.manifest_id,
                    "trusted_manifest_version": manifest.manifest_version,
                    "trusted_manifest_hash": manifest.manifest_hash,
                    "agent_render_contexts": [
                        {
                            "schema_version": 1,
                            "repo_agent_id": "app",
                            "values": [],
                        }
                    ],
                },
                "offline_plan": True,
                "required_phases": ["repository"],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def _repository_root_plan_input(tmp_path: Path, sha: str) -> Path:
    path = _offline_plan_input(tmp_path, sha)
    payload = json.loads(path.read_text(encoding="utf-8"))
    selected = payload["repository"]["selected_agents"][0]
    selected["root"] = "."
    selected["config_path"] = "agent/.foundry/foundry-opt.yaml"
    selected["editable_paths"] = ["agent/main.py"]
    root_path = tmp_path / "plan-input-root.json"
    root_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return root_path


def _evaluation_plan_input(tmp_path: Path, sha: str) -> Path:
    """Reviewed plan input carrying an executable resolved evaluation execution contract."""

    path = _offline_plan_input(tmp_path, sha)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["offline_plan"] = False
    payload["required_phases"] = ["repository", "evaluations"]
    payload["evaluations_phase"] = {
        "schema_version": 1,
        "agents": [evaluation_agent_payload(build_contract())],
    }
    evaluation_path = tmp_path / "plan-input-evaluations.json"
    evaluation_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return evaluation_path


def test_bootstrap_discover_plan_status_and_runtime_sha(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    state_root = tmp_path / "state"
    sha = "a" * 40
    plan_input = _offline_plan_input(tmp_path, sha)
    discovered = runner.invoke(app, ["bootstrap", "discover", "--repo-root", str(repo), "--repository-id", "org/repo", "--operation-id", "op-1", "--state-root", str(state_root), "--plan-input", str(plan_input)])
    assert discovered.exit_code == 0, discovered.stdout
    planned = runner.invoke(app, ["bootstrap", "plan", "--plan-input", str(plan_input), "--repository-id", "org/repo", "--repo-root", str(repo), "--operation-id", "op-1", "--state-root", str(state_root)])
    assert planned.exit_code == 0, planned.stdout
    status = runner.invoke(app, ["bootstrap", "status", "--repository-id", "org/repo", "--operation-id", "op-1", "--repo-root", str(repo), "--plan-input", str(plan_input), "--state-root", str(state_root)])
    assert status.exit_code == 0
    payload = json.loads(status.stdout)
    assert payload["runtime_commit"] == sha
    stale = runner.invoke(app, ["bootstrap", "plan", "--plan-input", str(plan_input), "--repository-id", "org/repo", "--repo-root", str(repo), "--operation-id", "op-1", "--state-root", str(state_root), "--runtime-commit", "b" * 40])
    assert stale.exit_code == 24


def test_repository_root_discovery_plans_managed_agent_directory(tmp_path: Path) -> None:
    repo = _repository_root_repo(tmp_path)
    state_root = tmp_path / "root-state"
    sha = "a" * 40
    plan_input = _repository_root_plan_input(tmp_path, sha)

    discovered = runner.invoke(
        app,
        [
            "bootstrap",
            "discover",
            "--repo-root",
            str(repo),
            "--repository-id",
            "org/repo",
            "--operation-id",
            "root-op",
            "--state-root",
            str(state_root),
            "--plan-input",
            str(plan_input),
        ],
    )
    assert discovered.exit_code == 0, discovered.stdout
    discovered_payload = json.loads(discovered.stdout)
    assert discovered_payload["agents"][0]["root"] == "."
    assert discovered_payload["agents"][0]["sourceRoot"] == "agent"

    planned = runner.invoke(
        app,
        [
            "bootstrap",
            "plan",
            "--plan-input",
            str(plan_input),
            "--repository-id",
            "org/repo",
            "--repo-root",
            str(repo),
            "--operation-id",
            "root-op",
            "--state-root",
            str(state_root),
        ],
    )
    assert planned.exit_code == 0, planned.stdout
    plan_hash = json.loads(planned.stdout)["plan_hash"]
    approval = ApprovalRecord.create(
        parent_plan_hash=plan_hash,
        phase="repository",
        actor="tester",
        summary="root regression",
    )
    approval_file = tmp_path / "approval-root.json"
    approval_file.write_text(
        json.dumps(approval.model_dump(mode="json")),
        encoding="utf-8",
    )
    applied = runner.invoke(
        app,
        [
            "bootstrap",
            "apply",
            "--repository-id",
            "org/repo",
            "--operation-id",
            "root-op",
            "--phase",
            "repository",
            "--approval-file",
            str(approval_file),
            "--plan-input",
            str(plan_input),
            "--repo-root",
            str(repo),
            "--state-root",
            str(state_root),
        ],
    )
    assert applied.exit_code == 0, applied.stdout
    registry = yaml.safe_load(
        (repo / ".foundry-opt" / "registry.yaml").read_text(encoding="utf-8")
    )
    assert registry["agents"][0]["root"] == "agent"
    assert registry["agents"][0]["config_path"] == "agent/.foundry/foundry-opt.yaml"


def test_bootstrap_apply_requires_hash_and_matches_active_plan(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    state_root = tmp_path / "state"
    sha = "a" * 40
    plan_input = _offline_plan_input(tmp_path, sha)
    runner.invoke(app, ["bootstrap", "discover", "--repo-root", str(repo), "--repository-id", "org/repo", "--operation-id", "op-2", "--state-root", str(state_root), "--plan-input", str(plan_input)])
    planned = runner.invoke(app, ["bootstrap", "plan", "--plan-input", str(plan_input), "--repository-id", "org/repo", "--repo-root", str(repo), "--operation-id", "op-2", "--state-root", str(state_root)])
    plan_hash = json.loads(planned.stdout)["plan_hash"]
    bad = tmp_path / "approval-bad.json"
    bad.write_text(json.dumps({"parent_plan_hash": plan_hash, "phase": "repository", "actor": "tester", "summary": "ok"}), encoding="utf-8")
    missing_hash = runner.invoke(app, ["bootstrap", "apply", "--repository-id", "org/repo", "--operation-id", "op-2", "--phase", "repository", "--approval-file", str(bad), "--plan-input", str(plan_input), "--repo-root", str(repo), "--state-root", str(state_root)])
    assert missing_hash.exit_code == 20
    approval = ApprovalRecord.create(parent_plan_hash=plan_hash, phase="repository", actor="tester", summary="ok")
    good = tmp_path / "approval-good.json"
    good.write_text(json.dumps(approval.model_dump(mode="json")), encoding="utf-8")
    stale = ApprovalRecord.create(parent_plan_hash="f" * 64, phase="repository", actor="tester", summary="ok")
    stale_file = tmp_path / "approval-stale.json"
    stale_file.write_text(json.dumps(stale.model_dump(mode="json")), encoding="utf-8")
    stale_result = runner.invoke(app, ["bootstrap", "apply", "--repository-id", "org/repo", "--operation-id", "op-2", "--phase", "repository", "--approval-file", str(stale_file), "--plan-input", str(plan_input), "--repo-root", str(repo), "--state-root", str(state_root)])
    assert stale_result.exit_code == 24
    ok = runner.invoke(app, ["bootstrap", "apply", "--repository-id", "org/repo", "--operation-id", "op-2", "--phase", "repository", "--approval-file", str(good), "--plan-input", str(plan_input), "--repo-root", str(repo), "--state-root", str(state_root)])
    assert ok.exit_code == 0, ok.stdout
    assert not (repo / "app" / ".foundry" / "foundry-opt.yaml").exists()
    assert not (repo / ".github" / "foundry-opt.lock.yml").exists()
    registry = yaml.safe_load(
        (repo / ".foundry-opt" / "registry.yaml").read_text(encoding="utf-8")
    )
    assert registry["agents"][0]["enabled"] is False


def test_evaluation_cli_flow_plans_applies_activates_and_reports(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    state_root = tmp_path / "state"
    sha = "a" * 40
    plan_input = _evaluation_plan_input(tmp_path, sha)
    adapter, fakes = build_fake_adapter()
    monkeypatch.setattr(drivers, "DefaultAzureCredential", lambda **kwargs: fake_credential())
    monkeypatch.setattr(drivers, "FoundryAdapter", lambda endpoint, credential: adapter)

    inventory = runner.invoke(
        app,
        ["bootstrap", "evaluation", "inventory", "--plan-input", str(plan_input), "--repo-root", str(repo)],
    )
    assert inventory.exit_code == 0, inventory.stdout
    assessment = json.loads(inventory.stdout)["assessments"][0]
    assert assessment["dataset_strategy"] == "synthetic_only"
    assert (assessment["planned_development_cases"], assessment["planned_validating_cases"]) == (20, 10)

    evaluation_plan = runner.invoke(
        app,
        ["bootstrap", "evaluation", "plan", "--plan-input", str(plan_input), "--repo-root", str(repo)],
    )
    assert evaluation_plan.exit_code == 0, evaluation_plan.stdout
    planned = json.loads(evaluation_plan.stdout)
    # One approval-bound composite onboarding action per agent.
    assert [action["kind"] for action in planned["actions"]] == ["evaluation_onboarding"]
    assert planned["stopped_agents"] == []
    assert planned["execution_contracts"][0]["contract_version"] == 3

    assert runner.invoke(app, ["bootstrap", "discover", "--repo-root", str(repo), "--repository-id", "org/repo", "--operation-id", "op-eval", "--state-root", str(state_root), "--plan-input", str(plan_input)]).exit_code == 0
    planned_operation = runner.invoke(app, ["bootstrap", "plan", "--plan-input", str(plan_input), "--repository-id", "org/repo", "--repo-root", str(repo), "--operation-id", "op-eval", "--state-root", str(state_root)])
    assert planned_operation.exit_code == 0, planned_operation.stdout
    plan_hash = json.loads(planned_operation.stdout)["plan_hash"]

    for phase in ("repository", "evaluations"):
        approval = ApprovalRecord.create(parent_plan_hash=plan_hash, phase=phase, actor="tester", summary="ok")
        approval_file = tmp_path / f"approval-{phase}.json"
        approval_file.write_text(json.dumps(approval.model_dump(mode="json")), encoding="utf-8")
        if phase == "repository":
            applied = runner.invoke(app, ["bootstrap", "apply", "--repository-id", "org/repo", "--operation-id", "op-eval", "--phase", phase, "--approval-file", str(approval_file), "--plan-input", str(plan_input), "--repo-root", str(repo), "--state-root", str(state_root)])
        else:
            applied = runner.invoke(app, ["bootstrap", "evaluation", "apply", "--repository-id", "org/repo", "--operation-id", "op-eval", "--approval-file", str(approval_file), "--plan-input", str(plan_input), "--repo-root", str(repo), "--state-root", str(state_root)])
        assert applied.exit_code == 0, applied.stdout

    applied_payload = json.loads(applied.stdout)
    assert applied_payload["sidecar_written"] is False
    sidecar = repo / "app" / ".foundry" / "foundry-opt.yaml"
    sidecar_payload = yaml.safe_load(sidecar.read_text(encoding="utf-8"))
    assert sidecar_payload["verification"]["mode"] == "off"

    pending = runner.invoke(app, ["bootstrap", "evaluation", "status", "--repository-id", "org/repo", "--operation-id", "op-eval", "--repo-root", str(repo), "--state-root", str(state_root)])
    assert pending.exit_code == 0
    pending_payload = json.loads(pending.stdout)
    assert pending_payload["phase_state"] == "applied"
    assert pending_payload["sidecar_activation_state"] == "not_started"
    assert pending_payload["activated"] is False
    assert pending_payload["next_action"] == "run bootstrap evaluation activate"

    activated = runner.invoke(app, ["bootstrap", "evaluation", "activate", "--repository-id", "org/repo", "--operation-id", "op-eval", "--plan-input", str(plan_input), "--repo-root", str(repo), "--state-root", str(state_root)])
    assert activated.exit_code == 0, activated.stdout
    assert sidecar.exists()
    registry = yaml.safe_load((repo / ".foundry-opt" / "registry.yaml").read_text(encoding="utf-8"))
    assert registry["agents"][0]["enabled"] is True
    assert fakes["agents"].delete_version_calls == [("draft-agent", "1")]

    final_status = json.loads(runner.invoke(app, ["bootstrap", "evaluation", "status", "--repository-id", "org/repo", "--operation-id", "op-eval", "--repo-root", str(repo), "--state-root", str(state_root)]).stdout)
    assert final_status["activated"] is True
    assert final_status["replacement"]["status"] == "activated"

    inspected = runner.invoke(app, ["bootstrap", "evaluation", "inspect", "--repository-id", "org/repo", "--operation-id", "op-eval", "--repo-root", str(repo), "--plan-input", str(plan_input), "--state-root", str(state_root)])
    assert inspected.exit_code == 0
    inspected_payload = json.loads(inspected.stdout)
    assert inspected_payload["human_rubric_editor"] is False
    contract = inspected_payload["contracts"][0]
    assert contract["persisted_sidecar"]["verification"]["lineage"]["activation_binding"]["runtime_commit"] == sha
    assert contract["bounds"]["required_safety_pass_rate"] == 1.0
    finalization = contract["finalization"]
    assert {item["provenance"] for item in finalization["evaluators"]} == {"auto_generated_unreviewed", "reused_existing"}
    assert finalization["split"]["development_case_count"] == 20


def test_evaluation_cli_failures_exit_nonzero(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    state_root = tmp_path / "state"
    sha = "a" * 40
    plan_input = _evaluation_plan_input(tmp_path, sha)

    runner.invoke(app, ["bootstrap", "discover", "--repo-root", str(repo), "--repository-id", "org/repo", "--operation-id", "op-fail", "--state-root", str(state_root), "--plan-input", str(plan_input)])
    unapplied = runner.invoke(app, ["bootstrap", "evaluation", "activate", "--repository-id", "org/repo", "--operation-id", "op-fail", "--plan-input", str(plan_input), "--repo-root", str(repo), "--state-root", str(state_root)])
    assert unapplied.exit_code == 25
    assert "applied evaluations phase receipt" in unapplied.stdout
    assert not (repo / "app" / ".foundry" / "foundry-opt.yaml").exists()

    stale = runner.invoke(app, ["bootstrap", "evaluation", "activate", "--repository-id", "org/repo", "--operation-id", "op-fail", "--plan-input", str(plan_input), "--repo-root", str(repo), "--state-root", str(state_root), "--runtime-commit", "b" * 40])
    assert stale.exit_code == 24

    replacement = tmp_path / "replace.json"
    replacement.write_text(json.dumps({"active_bundle_id": "old", "candidate_bundle_id": "new", "preserved_bundle_id": "old", "lineage_hash": "a" * 64, "status": "planned"}), encoding="utf-8")
    refused = runner.invoke(app, ["bootstrap", "evaluation", "replace", "--replacement-file", str(replacement), "--plan-input", str(plan_input), "--repo-root", str(repo)])
    assert refused.exit_code == 20
    assert "replacement-intent-required" in refused.stdout

    missing_contract = json.loads(plan_input.read_text(encoding="utf-8"))
    missing_contract["evaluations_phase"]["agents"][0].pop("onboarding_contract")
    missing_path = tmp_path / "plan-input-missing.json"
    missing_path.write_text(json.dumps(missing_contract), encoding="utf-8")
    blocked = runner.invoke(app, ["bootstrap", "evaluation", "plan", "--plan-input", str(missing_path), "--repo-root", str(repo)])
    assert blocked.exit_code == 20
    assert "approved onboarding contract" in blocked.stdout


def test_evaluation_subcommands_json(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    state_root = tmp_path / "state"
    sha = "a" * 40
    plan_input = _offline_plan_input(tmp_path, sha)
    runner.invoke(app, ["bootstrap", "discover", "--repo-root", str(repo), "--repository-id", "org/repo", "--operation-id", "op-3", "--state-root", str(state_root), "--plan-input", str(plan_input)])
    result = runner.invoke(app, ["bootstrap", "evaluation", "plan", "--plan-input", str(plan_input)])
    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["actions"] == []

    replacement = tmp_path / "replace.json"
    replacement.write_text(json.dumps({"active_bundle_id": "old", "candidate_bundle_id": "new", "preserved_bundle_id": "old", "lineage_hash": "a" * 64, "status": "planned"}), encoding="utf-8")
    replaced = runner.invoke(app, ["bootstrap", "evaluation", "replace", "--replacement-file", str(replacement), "--plan-input", str(plan_input)])
    assert replaced.exit_code == 20, replaced.stdout


def test_registered_deploy_plan_command_uses_repository_defaults(tmp_path: Path) -> None:
    template = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "foundry_opt"
        / "templates"
        / "customer-repo"
    )
    repository = tmp_path / "repo"
    shutil.copytree(template, repository)
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
    (repository / "agent" / ".foundry" / "foundry-opt.yaml").write_text(
        "\n".join(
            (
                "schema_version: 1",
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
                f"    objective_hash: {objective_hash}",
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
                "evaluation_lineage:",
                "  schema_version: 1",
                "  split_algorithm_version: evaluation-core-split/v4",
                f"  split_hash: {'a' * 64}",
                f"  split_lineage_hash: {'b' * 64}",
                "  development_case_count: 20",
                "  validating_case_count: 10",
                "  dataset_strategy: synthetic_only",
                f"  generation_context_fingerprint: {'c' * 64}",
                "  evaluator_provenance: reused_existing",
                f"  bundle_objective_hash: {objective_hash}",
                "  activation_binding:",
                "    schema_version: 1",
                "    operation_id: op",
                f"    plan_hash: {'d' * 64}",
                f"    approval_hash: {'e' * 64}",
                f"    receipt_hash: {'f' * 64}",
                f"    runtime_commit: {'a' * 40}",
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
    subprocess.run(
        ["git", "-C", str(repository), "init", "--quiet"],
        check=True,
        capture_output=True,
        text=True,
    )

    result = runner.invoke(
        app,
        [
            "deploy",
            "plan",
            "--repository",
            str(repository),
            "--changed-root",
            "agent",
            "--repo-agent-id",
            "example-agent",
            "--exact-source",
            "a" * 40,
            "--check-eligibility",
            "--use-repository-default-evaluators",
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "planned"
    assert payload["repo_agent_id"] == "example-agent"
    assert payload["default_evaluator_ids"]
