from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
import subprocess

import pytest
import yaml

from foundry_opt.bootstrap.errors import BootstrapApplyError, BootstrapConfigError
from foundry_opt.bootstrap.foundry_targets import (
    DefaultFoundryTargetResolutionHandler,
    FoundryProjectInventory,
)
from foundry_opt.bootstrap.runner import BootstrapRunner, FileBootstrapRunnerStateStore
from tests.bootstrap.fakes.evaluation_contract import ACCOUNT_RESOURCE_ID, build_sidecar_policy
from tests.bootstrap.fakes.foundry_env import PROJECT_ENDPOINT, build_code_archive, build_fake_adapter

RUNTIME_REPOSITORY = "https://github.com/example-org/foundry-opt-runtime.git"
RUNTIME_COMMIT = "a" * 40
REPOSITORY_REMOTE = "https://github.com/example-org/example-repo.git"
SECOND_ENDPOINT = "https://second.services.ai.azure.com/api/projects/second"
PROFILE_ENDPOINT = "https://profile.services.ai.azure.com/api/projects/profile"
METADATA_ENDPOINT = "https://metadata.services.ai.azure.com/api/projects/metadata"
SECOND_ACCOUNT_ID = (
    "/subscriptions/33333333-3333-3333-3333-333333333333/resourceGroups/example-rg"
    "/providers/Microsoft.CognitiveServices/accounts/second"
)
PROFILE_ACCOUNT_ID = (
    "/subscriptions/33333333-3333-3333-3333-333333333333/resourceGroups/example-rg"
    "/providers/Microsoft.CognitiveServices/accounts/profile"
)


class _FakeFoundryInventory:
    def __init__(
        self,
        *,
        latest_versions: Mapping[str, Mapping[str, str | None]],
        observations: Mapping[tuple[str, str, str], Mapping[str, object] | Exception | None] | None = None,
        inspect_errors: Mapping[str, Exception] | None = None,
        adapters: Mapping[str, object] | None = None,
    ) -> None:
        self._latest_versions = {
            endpoint: {str(name).casefold(): version for name, version in versions.items()}
            for endpoint, versions in latest_versions.items()
        }
        self._observations = dict(observations or {})
        self._inspect_errors = dict(inspect_errors or {})
        self._adapters = dict(adapters or {})
        self.inspect_calls: list[str] = []
        self.observe_calls: list[tuple[str, str, str, str, str]] = []

    def inspect_project(self, project_endpoint: str) -> FoundryProjectInventory:
        self.inspect_calls.append(project_endpoint)
        error = self._inspect_errors.get(project_endpoint)
        if error is not None:
            raise error
        return FoundryProjectInventory(
            project_endpoint=project_endpoint,
            agent_latest_versions=self._latest_versions.get(project_endpoint, {}),
        )

    def observe_agent(
        self,
        project_endpoint: str,
        *,
        agent_name: str,
        agent_version: str,
        source_root: str,
        package_root: str,
    ) -> Mapping[str, object]:
        self.observe_calls.append(
            (project_endpoint, agent_name, agent_version, source_root, package_root)
        )
        adapter = self._adapters.get(project_endpoint)
        if adapter is not None:
            return adapter.observe_agent_binding(
                agent_name=agent_name,
                agent_version=agent_version,
                source_root=source_root,
                package_root=package_root,
            )
        outcome = self._observations.get(
            (project_endpoint, agent_name.casefold(), agent_version)
        )
        if isinstance(outcome, Exception):
            raise outcome
        if outcome is None:
            return None  # type: ignore[return-value]
        return outcome


