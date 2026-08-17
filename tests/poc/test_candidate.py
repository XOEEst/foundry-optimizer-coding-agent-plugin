from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

import pytest

from foundry_opt.poc.candidate import (
    CandidatePolicyError,
    CandidateWorkspace,
)


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _create_repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.com")
    (repository / "src").mkdir()
    (repository / "tests").mkdir()
    (repository / "protected").mkdir()
    (repository / "docs").mkdir()
    (repository / "src/app.py").write_text("VALUE = 'base'\n", encoding="utf-8")
    (repository / "tests/test_app.py").write_text("def test_base():\n    assert True\n", encoding="utf-8")
    (repository / "protected/blocked.txt").write_text("protected\n", encoding="utf-8")
    (repository / "docs/notes.txt").write_text("notes\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "base")
    return repository, _git(repository, "rev-parse", "HEAD")


def _workspace(repository: Path, trusted_root: Path, base_commit: str) -> CandidateWorkspace:
    return CandidateWorkspace(
        repository,
        trusted_root,
        base_commit,
        editable_patterns=("src/**", "tests/**"),
        protected_patterns=("protected/**", ".git/**"),
        source_root="src",
    )


def test_prepare_finalize_and_apply_verified_patch(tmp_path: Path) -> None:
    repository, base_commit = _create_repository(tmp_path)
    trusted_root = tmp_path / "trusted"
    workspace = _workspace(repository, trusted_root, base_commit)

    prepared = workspace.prepare(
        "candidate-one",
        model="gpt-5-mini",
        hypothesis="improve the greeting logic",
    )
    (prepared.workspace_path / "src/app.py").write_text(
        "VALUE = 'candidate-one'\n",
        encoding="utf-8",
    )
    (prepared.workspace_path / "tests/test_app.py").write_text(
        "def test_candidate_one():\n    assert True\n",
        encoding="utf-8",
    )

    finalized = workspace.finalize("candidate-one")

    assert finalized.changed_paths == ("src/app.py", "tests/test_app.py")
    assert finalized.incremental_changed_paths == ("src/app.py", "tests/test_app.py")
    assert finalized.model == "gpt-5-mini"
    assert finalized.hypothesis == "improve the greeting logic"
    with zipfile.ZipFile(finalized.source_zip_path) as archive:
        assert archive.namelist() == ["app.py"]
        assert archive.read("app.py").decode("utf-8").replace("\r\n", "\n") == (
            "VALUE = 'candidate-one'\n"
        )

    destination = tmp_path / "destination"
    _git(repository, "worktree", "add", "--detach", str(destination), base_commit)

    applied = workspace.apply_winner(finalized, destination)

    assert applied.patch_sha256 == finalized.hashes.patch_sha256
    assert (destination / "src/app.py").read_text(encoding="utf-8") == "VALUE = 'candidate-one'\n"
    assert (destination / "tests/test_app.py").read_text(encoding="utf-8") == (
        "def test_candidate_one():\n    assert True\n"
    )


def test_finalize_rejects_test_only_candidate(tmp_path: Path) -> None:
    repository, base_commit = _create_repository(tmp_path)
    workspace = _workspace(repository, tmp_path / "trusted", base_commit)

    prepared = workspace.prepare(
        "candidate-one",
        model="gpt-5-mini",
        hypothesis="adjust tests only",
    )
    (prepared.workspace_path / "tests/test_app.py").write_text(
        "def test_only():\n    assert True\n",
        encoding="utf-8",
    )

    with pytest.raises(CandidatePolicyError, match="source_root"):
        workspace.finalize("candidate-one")


@pytest.mark.parametrize(
    ("relative_path", "error_pattern"),
    (
        ("protected/blocked.txt", "protected"),
        ("docs/notes.txt", "editable scope"),
    ),
)
def test_finalize_rejects_protected_and_out_of_scope_paths(
    tmp_path: Path,
    relative_path: str,
    error_pattern: str,
) -> None:
    repository, base_commit = _create_repository(tmp_path)
    workspace = _workspace(repository, tmp_path / "trusted", base_commit)

    prepared = workspace.prepare(
        "candidate-one",
        model="gpt-5-mini",
        hypothesis="touch the wrong file",
    )
    (prepared.workspace_path / relative_path).write_text("changed\n", encoding="utf-8")
    (prepared.workspace_path / "src/app.py").write_text(
        "VALUE = 'changed'\n",
        encoding="utf-8",
    )

    with pytest.raises(CandidatePolicyError, match=error_pattern):
        workspace.finalize("candidate-one")


def test_child_candidate_uses_parent_commit_and_cleanup_is_exact(tmp_path: Path) -> None:
    repository, base_commit = _create_repository(tmp_path)
    trusted_root = tmp_path / "trusted"
    workspace = _workspace(repository, trusted_root, base_commit)

    parent = workspace.prepare(
        "candidate-one",
        model="gpt-5-mini",
        hypothesis="first change",
    )
    (parent.workspace_path / "src/app.py").write_text(
        "VALUE = 'parent'\n",
        encoding="utf-8",
    )
    parent_finalized = workspace.finalize("candidate-one")
    assert parent_finalized.candidate_commit

    child = workspace.prepare(
        "candidate-two",
        model="gpt-5-mini",
        hypothesis="child follow-up",
        parent_id="candidate-one",
    )
    assert (child.workspace_path / "src/app.py").read_text(encoding="utf-8") == "VALUE = 'parent'\n"
    (child.workspace_path / "tests/test_app.py").write_text(
        "def test_child_only():\n    assert True\n",
        encoding="utf-8",
    )

    with pytest.raises(CandidatePolicyError, match="source_root"):
        workspace.finalize("candidate-two")

    workspace.cleanup("candidate-one", remove_artifacts=True)

    assert not parent.workspace_path.exists()
    assert not (trusted_root / "artifacts" / "candidate-one").exists()
    assert child.workspace_path.exists()
