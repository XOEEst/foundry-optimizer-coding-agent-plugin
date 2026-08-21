from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
import subprocess

import pytest
import yaml

from foundry_opt.bootstrap.errors import BootstrapApplyError
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


class _FakeAzureInventory:
    def __init__(self, mapping: Mapping[str, str | None]) -> None:
        self._mapping = dict(mapping)
        self.calls: list[str] = []

    def resolve_account_resource_id(self, project_endpoint: str) -> str | None:
        self.calls.append(project_endpoint)
        return self._mapping.get(project_endpoint)


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
    azure_inventory = _FakeAzureInventory({PROJECT_ENDPOINT: ACCOUNT_RESOURCE_ID})
    store = FileBootstrapRunnerStateStore(state_root=tmp_path / "state")
    runner = BootstrapRunner(
        state_store=store,
        target_resolution_handler=DefaultFoundryTargetResolutionHandler(
            foundry_inventory=foundry_inventory,
            azure_inventory=azure_inventory,
        ),
    )

    first = runner.start(repo)
    turn = _select_and_enable_all(runner, first)
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
    azure_inventory = _FakeAzureInventory({PROFILE_ENDPOINT: PROFILE_ACCOUNT_ID})
    store = FileBootstrapRunnerStateStore(state_root=tmp_path / "state")
    runner = BootstrapRunner(
        state_store=store,
        target_resolution_handler=DefaultFoundryTargetResolutionHandler(
            foundry_inventory=foundry_inventory,
            azure_inventory=azure_inventory,
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


def test_questions_only_cover_unresolved_fields_one_agent_at_a_time(tmp_path: Path) -> None:
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
    azure_inventory = _FakeAzureInventory(
        {
            PROJECT_ENDPOINT: ACCOUNT_RESOURCE_ID,
            SECOND_ENDPOINT: SECOND_ACCOUNT_ID,
        }
    )
    store = FileBootstrapRunnerStateStore(state_root=tmp_path / "state")
    runner = BootstrapRunner(
        state_store=store,
        target_resolution_handler=DefaultFoundryTargetResolutionHandler(
            foundry_inventory=foundry_inventory,
            azure_inventory=azure_inventory,
        ),
    )

    first = runner.start(repo)
    selected = _selected_ids(first)
    turn = _select_and_enable_all(runner, first)

    assert turn.state == "foundry_target_resolution"
    assert turn.next_question is not None
    assert turn.next_question.kind == "foundry_target"
    assert selected[0] in turn.next_question.title
    assert "`agent_name`" in turn.next_question.details_markdown
    assert "`project_endpoint`" not in turn.next_question.details_markdown

    first_question_id = turn.next_question.question_id
    turn = runner.answer(
        turn.operation_id,
        first_question_id,
        {"agent_name": "agent-a"},
    )

    assert turn.state == "foundry_target_resolution"
    assert turn.next_question is not None
    assert turn.next_question.question_id != first_question_id
    assert selected[1] in turn.next_question.title
    assert "`project_endpoint`" in turn.next_question.details_markdown
    assert "`agent_name`" in turn.next_question.details_markdown

    with pytest.raises(BootstrapApplyError, match="stale question id"):
        runner.answer(
            turn.operation_id,
            first_question_id,
            {"agent_name": "agent-a"},
        )

    final = runner.answer(
        turn.operation_id,
        turn.next_question.question_id,
        {
            "project_endpoint": SECOND_ENDPOINT,
            "agent_name": "agent-b",
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
    azure_inventory = _FakeAzureInventory({})
    store = FileBootstrapRunnerStateStore(state_root=tmp_path / "state")
    runner = BootstrapRunner(
        state_store=store,
        target_resolution_handler=DefaultFoundryTargetResolutionHandler(
            foundry_inventory=foundry_inventory,
            azure_inventory=azure_inventory,
        ),
    )

    first = runner.start(repo)
    turn = _select_and_enable_all(runner, first)
    record = store.load(turn.operation_id).foundry_targets[0].reviewed_target

    assert turn.state == "verification_policy"
    assert record.state == "blocked"
    assert record.deployment_ready is False
    assert expected_detail in (record.detail or "")
    assert foundry_inventory.inspect_calls == []
    assert foundry_inventory.observe_calls == []
    assert azure_inventory.calls == []


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
                    "expected_version": "1",
                },
            },
            {
                "root": "agents/u",
                "metadata": {
                    "project_endpoint": third_endpoint,
                    "agent_name": "agent-u",
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
    azure_inventory = _FakeAzureInventory(
        {
            PROJECT_ENDPOINT: ACCOUNT_RESOURCE_ID,
            third_endpoint: third_account_id,
        }
    )
    store = FileBootstrapRunnerStateStore(state_root=tmp_path / "state")
    runner = BootstrapRunner(
        state_store=store,
        target_resolution_handler=DefaultFoundryTargetResolutionHandler(
            foundry_inventory=foundry_inventory,
            azure_inventory=azure_inventory,
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
