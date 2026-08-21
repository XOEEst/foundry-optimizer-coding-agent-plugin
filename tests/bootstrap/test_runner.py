from __future__ import annotations

from pathlib import Path
import subprocess
from datetime import UTC, datetime

import pytest

from foundry_opt.bootstrap.errors import BootstrapApplyError
from foundry_opt.bootstrap.runner import (
    BootstrapChildReference,
    BootstrapRollbackHandlerProtocol,
    BootstrapRunner,
    BootstrapRunnerStateEnvelope,
    BootstrapStageOutcome,
    FileBootstrapRunnerStateStore,
    next_runner_generation,
)

RUNTIME_REPOSITORY = "https://github.com/example-org/foundry-opt-runtime.git"
RUNTIME_COMMIT = "a" * 40
REPOSITORY_REMOTE = "https://github.com/example-org/example-repo.git"


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _create_repository(tmp_path: Path) -> Path:
    repo = tmp_path / "customer"
    _write(
        repo / ".foundry" / "agent-metadata.yaml",
        "\n".join(
            (
                "schema_version: 1",
                "project_endpoint: https://example.invalid/projects/demo",
                "agent_name: root-agent",
            )
        )
        + "\n",
    )
    _write(
        repo / ".github" / "foundry-optimizer.yaml",
        "\n".join(
            (
                "schema_version: 1",
                "source_root: agent",
                "editable_paths: [agent/**]",
                "metadata_path: .foundry/agent-metadata.yaml",
            )
        )
        + "\n",
    )
    _write(
        repo / "agent" / "main.py",
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
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Runner Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "runner@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", REPOSITORY_REMOTE], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "--quiet", "-m", "initial"], check=True)
    return repo


class _RecordingRollbackHandler(BootstrapRollbackHandlerProtocol):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def rollback(
        self,
        *,
        operation: BootstrapRunnerStateEnvelope,
        step: str,
        child_ref: BootstrapChildReference,
    ) -> BootstrapStageOutcome:
        self.calls.append((operation.operation_id, step, child_ref.identifier))
        remaining = tuple(item for item in operation.child_refs if item.step != step)
        return BootstrapStageOutcome(
            stage="rolled_back",
            note=f"Rolled back {step} child work.",
            child_refs=remaining,
        )


class _RacingStateStore(FileBootstrapRunnerStateStore):
    def __init__(self, *, state_root: Path) -> None:
        super().__init__(state_root=state_root)
        self._inject_conflict = True

    def save(
        self,
        envelope: BootstrapRunnerStateEnvelope,
        *,
        expected_generation: int | None = None,
        expected_generation_hash: str | None = None,
    ) -> None:
        if expected_generation is not None and self._inject_conflict:
            current = self.load(envelope.operation_id)
            conflict = next_runner_generation(
                current,
                now=datetime.now(UTC),
                note="concurrent write",
            )
            self._inject_conflict = False
            super().save(conflict, expected_generation=current.generation, expected_generation_hash=current.generation_hash)
        super().save(
            envelope,
            expected_generation=expected_generation,
            expected_generation_hash=expected_generation_hash,
        )


@pytest.fixture(autouse=True)
def _runtime_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_OPT_RUNTIME_REPOSITORY", RUNTIME_REPOSITORY)
    monkeypatch.setenv("FOUNDRY_OPT_RUNTIME_COMMIT", RUNTIME_COMMIT)


def test_start_builds_a_discovery_turn(tmp_path: Path) -> None:
    repo = _create_repository(tmp_path)
    runner = BootstrapRunner(state_store=FileBootstrapRunnerStateStore(state_root=tmp_path / "state"))

    turn = runner.start(repo)

    assert turn.state == "agent_selection"
    assert turn.next_question is not None
    assert turn.next_question.kind == "agent_selection"
    assert turn.next_question.allow_multiple is True
    assert turn.available_actions[0].name == "answer"
    assert turn.resource_links.github[0].url == "https://github.com/example-org/example-repo/actions"


def test_start_owner_markdown_uses_discovery_review_without_raw_json(tmp_path: Path) -> None:
    repo = _create_repository(tmp_path)
    runner = BootstrapRunner(state_store=FileBootstrapRunnerStateStore(state_root=tmp_path / "state"))

    turn = runner.start(repo)

    assert "Discovery review" in turn.owner_markdown
    assert "Source root: agent" in turn.owner_markdown
    assert '"schema_version"' not in turn.owner_markdown
    assert '"repoAgentId"' not in turn.owner_markdown
    assert not turn.owner_markdown.lstrip().startswith("{")