class _ExplodingRepositoryHandler:
    def review(self, *, operation):
        raise AssertionError("blocked target must not render repository review")

    def approve(self, *, operation, approval):
        raise AssertionError("blocked target must not approve repository review")

    def rollback(self, *, operation, step, child_ref):
        raise AssertionError("blocked target has no repository work to roll back")


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _sidecar_payload(
    *,
    repo_agent_id: str,
    root: str,
    project_endpoint: str,
    agent_name: str,
    account_resource_id: str,
    expected_version: str | None = None,
) -> dict[str, object]:
    policy = build_sidecar_policy(repo_agent_id=repo_agent_id, root=root).model_dump(
        mode="json"
    )
    policy.pop("path", None)
    foundry_project = dict(policy["foundry_project"])
    foundry_project["project_endpoint"] = project_endpoint
    foundry_project["account_resource_id"] = account_resource_id
    foundry_project["agent_name"] = agent_name
    if expected_version is not None:
        foundry_project["expected_version"] = expected_version
    policy["foundry_project"] = foundry_project
    policy["repo_agent_id"] = repo_agent_id
    policy["shared_source_relations"] = []
    policy["schema_version"] = 2
    return policy


def _create_repository(tmp_path: Path, *, agents: Sequence[Mapping[str, object]]) -> Path:
    repo = tmp_path / "customer"
    for spec in agents:
        root = repo / str(spec["root"])
        (root / ".foundry").mkdir(parents=True, exist_ok=True)
        metadata = spec.get("metadata")
        if isinstance(metadata, Mapping):
            _write(
                root / ".foundry" / "agent-metadata.yaml",
                yaml.safe_dump(dict(metadata), sort_keys=False),
            )
        sidecar = spec.get("sidecar")
        if isinstance(sidecar, Mapping):
            _write(
                root / ".foundry" / "foundry-opt.yaml",
                yaml.safe_dump(dict(sidecar), sort_keys=False),
            )
        _write(
            root / "main.py",
            "\n".join(
                (
                    "from agent_framework import Agent",
                    "from agent_framework_foundry_hosting import ResponsesHostServer",
                    "def create_responses_host():",
                    "    return ResponsesHostServer(Agent())",
                )
            )
            + "\n",
        )
    subprocess.run(["git", "-C", str(repo), "init", "--quiet"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Runner Test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "runner@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", REPOSITORY_REMOTE],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "--quiet", "-m", "initial"], check=True)
    return repo


def _selected_ids(turn) -> list[str]:
    assert turn.next_question is not None
    return [choice.value for choice in turn.next_question.choices]


def _select_and_enable_all(runner: BootstrapRunner, first):
    turn = runner.answer(
        first.operation_id,
        first.next_question.question_id,
        _selected_ids(first),
    )
    while turn.state == "register_enable":
        assert turn.next_question is not None
        turn = runner.answer(
            turn.operation_id,
            turn.next_question.question_id,
            ["register_enabled"],
        )
    return turn


@pytest.fixture(autouse=True)
def _runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_OPT_RUNTIME_REPOSITORY", RUNTIME_REPOSITORY)
    monkeypatch.setenv("FOUNDRY_OPT_RUNTIME_COMMIT", RUNTIME_COMMIT)


def test_metadata_target_reuse_classifies_existing_aligned_without_mutation(
    tmp_path: Path,
) -> None:
    root = "agents/app"
    repo = _create_repository(
        tmp_path,
        agents=[
            {
                "root": root,
                "metadata": {
                    "project_endpoint": PROJECT_ENDPOINT,
                    "agent_name": "example-agent",
                    "expected_version": "1",
                },
            }
        ],
    )
    archive = build_code_archive(repo / "agents" / "app")
    adapter, fakes = build_fake_adapter(
        code_archive=archive,
        code_content_hash=hashlib.sha256(archive).hexdigest(),
    )
    foundry_inventory = _FakeFoundryInventory(
        latest_versions={PROJECT_ENDPOINT: {"example-agent": "1"}},
        adapters={PROJECT_ENDPOINT: adapter},
    )
    store = FileBootstrapRunnerStateStore(state_root=tmp_path / "state")
    runner = BootstrapRunner(
        state_store=store,
        target_resolution_handler=DefaultFoundryTargetResolutionHandler(
            foundry_inventory=foundry_inventory,
        ),
    )

    first = runner.start(repo)
    turn = _select_and_enable_all(runner, first)
    assert turn.state == "foundry_target_resolution"
    assert turn.next_question is not None
    assert turn.next_question.required_fields == ("account_resource_id",)
    assert foundry_inventory.inspect_calls == []

    turn = runner.answer(
        turn.operation_id,
        turn.next_question.question_id,
        {"account_resource_id": ACCOUNT_RESOURCE_ID},
    )
    record = store.load(turn.operation_id).foundry_targets[0].reviewed_target

    assert turn.state == "verification_policy"
    assert record.state == "existing_aligned"
    assert record.project_endpoint_source == "agent_metadata"
    assert record.agent_name_source == "agent_metadata"
    assert record.account_resource_id == ACCOUNT_RESOURCE_ID
    assert record.latest_agent_version == "1"
    assert record.deployment_ready is True
    assert foundry_inventory.inspect_calls == [PROJECT_ENDPOINT]
    assert foundry_inventory.observe_calls == [
        (PROJECT_ENDPOINT, "example-agent", "1", root, root)
    ]
    assert fakes["agents"].download_calls == [("example-agent", "1")]
    assert fakes["agents"].create_from_code_calls == []
    assert fakes["datasets"].create_calls == []
    assert fakes["dataset_jobs"].create_calls == []
    assert fakes["evaluator_jobs"].create_calls == []
    assert fakes["runs"].create_calls == []


