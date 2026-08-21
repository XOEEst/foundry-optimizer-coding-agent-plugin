from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from foundry_opt.bootstrap.connection_setup import (
    BootstrapConnectionSetupHandler,
    ConnectionSetupCoordinator,
)
from foundry_opt.bootstrap.foundry_targets import (
    DefaultFoundryTargetResolutionHandler,
)
from foundry_opt.bootstrap.local_commit import (
    BootstrapLocalCommitHandler,
    LocalGitCommitCoordinator,
)
from foundry_opt.bootstrap.local_deploy import (
    BootstrapLocalDeploymentHandler,
    LocalDeploymentCoordinator,
)
from foundry_opt.bootstrap.repository_setup import (
    BootstrapRepositorySetupHandler,
    RepositorySetupCoordinator,
)
from foundry_opt.bootstrap.runner import (
    BootstrapRunner,
    FileBootstrapRunnerStateStore,
)
from tests.bootstrap.test_connection_setup import _Drivers, _Inventory
from tests.bootstrap.test_foundry_targets import (
    _FakeAzureInventory,
    _FakeFoundryInventory,
)
from tests.bootstrap.test_local_deploy import _DeploymentAdapter
from tests.bootstrap.test_repository_setup import (
    ACCOUNT_RESOURCE_ID,
    LOCK_SHA,
    PROJECT_ENDPOINT,
    RUNTIME_COMMIT,
    RUNTIME_REPOSITORY,
    _operation,
)


