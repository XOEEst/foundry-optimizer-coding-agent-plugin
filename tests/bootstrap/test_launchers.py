from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import subprocess

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _runtime_repository(tmp_path: Path) -> tuple[Path, str, str]:
    repository = tmp_path / "runtime"
    repository.mkdir()
    lock = repository / "uv.lock"
    lock.write_text("version = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repository), "init", "--quiet"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Launcher Test"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "config",
            "user.email",
            "launcher@example.invalid",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "add", "uv.lock"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "commit", "--quiet", "-m", "runtime"],
        check=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    lock_hash = hashlib.sha256(lock.read_bytes()).hexdigest()
    return repository, sha, lock_hash


def test_powershell_launcher_contains_sha_and_uvlock_checks() -> None:
    text = (
        REPOSITORY_ROOT
        / "src"
        / "foundry_opt"
        / "bootstrap"
        / "launch-bootstrap.ps1"
    ).read_text(encoding="utf-8")
    assert text.lstrip().startswith("[CmdletBinding(")
    assert "\nparam(" in text
    assert "ls-remote" in text
    assert "uv.lock" in text
    assert "rev-parse HEAD" in text
    assert "uv sync --frozen" in text
    assert "uv run --no-sync" in text
    assert "FOUNDRY_OPT_RUNTIME_COMMIT" in text


def test_bash_launcher_contains_sha_and_uvlock_checks() -> None:
    text = (
        REPOSITORY_ROOT
        / "src"
        / "foundry_opt"
        / "bootstrap"
        / "launch-bootstrap.sh"
    ).read_text(encoding="utf-8")
    assert "ls-remote" in text
    assert "uv.lock" in text
    assert "rev-parse HEAD" in text
    assert "git -C \"$extract_root\" init" in text
    assert "uv sync --frozen" in text
    assert "exec uv run --no-sync" in text


def test_powershell_launcher_parses() -> None:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell is unavailable")
    path = (
        REPOSITORY_ROOT
        / "src"
        / "foundry_opt"
        / "bootstrap"
        / "launch-bootstrap.ps1"
    )
    completed = subprocess.run(
        [
            executable,
            "-NoProfile",
            "-Command",
            f"[scriptblock]::Create((Get-Content -Raw '{path}')) | Out-Null",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_bash_launcher_parses() -> None:
    executable = shutil.which("bash")
    if executable is None:
        pytest.skip("Bash is unavailable")
    path = (
        REPOSITORY_ROOT
        / "src"
        / "foundry_opt"
        / "bootstrap"
        / "launch-bootstrap.sh"
    )
    completed = subprocess.run(
        [executable, "-n"],
        check=False,
        capture_output=True,
        input=path.read_bytes(),
    )
    assert completed.returncode == 0, completed.stderr.decode(
        "utf-8",
        errors="replace",
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher behavior")
def test_powershell_launcher_executes_verified_runtime(tmp_path: Path) -> None:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell is unavailable")
    repository, sha, lock_hash = _runtime_repository(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "uv.log"
    (fake_bin / "uv.cmd").write_text(
        "\n".join(
            (
                "@echo off",
                "echo args=%*>>\"%UV_LOG%\"",
                "echo commit=%FOUNDRY_OPT_RUNTIME_COMMIT%>>\"%UV_LOG%\"",
                "echo lock=%FOUNDRY_OPT_RUNTIME_LOCK_SHA256%>>\"%UV_LOG%\"",
                "exit /b 0",
            )
        )
        + "\n",
        encoding="ascii",
    )
    env = {
        **os.environ,
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "UV_LOG": str(log),
    }
    script = (
        REPOSITORY_ROOT
        / "src"
        / "foundry_opt"
        / "bootstrap"
        / "launch-bootstrap.ps1"
    )

    completed = subprocess.run(
        [
            executable,
            "-NoProfile",
            "-File",
            str(script),
            "-Repository",
            str(repository),
            "-ExpectedLockSha256",
            lock_hash,
            "-Pin",
            sha,
            "-WorkRoot",
            str(tmp_path / "work"),
            "bootstrap",
            "status",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 0, completed.stderr
    output = log.read_text(encoding="utf-8")
    assert "args=sync --frozen --project" in output
    assert "args=run --no-sync --project" in output
    assert "foundry-opt bootstrap status" in output
    assert f"commit={sha}" in output
    assert f"lock={lock_hash}" in output


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher behavior")
def test_bash_launcher_executes_verified_runtime(tmp_path: Path) -> None:
    executable = shutil.which("bash")
    if executable is None:
        pytest.skip("Bash is unavailable")
    repository, sha, lock_hash = _runtime_repository(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "uv.log"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "\n".join(
            (
                "#!/usr/bin/env sh",
                'printf "args=%s\\n" "$*" >> "$UV_LOG"',
                'printf "commit=%s\\n" "$FOUNDRY_OPT_RUNTIME_COMMIT" >> "$UV_LOG"',
                'printf "lock=%s\\n" "$FOUNDRY_OPT_RUNTIME_LOCK_SHA256" >> "$UV_LOG"',
            )
        )
        + "\n",
        encoding="ascii",
    )
    fake_uv.chmod(0o755)
    env = {
        **os.environ,
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "UV_LOG": str(log),
    }
    script = (
        REPOSITORY_ROOT
        / "src"
        / "foundry_opt"
        / "bootstrap"
        / "launch-bootstrap.sh"
    )

    completed = subprocess.run(
        [
            executable,
            str(script),
            str(repository),
            lock_hash,
            sha,
            "main",
            str(tmp_path / "work"),
            "bootstrap",
            "status",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert completed.returncode == 0, completed.stderr
    output = log.read_text(encoding="utf-8")
    assert "args=sync --frozen --project" in output
    assert "args=run --no-sync --project" in output
    assert "foundry-opt bootstrap status" in output
    assert f"commit={sha}" in output
    assert f"lock={lock_hash}" in output
