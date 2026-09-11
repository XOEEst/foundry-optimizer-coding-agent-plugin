from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[2]
TEMPLATES = ROOT / "plugins" / "foundry-bootstrap" / "templates"


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True,
        env={**os.environ, "GIT_CONFIG_COUNT": "0"},
    )
    return result.stdout.strip()


def _init(root: Path) -> None:
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "core.autocrlf", "false")


def _commit_runtime(root: Path, version: str) -> str:
    (root / "uv.lock").write_text(f"version = {version}\n", encoding="utf-8")
    skill = root / "plugins" / "foundry-agent-optimizer" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(f"Runtime instructions {version}\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", f"Runtime {version}")
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def setup_repo(tmp_path: Path) -> tuple[Path, str, str]:
    remote = tmp_path / "runtime-repo"
    _init(remote)
    pinned = _commit_runtime(remote, "1")
    lock_hash = hashlib.sha256((remote / "uv.lock").read_bytes()).hexdigest()
    customer = tmp_path / "customer"
    _init(customer)
    skill = customer / ".github" / "skills" / "foundry-agent-optimizer" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes((TEMPLATES / "optimizer-runtime-skill.md").read_bytes())
    (customer / "registry.yaml").write_text(f"pin: {pinned}\n", encoding="utf-8")
    _git(customer, "add", ".")
    _git(customer, "commit", "-m", "Customer config")
    return remote, pinned, lock_hash


def _run_setup(
    root: Path, *, pinned: str, lock_hash: str, event: str, mode: str, session: str
) -> subprocess.CompletedProcess[str]:
    if os.name == "nt":
        git_bash = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"
        bash = str(git_bash) if git_bash.is_file() else None
    else:
        bash = shutil.which("bash")
    if bash is None:
        pytest.skip("Bash is needed to execute the Linux setup workflow")
    workflow = yaml.safe_load((TEMPLATES / "copilot-setup-steps.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["copilot-setup-steps"]["steps"]
    script = next(step["run"] for step in steps if "runtime_repository=" in step.get("run", ""))
    for token, value in {
        "__FOUNDRY_OPT_REPOSITORY__": "$PWD/runtime-repo",
        "__FOUNDRY_OPT_COMMIT__": pinned,
        "__FOUNDRY_OPT_PACKAGE_PATH__": ".",
        "__FOUNDRY_OPT_UV_LOCK_SHA256__": lock_hash,
        "__FOUNDRY_OPT_OPTIMIZER_SKILL_PATH__": "plugins/foundry-agent-optimizer",
    }.items():
        script = script.replace(token, value)
    script = script.replace('runtime_mode="main"', f'runtime_mode="{mode}"')
    (root / session).mkdir()
    return subprocess.run(
        [bash, "-c", 'uv() { printf "%s\\n" "$*" >> uv-invocations; }\n' + script],
        cwd=root, capture_output=True, text=True,
        env={
            **os.environ, "GIT_CONFIG_COUNT": "0", "GITHUB_EVENT_NAME": event,
            "GITHUB_WORKSPACE": "customer", "RUNNER_TEMP": session,
            "GITHUB_ENV": f"{session}/env", "GITHUB_PATH": f"{session}/path",
        },
    )


def test_main_is_resolved_once_per_session_without_customer_edits(
    tmp_path: Path, setup_repo: tuple[Path, str, str]
) -> None:
    remote, pinned, lock_hash = setup_repo
    second = _commit_runtime(remote, "2")
    customer = tmp_path / "customer"
    original_registry = (customer / "registry.yaml").read_bytes()
    first = _run_setup(tmp_path, pinned=pinned, lock_hash=lock_hash, event="dynamic", mode="main", session="first")
    assert first.returncode == 0, first.stderr
    assert f"FOUNDRY_OPT_RUNTIME_SHA={second}" in (tmp_path / "first" / "env").read_text()
    assert _git(tmp_path / "first" / "foundry-opt-shared", "rev-parse", "HEAD") == second
    third = _commit_runtime(remote, "3")
    next_session = _run_setup(tmp_path, pinned=pinned, lock_hash=lock_hash, event="dynamic", mode="main", session="next")
    assert next_session.returncode == 0, next_session.stderr
    assert f"FOUNDRY_OPT_RUNTIME_SHA={third}" in (tmp_path / "next" / "env").read_text()
    assert _git(tmp_path / "first" / "foundry-opt-shared", "rev-parse", "HEAD") == second
    assert (customer / "registry.yaml").read_bytes() == original_registry
    assert _git(customer, "status", "--porcelain") == ""
    assert all("sync --frozen" in call for call in (tmp_path / "uv-invocations").read_text().splitlines())


@pytest.mark.parametrize(("event", "mode"), [("push", "main"), ("workflow_dispatch", "main"), ("dynamic", "pinned")])
def test_non_dynamic_or_pinned_setup_uses_recorded_commit(
    tmp_path: Path, setup_repo: tuple[Path, str, str], event: str, mode: str
) -> None:
    remote, pinned, lock_hash = setup_repo
    if mode == "pinned":
        skill = tmp_path / "customer" / ".github" / "skills" / "foundry-agent-optimizer" / "SKILL.md"
        skill.write_bytes((remote / "plugins" / "foundry-agent-optimizer" / "SKILL.md").read_bytes())
        _git(tmp_path / "customer", "add", ".")
        _git(tmp_path / "customer", "commit", "-m", "Pinned skill")
    _commit_runtime(remote, "2")
    result = _run_setup(tmp_path, pinned=pinned, lock_hash=lock_hash, event=event, mode=mode, session="pinned")
    assert result.returncode == 0, result.stderr
    assert f"FOUNDRY_OPT_RUNTIME_SHA={pinned}" in (tmp_path / "pinned" / "env").read_text()
    assert _git(tmp_path / "customer", "status", "--porcelain") == ""


def test_missing_main_fails_without_falling_back_to_recorded_pin(
    tmp_path: Path, setup_repo: tuple[Path, str, str]
) -> None:
    remote, pinned, lock_hash = setup_repo
    _git(remote, "branch", "-m", "other")
    result = _run_setup(tmp_path, pinned=pinned, lock_hash=lock_hash, event="dynamic", mode="main", session="failed")
    assert result.returncode != 0
    assert not (tmp_path / "failed" / "env").exists()
    assert not (tmp_path / "uv-invocations").exists()


def test_main_without_optimizer_skill_fails_before_install(
    tmp_path: Path, setup_repo: tuple[Path, str, str]
) -> None:
    remote, pinned, lock_hash = setup_repo
    _git(remote, "rm", "plugins/foundry-agent-optimizer/SKILL.md")
    _git(remote, "commit", "-m", "Incompatible runtime")
    result = _run_setup(tmp_path, pinned=pinned, lock_hash=lock_hash, event="dynamic", mode="main", session="failed")
    assert result.returncode != 0
    assert not (tmp_path / "uv-invocations").exists()


def test_pinned_setup_still_requires_approved_lock_digest(
    tmp_path: Path, setup_repo: tuple[Path, str, str]
) -> None:
    _, pinned, _ = setup_repo
    result = _run_setup(tmp_path, pinned=pinned, lock_hash="0" * 64, event="push", mode="main", session="failed")
    assert result.returncode != 0
    assert not (tmp_path / "uv-invocations").exists()
