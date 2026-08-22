from __future__ import annotations

from dataclasses import dataclass
import importlib.util
import io
import json
import os
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
    required_fields: tuple[str, ...] = ()
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
            required_fields=("account_resource_id",),
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
    assert machine_turn["next_question"]["required_fields"] == [
        "account_resource_id"
    ]


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


def test_answer_uses_dedicated_foundry_target_flags() -> None:
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
            "foundry_target:1:def456",
            "--project-endpoint",
            "https://example.services.ai.azure.com/api/projects/example",
            "--agent-name",
            "example-agent",
            "--account-resource-id",
            (
                "/subscriptions/11111111-1111-1111-1111-111111111111/"
                "resourceGroups/example-rg/providers/"
                "Microsoft.CognitiveServices/accounts/example"
            ),
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
            "foundry_target:1:def456",
            {
                "project_endpoint": "https://example.services.ai.azure.com/api/projects/example",
                "agent_name": "example-agent",
                "account_resource_id": (
                    "/subscriptions/11111111-1111-1111-1111-111111111111/"
                    "resourceGroups/example-rg/providers/"
                    "Microsoft.CognitiveServices/accounts/example"
                ),
            },
        )
    ]


def test_answer_uses_retry_without_raw_json() -> None:
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
            "foundry_target:1:def456",
            "--retry",
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
            "foundry_target:1:def456",
            {"retry": "true"},
        )
    ]
    parser = module._build_parser()
    subparsers = next(
        action for action in parser._actions if getattr(action, "choices", None)
    )
    answer_parser = subparsers.choices["answer"]
    option_strings = {
        option
        for action in answer_parser._actions
        for option in action.option_strings
    }
    assert "--response-json" not in option_strings


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


def test_downloaded_skill_reexecs_before_importing_ambient_runtime(
    tmp_path: Path,
) -> None:
    downloaded_root = tmp_path / "downloaded" / "foundry-bootstrap"
    downloaded_script = downloaded_root / "scripts" / "bootstrap.py"
    downloaded_script.parent.mkdir(parents=True)
    shutil.copyfile(SCRIPT_PATH, downloaded_script)
    skill_lock = downloaded_root / "skill.lock.json"
    runtime_commit = "a" * 40
    skill_lock.write_text(
        json.dumps(
            {
                "package_path": ".",
                "runtime_commit": runtime_commit,
                "runtime_repository": "https://github.com/example/runtime.git",
                "schema_version": 1,
                "uv_lock_sha256": "b" * 64,
            }
        ),
        encoding="utf-8",
    )

    ambient_root = tmp_path / "ambient"
    ambient_bootstrap = ambient_root / "foundry_opt" / "bootstrap"
    ambient_bootstrap.mkdir(parents=True)
    marker = tmp_path / "ambient-imported"
    (ambient_root / "foundry_opt" / "__init__.py").write_text("", encoding="utf-8")
    (ambient_bootstrap / "__init__.py").write_text(
        "\n".join(
            (
                "import os",
                "from pathlib import Path",
                'Path(os.environ["AMBIENT_MARKER"]).write_text("imported", encoding="utf-8")',
                "class AmbientRuntime: pass",
                "BootstrapLocalCommitHandler = AmbientRuntime",
                "BootstrapLocalDeploymentHandler = AmbientRuntime",
                "BootstrapConnectionSetupHandler = AmbientRuntime",
                "BootstrapRepositorySetupHandler = AmbientRuntime",
                "BootstrapRunner = AmbientRuntime",
                "LocalDeploymentCoordinator = AmbientRuntime",
                "LocalGitCommitCoordinator = AmbientRuntime",
                "ConnectionSetupCoordinator = AmbientRuntime",
                "RepositorySetupCoordinator = AmbientRuntime",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    (ambient_bootstrap / "runner.py").write_text(
        "class FileBootstrapRunnerStateStore: pass\n",
        encoding="utf-8",
    )

    harness = """
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

script_path = Path(sys.argv[1])
skill_lock = Path(sys.argv[2])
state_root = Path(sys.argv[3])
marker = Path(sys.argv[4])
spec = importlib.util.spec_from_file_location("downloaded_bootstrap", script_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
installer_calls = []
module._resolve_runtime_python_from_installer = (
    lambda *args, **kwargs: installer_calls.append(True) or "verified-python"
)
reexec = {}
def fake_run(command, **kwargs):
    reexec["command"] = command
    reexec["env"] = kwargs.get("env", {})
    return SimpleNamespace(returncode=23)
module.subprocess.run = fake_run
return_code = None
try:
    module._load_production_runner_factory(
        ("status", "--operation-id", "op"),
        script_path=script_path,
        private_state_root=state_root,
        skill_lock_argument=str(skill_lock),
    )
except module._ReexecRequested as exc:
    return_code = exc.return_code
print(json.dumps({
    "ambient_ran": marker.exists(),
    "installer_calls": len(installer_calls),
    "pythonpath_forwarded": "PYTHONPATH" in reexec.get("env", {}),
    "return_code": return_code,
    "runtime_ready": reexec.get("env", {}).get(module._RUNTIME_READY_ENV),
    "runtime_commit": os.environ.get(module._RUNTIME_COMMIT_ENV),
}))
"""
    env = dict(os.environ)
    env.pop("FOUNDRY_BOOTSTRAP_RUNTIME_READY", None)
    env["AMBIENT_MARKER"] = str(marker)
    env["PYTHONPATH"] = str(ambient_root)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            harness,
            str(downloaded_script),
            str(skill_lock),
            str(tmp_path / "private-state"),
            str(marker),
        ],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env=env,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        "ambient_ran": False,
        "installer_calls": 1,
        "pythonpath_forwarded": False,
        "return_code": 23,
        "runtime_ready": "1",
        "runtime_commit": runtime_commit,
    }
