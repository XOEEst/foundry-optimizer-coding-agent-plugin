from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from foundry_opt.packaging.deterministic_zip import (
    DeterministicZipBuilder,
    TreeFingerprint,
)


_IDENTIFIER_PATTERN: Final = r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
_COMMIT_PATTERN: Final = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
_SHA256_PATTERN: Final = r"^[0-9a-f]{64}$"
_ASCII_TEXT_PATTERN: Final = re.compile(r"^[\x20-\x7e]+$")
_CONTROL_PATTERN: Final = re.compile(r"[\x00-\x1f\x7f]")
_GLOB_CHARS: Final = frozenset("*?")
_PATCH_NAME: Final = "candidate.patch"
_SOURCE_ZIP_NAME: Final = "source.zip"
_MANIFEST_NAME: Final = "candidate.json"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CandidateError(RuntimeError):
    """Base error for the optimize-job candidate workspace."""


class CandidatePolicyError(CandidateError):
    """A candidate violates the bounded edit policy."""


class CandidateNotFoundError(CandidateError):
    """A prepared or finalized candidate could not be located."""


class CandidateVerificationError(CandidateError):
    """A trusted candidate artifact or projection could not be verified."""


@dataclass(frozen=True, slots=True)
class _PathMatcher:
    source: str
    expression: re.Pattern[str]

    def matches(self, path: str) -> bool:
        return self.expression.fullmatch(path) is not None


class CandidateHashes(_FrozenModel):
    patch_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_tree_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_zip_sha256: str = Field(pattern=_SHA256_PATTERN)


class PreparedCandidate(_FrozenModel):
    candidate_id: str = Field(pattern=_IDENTIFIER_PATTERN)
    parent_id: str | None = Field(default=None, pattern=_IDENTIFIER_PATTERN)
    model: str = Field(min_length=1, max_length=128)
    hypothesis: str = Field(min_length=1, max_length=512)
    base_commit: str = Field(pattern=_COMMIT_PATTERN)
    origin_commit: str = Field(pattern=_COMMIT_PATTERN)
    workspace_path: Path

    @field_validator("model", "hypothesis")
    @classmethod
    def validate_text(cls, value: str) -> str:
        normalized = _normalize_text(value)
        if not _ASCII_TEXT_PATTERN.fullmatch(normalized):
            raise ValueError("text must be printable ASCII")
        return normalized


