from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from foundry_opt.cli import app
from foundry_opt.bootstrap.receipts import ApprovalRecord

runner = CliRunner()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".foundry").mkdir()
    (repo / ".foundry" / "agent-metadata.yaml").write_text("agent_name: root\nsource_root: app\npackage_root: app\n", encoding="utf-8")
    (repo / "app").mkdir()
    (repo / "app" / "main.py").write_text("import fastapi\napp = fastapi.FastAPI()\n", encoding="utf-8")
    return repo


def test_bootstrap_discover_plan_status_and_runtime_sha(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    state_root = tmp_path / "state"
    sha = "a" * 40
    discovered = runner.invoke(app, ["bootstrap", "discover", "--repo-root", str(repo), "--repository-id", "org/repo", "--operation-id", "op-1", "--state-root", str(state_root), "--runtime-commit", sha])
    assert discovered.exit_code == 0, discovered.stdout
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps({"selectedAgents": [{"repoAgentId": "root"}], "desiredConfiguration": [], "offline": True}), encoding="utf-8")
    planned = runner.invoke(app, ["bootstrap", "plan", "--selection-file", str(selection), "--repository-id", "org/repo", "--repo-root", str(repo), "--operation-id", "op-1", "--state-root", str(state_root), "--runtime-commit", sha])
    assert planned.exit_code == 0, planned.stdout
    status = runner.invoke(app, ["bootstrap", "status", "--repository-id", "org/repo", "--operation-id", "op-1", "--state-root", str(state_root), "--runtime-commit", sha])
    assert status.exit_code == 0
    payload = json.loads(status.stdout)
    assert payload["runtime_commit"] == sha
    stale = runner.invoke(app, ["bootstrap", "plan", "--selection-file", str(selection), "--repository-id", "org/repo", "--repo-root", str(repo), "--operation-id", "op-1", "--state-root", str(state_root), "--runtime-commit", "b" * 40])
    assert stale.exit_code == 24


def test_bootstrap_apply_requires_hash_and_matches_active_plan(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    state_root = tmp_path / "state"
    sha = "a" * 40
    runner.invoke(app, ["bootstrap", "discover", "--repo-root", str(repo), "--repository-id", "org/repo", "--operation-id", "op-2", "--state-root", str(state_root), "--runtime-commit", sha])
    selection = tmp_path / "selection.json"
    selection.write_text(json.dumps({"selectedAgents": [{"repoAgentId": "root"}], "desiredConfiguration": [], "offline": True}), encoding="utf-8")
    planned = runner.invoke(app, ["bootstrap", "plan", "--selection-file", str(selection), "--repository-id", "org/repo", "--repo-root", str(repo), "--operation-id", "op-2", "--state-root", str(state_root), "--runtime-commit", sha])
    plan_hash = json.loads(planned.stdout)["plan_hash"]
    bad = tmp_path / "approval-bad.json"
    bad.write_text(json.dumps({"parent_plan_hash": plan_hash, "phase": "repository", "actor": "tester", "summary": "ok"}), encoding="utf-8")
    missing_hash = runner.invoke(app, ["bootstrap", "apply", "--repository-id", "org/repo", "--operation-id", "op-2", "--phase", "repository", "--approval-file", str(bad), "--state-root", str(state_root), "--runtime-commit", sha])
    assert missing_hash.exit_code == 20
    approval = ApprovalRecord.create(parent_plan_hash=plan_hash, phase="repository", actor="tester", summary="ok")
    good = tmp_path / "approval-good.json"
    good.write_text(json.dumps(approval.model_dump(mode="json")), encoding="utf-8")
    stale = ApprovalRecord.create(parent_plan_hash="f" * 64, phase="repository", actor="tester", summary="ok")
    stale_file = tmp_path / "approval-stale.json"
    stale_file.write_text(json.dumps(stale.model_dump(mode="json")), encoding="utf-8")
    stale_result = runner.invoke(app, ["bootstrap", "apply", "--repository-id", "org/repo", "--operation-id", "op-2", "--phase", "repository", "--approval-file", str(stale_file), "--state-root", str(state_root), "--runtime-commit", sha])
    assert stale_result.exit_code == 24
    ok = runner.invoke(app, ["bootstrap", "apply", "--repository-id", "org/repo", "--operation-id", "op-2", "--phase", "repository", "--approval-file", str(good), "--state-root", str(state_root), "--runtime-commit", sha])
    assert ok.exit_code == 0, ok.stdout


def test_evaluation_subcommands_json(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    state_root = tmp_path / "state"
    sha = "a" * 40
    runner.invoke(app, ["bootstrap", "discover", "--repo-root", str(repo), "--repository-id", "org/repo", "--operation-id", "op-3", "--state-root", str(state_root), "--runtime-commit", sha])
    replacement = tmp_path / "replace.json"
    replacement.write_text(json.dumps({"active_bundle_id": "old", "candidate_bundle_id": "new", "preserved_bundle_id": "old", "lineage_hash": "a" * 64, "status": "planned"}), encoding="utf-8")
    for args in (
        ["bootstrap", "evaluation", "plan", "--replacement-file", str(replacement)],
        ["bootstrap", "evaluation", "apply", "--replacement-file", str(replacement)],
        ["bootstrap", "evaluation", "replace", "--replacement-file", str(replacement)],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.stdout
