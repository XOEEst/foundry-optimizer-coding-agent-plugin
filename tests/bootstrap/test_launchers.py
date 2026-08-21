from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CANONICAL_LAUNCHER_ROOT = (
    REPOSITORY_ROOT / "plugins" / "foundry-bootstrap" / "scripts"
)
LEGACY_LAUNCHER_ROOT = REPOSITORY_ROOT / "src" / "foundry_opt" / "bootstrap"


def _runtime_repository(
    tmp_path: Path,
    *,
    package_path: str = ".",
) -> tuple[Path, str, str]:
    repository = tmp_path / "runtime"
    repository.mkdir()
    package_root = repository if package_path == "." else repository / package_path
    package_root.mkdir(parents=True, exist_ok=True)
    lock = package_root / "uv.lock"
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
        ["git", "-C", str(repository), "add", "."],
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


def _skill_lock_path(
    tmp_path: Path,
    *,
    repository: Path,
    sha: str,
    lock_hash: str,
    package_path: str,
) -> Path:
    path = tmp_path / "skill.lock.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtime_repository": str(repository),
                "runtime_commit": sha,
                "uv_lock_sha256": lock_hash,
                "package_path": package_path,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_powershell_source_wrapper_delegates_to_canonical_script() -> None:
    text = (LEGACY_LAUNCHER_ROOT / "launch-bootstrap.ps1").read_text(encoding="utf-8")
    assert "install-runtime.ps1" in text
    assert "source checkout" in text.lower()


def test_powershell_canonical_launcher_contains_exact_pin_and_lock_checks() -> None:
    text = (CANONICAL_LAUNCHER_ROOT / "install-runtime.ps1").read_text(encoding="utf-8")
    assert text.lstrip().startswith("[CmdletBinding(")
    assert "\nparam(" in text
    assert "runtime_commit" in text
    assert "uv_lock_sha256" in text
    assert "package_path" in text
    assert "Invoke-CheckedCommand uv -ArgumentList @(\"sync\", \"--frozen\"" in text
    assert "FOUNDRY_BOOTSTRAP_EMIT_RUNTIME_PYTHON" in text
    assert 'python -c "import sys; print(sys.executable)"' in text
    assert "uv run --no-sync" in text
    assert "floating refs like '$Ref' are not allowed for privileged use" in text


def test_bash_source_wrapper_delegates_to_canonical_script() -> None:
    text = (LEGACY_LAUNCHER_ROOT / "launch-bootstrap.sh").read_text(encoding="utf-8")
    assert "install-runtime.sh" in text
    assert "source checkout" in text.lower()


def test_bash_canonical_launcher_contains_exact_pin_and_lock_checks() -> None:
    text = (CANONICAL_LAUNCHER_ROOT / "install-runtime.sh").read_text(encoding="utf-8")
    assert "runtime_commit" in text
    assert "uv_lock_sha256" in text
    assert "package_path" in text
    assert 'git -C "$extract_root" init' in text
    assert "command -v python3 || command -v python" in text
    assert "uv sync --frozen" in text
    assert "FOUNDRY_BOOTSTRAP_EMIT_RUNTIME_PYTHON" in text
    assert "python -c 'import sys; print(sys.executable)'" in text
    assert "exec uv run --no-sync" in text
    assert "floating refs like '$ref' are not allowed for privileged use" in text


def test_powershell_launchers_parse() -> None:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell is unavailable")
    for path in (
        LEGACY_LAUNCHER_ROOT / "launch-bootstrap.ps1",
        CANONICAL_LAUNCHER_ROOT / "install-runtime.ps1",
    ):
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


def test_bash_launchers_parse() -> None:
    executable = shutil.which("bash")
    if executable is None:
        pytest.skip("Bash is unavailable")
    for path in (
        LEGACY_LAUNCHER_ROOT / "launch-bootstrap.sh",
        CANONICAL_LAUNCHER_ROOT / "install-runtime.sh",
    ):
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
def test_powershell_legacy_wrapper_executes_verified_runtime(tmp_path: Path) -> None:
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
    script = LEGACY_LAUNCHER_ROOT / "launch-bootstrap.ps1"

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
def test_bash_legacy_wrapper_executes_verified_runtime(tmp_path: Path) -> None:
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
    script = LEGACY_LAUNCHER_ROOT / "launch-bootstrap.sh"

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


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher behavior")
def test_powershell_canonical_launcher_reads_skill_lock_contract(tmp_path: Path) -> None:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell is unavailable")
    repository, sha, lock_hash = _runtime_repository(tmp_path, package_path="pkg")
    skill_lock = _skill_lock_path(
        tmp_path,
        repository=repository,
        sha=sha,
        lock_hash=lock_hash,
        package_path="pkg",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "uv.log"
    (fake_bin / "uv.cmd").write_text(
        "\n".join(
            (
                "@echo off",
                "echo args=%*>>\"%UV_LOG%\"",
                "echo package=%FOUNDRY_OPT_RUNTIME_PACKAGE_PATH%>>\"%UV_LOG%\"",
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

    completed = subprocess.run(
        [
            executable,
            "-NoProfile",
            "-File",
            str(CANONICAL_LAUNCHER_ROOT / "install-runtime.ps1"),
            "-SkillLockPath",
            str(skill_lock),
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
    assert "package=pkg" in output


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher behavior")
def test_bash_canonical_launcher_reads_skill_lock_contract(tmp_path: Path) -> None:
    executable = shutil.which("bash")
    if executable is None:
        pytest.skip("Bash is unavailable")
    repository, sha, lock_hash = _runtime_repository(tmp_path, package_path="pkg")
    skill_lock = _skill_lock_path(
        tmp_path,
        repository=repository,
        sha=sha,
        lock_hash=lock_hash,
        package_path="pkg",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log = tmp_path / "uv.log"
    fake_uv = fake_bin / "uv"
    fake_uv.write_text(
        "\n".join(
            (
                "#!/usr/bin/env sh",
                'printf "args=%s\\n" "$*" >> "$UV_LOG"',
                'printf "package=%s\\n" "$FOUNDRY_OPT_RUNTIME_PACKAGE_PATH" >> "$UV_LOG"',
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

    completed = subprocess.run(
        [
            executable,
            str(CANONICAL_LAUNCHER_ROOT / "install-runtime.sh"),
            "--skill-lock",
            str(skill_lock),
            "--work-root",
            str(tmp_path / "work"),
            "--",
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
    assert "package=pkg" in output


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher behavior")
def test_powershell_canonical_launcher_refuses_floating_runtime(tmp_path: Path) -> None:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell is unavailable")
    repository, _, lock_hash = _runtime_repository(tmp_path)

    completed = subprocess.run(
        [
            executable,
            "-NoProfile",
            "-File",
            str(CANONICAL_LAUNCHER_ROOT / "install-runtime.ps1"),
            "-Repository",
            str(repository),
            "-ExpectedLockSha256",
            lock_hash,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "floating refs" in completed.stderr


@pytest.mark.skipif(os.name == "nt", reason="POSIX launcher behavior")
def test_bash_canonical_launcher_refuses_floating_runtime(tmp_path: Path) -> None:
    executable = shutil.which("bash")
    if executable is None:
        pytest.skip("Bash is unavailable")
    repository, _, lock_hash = _runtime_repository(tmp_path)

    completed = subprocess.run(
        [
            executable,
            str(CANONICAL_LAUNCHER_ROOT / "install-runtime.sh"),
            str(repository),
            lock_hash,
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "floating refs" in completed.stderr
