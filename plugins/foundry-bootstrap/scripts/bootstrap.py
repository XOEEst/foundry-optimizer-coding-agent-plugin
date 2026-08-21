#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, is_dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any, Protocol, TextIO

_OWNER_MARKDOWN_BEGIN = "<<<FOUNDRY_BOOTSTRAP_OWNER_MARKDOWN>>>"
_OWNER_MARKDOWN_END = "<<<END_FOUNDRY_BOOTSTRAP_OWNER_MARKDOWN>>>"
_TURN_BEGIN = "<<<FOUNDRY_BOOTSTRAP_TURN>>>"
_TURN_END = "<<<END_FOUNDRY_BOOTSTRAP_TURN>>>"
_EMIT_RUNTIME_PYTHON_ENV = "FOUNDRY_BOOTSTRAP_EMIT_RUNTIME_PYTHON"
_RUNTIME_READY_ENV = "FOUNDRY_BOOTSTRAP_RUNTIME_READY"
_RUNTIME_REPOSITORY_ENV = "FOUNDRY_OPT_RUNTIME_REPOSITORY"
_RUNTIME_COMMIT_ENV = "FOUNDRY_OPT_RUNTIME_COMMIT"
_RUNTIME_LOCK_ENV = "FOUNDRY_OPT_RUNTIME_LOCK_SHA256"
_RUNTIME_PACKAGE_PATH_ENV = "FOUNDRY_OPT_RUNTIME_PACKAGE_PATH"
_SKILL_LOCK_NAME = "skill.lock.json"
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA64_RE = re.compile(r"^[0-9a-f]{64}$")


class _RunnerProtocol(Protocol):
    def start(self, repository: str | Path) -> object: ...
    def answer(self, operation_id: str, question_id: str, answer: object) -> object: ...
    def approve(self, operation_id: str, step: str, actor: str, summary: str) -> object: ...
    def status(self, operation_id: str) -> object: ...
    def rollback(self, operation_id: str, step: str) -> object: ...


_RunnerFactory = Callable[[Path], _RunnerProtocol]


class _ReexecRequested(Exception):
    def __init__(self, return_code: int) -> None:
        super().__init__(str(return_code))
        self.return_code = return_code


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def _default_private_state_root() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "foundry-opt" / "bootstrap"
    return Path.home() / ".foundry-opt" / "bootstrap"


def _resolve_private_state_root(value: str | None) -> Path:
    candidate = Path(value).expanduser() if value else _default_private_state_root()
    return candidate.resolve()


def _runner_state_root(private_state_root: Path) -> Path:
    return private_state_root / "runner"