def test_existing_profile_takes_priority_over_agent_metadata(tmp_path: Path) -> None:
    root = "agents/app"
    repo = _create_repository(
        tmp_path,
        agents=[
            {
                "root": root,
                "metadata": {
                    "project_endpoint": METADATA_ENDPOINT,
                    "agent_name": "metadata-agent",
                    "expected_version": "1",
                },
                "sidecar": _sidecar_payload(
                    repo_agent_id="app",
                    root=root,
                    project_endpoint=PROFILE_ENDPOINT,
                    agent_name="profile-agent",
                    account_resource_id=PROFILE_ACCOUNT_ID,
                    expected_version="9",
                ),
            }
        ],
    )
    foundry_inventory = _FakeFoundryInventory(
        latest_versions={PROFILE_ENDPOINT: {"profile-agent": "9"}}
    )
    store = FileBootstrapRunnerStateStore(state_root=tmp_path / "state")
    runner = BootstrapRunner(
        state_store=store,
        target_resolution_handler=DefaultFoundryTargetResolutionHandler(
            foundry_inventory=foundry_inventory,
        ),
    )

    first = runner.start(repo)
    turn = _select_and_enable_all(runner, first)
    record = store.load(turn.operation_id).foundry_targets[0].reviewed_target

    assert turn.state == "verification_policy"
    assert record.project_endpoint == PROFILE_ENDPOINT
    assert record.project_endpoint_source == "existing_profile"
    assert record.agent_name == "profile-agent"
    assert record.agent_name_source == "existing_profile"
    assert record.state == "existing_unknown"
    assert foundry_inventory.inspect_calls == [PROFILE_ENDPOINT]
    assert METADATA_ENDPOINT not in foundry_inventory.inspect_calls


