from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

from typer.testing import CliRunner
import yaml

from foundry_opt.cli import app
from foundry_opt.bootstrap.input_contracts import TrustedTemplateManifest
from foundry_opt.bootstrap.receipts import ApprovalRecord

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


def test_evaluation_plan_fails_closed_without_executable_action_contract(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)
    state_root = tmp_path / "state"
    path = _offline_plan_input(tmp_path, "a" * 40)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["offline_plan"] = False
    payload["required_phases"] = ["repository", "evaluations"]
    payload["evaluations_phase"] = {
        "schema_version": 1,
        "agents": [
            {
                "schema_version": 1,
                "repo_agent_id": "app",
                "sidecar_path": "app/.foundry/foundry-opt.yaml",
                "project_endpoint": "https://example.services.ai.azure.com/api/projects/example",
                "account_resource_id": "/subscriptions/33333333-3333-3333-3333-333333333333/resourceGroups/example/providers/Microsoft.CognitiveServices/accounts/example",
                "agent_name": "example-agent",
                "agent_version": "1",
                "existing_dataset_ids": [
                    "azureai://accounts/example/projects/example/data/development/versions/1",
                    "azureai://accounts/example/projects/example/data/validating/versions/1",
                ],
                "existing_evaluator_ids": [
                    "azureai://accounts/example/projects/example/evaluators/quality/versions/1"
                ],
                "existing_definition_ids": [
                    "eval-development",
                    "eval-validating",
                ],
                "generation_mode": "reuse_reviewed_sources",
                "generation_sources": [
                    {
                        "schema_version": 1,
                        "kind": "reviewed_file",
                        "path": "app/main.py",
                    }
                ],
                "model_deployment": "baseline-model",
                "trace_window": "P14D",
                "connection_name": "foundry-default",
                "target_sample_count": 30,
                "replacement_intent": False,
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    runner.invoke(
        app,
        [
            "bootstrap",
            "discover",
            "--repo-root",
            str(repo),
            "--repository-id",
            "org/repo",
            "--operation-id",
            "op-eval",
            "--state-root",
            str(state_root),
            "--plan-input",
            str(path),
        ],
    )

    planned = runner.invoke(
        app,
        [
            "bootstrap",
            "plan",
            "--plan-input",
            str(path),
            "--repository-id",
            "org/repo",
            "--repo-root",
            str(repo),
            "--operation-id",
            "op-eval",
            "--state-root",
            str(state_root),
        ],
    )

    assert planned.exit_code == 20
    assert "resolved sidecar/action contract" in planned.stdout


def test_evaluation_subcommands_json(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    state_root = tmp_path / "state"
    sha = "a" * 40
    plan_input = _offline_plan_input(tmp_path, sha)
    runner.invoke(app, ["bootstrap", "discover", "--repo-root", str(repo), "--repository-id", "org/repo", "--operation-id", "op-3", "--state-root", str(state_root), "--plan-input", str(plan_input)])
    replacement = tmp_path / "replace.json"
    replacement.write_text(json.dumps({"active_bundle_id": "old", "candidate_bundle_id": "new", "preserved_bundle_id": "old", "lineage_hash": "a" * 64, "status": "planned"}), encoding="utf-8")
    for args in (
        ["bootstrap", "evaluation", "plan", "--plan-input", str(plan_input)],
        ["bootstrap", "evaluation", "replace", "--replacement-file", str(replacement), "--plan-input", str(plan_input)],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.stdout


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