def test_skill_runner_completes_one_click_bootstrap_with_approved_seams(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOUNDRY_OPT_RUNTIME_REPOSITORY", RUNTIME_REPOSITORY)
    monkeypatch.setenv("FOUNDRY_OPT_RUNTIME_COMMIT", RUNTIME_COMMIT)
    monkeypatch.setenv("FOUNDRY_OPT_RUNTIME_LOCK_SHA256", LOCK_SHA)
    seed = _operation(tmp_path)
    repository = Path(seed.repository_binding.repository_root)
    repository_coordinator = RepositorySetupCoordinator(
        state_root=tmp_path / "repository-state"
    )
    commit_coordinator = LocalGitCommitCoordinator(
        state_root=tmp_path / "commit-state"
    )
    deployment_adapter = _DeploymentAdapter()
    runner = BootstrapRunner(
        state_store=FileBootstrapRunnerStateStore(
            state_root=tmp_path / "runner-state"
        ),
        target_resolution_handler=DefaultFoundryTargetResolutionHandler(
            foundry_inventory=_FakeFoundryInventory(
                latest_versions={PROJECT_ENDPOINT: {}}
            ),
            azure_inventory=_FakeAzureInventory(
                {PROJECT_ENDPOINT: ACCOUNT_RESOURCE_ID}
            ),
        ),
        repository_handler=BootstrapRepositorySetupHandler(
            coordinator=repository_coordinator
        ),
        connection_handler=BootstrapConnectionSetupHandler(
            coordinator=ConnectionSetupCoordinator(
                inventory=_Inventory(),
                drivers=_Drivers(),
                repository_coordinator=repository_coordinator,
                state_root=tmp_path / "connection-state",
            )
        ),
        commit_handler=BootstrapLocalCommitHandler(
            coordinator=commit_coordinator
        ),
        deployment_handler=BootstrapLocalDeploymentHandler(
            coordinator=LocalDeploymentCoordinator(
                adapter=deployment_adapter,
                commit_coordinator=commit_coordinator,
                state_root=tmp_path / "deployment-state",
            )
        ),
    )

    turn = runner.start(repository)
    assert turn.state == "agent_selection"
    selected_id = turn.next_question.choices[0].value
    turn = runner.answer(
        turn.operation_id,
        turn.next_question.question_id,
        [selected_id],
    )
    assert turn.state == "register_enable"
    turn = runner.answer(
        turn.operation_id,
        turn.next_question.question_id,
        ["register_enabled"],
    )
    assert turn.state == "foundry_target_resolution"
    turn = runner.answer(
        turn.operation_id,
        turn.next_question.question_id,
        {
            "project_endpoint": PROJECT_ENDPOINT,
            "agent_name": "example-agent",
        },
    )
    assert turn.state == "verification_policy"
    turn = runner.answer(
        turn.operation_id,
        turn.next_question.question_id,
        ["no_evidence"],
    )
    assert turn.state == "repository_approval"
    turn = runner.approve(
        turn.operation_id,
        "repository",
        "repo-owner",
        "Apply the reviewed repository bootstrap plan.",
    )
    assert turn.state == "connection_approval"
    turn = runner.approve(
        turn.operation_id,
        "connection",
        "repo-owner",
        "Apply the reviewed GitHub and Azure connection.",
    )
    assert turn.state == "commit_approval"
    turn = runner.approve(
        turn.operation_id,
        "commit",
        "repo-owner",
        "Create the reviewed local bootstrap commit.",
    )
    assert turn.state == "deployment_approval"
    turn = runner.approve(
        turn.operation_id,
        "deployment",
        "repo-owner",
        "Deploy the reviewed exact commit.",
    )

    assert turn.state == "final_handoff"
    assert turn.next_question is None
    assert len(deployment_adapter.calls) == 1
    assert deployment_adapter.calls[0].commit_sha == (
        __import__("subprocess")
        .run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
        .stdout.strip()
    )
    assert "Local Foundry deployment completed" in turn.owner_markdown
    assert turn.resource_links.github
    assert turn.resource_links.azure
    assert turn.resource_links.foundry
    assert '"schema_version"' not in turn.owner_markdown
    registry = yaml.safe_load(
        (repository / ".foundry-opt" / "registry.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert registry["identity"]["kind"] == "user_assigned_managed_identity"
    assert registry["identity"]["client_id"] == (
        "44444444-4444-4444-4444-444444444444"
    )


def test_registered_disabled_agent_needs_no_target_connection_or_deployment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOUNDRY_OPT_RUNTIME_REPOSITORY", RUNTIME_REPOSITORY)
    monkeypatch.setenv("FOUNDRY_OPT_RUNTIME_COMMIT", RUNTIME_COMMIT)
    monkeypatch.setenv("FOUNDRY_OPT_RUNTIME_LOCK_SHA256", LOCK_SHA)
    seed = _operation(tmp_path)
    repository = Path(seed.repository_binding.repository_root)
    repository_coordinator = RepositorySetupCoordinator(
        state_root=tmp_path / "repository-state"
    )
    commit_coordinator = LocalGitCommitCoordinator(
        state_root=tmp_path / "commit-state"
    )
    foundry_inventory = _FakeFoundryInventory(latest_versions={})
    azure_inventory = _FakeAzureInventory({})
    runner = BootstrapRunner(
        state_store=FileBootstrapRunnerStateStore(
            state_root=tmp_path / "runner-state"
        ),
        target_resolution_handler=DefaultFoundryTargetResolutionHandler(
            foundry_inventory=foundry_inventory,
            azure_inventory=azure_inventory,
        ),
        repository_handler=BootstrapRepositorySetupHandler(
            coordinator=repository_coordinator
        ),
        commit_handler=BootstrapLocalCommitHandler(
            coordinator=commit_coordinator
        ),
    )

    turn = runner.start(repository)
    turn = runner.answer(
        turn.operation_id,
        turn.next_question.question_id,
        [turn.next_question.choices[0].value],
    )
    assert turn.state == "register_enable"
    turn = runner.answer(
        turn.operation_id,
        turn.next_question.question_id,
        ["register_disabled"],
    )
    assert turn.state == "repository_approval"
    turn = runner.approve(
        turn.operation_id,
        "repository",
        "repo-owner",
        "Register the agent disabled.",
    )
    assert turn.state == "commit_approval"
    turn = runner.approve(
        turn.operation_id,
        "commit",
        "repo-owner",
        "Commit the disabled registration.",
    )

    assert turn.state == "final_handoff"
    assert foundry_inventory.inspect_calls == []
    assert azure_inventory.calls == []
    registry = yaml.safe_load(
        (repository / ".foundry-opt" / "registry.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert registry["agents"][0]["enabled"] is False
    assert not (
        repository / "agent" / ".foundry" / "foundry-opt.yaml"
    ).exists()