def _runtime_work_root(private_state_root: Path) -> Path:
    return private_state_root / "runtime"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bootstrap.py",
        description="Owner bridge over BootstrapRunner for the downloadable foundry-bootstrap skill.",
    )
    parser.add_argument(
        "--state-root",
        help="Private bootstrap state root. Defaults to the OS-specific foundry-opt bootstrap directory.",
    )
    parser.add_argument(
        "--skill-lock",
        help="Path to a materialized skill.lock.json used for verified runtime installation when needed.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="Start a bootstrap operation.")
    start.add_argument(
        "--repository",
        default=".",
        help="Repository path to bootstrap. Defaults to the current working directory.",
    )

    answer = subparsers.add_parser("answer", help="Answer the current bootstrap question.")
    answer.add_argument("--operation-id", required=True)
    answer.add_argument("--question-id", required=True)
    answer_group = answer.add_mutually_exclusive_group(required=True)
    answer_group.add_argument(
        "--choice",
        action="append",
        dest="choices",
        help="Repeat for each selected choice value.",
    )
    answer_group.add_argument(
        "--response",
        help="Free-form string response.",
    )
    answer_group.add_argument(
        "--response-json",
        help="Structured JSON response for advanced or recovery flows.",
    )
    answer_group.add_argument(
        "--yes",
        action="store_const",
        const=True,
        dest="boolean_response",
        help="Boolean yes response.",
    )
    answer_group.add_argument(
        "--no",
        action="store_const",
        const=False,
        dest="boolean_response",
        help="Boolean no response.",
    )

    approve = subparsers.add_parser("approve", help="Record an owner approval.")
    approve.add_argument("--operation-id", required=True)
    approve.add_argument(
        "--step",
        required=True,
        choices=("repository", "connection", "commit", "deployment"),
    )
    approve.add_argument("--actor", required=True)
    approve.add_argument("--summary", required=True)

    status = subparsers.add_parser("status", help="Resume and inspect an operation.")
    status.add_argument("--operation-id", required=True)

    rollback = subparsers.add_parser("rollback", help="Rollback a recorded child step.")
    rollback.add_argument("--operation-id", required=True)
    rollback.add_argument(
        "--step",
        required=True,
        choices=("repository", "connection", "commit", "deployment"),
    )

    return parser


def _resolve_repository_root(value: str) -> Path:
    candidate = Path(value).expanduser().resolve()
    if not candidate.exists():
        raise RuntimeError(f"repository path does not exist: {candidate}")
    if not candidate.is_dir():
        raise RuntimeError(f"repository path is not a directory: {candidate}")
    completed = subprocess.run(
        ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "unable to locate repository root"
        raise RuntimeError(message)
    return Path(completed.stdout.strip()).resolve()


def _locate_source_checkout(script_path: Path) -> Path | None:
    for parent in (script_path.parent, *script_path.parents):
        src_root = parent / "src" / "foundry_opt" / "bootstrap" / "runner.py"
        if (parent / "pyproject.toml").is_file() and src_root.is_file():
            return parent
    return None


def _git_output(repository_root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository_root), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "git command failed"
        raise RuntimeError(message)
    return completed.stdout.strip()


def _ensure_runtime_env_from_source_checkout(repository_root: Path) -> None:
    try:
        runtime_repository = _git_output(repository_root, "remote", "get-url", "origin")
    except RuntimeError:
        runtime_repository = repository_root.as_uri()
    runtime_commit = _git_output(repository_root, "rev-parse", "HEAD").lower()
    os.environ[_RUNTIME_REPOSITORY_ENV] = runtime_repository
    os.environ[_RUNTIME_COMMIT_ENV] = runtime_commit


def _load_skill_lock(path: Path) -> dict[str, str]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"skill lock does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"skill lock is not valid JSON: {path}") from exc
    if not isinstance(document, dict):
        raise RuntimeError("skill lock must contain a JSON object")
    resolved: dict[str, str] = {}
    for field in ("runtime_repository", "runtime_commit", "uv_lock_sha256", "package_path"):
        value = document.get(field)
        if not isinstance(value, str) or not value:
            raise RuntimeError(f"skill lock is missing {field}")
        resolved[field] = value
    runtime_commit = resolved["runtime_commit"].lower()
    lock_sha = resolved["uv_lock_sha256"].lower()
    if _SHA40_RE.fullmatch(runtime_commit) is None:
        raise RuntimeError("skill lock runtime_commit must be a full 40 character SHA")
    if _SHA64_RE.fullmatch(lock_sha) is None:
        raise RuntimeError("skill lock uv_lock_sha256 must be a 64 character SHA-256")
    resolved["runtime_commit"] = runtime_commit
    resolved["uv_lock_sha256"] = lock_sha
    return resolved


def _resolve_skill_lock(script_path: Path, provided: str | None) -> Path:
    if provided:
        return Path(provided).expanduser().resolve()
    candidate = (script_path.parent.parent / _SKILL_LOCK_NAME).resolve()
    if candidate.is_file():
        return candidate
    raise RuntimeError(
        "runtime import is unavailable and no materialized skill.lock.json was found beside the skill"
    )


def _ensure_runtime_env_from_skill_lock(skill_lock_path: Path) -> dict[str, str]:
    contract = _load_skill_lock(skill_lock_path)
    os.environ[_RUNTIME_REPOSITORY_ENV] = contract["runtime_repository"]
    os.environ[_RUNTIME_COMMIT_ENV] = contract["runtime_commit"]
    os.environ[_RUNTIME_LOCK_ENV] = contract["uv_lock_sha256"]
    os.environ[_RUNTIME_PACKAGE_PATH_ENV] = contract["package_path"]
    return contract


def _resolve_runtime_python_from_installer(
    script_path: Path,
    *,
    skill_lock_path: Path,
    private_state_root: Path,
) -> str:
    script_directory = script_path.parent
    env = dict(os.environ)
    env[_EMIT_RUNTIME_PYTHON_ENV] = "1"
    work_root = _runtime_work_root(private_state_root)
    if os.name == "nt":
        executable = shutil.which("pwsh") or shutil.which("powershell")
        if executable is None:
            raise RuntimeError("PowerShell is required to install the verified runtime")
        launcher = script_directory / "install-runtime.ps1"
        command = [
            executable,
            "-NoProfile",
            "-File",
            str(launcher),
            "-SkillLockPath",
            str(skill_lock_path),
            "-WorkRoot",
            str(work_root),
        ]
    else:
        executable = shutil.which("bash")
        if executable is None:
            raise RuntimeError("bash is required to install the verified runtime")
        launcher = script_directory / "install-runtime.sh"
        command = [
            executable,
            str(launcher),
            "--skill-lock",
            str(skill_lock_path),
            "--work-root",
            str(work_root),
        ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "verified runtime install failed"
        raise RuntimeError(message)
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError("verified runtime install did not report a Python executable")
    return lines[-1]


def _load_production_runner_factory(
    argv: Sequence[str],
    *,
    script_path: Path,
    private_state_root: Path,
    skill_lock_argument: str | None,
) -> _RunnerFactory:
    source_checkout = _locate_source_checkout(script_path)
    if source_checkout is not None:
        sys.path.insert(0, str(source_checkout / "src"))
        _ensure_runtime_env_from_source_checkout(source_checkout)
    else:
        skill_lock_path = _resolve_skill_lock(script_path, skill_lock_argument)
        _ensure_runtime_env_from_skill_lock(skill_lock_path)

    try:
        from foundry_opt.bootstrap import BootstrapRunner
        from foundry_opt.bootstrap.runner import FileBootstrapRunnerStateStore
    except (ImportError, ModuleNotFoundError) as exc:
        if os.environ.get(_RUNTIME_READY_ENV) == "1":
            raise RuntimeError(
                "verified runtime install completed but BootstrapRunner is still unavailable"
            ) from exc
        skill_lock_path = _resolve_skill_lock(script_path, skill_lock_argument)
        contract = _ensure_runtime_env_from_skill_lock(skill_lock_path)
        runtime_python = _resolve_runtime_python_from_installer(
            script_path,
            skill_lock_path=skill_lock_path,
            private_state_root=private_state_root,
        )
        env = dict(os.environ)
        env[_RUNTIME_READY_ENV] = "1"
        env[_RUNTIME_REPOSITORY_ENV] = contract["runtime_repository"]
        env[_RUNTIME_COMMIT_ENV] = contract["runtime_commit"]
        env[_RUNTIME_LOCK_ENV] = contract["uv_lock_sha256"]
        env[_RUNTIME_PACKAGE_PATH_ENV] = contract["package_path"]
        completed = subprocess.run(
            [runtime_python, str(script_path), *argv],
            check=False,
            env=env,
        )
        raise _ReexecRequested(completed.returncode)

    def _factory(private_root: Path) -> _RunnerProtocol:
        return BootstrapRunner(
            state_store=FileBootstrapRunnerStateStore(
                state_root=_runner_state_root(private_root),
            )
        )

    return _factory


def _coerce_answer(args: argparse.Namespace) -> object:
    if args.choices:
        return list(args.choices)
    if args.response is not None:
        return args.response
    if args.response_json is not None:
        try:
            return json.loads(args.response_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError("answer JSON is invalid") from exc
    if args.boolean_response is not None:
        return args.boolean_response
    raise RuntimeError("answer requires --choice, --response, --response-json, --yes, or --no")


def _as_jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _as_jsonable(value.model_dump(mode="json"))
    if is_dataclass(value):
        return _as_jsonable(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _as_jsonable(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_as_jsonable(child) for child in value]
    if isinstance(value, list):
        return [_as_jsonable(child) for child in value]
    return value


def _turn_payload(turn: object) -> tuple[str, dict[str, Any]]:
    if hasattr(turn, "model_dump"):
        payload = turn.model_dump(mode="json")
    else:
        payload = {
            "owner_markdown": getattr(turn, "owner_markdown"),
            "next_question": getattr(turn, "next_question", None),
            "available_actions": getattr(turn, "available_actions"),
            "operation_id": getattr(turn, "operation_id"),
            "state": getattr(turn, "state"),
            "resource_links": getattr(turn, "resource_links"),
        }
    normalized = _as_jsonable(payload)
    if not isinstance(normalized, dict):
        raise RuntimeError("bootstrap turn payload is invalid")
    owner_markdown = normalized.pop("owner_markdown", None)
    if not isinstance(owner_markdown, str):
        raise RuntimeError("bootstrap turn is missing owner_markdown")
    return owner_markdown, normalized


def _emit_turn(turn: object, *, stdout: TextIO) -> None:
    owner_markdown, machine_payload = _turn_payload(turn)
    stdout.write(f"{_OWNER_MARKDOWN_BEGIN}\n")
    stdout.write(owner_markdown.rstrip("\n"))
    stdout.write(f"\n{_OWNER_MARKDOWN_END}\n")
    stdout.write(f"{_TURN_BEGIN}\n")
    stdout.write(_canonical_json(machine_payload))
    stdout.write(f"\n{_TURN_END}\n")


def main(
    argv: Sequence[str] | None = None,
    *,
    runner_factory: _RunnerFactory | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    parser = _build_parser()
    resolved_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parser.parse_args(resolved_argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    private_state_root = _resolve_private_state_root(args.state_root)
    script_path = Path(__file__).resolve()
    try:
        runner_factory = runner_factory or _load_production_runner_factory(
            resolved_argv,
            script_path=script_path,
            private_state_root=private_state_root,
            skill_lock_argument=args.skill_lock,
        )
        runner = runner_factory(private_state_root)

        if args.command == "start":
            repository_root = _resolve_repository_root(args.repository)
            turn = runner.start(repository_root)
        elif args.command == "answer":
            turn = runner.answer(
                args.operation_id,
                args.question_id,
                _coerce_answer(args),
            )
        elif args.command == "approve":
            turn = runner.approve(
                args.operation_id,
                args.step,
                args.actor,
                args.summary,
            )
        elif args.command == "status":
            turn = runner.status(args.operation_id)
        elif args.command == "rollback":
            turn = runner.rollback(args.operation_id, args.step)
        else:
            raise RuntimeError(f"unsupported command: {args.command}")
        _emit_turn(turn, stdout=stdout)
        return 0
    except _ReexecRequested as exc:
        return exc.return_code
    except Exception as exc:
        stderr.write(f"{exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
