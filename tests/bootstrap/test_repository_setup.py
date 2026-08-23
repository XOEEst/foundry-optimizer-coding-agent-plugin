from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from foundry_opt.bootstrap.contracts import ReviewedFoundryTarget, RootRegistry
from foundry_opt.bootstrap.operation_state import (
    DiscoveredAgentRecord,
    SelectionPlan,
)
from foundry_opt.bootstrap.repository_setup import (
    BootstrapRepositorySetupHandler,
    RepositorySetupCoordinator,
    build_repository_plan_input,
)
from foundry_opt.bootstrap.runner import (
    BootstrapApprovalRecord,
    BootstrapFoundryTargetRecord,
    BootstrapRegistrationIntent,
    BootstrapRunnerStateEnvelope,
    BootstrapVerificationChoice,
    RepositoryBinding,
    RuntimeBinding,
)

RUNTIME_REPOSITORY = "https://github.com/example/foundry-runtime.git"
RUNTIME_COMMIT = "1" * 40
LOCK_SHA = "2" * 64
PROJECT_ENDPOINT = "https://example.services.ai.azure.com/api/projects/example"
ACCOUNT_RESOURCE_ID = (
    "/subscriptions/33333333-3333-3333-3333-333333333333/resourceGroups/rg/"
    "providers/Microsoft.CognitiveServices/accounts/example"
)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


@pytest.fixture(autouse=True)
def _runtime_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_OPT_RUNTIME_LOCK_SHA256", LOCK_SHA)


