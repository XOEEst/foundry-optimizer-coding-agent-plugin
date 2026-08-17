from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

from foundry_opt.packaging import build_deterministic_zip


DEFAULT_MAX_GIT_ARCHIVE_BYTES = 128 * 1024 * 1024
_COMMIT_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


class SourcePackagingError(RuntimeError):
    """An immutable Git source tree could not be packaged safely."""


@dataclass(frozen=True, slots=True)
class PackagedSource:
    commit: str
    source_root: str
    archive_bytes: bytes
    tree_sha256: str
    zip_sha256: str

    def __post_init__(self) -> None:
        if _COMMIT_PATTERN.fullmatch(self.commit) is None:
            raise ValueError("commit must be a lowercase Git object ID")
        if not self.source_root:
            raise ValueError("source_root must not be empty")
        if hashlib.sha256(self.archive_bytes).hexdigest() != self.zip_sha256:
            raise ValueError("archive_bytes do not match zip_sha256")


def package_git_source(
    repository: Path,
    *,
    commit: str,
    source_root: str,
    work_root: Path | None = None,
    check_deadline: Callable[[], None] = lambda: None,
    max_git_archive_bytes: int = DEFAULT_MAX_GIT_ARCHIVE_BYTES,
) -> PackagedSource:
    repository_root = _repository_root(repository)
    normalized_commit = _validate_commit(commit)
    normalized_source_root = _validate_source_root(source_root)
    if max_git_archive_bytes <= 0:
        raise ValueError("max_git_archive_bytes must be positive")

    temporary_parent = _temporary_parent(repository_root, work_root)
    with tempfile.TemporaryDirectory(
        prefix="foundry-opt-source-",
        dir=str(temporary_parent),
    ) as temporary:
        temporary_root = Path(temporary)
        extraction_root = temporary_root / "tree"
        zip_path = temporary_root / "source.zip"
        extraction_root.mkdir()
        source_path = _extract_source_tree(
            repository_root,
            commit=normalized_commit,
            source_root=normalized_source_root,
            destination=extraction_root,
            max_archive_bytes=max_git_archive_bytes,
        )
        built = build_deterministic_zip(
            source_path,
            zip_path,
            includes=("*", "**/*"),
            excludes=(".git", ".git/**"),
            check_deadline=check_deadline,
        )
        try:
            archive_bytes = zip_path.read_bytes()
        except OSError as error:
            raise SourcePackagingError("source ZIP could not be read") from error
        if hashlib.sha256(archive_bytes).hexdigest() != built.zip_sha256:
            raise SourcePackagingError("source ZIP changed after deterministic packaging")
        return PackagedSource(
            commit=normalized_commit,
            source_root=normalized_source_root,
            archive_bytes=archive_bytes,
            tree_sha256=built.tree_sha256,
            zip_sha256=built.zip_sha256,
        )


def _extract_source_tree(
    repository: Path,
    *,
    commit: str,
    source_root: str,
    destination: Path,
    max_archive_bytes: int,
) -> Path:
    arguments = ["archive", "--format=tar", commit]
    if source_root != ".":
        arguments.extend(["--", source_root])
    archive_bytes = _git_bytes(repository, *arguments)
    if len(archive_bytes) > max_archive_bytes:
        raise SourcePackagingError("Git source archive exceeded the configured size limit")
    try:
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:*") as archive:
            for member in archive.getmembers():
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts:
                    raise SourcePackagingError("Git source archive returned an unsafe path")
                target = destination.joinpath(*path.parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise SourcePackagingError(
                        "Git source archive returned an unsupported member type"
                    )
                file_object = archive.extractfile(member)
                if file_object is None:
                    raise SourcePackagingError(
                        "Git source archive member could not be read"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as stream:
                    shutil.copyfileobj(file_object, stream)
    except tarfile.TarError as error:
        raise SourcePackagingError(
            "git archive did not produce a valid tar stream"
        ) from error
    source_path = (
        destination
        if source_root == "."
        else destination.joinpath(*PurePosixPath(source_root).parts)
    )
    if not source_path.is_dir():
        raise SourcePackagingError(
            "source_root was missing from the requested Git commit"
        )
    return source_path


def _repository_root(repository: Path) -> Path:
    try:
        resolved = Path(repository).resolve(strict=True)
    except OSError as error:
        raise SourcePackagingError("repository could not be resolved") from error
    discovered = _git_text(resolved, "rev-parse", "--show-toplevel")
    try:
        root = Path(discovered).resolve(strict=True)
    except OSError as error:
        raise SourcePackagingError("Git worktree root could not be resolved") from error
    if root != resolved:
        raise SourcePackagingError("repository must be the Git worktree root")
    return root


def _temporary_parent(repository: Path, work_root: Path | None) -> Path:
    if work_root is None:
        parent = Path(tempfile.gettempdir()).resolve(strict=True)
    else:
        try:
            parent = Path(work_root).resolve(strict=False)
            parent.mkdir(parents=True, exist_ok=True)
            parent = parent.resolve(strict=True)
        except OSError as error:
            raise SourcePackagingError("source packaging work_root is unavailable") from error
    if parent == repository or parent.is_relative_to(repository):
        raise SourcePackagingError(
            "source packaging work_root must live outside the repository"
        )
    return parent


def _validate_commit(value: str) -> str:
    if _COMMIT_PATTERN.fullmatch(value) is None:
        raise SourcePackagingError("commit must be a lowercase Git object ID")
    return value


def _validate_source_root(value: str) -> str:
    if value == ".":
        return value
    if not value or "\\" in value or value.startswith("/") or value.endswith("/"):
        raise SourcePackagingError("source_root must be a repository-relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise SourcePackagingError("source_root contains an unsafe path segment")
    return path.as_posix()


def _git_text(repository: Path, *arguments: str) -> str:
    completed = _run_git(repository, *arguments, text=True)
    return completed.stdout.rstrip("\n")


def _git_bytes(repository: Path, *arguments: str) -> bytes:
    completed = _run_git(repository, *arguments, text=False)
    return completed.stdout


def _run_git(
    repository: Path,
    *arguments: str,
    text: bool,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=False,
            capture_output=True,
            text=text,
            stdin=subprocess.DEVNULL,
            timeout=30,
            env=_git_environment(),
        )
    except subprocess.TimeoutExpired as error:
        raise SourcePackagingError("git command timed out") from error
    except OSError as error:
        raise SourcePackagingError("git could not be executed") from error
    if completed.returncode != 0:
        if text:
            detail = completed.stderr.strip() or completed.stdout.strip()
        else:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise SourcePackagingError(
            f"git command failed: {detail or 'unknown failure'}"
        )
    return completed


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_CONFIG",
        "PYTHONHOME",
        "PYTHONPATH",
    ):
        environment.pop(key, None)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    return environment
