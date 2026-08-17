from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path

import pytest

from foundry_opt.poc.source import SourcePackagingError, package_git_source


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.invalid")
    (repository / "agent").mkdir()
    (repository / "tests").mkdir()
    (repository / "agent" / "main.py").write_text(
        "VALUE = 'committed'\n",
        encoding="utf-8",
    )
    (repository / "tests" / "test_main.py").write_text(
        "def test_main():\n    assert True\n",
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "base")
    return repository, _git(repository, "rev-parse", "HEAD")


def test_package_git_source_is_deterministic_and_uses_exact_commit(
    tmp_path: Path,
) -> None:
    repository, commit = _repository(tmp_path)
    work_root = tmp_path / "work"
    first = package_git_source(
        repository,
        commit=commit,
        source_root="agent",
        work_root=work_root,
    )
    (repository / "agent" / "main.py").write_text(
        "VALUE = 'working-tree'\n",
        encoding="utf-8",
    )
    second = package_git_source(
        repository,
        commit=commit,
        source_root="agent",
        work_root=work_root,
    )

    assert first.archive_bytes == second.archive_bytes
    assert first.tree_sha256 == second.tree_sha256
    assert first.zip_sha256 == second.zip_sha256
    archive_path = tmp_path / "source.zip"
    archive_path.write_bytes(first.archive_bytes)
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.namelist() == ["main.py"]
        assert archive.read("main.py") == b"VALUE = 'committed'\n"


def test_package_git_source_rejects_work_root_inside_repository(
    tmp_path: Path,
) -> None:
    repository, commit = _repository(tmp_path)

    with pytest.raises(
        SourcePackagingError,
        match="work_root must live outside",
    ):
        package_git_source(
            repository,
            commit=commit,
            source_root="agent",
            work_root=repository / ".tmp",
        )


def test_package_git_source_rejects_missing_source_root(tmp_path: Path) -> None:
    repository, commit = _repository(tmp_path)

    with pytest.raises(SourcePackagingError, match="git command failed"):
        package_git_source(
            repository,
            commit=commit,
            source_root="missing",
            work_root=tmp_path / "work",
        )
