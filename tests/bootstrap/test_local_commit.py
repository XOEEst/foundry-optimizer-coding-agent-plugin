from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from foundry_opt.bootstrap.local_commit import (
    LocalCommitApproval,
    LocalGitCommitCoordinator,
    bootstrap_branch_name,
)
from foundry_opt.bootstrap.errors import BootstrapApplyError

RUNTIME_REPOSITORY = "https://github.com/example-org/foundry-opt-runtime.git"
RUNTIME_COMMIT = "a" * 40
REPOSITORY_ID = "example-org/example-repo"
REPOSITORY_REMOTE = "https://github.com/example-org/example-repo.git"
REPOSITORY_PLAN_HASH = "b" * 64


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _create_repository(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write(
        root / ".foundry-opt" / "registry.yaml",
        "\n".join(
            (
                "schema_version: 1",
                "distribution:",
                "  schema_version: 1",
                "  repository: https://github.com/example/shared.git",
                "  channel: wave4",
                "  pin: " + ("c" * 40),
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
            )
        )
        + "\n",
    )
    _write(
        root / "agent" / ".foundry" / "foundry-opt.yaml",
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
                "verification:",
                "  schema_version: 1",
                "  mode: 'off'",
                "  repository_checks: []",
                "  evaluation_gate_policy: 'allow_no_evidence'",
                "  bundle: null",
                "  lineage: null",
            )
        )
        + "\n",
    )
    _write(
        root / "agent" / "main.py",
        "\n".join(
            (
                "from agent_framework import Agent",
                "from agent_framework_foundry_hosting import ResponsesHostServer",
                "",
                "def create_responses_host():",
                "    return ResponsesHostServer(Agent())",
            )
        )
        + "\n",
    )
    _write(root / "README.md", "# Example repo\n")
    subprocess.run(["git", "-C", str(root), "init", "--quiet"], check=True)
    subprocess.run(["git", "-C", str(root), "checkout", "-qb", "main"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Local Commit Test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "local-commit@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(root), "remote", "add", "origin", REPOSITORY_REMOTE], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "--quiet", "-m", "initial"], check=True)
    return root


def _review(
    tmp_path: Path,
    repo: Path,
    *,
    managed_paths: tuple[str, ...],
    reviewed_existing_paths: tuple[str, ...] = (),
) -> tuple[LocalGitCommitCoordinator, object]:
    coordinator = LocalGitCommitCoordinator(state_root=tmp_path / "state")
    review = coordinator.build_review(
        repo,
        operation_id="bootstrap-op",
        repository_identity=REPOSITORY_ID,
        runtime_repository=RUNTIME_REPOSITORY,
        runtime_commit=RUNTIME_COMMIT,
        repository_plan_hash=REPOSITORY_PLAN_HASH,
        managed_paths=managed_paths,
        reviewed_existing_paths=reviewed_existing_paths,
        selected_agent_ids=("example-agent",),
    )
    return coordinator, review


def test_local_commit_clean_success_records_exact_hashes(tmp_path: Path) -> None:
    repo = _create_repository(tmp_path)
    (repo / "agent" / "main.py").write_text("print('bootstrap source')\n", encoding="utf-8")
    coordinator, review = _review(tmp_path, repo, managed_paths=("agent/main.py",))

    approval = coordinator.create_approval(review, actor="owner", summary="approve exact source")
    receipt = coordinator.apply(review, approval)
    status = coordinator.status(review)

    assert review.branch_name == "foundry-opt/bootstrap/bootstrap-op"
    assert receipt.commit_sha == _git(repo, "rev-parse", "HEAD")
    assert receipt.tree_sha == _git(repo, "rev-parse", "HEAD^{tree}")
    assert _git(repo, "branch", "--show-current") == receipt.branch_name
    assert receipt.registry_path == ".foundry-opt/registry.yaml"
    assert receipt.profile_hashes[0].profile_path == "agent/.foundry/foundry-opt.yaml"
    assert receipt.agent_hashes[0].repo_agent_id == "example-agent"
    assert receipt.committed_paths == ("agent/main.py",)
    assert _git(repo, "status", "--porcelain") == ""
    assert status.overall_state == "committed"
    assert status.commit_sha == receipt.commit_sha
    assert status.rollback_ready is True


def test_local_commit_review_refuses_preexisting_unrelated_dirty_file(tmp_path: Path) -> None:
    repo = _create_repository(tmp_path)
    (repo / "agent" / "main.py").write_text("print('bootstrap source')\n", encoding="utf-8")
    (repo / "README.md").write_text("# unrelated\n", encoding="utf-8")
    coordinator = LocalGitCommitCoordinator(state_root=tmp_path / "state")

    with pytest.raises(BootstrapApplyError, match="unrelated dirty paths"):
        coordinator.build_review(
            repo,
            operation_id="bootstrap-op",
            repository_identity=REPOSITORY_ID,
            runtime_repository=RUNTIME_REPOSITORY,
            runtime_commit=RUNTIME_COMMIT,
            repository_plan_hash=REPOSITORY_PLAN_HASH,
            managed_paths=("agent/main.py",),
            selected_agent_ids=("example-agent",),
        )


def test_local_commit_apply_refuses_post_review_drift(tmp_path: Path) -> None:
    repo = _create_repository(tmp_path)
    target = repo / "agent" / "main.py"
    target.write_text("print('one')\n", encoding="utf-8")
    coordinator, review = _review(tmp_path, repo, managed_paths=("agent/main.py",))
    approval = coordinator.create_approval(review, actor="owner", summary="approve exact source")

    target.write_text("print('two')\n", encoding="utf-8")

    with pytest.raises(BootstrapApplyError, match="drifted after repository plan review"):
        coordinator.apply(review, approval)


def test_local_commit_review_enforces_exact_path_scope(tmp_path: Path) -> None:
    repo = _create_repository(tmp_path)
    (repo / "agent" / "main.py").write_text("print('bootstrap source')\n", encoding="utf-8")
    (repo / "agent" / "extra.py").write_text("print('extra')\n", encoding="utf-8")
    coordinator = LocalGitCommitCoordinator(state_root=tmp_path / "state")

    with pytest.raises(BootstrapApplyError, match="unrelated dirty paths"):
        coordinator.build_review(
            repo,
            operation_id="bootstrap-op",
            repository_identity=REPOSITORY_ID,
            runtime_repository=RUNTIME_REPOSITORY,
            runtime_commit=RUNTIME_COMMIT,
            repository_plan_hash=REPOSITORY_PLAN_HASH,
            managed_paths=("agent/main.py",),
            selected_agent_ids=("example-agent",),
        )


def test_local_commit_branch_name_is_deterministic() -> None:
    assert bootstrap_branch_name("Bootstrap_Op") == "foundry-opt/bootstrap/bootstrap_op"
    assert bootstrap_branch_name("Bootstrap Local Commit") == "foundry-opt/bootstrap/bootstrap-local-commit"


def test_local_commit_refuses_empty_commit(tmp_path: Path) -> None:
    repo = _create_repository(tmp_path)
    coordinator, review = _review(tmp_path, repo, managed_paths=("agent/main.py",))
    approval = coordinator.create_approval(review, actor="owner", summary="approve exact source")

    with pytest.raises(BootstrapApplyError, match="empty commit"):
        coordinator.apply(review, approval)


def test_local_commit_apply_is_idempotent_for_exact_commit(tmp_path: Path) -> None:
    repo = _create_repository(tmp_path)
    (repo / "agent" / "main.py").write_text("print('bootstrap source')\n", encoding="utf-8")
    coordinator, review = _review(tmp_path, repo, managed_paths=("agent/main.py",))
    approval = coordinator.create_approval(review, actor="owner", summary="approve exact source")

    first = coordinator.apply(review, approval)
    second = coordinator.apply(review, approval)

    assert second.commit_sha == first.commit_sha
    assert second.receipt_hash == first.receipt_hash
    assert _git(repo, "rev-list", "--count", "HEAD") == "2"


def test_local_commit_refuses_stale_approval(tmp_path: Path) -> None:
    repo = _create_repository(tmp_path)
    (repo / "agent" / "main.py").write_text("print('bootstrap source')\n", encoding="utf-8")
    coordinator, review = _review(tmp_path, repo, managed_paths=("agent/main.py",))
    stale = LocalCommitApproval.create(
        repository_identity=REPOSITORY_ID,
        operation_id="bootstrap-op",
        runtime_repository=RUNTIME_REPOSITORY,
        runtime_commit=RUNTIME_COMMIT,
        repository_plan_hash="d" * 64,
        review_hash=review.review_hash,
        actor="owner",
        summary="stale",
    )

    with pytest.raises(BootstrapApplyError, match="exact review, runtime, and repository plan"):
        coordinator.apply(review, stale)


def test_local_commit_rollback_restores_original_branch_and_dirty_snapshot(tmp_path: Path) -> None:
    repo = _create_repository(tmp_path)
    original_branch = _git(repo, "branch", "--show-current")
    target = repo / "agent" / "main.py"
    target.write_text("print('bootstrap source')\n", encoding="utf-8")
    coordinator, review = _review(tmp_path, repo, managed_paths=("agent/main.py",))
    approval = coordinator.create_approval(review, actor="owner", summary="approve exact source")
    coordinator.apply(review, approval)

    status = coordinator.rollback(review, approval)
    repeated = coordinator.rollback(review, approval)

    assert status.overall_state == "rolled_back"
    assert repeated == status
    assert _git(repo, "branch", "--show-current") == original_branch
    assert _git(repo, "rev-parse", "HEAD") == review.base_commit
    assert "bootstrap source" in target.read_text(encoding="utf-8")
    dirty_paths = {
        line.split(maxsplit=1)[1]
        for line in _git(repo, "status", "--porcelain").splitlines()
        if line
    }
    assert dirty_paths == {"agent/main.py"}


def test_local_commit_rollback_fails_closed_when_bootstrap_branch_advances(tmp_path: Path) -> None:
    repo = _create_repository(tmp_path)
    target = repo / "agent" / "main.py"
    target.write_text("print('bootstrap source')\n", encoding="utf-8")
    coordinator, review = _review(tmp_path, repo, managed_paths=("agent/main.py",))
    approval = coordinator.create_approval(review, actor="owner", summary="approve exact source")
    coordinator.apply(review, approval)
    target.write_text("print('advanced')\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "--quiet", "-m", "advance"], check=True)

    with pytest.raises(BootstrapApplyError, match="exact reviewed commit SHA|bootstrap branch ref"):
        coordinator.rollback(review, approval)