def test_questions_cover_all_unresolved_fields_one_agent_at_a_time(tmp_path: Path) -> None:
    repo = _create_repository(
        tmp_path,
        agents=[
            {
                "root": "agents/a",
                "metadata": {
                    "project_endpoint": PROJECT_ENDPOINT,
                },
            },
            {
                "root": "agents/b",
            },
        ],
    )
    foundry_inventory = _FakeFoundryInventory(
        latest_versions={
            PROJECT_ENDPOINT: {},
            SECOND_ENDPOINT: {},
        }
    )
    store = FileBootstrapRunnerStateStore(state_root=tmp_path / "state")
    runner = BootstrapRunner(
        state_store=store,
        target_resolution_handler=DefaultFoundryTargetResolutionHandler(
            foundry_inventory=foundry_inventory,
        ),
    )

    first = runner.start(repo)
    selected = _selected_ids(first)
    turn = _select_and_enable_all(runner, first)

    assert turn.state == "foundry_target_resolution"
    assert turn.next_question is not None
    assert turn.next_question.kind == "foundry_target"
    assert selected[0] in turn.next_question.title
    assert turn.next_question.required_fields == (
        "agent_name",
        "account_resource_id",
    )

    first_question_id = turn.next_question.question_id
    turn = runner.answer(
        turn.operation_id,
        first_question_id,
        {
            "agent_name": "agent-a",
            "account_resource_id": ACCOUNT_RESOURCE_ID,
        },
    )

    assert turn.state == "foundry_target_resolution"
    assert turn.next_question is not None
    assert turn.next_question.question_id != first_question_id
    assert selected[1] in turn.next_question.title
    assert turn.next_question.required_fields == (
        "project_endpoint",
        "agent_name",
        "account_resource_id",
    )

    with pytest.raises(BootstrapApplyError, match="stale question id"):
        runner.answer(
            turn.operation_id,
            first_question_id,
            {
                "agent_name": "agent-a",
                "account_resource_id": ACCOUNT_RESOURCE_ID,
            },
        )

    resumed = BootstrapRunner(
        state_store=FileBootstrapRunnerStateStore(state_root=tmp_path / "state"),
        target_resolution_handler=DefaultFoundryTargetResolutionHandler(
            foundry_inventory=foundry_inventory,
        ),
    ).start(repo)
    assert resumed.operation_id == turn.operation_id
    assert resumed.next_question == turn.next_question

    final = runner.answer(
        resumed.operation_id,
        resumed.next_question.question_id,
        {
            "project_endpoint": SECOND_ENDPOINT,
            "agent_name": "agent-b",
            "account_resource_id": SECOND_ACCOUNT_ID,
        },
    )
    records = {
        item.repo_agent_id: item.reviewed_target
        for item in store.load(final.operation_id).foundry_targets
    }

    assert final.state == "verification_policy"
    assert {item.state for item in records.values()} == {"new_target"}
    assert foundry_inventory.inspect_calls == [PROJECT_ENDPOINT, SECOND_ENDPOINT]
    assert all(item.deployment_ready for item in records.values())


def test_duplicate_normalized_targets_across_agents_are_rejected(
    tmp_path: Path,
) -> None:
    repo = _create_repository(
        tmp_path,
        agents=[
            {
                "root": "agents/a",
                "metadata": {
                    "project_endpoint": PROJECT_ENDPOINT,
                    "agent_name": "example-agent",
                    "foundry_account_resource_id": ACCOUNT_RESOURCE_ID,
                },
            },
            {
                "root": "agents/b",
                "metadata": {
                    "project_endpoint": f"{PROJECT_ENDPOINT}/",
                    "agent_name": "EXAMPLE-AGENT",
                    "foundry_account_resource_id": ACCOUNT_RESOURCE_ID,
                },
            },
        ],
    )
    runner = BootstrapRunner(
        state_store=FileBootstrapRunnerStateStore(state_root=tmp_path / "state"),
        target_resolution_handler=DefaultFoundryTargetResolutionHandler(
            foundry_inventory=_FakeFoundryInventory(
                latest_versions={PROJECT_ENDPOINT: {}}
            ),
        ),
    )

    first = runner.start(repo)

    with pytest.raises(BootstrapApplyError, match="duplicate Foundry target"):
        _select_and_enable_all(runner, first)


@pytest.mark.parametrize(
    ("metadata", "expected_detail"),
    [
        (
            {
                "project_endpoint": "https://example.invalid/projects/demo",
                "agent_name": "example-agent",
            },
            "invalid project_endpoint",
        ),
        (
            {
                "project_endpoint": PROJECT_ENDPOINT,
                "agent_name": "invalid name",
            },
            "invalid agent_name",
        ),
    ],
)
def test_invalid_metadata_blocks_the_target_without_inventory_calls(
    tmp_path: Path,
    metadata: Mapping[str, str],
    expected_detail: str,
) -> None:
    repo = _create_repository(
        tmp_path,
        agents=[
            {
                "root": "agents/app",
                "metadata": metadata,
            }
        ],
    )
    foundry_inventory = _FakeFoundryInventory(latest_versions={})
    store = FileBootstrapRunnerStateStore(state_root=tmp_path / "state")
    runner = BootstrapRunner(
        state_store=store,
        target_resolution_handler=DefaultFoundryTargetResolutionHandler(
            foundry_inventory=foundry_inventory,
        ),
    )

    first = runner.start(repo)
    turn = _select_and_enable_all(runner, first)
    record = store.load(turn.operation_id).foundry_targets[0].reviewed_target

    assert turn.state == "foundry_target_resolution"
    assert turn.next_question is not None
    assert turn.next_question.kind == "foundry_target"
    assert "blocked" in turn.next_question.details_markdown.casefold()
    assert [action.name for action in turn.available_actions] == ["answer", "status"]
    assert record.state == "blocked"
    assert record.deployment_ready is False
    assert expected_detail in (record.detail or "")
    assert foundry_inventory.inspect_calls == []
    assert foundry_inventory.observe_calls == []


