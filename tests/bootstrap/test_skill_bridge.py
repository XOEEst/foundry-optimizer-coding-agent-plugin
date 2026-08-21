from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = (
    REPOSITORY_ROOT
    / "plugins"
    / "foundry-bootstrap"
    / "scripts"
    / "bootstrap.py"
)


@dataclass(frozen=True)
class _Choice:
    value: str
    label: str
    detail: str | None = None


@dataclass(frozen=True)
class _Question:
    question_id: str
    kind: str
    title: str
    details_markdown: str
    allow_multiple: bool = False
    choices: tuple[_Choice, ...] = ()


@dataclass(frozen=True)
class _Action:
    name: str
    step: str | None = None


@dataclass(frozen=True)
class _Turn:
    owner_markdown: str
    next_question: _Question | None
    available_actions: tuple[_Action, ...]
    operation_id: str
    state: str
    resource_links: dict[str, object]


def _load_bridge_module():
    spec = importlib.util.spec_from_file_location("foundry_bootstrap_skill_bridge", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    shutil.rmtree(SCRIPT_PATH.parent / "__pycache__", ignore_errors=True)
    return module


def _create_repo(tmp_path: Path) -> Path:
    repository = tmp_path / "customer"
    repository.mkdir()
    subprocess.run(["git", "-C", str(repository), "init", "--quiet"], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.name", "Skill Bridge Test"], check=True)
    subprocess.run(["git", "-C", str(repository), "config", "user.email", "skill-bridge@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repository), "commit", "--allow-empty", "--quiet", "-m", "init"], check=True)
    return repository.resolve()


def _extract(output: str, begin: str, end: str) -> str:
    return output.split(begin, 1)[1].split(end, 1)[0].strip()


class _FakeRunner:
    def __init__(
        self,
        *,
        start_turn: _Turn | None = None,
        answer_turn: _Turn | None = None,
        approve_turn: _Turn | None = None,
        status_turn: _Turn | None = None,
        rollback_turn: _Turn | None = None,
        answer_error: Exception | None = None,
    ) -> None:
        self.start_turn = start_turn
        self.answer_turn = answer_turn
        self.approve_turn = approve_turn
        self.status_turn = status_turn
        self.rollback_turn = rollback_turn
        self.answer_error = answer_error
        self.calls: list[tuple[object, ...]] = []

    def start(self, repository: str | Path) -> _Turn:
        self.calls.append(("start", Path(repository).resolve()))
        assert self.start_turn is not None
        return self.start_turn

    def answer(self, operation_id: str, question_id: str, answer: object) -> _Turn:
        self.calls.append(("answer", operation_id, question_id, answer))
        if self.answer_error is not None:
            raise self.answer_error
        assert self.answer_turn is not None
        return self.answer_turn

    def approve(self, operation_id: str, step: str, actor: str, summary: str) -> _Turn:
        self.calls.append(("approve", operation_id, step, actor, summary))
        assert self.approve_turn is not None
        return self.approve_turn

    def status(self, operation_id: str) -> _Turn:
        self.calls.append(("status", operation_id))
        assert self.status_turn is not None
        return self.status_turn

    def rollback(self, operation_id: str, step: str) -> _Turn:
        self.calls.append(("rollback", operation_id, step))
        assert self.rollback_turn is not None
        return self.rollback_turn


def _start_turn() -> _Turn:
    return _Turn(
        owner_markdown="## Bootstrap preflight\n- Start here",
        next_question=_Question(
            question_id="agent_selection:0:abc123",
            kind="agent_selection",
            title="Select the repository agents to bootstrap",
            details_markdown="Choose one or more discovered `repoAgentId` values.",
            allow_multiple=True,
            choices=(
                _Choice(value="root-agent", label="root-agent (.)"),
                _Choice(value="leaf-agent", label="leaf-agent (agents/leaf)"),
            ),
        ),
        available_actions=(
            _Action(name="answer"),
            _Action(name="status"),
        ),
        operation_id="bootstrap-123",
        state="agent_selection",
        resource_links={"github": [{"label": "Actions", "url": "https://github.com/example/repo/actions"}]},
    )


def _status_turn() -> _Turn:
    return _Turn(
        owner_markdown="## Bridge state\n- Selection recorded.",
        next_question=_Question(
            question_id="foundry_target:1:def456",
            kind="foundry_target",
            title="Resolve the reviewed Foundry target",
            details_markdown="Provide the reviewed Foundry target.",
        ),
        available_actions=(
            _Action(name="answer"),
            _Action(name="status"),
        ),
        operation_id="bootstrap-123",
        state="foundry_target_resolution",
        resource_links={"github": [{"label": "Actions", "url": "https://github.com/example/repo/actions"}]},
    )


def _rollback_turn() -> _Turn:
    return _Turn(
        owner_markdown="## Status\n- Recorded child work was rolled back.",
        next_question=None,
        available_actions=(_Action(name="status"),),
        operation_id="bootstrap-123",
        state="rolled_back",
        resource_links={"github": [{"label": "Actions", "url": "https://github.com/example/repo/actions"}]},
    )


def test_start_emits_owner_markdown_separately_from_machine_turn(tmp_path: Path) -> None:
    module = _load_bridge_module()
    repository = _create_repo(tmp_path)
    runner = _FakeRunner(start_turn=_start_turn())
    private_state_root = (tmp_path / "private-state").resolve()
    seen_state_roots: list[Path] = []

    def _factory(state_root: Path) -> _FakeRunner:
        seen_state_roots.append(state_root)
        return runner

    stdout = io.StringIO()
    stderr = io.StringIO()
    exit_code = module.main(
        [
            "--state-root",
            str(private_state_root),
            "start",
            "--repository",
            str(repository),
        ],
        runner_factory=_factory,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert stderr.getvalue() == ""
    assert seen_state_roots == [private_state_root]
    assert runner.calls == [("start", repository)]
    owner_markdown = _extract(
        stdout.getvalue(),
        module._OWNER_MARKDOWN_BEGIN,
        module._OWNER_MARKDOWN_END,
    )
    machine_turn = json.loads(
        _extract(stdout.getvalue(), module._TURN_BEGIN, module._TURN_END)
    )
    assert owner_markdown == _start_turn().owner_markdown
    assert owner_markdown.lstrip().startswith("## Bootstrap preflight")
    assert '"operation_id"' not in owner_markdown
    assert "owner_markdown" not in machine_turn
    assert machine_turn["operation_id"] == "bootstrap-123"
    assert machine_turn["state"] == "agent_selection"
    assert machine_turn["next_question"]["question_id"] == "agent_selection:0:abc123"


def test_answer_uses_choice_arguments_with_fake_runner() -> None:
    module = _load_bridge_module()
    runner = _FakeRunner(answer_turn=_status_turn())
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = module.main(
        [
            "answer",
            "--operation-id",
            "bootstrap-123",
            "--question-id",
            "agent_selection:0:abc123",
            "--choice",
            "root-agent",
            "--choice",
            "leaf-agent",
        ],
        runner_factory=lambda _: runner,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert stderr.getvalue() == ""
    assert runner.calls == [
        (
            "answer",
            "bootstrap-123",
            "agent_selection:0:abc123",
            ["root-agent", "leaf-agent"],
        )
    ]
    machine_turn = json.loads(
        _extract(stdout.getvalue(), module._TURN_BEGIN, module._TURN_END)
    )
    assert machine_turn["state"] == "foundry_target_resolution"
    assert machine_turn["next_question"]["kind"] == "foundry_target"


def test_approve_passes_exact_owner_approval_details() -> None:
    module = _load_bridge_module()
    runner = _FakeRunner(approve_turn=_status_turn())
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = module.main(
        [
            "approve",
            "--operation-id",
            "bootstrap-123",
            "--step",
            "connection",
            "--actor",
            "repo-owner",
            "--summary",
            "Approved the reviewed GitHub and Azure connection plan.",
        ],
        runner_factory=lambda _: runner,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert stderr.getvalue() == ""
    assert runner.calls == [
        (
            "approve",
            "bootstrap-123",
            "connection",
            "repo-owner",
            "Approved the reviewed GitHub and Azure connection plan.",
        )
    ]


def test_status_resumes_with_only_an_operation_id() -> None:
    module = _load_bridge_module()
    runner = _FakeRunner(status_turn=_status_turn())
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = module.main(
        [
            "status",
            "--operation-id",
            "bootstrap-123",
        ],
        runner_factory=lambda _: runner,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert stderr.getvalue() == ""
    assert runner.calls == [("status", "bootstrap-123")]
    machine_turn = json.loads(
        _extract(stdout.getvalue(), module._TURN_BEGIN, module._TURN_END)
    )
    assert machine_turn["next_question"]["question_id"] == "foundry_target:1:def456"


def test_rollback_uses_the_recorded_step_with_fake_runner() -> None:
    module = _load_bridge_module()
    runner = _FakeRunner(rollback_turn=_rollback_turn())
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = module.main(
        [
            "rollback",
            "--operation-id",
            "bootstrap-123",
            "--step",
            "connection",
        ],
        runner_factory=lambda _: runner,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert stderr.getvalue() == ""
    assert runner.calls == [("rollback", "bootstrap-123", "connection")]
    machine_turn = json.loads(
        _extract(stdout.getvalue(), module._TURN_BEGIN, module._TURN_END)
    )
    assert machine_turn["state"] == "rolled_back"
    assert machine_turn["next_question"] is None


def test_answer_rejects_malformed_structured_response() -> None:
    module = _load_bridge_module()
    runner = _FakeRunner(answer_turn=_status_turn())
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = module.main(
        [
            "answer",
            "--operation-id",
            "bootstrap-123",
            "--question-id",
            "agent_selection:0:abc123",
            "--response-json",
            "{not-json",
        ],
        runner_factory=lambda _: runner,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 1
    assert stdout.getvalue() == ""
    assert "answer JSON is invalid" in stderr.getvalue()
    assert runner.calls == []


def test_answer_reports_stale_question_errors() -> None:
    module = _load_bridge_module()
    runner = _FakeRunner(answer_error=RuntimeError("stale question id"))
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = module.main(
        [
            "answer",
            "--operation-id",
            "bootstrap-123",
            "--question-id",
            "agent_selection:0:abc123",
            "--choice",
            "root-agent",
        ],
        runner_factory=lambda _: runner,
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 1
    assert stdout.getvalue() == ""
    assert "stale question id" in stderr.getvalue()
    assert runner.calls == [
        ("answer", "bootstrap-123", "agent_selection:0:abc123", ["root-agent"])
    ]


def test_source_checkout_factory_wires_the_complete_owner_flow(
    tmp_path: Path,
) -> None:
    module = _load_bridge_module()
    private_state_root = tmp_path / "private-state"

    factory = module._load_production_runner_factory(
        (),
        script_path=SCRIPT_PATH,
        private_state_root=private_state_root,
        skill_lock_argument=None,
    )
    runner = factory(private_state_root)

    assert runner._repository_handler is not None
    assert runner._connection_handler is not None
    assert runner._commit_handler is not None
    assert runner._deployment_handler is not None
    assert len(module.os.environ[module._RUNTIME_LOCK_ENV]) == 64
