from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_powershell_launcher_contains_sha_and_uvlock_checks() -> None:
    text = (
        REPOSITORY_ROOT
        / "src"
        / "foundry_opt"
        / "bootstrap"
        / "launch-bootstrap.ps1"
    ).read_text(encoding="utf-8")
    assert text.lstrip().startswith("param(")
    assert "ls-remote" in text
    assert "uv.lock" in text
    assert "rev-parse HEAD" in text
    assert "uv sync --frozen" in text
    assert '.venv\\Scripts\\foundry-opt.exe' in text
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
    assert 'exec "$extract_root/.venv/bin/foundry-opt"' in text


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
    drive = path.drive.rstrip(":").lower()
    wsl_path = f"/mnt/{drive}/{path.as_posix().split(':/', 1)[1]}"
    completed = subprocess.run(
        [executable, "-lc", f"bash -n '{wsl_path}'"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