@pytest.mark.parametrize(
    ("answer", "expected_endpoint", "expected_account_id"),
    [
        (
            {"account_resource_id": ACCOUNT_RESOURCE_ID},
            PROJECT_ENDPOINT,
            ACCOUNT_RESOURCE_ID,
        ),
        (
            {
                "project_endpoint": SECOND_ENDPOINT,
                "account_resource_id": SECOND_ACCOUNT_ID,
            },
            SECOND_ENDPOINT,
            SECOND_ACCOUNT_ID,
        ),
    ],
)
def test_skill_resolved_account_is_required_and_accepts_corrected_target(
    tmp_path: Path,
    answer: Mapping[str, str],
    expected_endpoint: str,
    expected_account_id: str,
) -> None:
    repo = _create_repository(
        tmp_path,
        agents=[
            {
                "root": "agents/app",
                "metadata": {
                    "project_endpoint": PROJECT_ENDPOINT,
                    "agent_name": "example-agent",
                },
            }
        ],
    )
    foundry_inventory = _FakeFoundryInventory(
        latest_versions={
            PROJECT_ENDPOINT: {},
            SECOND_ENDPOINT: {},
        }
    )
    store = FileBootstrapRunnerStateStore(state_root=tmp_path / "state")
    runner = BootstrapRunner(
        state_store=store,
        target_resolution_handler=DefaultFoundryTargetResolutionHandler(
            foundry_inventory=foundry_inventory,
        ),
        repository_handler=_ExplodingRepositoryHandler(),
    )

    first = runner.start(repo)
    unresolved = _select_and_enable_all(runner, first)
    status = runner.status(unresolved.operation_id)

    assert unresolved.state == "foundry_target_resolution"
    assert unresolved.next_question is not None
    assert unresolved.next_question.required_fields == ("account_resource_id",)
    assert "Azure account lookup" in unresolved.owner_markdown
    assert "coding-agent Azure tools" in unresolved.next_question.details_markdown
    assert status.owner_markdown == unresolved.owner_markdown
    assert [action.name for action in status.available_actions] == ["answer", "status"]
    assert foundry_inventory.inspect_calls == []

    recovered = runner.answer(
        unresolved.operation_id,
        unresolved.next_question.question_id,
        answer,
    )
    record = store.load(recovered.operation_id).foundry_targets[0].reviewed_target

    assert recovered.state == "verification_policy"
    assert record.state == "new_target"
    assert record.project_endpoint == expected_endpoint
    assert record.agent_name == "example-agent"
    assert record.account_resource_id == expected_account_id
    assert record.deployment_ready is True


def test_skill_resolved_account_must_match_the_submitted_endpoint(
    tmp_path: Path,
) -> None:
    repo = _create_repository(
        tmp_path,
        agents=[
            {
                "root": "agents/app",
            }
        ],
    )
    foundry_inventory = _FakeFoundryInventory(
        latest_versions={PROJECT_ENDPOINT: {}}
    )
    runner = BootstrapRunner(
        state_store=FileBootstrapRunnerStateStore(state_root=tmp_path / "state"),
        target_resolution_handler=DefaultFoundryTargetResolutionHandler(
            foundry_inventory=foundry_inventory,
        ),
    )

    first = runner.start(repo)
    unresolved = _select_and_enable_all(runner, first)

    assert unresolved.next_question is not None
    assert unresolved.next_question.required_fields == (
        "project_endpoint",
        "agent_name",
        "account_resource_id",
    )
    with pytest.raises(
        BootstrapConfigError,
        match="account must match the Foundry project endpoint",
    ):
        runner.answer(
            unresolved.operation_id,
            unresolved.next_question.question_id,
            {
                "account_resource_id": SECOND_ACCOUNT_ID,
                "project_endpoint": PROJECT_ENDPOINT,
                "agent_name": "example-agent",
            },
        )
    assert foundry_inventory.inspect_calls == []