def _operation(tmp_path: Path) -> BootstrapRunnerStateEnvelope:
    repository = tmp_path / "repo"
    (repository / "agent").mkdir(parents=True)
    (repository / "agent" / "main.py").write_text(
        "\n".join(
            (
                "from agent_framework import Agent",
                "from agent_framework_foundry_hosting import ResponsesHostServer",
                "def create_responses_host():",
                "    return ResponsesHostServer(Agent())",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.name", "Repository Setup Test")
    _git(
        repository,
        "config",
        "user.email",
        "repository-setup@example.invalid",
    )
    _git(repository, "remote", "add", "origin", "https://github.com/example/repo.git")
    _git(repository, "add", ".")
    _git(repository, "commit", "--quiet", "-m", "initial")
    commit = _git(repository, "rev-parse", "HEAD")
    target = ReviewedFoundryTarget(
        state="new_target",
        project_endpoint=PROJECT_ENDPOINT,
        project_endpoint_source="owner_answer",
        agent_name="example-agent",
        agent_name_source="owner_answer",
        account_resource_id=ACCOUNT_RESOURCE_ID,
        deployment_ready=True,
        detail="project access succeeded and the name is available",
    )
    return BootstrapRunnerStateEnvelope.create(
        generation=5,
        operation_id="bootstrap-repository-setup",
        lifecycle_stage="repository_approval",
        started_at="2026-08-21T00:00:00Z",
        updated_at="2026-08-21T00:05:00Z",
        repository_binding=RepositoryBinding(
            repository_root=str(repository.resolve()),
            repository_id="example/repo",
            repository_url="https://github.com/example/repo.git",
            head_commit=commit,
            branch_name="main",
        ),
        runtime_binding=RuntimeBinding(
            runtime_repository=RUNTIME_REPOSITORY,
            runtime_commit=RUNTIME_COMMIT,
        ),
        selection_plan=SelectionPlan(
            repository_root=str(repository.resolve()),
            selected_agent_ids=("example-agent",),
            binding_assessments=(),
            discovery_fingerprints=(),
            blockers=(),
            discovered_agents=(
                DiscoveredAgentRecord(
                    repo_agent_id="example-agent",
                    root="agent",
                    source_root="agent",
                    package_root="agent",
                    source_fingerprint="3" * 64,
                    package_fingerprint="4" * 64,
                    classification="ready-unbound",
                    confidence=1.0,
                ),
            ),
        ),
        foundry_targets=(
            BootstrapFoundryTargetRecord(
                repo_agent_id="example-agent",
                root="agent",
                reviewed_target=target,
            ),
        ),
        registration_intents=(
            BootstrapRegistrationIntent(
                repo_agent_id="example-agent",
                intent="register_enabled",
            ),
        ),
        verification_choices=(
            BootstrapVerificationChoice(
                repo_agent_id="example-agent",
                choice="no_evidence",
            ),
        ),
    )


def test_repository_plan_input_creates_a_quick_profile_with_reviewed_target(
    tmp_path: Path,
) -> None:
    operation = _operation(tmp_path)

    plan_input, assumptions = build_repository_plan_input(operation)
    selected = plan_input.repository.selected_agents[0]
    profile = selected.profile_document

    assert selected.enabled is True
    assert profile is not None
    assert profile.foundry_target is not None
    assert profile.foundry_target.state == "new_target"
    assert profile.verification.mode == "off"
    assert profile.deployment.enabled is True
    assert any("hosted Python 3.13" in item for item in assumptions)


def test_repository_plan_preserves_existing_registry_connection_and_agents(
    tmp_path: Path,
) -> None:
    operation = _operation(tmp_path)
    repository = Path(operation.repository_binding.repository_root)
    existing_identity = (
        "/subscriptions/33333333-3333-3333-3333-333333333333/"
        "resourceGroups/identity-rg/providers/Microsoft.ManagedIdentity/"
        "userAssignedIdentities/existing-foundry-opt"
    )
    registry_path = repository / ".foundry-opt" / "registry.yaml"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "distribution": {
                    "schema_version": 1,
                    "repository": RUNTIME_REPOSITORY,
                    "channel": "existing",
                    "pin": RUNTIME_COMMIT,
                },
                "github": {
                    "schema_version": 1,
                    "optimizer_environment": "existing-optimizer",
                    "deployment_environment": "existing-production",
                    "client_id_variable": "EXISTING_CLIENT_ID",
                    "oidc_subject_prefix": "repo:example@123/repo@456",
                },
                "identity": {
                    "schema_version": 1,
                    "kind": "user_assigned_managed_identity",
                    "resource_id": existing_identity,
                    "client_id": "44444444-4444-4444-4444-444444444444",
                },
                "agents": [
                    {
                        "schema_version": 1,
                        "agent_id": "existing-agent",
                        "root": "existing",
                        "config_path": "existing/.foundry/foundry-opt.yaml",
                        "enabled": False,
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    state = RepositorySetupCoordinator(state_root=tmp_path / "state").build(
        operation
    )
    registry_payload = next(
        action.template_payload
        for action in state.plan.actions
        if action.template_payload is not None
        and action.template_payload.template_id == "registry"
    )
    rendered = RootRegistry.from_document(
        registry_payload.rendered_template
    )

    assert rendered.identity.resource_id == existing_identity
    assert rendered.github.optimizer_environment == "existing-optimizer"
    assert rendered.github.client_id_variable == "EXISTING_CLIENT_ID"
    assert {item.agent_id for item in rendered.agents} == {
        "existing-agent",
        "example-agent",
    }


def test_repository_handler_applies_reviewed_files_and_returns_commit_context(
    tmp_path: Path,
) -> None:
    operation = _operation(tmp_path)
    coordinator = RepositorySetupCoordinator(state_root=tmp_path / "state")
    handler = BootstrapRepositorySetupHandler(coordinator=coordinator)
    review = handler.review(operation=operation)
    approval = BootstrapApprovalRecord.create(
        step="repository",
        actor="repo-owner",
        summary="Apply the reviewed repository bootstrap files.",
        approved_at="2026-08-21T00:06:00Z",
        state_generation=operation.generation,
        state_generation_hash=operation.generation_hash,
    )

    outcome = handler.approve(operation=operation, approval=approval)
    repository = Path(operation.repository_binding.repository_root)
    registry = yaml.safe_load(
        (repository / ".foundry-opt" / "registry.yaml").read_text(
            encoding="utf-8"
        )
    )
    profile = yaml.safe_load(
        (
            repository
            / "agent"
            / ".foundry"
            / "foundry-opt.yaml"
        ).read_text(encoding="utf-8")
    )

    assert "Repository files:" in review.render_markdown()
    assert outcome.stage == "connection_approval"
    assert outcome.child_refs is not None
    assert outcome.child_refs[-1].step == "repository"
    assert registry["agents"][0]["enabled"] is True
    assert profile["foundry_target"]["state"] == "new_target"
    local_commit = outcome.handler_context["local_commit"]
    assert local_commit["commit_agent_ids"] == ["example-agent"]
    assert ".foundry-opt/registry.yaml" in local_commit["managed_paths"]


def test_repository_rollback_is_idempotent_after_parent_cas_loss(
    tmp_path: Path,
) -> None:
    operation = _operation(tmp_path)
    coordinator = RepositorySetupCoordinator(state_root=tmp_path / "state")
    coordinator.approve(
        operation,
        actor="repo-owner",
        summary="Apply the reviewed repository bootstrap files.",
    )

    first = coordinator.rollback(operation)
    second = coordinator.rollback(operation)

    assert first.lifecycle_state == "rolled_back"
    assert second == first
