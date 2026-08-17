from __future__ import annotations

from pathlib import Path


def test_powershell_launcher_contains_sha_and_uvlock_checks() -> None:
    text = Path("Q:\\GIT\\worktrees\\foundry-opt-cli\\src\\foundry_opt\\bootstrap\\launch-bootstrap.ps1").read_text(encoding="utf-8")
    assert "ls-remote" in text
    assert "uv.lock" in text
    assert "rev-parse HEAD" in text


def test_bash_launcher_contains_sha_and_uvlock_checks() -> None:
    text = Path("Q:\\GIT\\worktrees\\foundry-opt-cli\\src\\foundry_opt\\bootstrap\\launch-bootstrap.sh").read_text(encoding="utf-8")
    assert "ls-remote" in text
    assert "uv.lock" in text
    assert "rev-parse HEAD" in text