class FinalizedCandidate(_FrozenModel):
    candidate_id: str = Field(pattern=_IDENTIFIER_PATTERN)
    parent_id: str | None = Field(default=None, pattern=_IDENTIFIER_PATTERN)
    model: str = Field(min_length=1, max_length=128)
    hypothesis: str = Field(min_length=1, max_length=512)
    base_commit: str = Field(pattern=_COMMIT_PATTERN)
    origin_commit: str = Field(pattern=_COMMIT_PATTERN)
    candidate_commit: str = Field(pattern=_COMMIT_PATTERN)
    source_root: str = Field(min_length=1, max_length=256)
    workspace_path: Path
    changed_paths: tuple[str, ...]
    incremental_changed_paths: tuple[str, ...]
    hashes: CandidateHashes
    patch_path: Path
    source_zip_path: Path

    @field_validator("model", "hypothesis")
    @classmethod
    def validate_text(cls, value: str) -> str:
        normalized = _normalize_text(value)
        if not _ASCII_TEXT_PATTERN.fullmatch(normalized):
            raise ValueError("text must be printable ASCII")
        return normalized

    @field_validator("changed_paths", "incremental_changed_paths")
    @classmethod
    def validate_paths(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        canonical = tuple(_normalize_repository_path(path) for path in value)
        if len(canonical) != len(set(canonical)):
            raise ValueError("changed paths must be unique")
        return canonical


class AppliedPatch(_FrozenModel):
    candidate_id: str = Field(pattern=_IDENTIFIER_PATTERN)
    destination_checkout: Path
    base_commit: str = Field(pattern=_COMMIT_PATTERN)
    patch_sha256: str = Field(pattern=_SHA256_PATTERN)


class _CandidateManifest(_FrozenModel):
    prepared: PreparedCandidate
    finalized: FinalizedCandidate | None = None


class CandidateWorkspace:
    """Create, package, project, and cleanup trusted optimize-job candidates."""

    def __init__(
        self,
        repository: Path,
        trusted_root: Path,
        base_commit: str,
        *,
        editable_patterns: tuple[str, ...] | list[str],
        protected_patterns: tuple[str, ...] | list[str] = (),
        source_root: str = ".",
        git_executable: str = "git",
    ) -> None:
        self._repository = Path(repository)
        if not self._repository.is_dir():
            raise CandidateVerificationError(
                "repository must exist before creating candidates"
            )
        self._git_executable = git_executable
        self._base_commit = _validate_commit(base_commit)
        self._source_root = _normalize_source_root(source_root)
        self._source_root_text = (
            "." if self._source_root is None else self._source_root.as_posix()
        )
        editable = tuple(editable_patterns)
        if not editable:
            raise CandidateVerificationError(
                "at least one editable glob pattern is required"
            )
        self._editable_patterns = tuple(_compile_glob(pattern) for pattern in editable)
        self._protected_patterns = tuple(
            _compile_glob(pattern) for pattern in protected_patterns
        )
        self._trusted_root = Path(trusted_root)
        self._verify_trusted_root()
        self._worktrees_root = self._trusted_root / "worktrees"
        self._artifacts_root = self._trusted_root / "artifacts"
        self._worktrees_root.mkdir(parents=True, exist_ok=True)
        self._artifacts_root.mkdir(parents=True, exist_ok=True)
        self._verify_commit_exists(self._base_commit)
        self._verify_source_root_exists()
        self._zip_builder = DeterministicZipBuilder(
            includes=("*", "**/*"),
            excludes=(".git", ".git/**"),
        )

    @property
    def repository(self) -> Path:
        return self._repository

    @property
    def trusted_root(self) -> Path:
        return self._trusted_root

    @property
    def base_commit(self) -> str:
        return self._base_commit

    @property
    def source_root(self) -> str:
        return self._source_root_text

    def prepare(
        self,
        candidate_id: str,
        *,
        model: str,
        hypothesis: str,
        parent_id: str | None = None,
    ) -> PreparedCandidate:
        normalized_id = _validate_identifier(candidate_id)
        normalized_parent = (
            None if parent_id is None else _validate_identifier(parent_id)
        )
        if normalized_parent == normalized_id:
            raise CandidateVerificationError("candidate cannot name itself as parent")
        origin_commit = self._resolve_origin_commit(normalized_parent)
        prepared = PreparedCandidate(
            candidate_id=normalized_id,
            parent_id=normalized_parent,
            model=model,
            hypothesis=hypothesis,
            base_commit=self._base_commit,
            origin_commit=origin_commit,
            workspace_path=self._workspace_path(normalized_id),
        )
        manifest = self._load_manifest_or_none(normalized_id)
        if manifest is not None:
            if manifest.prepared != prepared:
                raise CandidateVerificationError(
                    "candidate preparation input changed after the first handoff"
                )
            if manifest.finalized is None and not prepared.workspace_path.is_dir():
                raise CandidateNotFoundError(
                    "candidate workspace is missing from the trusted root"
                )
            return manifest.prepared
        if prepared.workspace_path.exists():
            raise CandidateVerificationError(
                "candidate workspace path already exists outside trusted control"
            )
        prepared.workspace_path.parent.mkdir(parents=True, exist_ok=True)
        self._git(
            self._repository,
            "worktree",
            "add",
            "--detach",
            str(prepared.workspace_path),
            origin_commit,
        )
        self._write_manifest(
            _CandidateManifest(
                prepared=prepared,
                finalized=None,
            )
        )
        return prepared

    def finalize(self, candidate_id: str) -> FinalizedCandidate:
        manifest = self._load_manifest(_validate_identifier(candidate_id))
        if manifest.finalized is not None:
            return manifest.finalized
        prepared = manifest.prepared
        if not prepared.workspace_path.is_dir():
            raise CandidateNotFoundError(
                "candidate workspace does not exist under the trusted root"
            )
        head_commit = self._git_text(
            prepared.workspace_path,
            "rev-parse",
            "HEAD",
        )
        if head_commit != prepared.origin_commit:
            raise CandidateVerificationError(
                "candidate workspace head drifted from its immutable origin commit"
            )
        self._git(prepared.workspace_path, "add", "-A", "--", ".")
        changed_paths = self._validate_changed_paths(
            self._diff_name_only(prepared.workspace_path, prepared.base_commit)
        )
        incremental_changed_paths = self._validate_changed_paths(
            self._diff_name_only(prepared.workspace_path, prepared.origin_commit)
        )
        if not changed_paths:
            raise CandidatePolicyError("candidate contains no repository changes")
        if not self._has_source_root_change(incremental_changed_paths):
            raise CandidatePolicyError(
                "candidate must change at least one deployable path under source_root"
            )
        patch_bytes = self._git_bytes(
            prepared.workspace_path,
            "diff",
            "--cached",
            "--binary",
            "--full-index",
            "--no-renames",
            prepared.base_commit,
            "--",
        )
        if not patch_bytes.strip():
            raise CandidatePolicyError("candidate contains no repository changes")
        source_root_path = self._materialized_source_root(prepared.workspace_path)
        fingerprint_before = self._zip_builder.fingerprint(
            source_root_path,
            check_deadline=_noop_deadline,
        )
        artifact_directory = self._artifact_directory(prepared.candidate_id)
        artifact_directory.mkdir(parents=True, exist_ok=True)
        patch_path = artifact_directory / _PATCH_NAME
        source_zip_path = artifact_directory / _SOURCE_ZIP_NAME
        temp_patch = patch_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        temp_zip = source_zip_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        _atomic_write_bytes(temp_patch, patch_bytes)
        zip_result = self._zip_builder.build(
            source_root_path,
            temp_zip,
            check_deadline=_noop_deadline,
        )
        fingerprint_after = self._zip_builder.fingerprint(
            source_root_path,
            check_deadline=_noop_deadline,
        )
        _assert_fingerprint_matches(
            before=fingerprint_before,
            after=fingerprint_after,
            built=zip_result,
        )
        os.replace(temp_patch, patch_path)
        os.replace(temp_zip, source_zip_path)
        hashes = CandidateHashes(
            patch_sha256=_sha256_bytes(patch_bytes),
            source_tree_sha256=zip_result.tree_sha256,
            source_zip_sha256=zip_result.zip_sha256,
        )
        tree_id = self._git_text(prepared.workspace_path, "write-tree")
        candidate_commit = self._git_text(
            prepared.workspace_path,
            "commit-tree",
            tree_id,
            "-p",
            prepared.origin_commit,
            "-m",
            f"foundry-opt-poc/{prepared.candidate_id}",
            env=_deterministic_git_identity(),
        )
        finalized = FinalizedCandidate(
            candidate_id=prepared.candidate_id,
            parent_id=prepared.parent_id,
            model=prepared.model,
            hypothesis=prepared.hypothesis,
            base_commit=prepared.base_commit,
            origin_commit=prepared.origin_commit,
            candidate_commit=candidate_commit,
            source_root=self._source_root_text,
            workspace_path=prepared.workspace_path,
            changed_paths=changed_paths,
            incremental_changed_paths=incremental_changed_paths,
            hashes=hashes,
            patch_path=patch_path,
            source_zip_path=source_zip_path,
        )
        self._write_manifest(
            _CandidateManifest(
                prepared=prepared,
                finalized=finalized,
            )
        )
        return finalized

    def finalized(self, candidate_id: str) -> FinalizedCandidate:
        manifest = self._load_manifest(_validate_identifier(candidate_id))
        if manifest.finalized is None:
            raise CandidateNotFoundError("candidate has not been finalized")
        return manifest.finalized

    def apply_winner(
        self,
        candidate: str | FinalizedCandidate,
        destination_checkout: Path,
    ) -> AppliedPatch:
        finalized = (
            self.finalized(candidate)
            if isinstance(candidate, str)
            else candidate
        )
        checkout = Path(destination_checkout)
        if not checkout.is_dir():
            raise CandidateVerificationError("destination checkout does not exist")
        current_head = self._git_text(checkout, "rev-parse", "HEAD")
        if current_head != finalized.base_commit:
            raise CandidateVerificationError(
                "destination checkout is not at the candidate base commit"
            )
        patch_bytes = finalized.patch_path.read_bytes()
        patch_sha256 = _sha256_bytes(patch_bytes)
        if patch_sha256 != finalized.hashes.patch_sha256:
            raise CandidateVerificationError(
                "candidate patch hash does not match the finalized artifact"
            )
        self._git(
            checkout,
            "apply",
            "--check",
            "--binary",
            str(finalized.patch_path),
        )
        self._git(
            checkout,
            "apply",
            "--binary",
            str(finalized.patch_path),
        )
        return AppliedPatch(
            candidate_id=finalized.candidate_id,
            destination_checkout=checkout,
            base_commit=finalized.base_commit,
            patch_sha256=patch_sha256,
        )

    def cleanup(
        self,
        candidate_id: str,
        *,
        remove_artifacts: bool = False,
    ) -> None:
        normalized_id = _validate_identifier(candidate_id)
        manifest = self._load_manifest_or_none(normalized_id)
        workspace_path = (
            manifest.prepared.workspace_path
            if manifest is not None
            else self._workspace_path(normalized_id)
        )
        if self._is_registered_worktree(workspace_path):
            self._git(self._repository, "worktree", "remove", "--force", str(workspace_path))
        elif workspace_path.exists():
            self._assert_owned_directory(workspace_path, self._worktrees_root)
            shutil.rmtree(workspace_path)
        if remove_artifacts:
            artifact_directory = self._artifact_directory(normalized_id)
            if artifact_directory.exists():
                self._assert_owned_directory(artifact_directory, self._artifacts_root)
                shutil.rmtree(artifact_directory)

    def _resolve_origin_commit(self, parent_id: str | None) -> str:
        if parent_id is None:
            return self._base_commit
        parent = self.finalized(parent_id)
        if parent.base_commit != self._base_commit:
            raise CandidateVerificationError(
                "child candidate must share the same immutable base commit"
            )
        return parent.candidate_commit

    def _workspace_path(self, candidate_id: str) -> Path:
        return self._worktrees_root / candidate_id

    def _artifact_directory(self, candidate_id: str) -> Path:
        return self._artifacts_root / candidate_id

    def _manifest_path(self, candidate_id: str) -> Path:
        return self._artifact_directory(candidate_id) / _MANIFEST_NAME

    def _load_manifest(self, candidate_id: str) -> _CandidateManifest:
        manifest = self._load_manifest_or_none(candidate_id)
        if manifest is None:
            raise CandidateNotFoundError("candidate manifest does not exist")
        return manifest

    def _load_manifest_or_none(
        self,
        candidate_id: str,
    ) -> _CandidateManifest | None:
        path = self._manifest_path(candidate_id)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return _CandidateManifest.model_validate(payload)
        except OSError as error:
            raise CandidateVerificationError(
                "candidate manifest could not be read"
            ) from error
        except (json.JSONDecodeError, ValidationError) as error:
            raise CandidateVerificationError(
                "candidate manifest is not schema valid"
            ) from error

    def _write_manifest(self, manifest: _CandidateManifest) -> None:
        path = self._manifest_path(manifest.prepared.candidate_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_bytes(
            path,
            _canonical_json_bytes(manifest.model_dump(mode="json")),
        )

    def _verify_trusted_root(self) -> None:
        repository = _absolute(self._repository)
        trusted_root = _absolute(self._trusted_root)
        if trusted_root == repository or _is_relative_to(trusted_root, repository):
            raise CandidateVerificationError(
                "trusted_root must be outside the repository checkout"
            )

    def _verify_commit_exists(self, commit: str) -> None:
        self._git(self._repository, "rev-parse", "--verify", commit)

    def _verify_source_root_exists(self) -> None:
        if self._source_root is None:
            return
        tree_name = self._source_root.as_posix()
        try:
            mode = self._git_text(
                self._repository,
                "cat-file",
                "-t",
                f"{self._base_commit}:{tree_name}",
            )
        except CandidateVerificationError as error:
            raise CandidateVerificationError(
                "source_root must exist at the immutable base commit"
            ) from error
        if mode != "tree":
            raise CandidateVerificationError(
                "source_root must name a directory at the immutable base commit"
            )

    def _materialized_source_root(self, workspace: Path) -> Path:
        if self._source_root is None:
            return workspace
        return workspace.joinpath(*self._source_root.parts)

    def _diff_name_only(self, workspace: Path, commit: str) -> tuple[str, ...]:
        raw = self._git_bytes(
            workspace,
            "diff",
            "--cached",
            "--name-only",
            "-z",
            commit,
            "--",
        )
        values = [item.decode("utf-8") for item in raw.split(b"\x00") if item]
        return tuple(
            sorted(
                (_normalize_repository_path(value) for value in values),
                key=lambda item: (item.casefold(), item),
            )
        )

    def _validate_changed_paths(
        self,
        paths: tuple[str, ...],
    ) -> tuple[str, ...]:
        validated: list[str] = []
        for path in paths:
            normalized = _normalize_repository_path(path)
            if any(matcher.matches(normalized) for matcher in self._protected_patterns):
                raise CandidatePolicyError(f"changed path is protected: {normalized}")
            if not any(matcher.matches(normalized) for matcher in self._editable_patterns):
                raise CandidatePolicyError(
                    f"changed path is outside the editable scope: {normalized}"
                )
            validated.append(normalized)
        return tuple(
            sorted(
                validated,
                key=lambda item: (item.casefold(), item),
            )
        )

    def _has_source_root_change(self, paths: tuple[str, ...]) -> bool:
        for path in paths:
            if self._source_root is None:
                return True
            parts = PurePosixPath(path).parts
            root_parts = self._source_root.parts
            if parts[: len(root_parts)] == root_parts:
                return True
        return False

    def _is_registered_worktree(self, workspace_path: Path) -> bool:
        raw = self._git_text(self._repository, "worktree", "list", "--porcelain")
        target = _absolute(workspace_path)
        for line in raw.splitlines():
            if not line.startswith("worktree "):
                continue
            if _absolute(Path(line.removeprefix("worktree ").strip())) == target:
                return True
        return False

    def _assert_owned_directory(self, path: Path, expected_parent: Path) -> None:
        absolute_path = _absolute(path)
        absolute_parent = _absolute(expected_parent)
        if absolute_path.parent != absolute_parent:
            raise CandidateVerificationError(
                "refusing to remove a path outside the trusted candidate root"
            )

    def _git(
        self,
        cwd: Path,
        *arguments: str,
        env: dict[str, str] | None = None,
    ) -> None:
        self._run_git(cwd, *arguments, env=env, text=False)

    def _git_text(
        self,
        cwd: Path,
        *arguments: str,
        env: dict[str, str] | None = None,
    ) -> str:
        result = self._run_git(cwd, *arguments, env=env, text=True)
        return result.stdout.rstrip("\n")

    def _git_bytes(self, cwd: Path, *arguments: str) -> bytes:
        result = self._run_git(cwd, *arguments, text=False)
        return result.stdout

    def _run_git(
        self,
        cwd: Path,
        *arguments: str,
        env: dict[str, str] | None = None,
        text: bool,
    ) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                [self._git_executable, *arguments],
                cwd=cwd,
                env=env,
                check=True,
                capture_output=True,
                text=text,
            )
        except subprocess.CalledProcessError as error:
            stderr = error.stderr if text else error.stderr.decode("utf-8", "replace")
            raise CandidateVerificationError(stderr.strip() or "git command failed") from error
        except OSError as error:
            raise CandidateVerificationError("git command could not be executed") from error


def _deterministic_git_identity() -> dict[str, str]:
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_AUTHOR_NAME": "foundry-opt-poc",
            "GIT_AUTHOR_EMAIL": "foundry-opt-poc@example.invalid",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+0000",
            "GIT_COMMITTER_NAME": "foundry-opt-poc",
            "GIT_COMMITTER_EMAIL": "foundry-opt-poc@example.invalid",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+0000",
        }
    )
    return environment