def test_foundry_inventory_failure_stays_renderable_and_retries(
    tmp_path: Path,
) -> None:
    repo = _create_repository(
        tmp_path,
        agents=[
            {
                "root": "agents/app",
                "metadata": {
                    "project_endpoint": PROJECT_ENDPOINT,
                    "agent_name": "example-agent",
                    "foundry_account_resource_id": ACCOUNT_RESOURCE_ID,
                },
            }
        ],
    )
    foundry_inventory = _FakeFoundryInventory(
        latest_versions={PROJECT_ENDPOINT: {}},
        inspect_errors={PROJECT_ENDPOINT: RuntimeError("access denied")},
    )
    store = FileBootstrapRunnerStateStore(state_root=tmp_path / "state")
    runner = BootstrapRunner(
        state_store=store,
        target_resolution_handler=DefaultFoundryTargetResolutionHandler(
            foundry_inventory=foundry_inventory,
        ),
        repository_handler=_ExplodingRepositoryHandler(),
    )

    first = runner.start(repo)
    blocked = _select_and_enable_all(runner, first)
    status = runner.status(blocked.operation_id)

    assert blocked.state == "foundry_target_resolution"
    assert blocked.next_question is not None
    assert blocked.next_question.required_fields == ()
    assert "project inventory failed" in blocked.owner_markdown
    assert "retry" in blocked.next_question.details_markdown.casefold()
    assert status.owner_markdown == blocked.owner_markdown

    foundry_inventory._inspect_errors.clear()
    recovered = runner.answer(
        blocked.operation_id,
        blocked.next_question.question_id,
        {"retry": "true"},
    )
    record = store.load(recovered.operation_id).foundry_targets[0].reviewed_target

    assert recovered.state == "verification_policy"
    assert record.state == "new_target"
    assert record.account_resource_id == ACCOUNT_RESOURCE_ID


def test_existing_diverged_and_existing_unknown_targets_are_recorded(tmp_path: Path) -> None:
    third_endpoint = "https://third.services.ai.azure.com/api/projects/third"
    third_account_id = (
        "/subscriptions/33333333-3333-3333-3333-333333333333/resourceGroups/example-rg"
        "/providers/Microsoft.CognitiveServices/accounts/third"
    )
    repo = _create_repository(
        tmp_path,
        agents=[
            {
                "root": "agents/c",
                "metadata": {
                    "project_endpoint": PROJECT_ENDPOINT,
                    "agent_name": "agent-c",
                    "foundry_account_resource_id": ACCOUNT_RESOURCE_ID,
                    "expected_version": "1",
                },
            },
            {
                "root": "agents/u",
                "metadata": {
                    "project_endpoint": third_endpoint,
                    "agent_name": "agent-u",
                    "foundry_account_resource_id": third_account_id,
                },
            },
        ],
    )
    foundry_inventory = _FakeFoundryInventory(
        latest_versions={
            PROJECT_ENDPOINT: {"agent-c": "2"},
            third_endpoint: {"agent-u": "9"},
        }
    )
    store = FileBootstrapRunnerStateStore(state_root=tmp_path / "state")
    runner = BootstrapRunner(
        state_store=store,
        target_resolution_handler=DefaultFoundryTargetResolutionHandler(
            foundry_inventory=foundry_inventory,
        ),
    )

    first = runner.start(repo)
    turn = _select_and_enable_all(runner, first)
    records = {
        item.repo_agent_id: item.reviewed_target
        for item in store.load(turn.operation_id).foundry_targets
    }

    assert turn.state == "verification_policy"
    assert {item.state for item in records.values()} == {
        "existing_diverged",
        "existing_unknown",
    }
    diverged = next(
        item for item in records.values() if item.state == "existing_diverged"
    )
    unknown = next(
        item for item in records.values() if item.state == "existing_unknown"
    )
    assert diverged.deployment_ready is True
    assert unknown.deployment_ready is False