def test_answer_accepts_valid_selection_and_advances_to_foundry_target_resolution(tmp_path: Path) -> None:
    repo = _create_repository(tmp_path)
    runner = BootstrapRunner(state_store=FileBootstrapRunnerStateStore(state_root=tmp_path / "state"))
    first = runner.start(repo)

    turn = runner.answer(
        first.operation_id,
        first.next_question.question_id,
        [first.next_question.choices[0].value],
    )

    assert turn.state == "foundry_target_resolution"
    assert turn.next_question is not None
    assert turn.next_question.kind == "foundry_target"
    assert "Selected stable IDs" in turn.owner_markdown


def test_answer_rejects_invalid_selection(tmp_path: Path) -> None:
    repo = _create_repository(tmp_path)
    store = FileBootstrapRunnerStateStore(state_root=tmp_path / "state")
    runner = BootstrapRunner(state_store=store)
    first = runner.start(repo)

    with pytest.raises(BootstrapApplyError, match="unknown repoAgentId"):
        runner.answer(first.operation_id, first.next_question.question_id, ["missing-agent"])

    status = runner.status(first.operation_id)
    assert status.state == "agent_selection"


def test_answer_rejects_stale_selection_question(tmp_path: Path) -> None:
    repo = _create_repository(tmp_path)
    runner = BootstrapRunner(state_store=FileBootstrapRunnerStateStore(state_root=tmp_path / "state"))
    first = runner.start(repo)
    runner.answer(
        first.operation_id,
        first.next_question.question_id,
        [first.next_question.choices[0].value],
    )

    with pytest.raises(BootstrapApplyError, match="stale question id"):
        runner.answer(
            first.operation_id,
            first.next_question.question_id,
            [first.next_question.choices[0].value],
        )


def test_persistence_and_status_resume_use_private_state_store(tmp_path: Path) -> None:
    repo = _create_repository(tmp_path)
    state_root = tmp_path / "private-state"
    first_runner = BootstrapRunner(state_store=FileBootstrapRunnerStateStore(state_root=state_root))
    second_runner = BootstrapRunner(state_store=FileBootstrapRunnerStateStore(state_root=state_root))
    first = first_runner.start(repo)

    resumed = second_runner.status(first.operation_id)

    assert resumed.operation_id == first.operation_id
    assert resumed.state == first.state
    assert resumed.next_question == first.next_question
    assert resumed.resource_links == first.resource_links


def test_concurrent_generation_conflict_is_rejected(tmp_path: Path) -> None:
    repo = _create_repository(tmp_path)
    store = _RacingStateStore(state_root=tmp_path / "state")
    runner = BootstrapRunner(state_store=store)
    first = runner.start(repo)

    with pytest.raises(BootstrapApplyError, match="generation conflict"):
        runner.answer(
            first.operation_id,
            first.next_question.question_id,
            [first.next_question.choices[0].value],
        )


def test_status_refuses_repository_resume_after_commit_changes(tmp_path: Path) -> None:
    repo = _create_repository(tmp_path)
    runner = BootstrapRunner(state_store=FileBootstrapRunnerStateStore(state_root=tmp_path / "state"))
    turn = runner.start(repo)
    _write(repo / "agent" / "extra.py", "print('change')\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "--quiet", "-m", "change"], check=True)

    with pytest.raises(BootstrapApplyError, match="exact repository root, identity, and commit"):
        runner.status(turn.operation_id)


def test_rollback_delegates_to_child_handler(tmp_path: Path) -> None:
    repo = _create_repository(tmp_path)
    state_root = tmp_path / "state"
    store = FileBootstrapRunnerStateStore(state_root=state_root)
    rollback_handler = _RecordingRollbackHandler()
    runner = BootstrapRunner(state_store=store, rollback_handler=rollback_handler)
    turn = runner.start(repo)
    envelope = store.load(turn.operation_id)
    updated = next_runner_generation(
        envelope,
        now=datetime.now(UTC),
        lifecycle_stage="final_handoff",
        child_refs=(
            BootstrapChildReference(
                step="connection",
                kind="connection-operation",
                identifier="connect-op-1",
                summary="planned connection child",
            ),
        ),
        note="child work recorded",
    )
    store.save(updated, expected_generation=envelope.generation, expected_generation_hash=envelope.generation_hash)

    rolled = runner.rollback(turn.operation_id, "connection")

    assert rollback_handler.calls == [(turn.operation_id, "connection", "connect-op-1")]
    assert rolled.state == "rolled_back"
    assert rolled.resource_links.github[0].url == "https://github.com/example-org/example-repo/actions"