def _assert_fingerprint_matches(
    *,
    before: TreeFingerprint,
    after: TreeFingerprint,
    built,
) -> None:
    if before.entries != after.entries or before.tree_sha256 != after.tree_sha256:
        raise CandidateVerificationError(
            "source_root changed while the deterministic source archive was built"
        )
    if before.tree_sha256 != built.tree_sha256 or before.entries != built.entries:
        raise CandidateVerificationError(
            "built source archive does not match the finalized source_root tree"
        )


def _atomic_write_bytes(destination: Path, content: bytes) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, destination)


def _canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _validate_identifier(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(_IDENTIFIER_PATTERN, value) is None:
        raise CandidateVerificationError(f"invalid candidate identifier: {value!r}")
    return value


def _validate_commit(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(_COMMIT_PATTERN, value) is None:
        raise CandidateVerificationError(f"invalid git commit: {value!r}")
    return value


def _normalize_text(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("text must be a string")
    normalized = " ".join(value.split()).strip()
    if not normalized:
        raise ValueError("text must not be empty")
    if _CONTROL_PATTERN.search(normalized) is not None:
        raise ValueError("text must not contain control characters")
    if "<" in normalized or ">" in normalized:
        raise ValueError("text must not contain HTML delimiters")
    return normalized


def _normalize_repository_path(value: str) -> str:
    if not isinstance(value, str):
        raise CandidatePolicyError("changed path must be a string")
    if not value or "\\" in value or ":" in value or "\x00" in value:
        raise CandidatePolicyError(f"unsafe repository path: {value!r}")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        value == "."
        or posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or ".." in posix.parts
        or ".." in windows.parts
        or any(part in {"", "."} for part in posix.parts)
    ):
        raise CandidatePolicyError(f"unsafe repository path: {value!r}")
    return posix.as_posix()


def _normalize_source_root(value: str) -> PurePosixPath | None:
    if value == ".":
        return None
    normalized = _normalize_repository_path(value)
    return PurePosixPath(normalized)


def _compile_glob(pattern: str) -> _PathMatcher:
    normalized = _normalize_repository_path(pattern.rstrip("/").removeprefix("./"))
    if not any(character in normalized for character in _GLOB_CHARS):
        expression = re.escape(normalized) + r"(?:/.*)?"
        return _PathMatcher(normalized, re.compile(expression))
    expression_parts: list[str] = []
    index = 0
    while index < len(normalized):
        character = normalized[index]
        if character == "*":
            if index + 1 < len(normalized) and normalized[index + 1] == "*":
                index += 2
                if index < len(normalized) and normalized[index] == "/":
                    expression_parts.append(r"(?:.*/)?")
                    index += 1
                else:
                    expression_parts.append(r".*")
                continue
            expression_parts.append(r"[^/]*")
        elif character == "?":
            expression_parts.append(r"[^/]")
        else:
            expression_parts.append(re.escape(character))
        index += 1
    return _PathMatcher(normalized, re.compile("".join(expression_parts)))


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _noop_deadline() -> None:
    return None

