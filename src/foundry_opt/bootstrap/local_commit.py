from __future__ import annotations

import base64
import hashlib
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self, cast

from pydantic import Field, StringConstraints, field_validator, model_validator

from foundry_opt.bootstrap.canonical import canonical_json_bytes, canonical_sha256, safe_persisted_document
from foundry_opt.bootstrap.contracts import AgentId, BootstrapDocument, BootstrapPlan, BootstrapSidecar, GitCommit, RepositoryIdentity, RepositoryUrl, RootRegistry, Sha256
from foundry_opt.bootstrap.discovery import _ScanCache, _fingerprint_root
from foundry_opt.bootstrap.errors import BootstrapApplyError, BootstrapConfigError
from foundry_opt.bootstrap.operation_state import default_state_root
from foundry_opt.bootstrap.repository.engine import LOCK_PATH
from foundry_opt.poc.config import validate_repository_relative_path

_STEP_ID = "bootstrap-local-commit"
_BRANCH_PREFIX = "foundry-opt/bootstrap/"
_STATE_FILE_NAME = "state.json"
_LOCK_FILE_NAME = "state.lock"
_MAX_STATE_BYTES = 2 * 1024 * 1024
_DEFAULT_REGISTRY_PATH = ".foundry-opt/registry.yaml"
LOCAL_COMMIT_CONTEXT_KEY = "local_commit"

LocalCommitLifecycleState = Literal["reviewed", "committed", "rolled_back"]
LocalCommitReviewKind = Literal["managed", "existing"]
LocalCommitNextStage = Literal["deployment_approval", "final_handoff"]


def _jsonable(value: object) -> object:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")  # type: ignore[no-any-return]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _normalize_exact_paths(values: Sequence[str], *, field: str) -> tuple[str, ...]:
    seen: dict[str, str] = {}
    normalized: list[str] = []
    for value in values:
        path = validate_repository_relative_path(str(value), field=field)
        key = path.casefold()
        if key in seen:
            raise BootstrapConfigError(
                f"{field} contains case-fold duplicate values: {seen[key]!r} and {path!r}"
            )
        seen[key] = path
        normalized.append(path)
    return tuple(sorted(normalized, key=lambda item: (item.casefold(), item)))


def _normalize_agent_root(value: str) -> str:
    if value == ".":
        return value
    return validate_repository_relative_path(value, field="root")


def _github_remote_identity(value: str) -> tuple[str, str]:
    patterns = (
        r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$",
        r"^git@github\.com:(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?$",
        r"^ssh://git@github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$",
    )
    for pattern in patterns:
        match = re.fullmatch(pattern, value)
        if match is not None:
            return match.group("owner"), match.group("repo")
    raise BootstrapConfigError("repository remote must target github.com/owner/repo")


def _normalize_branch_name(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise BootstrapConfigError("branch_name must not be empty")
    if any(ord(ch) < 32 for ch in normalized) or "\x7f" in normalized:
        raise BootstrapConfigError("branch_name contains control characters")
    return normalized


def bootstrap_branch_name(operation_id: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", operation_id).strip("-./").lower()
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        raise BootstrapConfigError("operation_id does not produce a safe bootstrap branch name")
    branch_name = f"{_BRANCH_PREFIX}{slug}"
    _normalize_branch_name(branch_name)
    return branch_name


def default_local_commit_message(operation_id: str) -> str:
    return f"bootstrap: freeze exact deployment source for {operation_id}"


def build_local_commit_context(
    plan: BootstrapPlan,
    *,
    reviewed_existing_paths: Sequence[str] = (),
    commit_agent_ids: Sequence[str] | None = None,
    commit_message: str | None = None,
    next_stage: LocalCommitNextStage = "deployment_approval",
) -> dict[str, object]:
    managed_paths = {LOCK_PATH}
    for action in plan.actions:
        if action.phase != "repository":
            continue
        if action.template_payload is not None:
            managed_paths.add(action.template_payload.destination_path)
        for diagnostic in action.diagnostics:
            if not diagnostic.startswith("conflict:"):
                continue
            sibling = diagnostic.split(":", 1)[1].strip()
            if sibling:
                managed_paths.add(validate_repository_relative_path(sibling, field="managed_path"))
    return {
        LOCAL_COMMIT_CONTEXT_KEY: {
            "repository_plan_hash": plan.plan_hash,
            "managed_paths": sorted(managed_paths, key=lambda item: (item.casefold(), item)),
            "reviewed_existing_paths": list(_normalize_exact_paths(reviewed_existing_paths, field="reviewed_existing_paths")),
            "commit_agent_ids": (
                None
                if commit_agent_ids is None
                else list(
                    _casefold_unique_ids(
                        commit_agent_ids,
                        field="commit_agent_ids",
                    )
                )
            ),
            "commit_summary": commit_message or default_local_commit_message(plan.operation_id),
            "next_stage": next_stage,
        }
    }


def _casefold_unique_ids(
        values: Sequence[str],
        *,
        field: str,
) -> tuple[str, ...]:
        seen: dict[str, str] = {}
        result: list[str] = []
        for value in values:
            normalized = str(value)
            key = normalized.casefold()
            if key in seen:
                raise BootstrapConfigError(
                    f"{field} contains duplicate ids: {seen[key]!r} and {normalized!r}"
                )
            seen[key] = normalized
            result.append(normalized)
        return tuple(sorted(result, key=lambda item: (item.casefold(), item)))


@dataclass(frozen=True, slots=True)
class GitCommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def stdout_text(self) -> str:
        return self.stdout.decode("utf-8", errors="replace")

    @property
    def stderr_text(self) -> str:
        return self.stderr.decode("utf-8", errors="replace")


class GitCommandAdapterProtocol(Protocol):
    def run(self, repository_root: Path, *args: str) -> GitCommandResult: ...


class SubprocessGitCommandAdapter(GitCommandAdapterProtocol):
    def run(self, repository_root: Path, *args: str) -> GitCommandResult:
        completed = subprocess.run(
            ["git", "-C", str(repository_root), *args],
            check=False,
            capture_output=True,
        )
        return GitCommandResult(
            returncode=int(completed.returncode),
            stdout=bytes(completed.stdout),
            stderr=bytes(completed.stderr),
        )


class LocalCommitSelectedAgent(BootstrapDocument):
    repo_agent_id: AgentId
    root: str
    profile_path: str

    @field_validator("root")
    @classmethod
    def _validate_root(cls, value: str) -> str:
        return _normalize_agent_root(value)

    @field_validator("profile_path")
    @classmethod
    def _validate_profile_path(cls, value: str) -> str:
        return validate_repository_relative_path(value, field="profile_path")


class LocalCommitWorktreeEntry(BootstrapDocument):
    review_kind: LocalCommitReviewKind
    path: str
    index_status: Annotated[str, StringConstraints(min_length=1, max_length=1)]
    worktree_status: Annotated[str, StringConstraints(min_length=1, max_length=1)]
    index_object_id: GitCommit | None = None
    worktree_sha256: Sha256 | None = None

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return validate_repository_relative_path(value, field="path")

    @model_validator(mode="after")
    def _validate_status(self) -> Self:
        allowed = {" ", "?", "M", "A", "D"}
        if self.index_status not in allowed or self.worktree_status not in allowed:
            raise BootstrapApplyError("local commit review encountered an unsupported git status")
        if (self.index_status == "?") != (self.worktree_status == "?"):
            raise BootstrapApplyError("local commit review encountered an invalid untracked git status")
        if self.index_status not in {" ", "?"} and self.worktree_status not in {" ", "?"}:
            raise BootstrapApplyError(
                "local commit review does not support partially staged or multiply dirty paths"
            )
        return self

    @property
    def status_token(self) -> str:
        return "??" if self.index_status == "?" else f"{self.index_status}{self.worktree_status}"

    @property
    def is_staged(self) -> bool:
        return self.index_status not in {" ", "?"}


class LocalCommitRollbackSnapshot(BootstrapDocument):
    path: str
    exists: bool
    bytes_b64: str | None = None
    index_status: Annotated[str, StringConstraints(min_length=1, max_length=1)]
    worktree_status: Annotated[str, StringConstraints(min_length=1, max_length=1)]

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        return validate_repository_relative_path(value, field="path")

    @model_validator(mode="after")
    def _validate_payload(self) -> Self:
        if self.exists and self.bytes_b64 is None:
            raise BootstrapConfigError("rollback snapshot for an existing path requires bytes_b64")
        if not self.exists and self.bytes_b64 is not None:
            raise BootstrapConfigError("rollback snapshot for a missing path must not carry bytes_b64")
        return self

    def decoded_content(self) -> bytes | None:
        if self.bytes_b64 is None:
            return None
        return base64.b64decode(self.bytes_b64.encode("ascii"))


class LocalCommitReview(BootstrapDocument):
    step_id: Literal["bootstrap-local-commit"] = _STEP_ID
    operation_id: Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]
    repository_root: str
    repository_identity: RepositoryIdentity
    runtime_repository: RepositoryUrl
    runtime_commit: GitCommit
    repository_plan_hash: Sha256
    base_commit: GitCommit
    original_branch: str | None = None
    branch_name: str
    proposed_message: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    registry_path: str = _DEFAULT_REGISTRY_PATH
    managed_paths: tuple[str, ...]
    reviewed_existing_paths: tuple[str, ...] = ()
    selected_agents: tuple[LocalCommitSelectedAgent, ...]
    entries: tuple[LocalCommitWorktreeEntry, ...]
    review_hash: Sha256

    @field_validator("repository_root")
    @classmethod
    def _validate_repository_root(cls, value: str) -> str:
        if not value:
            raise BootstrapConfigError("repository_root is required")
        return value

    @field_validator("original_branch")
    @classmethod
    def _validate_original_branch(cls, value: str | None) -> str | None:
        return _normalize_branch_name(value)

    @field_validator("branch_name")
    @classmethod
    def _validate_branch_name(cls, value: str) -> str:
        normalized = _normalize_branch_name(value)
        assert normalized is not None
        return normalized

    @field_validator("registry_path")
    @classmethod
    def _validate_registry_path(cls, value: str) -> str:
        return validate_repository_relative_path(value, field="registry_path")

    @field_validator("managed_paths", "reviewed_existing_paths")
    @classmethod
    def _validate_paths(cls, value: Sequence[str], info) -> tuple[str, ...]:
        return _normalize_exact_paths(value, field=str(info.field_name))

    @field_validator("selected_agents")
    @classmethod
    def _validate_selected_agents(
        cls,
        value: Sequence[LocalCommitSelectedAgent],
    ) -> tuple[LocalCommitSelectedAgent, ...]:
        payload = tuple(value)
        seen: dict[str, str] = {}
        for item in payload:
            key = item.repo_agent_id.casefold()
            if key in seen:
                raise BootstrapConfigError(
                    f"selected_agents contains case-fold duplicate ids: {seen[key]!r} and {item.repo_agent_id!r}"
                )
            seen[key] = item.repo_agent_id
        return tuple(sorted(payload, key=lambda item: (item.repo_agent_id.casefold(), item.repo_agent_id)))

    @field_validator("entries")
    @classmethod
    def _validate_entries(
        cls,
        value: Sequence[LocalCommitWorktreeEntry],
    ) -> tuple[LocalCommitWorktreeEntry, ...]:
        payload = tuple(value)
        seen: dict[str, str] = {}
        for item in payload:
            key = item.path.casefold()
            if key in seen:
                raise BootstrapConfigError(
                    f"entries contains case-fold duplicate paths: {seen[key]!r} and {item.path!r}"
                )
            seen[key] = item.path
        return tuple(sorted(payload, key=lambda item: (item.path.casefold(), item.path)))

    def _hash_payload(self) -> dict[str, object]:
        return {
            "step_id": self.step_id,
            "operation_id": self.operation_id,
            "repository_root": self.repository_root,
            "repository_identity": self.repository_identity,
            "runtime_repository": self.runtime_repository,
            "runtime_commit": self.runtime_commit,
            "repository_plan_hash": self.repository_plan_hash,
            "base_commit": self.base_commit,
            "original_branch": self.original_branch,
            "branch_name": self.branch_name,
            "proposed_message": self.proposed_message,
            "registry_path": self.registry_path,
            "managed_paths": list(self.managed_paths),
            "reviewed_existing_paths": list(self.reviewed_existing_paths),
            "selected_agents": [item.model_dump(mode="json") for item in self.selected_agents],
            "entries": [item.model_dump(mode="json") for item in self.entries],
        }

    @classmethod
    def create(cls, **values: object) -> "LocalCommitReview":
        payload = _jsonable(dict(values))
        if "step_id" not in payload:
            payload["step_id"] = _STEP_ID
        validated = cls.model_validate({**payload, "review_hash": "0" * 64})
        return cls.model_validate(
            {
                **validated.model_dump(mode="json", exclude={"review_hash"}),
                "review_hash": canonical_sha256(validated._hash_payload()),
            }
        )

    @model_validator(mode="after")
    def _validate_hash(self) -> Self:
        if self.review_hash == "0" * 64:
            return self
        if self.review_hash != canonical_sha256(self._hash_payload()):
            raise BootstrapApplyError("local commit review hash does not match the canonical payload")
        return self

    @property
    def reviewed_paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.entries)

    def render_markdown(self) -> str:
        return "\n".join(self._render_lines(markdown=True))

    def render_text(self) -> str:
        return "\n".join(self._render_lines(markdown=False))

    def _render_lines(self, *, markdown: bool) -> list[str]:
        heading = "## Local commit review" if markdown else "Local commit review"
        lines = [heading]
        lines.append(f"- Base commit: {self.base_commit[:12]}")
        lines.append(f"- Current branch: {self.original_branch or '(detached)'}")
        lines.append(f"- Bootstrap branch: {self.branch_name}")
        lines.append(f"- Proposed message: {self.proposed_message}")
        lines.append(f"- Reviewed paths: {len(self.entries)}")
        if not self.entries:
            lines.append("  - none")
        for entry in self.entries:
            lines.append(f"  - {entry.review_kind}: {entry.status_token} {entry.path}")
        return lines


class LocalCommitApproval(BootstrapDocument):
    step_id: Literal["bootstrap-local-commit"] = _STEP_ID
    repository_identity: RepositoryIdentity
    operation_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    runtime_repository: RepositoryUrl
    runtime_commit: GitCommit
    repository_plan_hash: Sha256
    review_hash: Sha256
    actor: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    summary: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    approval_hash: Sha256

    def _hash_payload(self) -> dict[str, object]:
        return {
            "step_id": self.step_id,
            "repository_identity": self.repository_identity,
            "operation_id": self.operation_id,
            "runtime_repository": self.runtime_repository,
            "runtime_commit": self.runtime_commit,
            "repository_plan_hash": self.repository_plan_hash,
            "review_hash": self.review_hash,
            "actor": self.actor,
            "summary": self.summary,
        }

    @classmethod
    def create(
        cls,
        *,
        repository_identity: str,
        operation_id: str,
        runtime_repository: str,
        runtime_commit: str,
        repository_plan_hash: str,
        review_hash: str,
        actor: str,
        summary: str,
    ) -> "LocalCommitApproval":
        payload = {
            "step_id": _STEP_ID,
            "repository_identity": repository_identity,
            "operation_id": operation_id,
            "runtime_repository": runtime_repository,
            "runtime_commit": runtime_commit,
            "repository_plan_hash": repository_plan_hash,
            "review_hash": review_hash,
            "actor": actor,
            "summary": summary,
        }
        safe_persisted_document(payload)
        return cls.model_validate({**payload, "approval_hash": canonical_sha256(payload)})

    @model_validator(mode="after")
    def _validate_hash(self) -> Self:
        if self.approval_hash != canonical_sha256(self._hash_payload()):
            raise BootstrapApplyError("local commit approval hash does not match the approval payload")
        return self


class LocalCommitProfileHash(BootstrapDocument):
    repo_agent_id: AgentId
    profile_path: str
    sha256: Sha256

    @field_validator("profile_path")
    @classmethod
    def _validate_profile_path(cls, value: str) -> str:
        return validate_repository_relative_path(value, field="profile_path")


class LocalCommitAgentHash(BootstrapDocument):
    repo_agent_id: AgentId
    source_root: str
    source_sha256: Sha256
    package_root: str
    package_sha256: Sha256

    @field_validator("source_root")
    @classmethod
    def _validate_source_root(cls, value: str) -> str:
        return _normalize_agent_root(value)

    @field_validator("package_root")
    @classmethod
    def _validate_package_root(cls, value: str) -> str:
        return _normalize_agent_root(value)


class LocalCommitReceipt(BootstrapDocument):
    step_id: Literal["bootstrap-local-commit"] = _STEP_ID
    operation_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    repository_identity: RepositoryIdentity
    runtime_repository: RepositoryUrl
    runtime_commit: GitCommit
    repository_plan_hash: Sha256
    review_hash: Sha256
    approval_hash: Sha256
    base_commit: GitCommit
    branch_name: str
    commit_sha: GitCommit
    tree_sha: GitCommit
    commit_message: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    registry_path: str
    registry_sha256: Sha256
    committed_paths: tuple[str, ...]
    profile_hashes: tuple[LocalCommitProfileHash, ...]
    agent_hashes: tuple[LocalCommitAgentHash, ...]
    receipt_hash: Sha256

    @field_validator("branch_name")
    @classmethod
    def _validate_branch_name(cls, value: str) -> str:
        normalized = _normalize_branch_name(value)
        assert normalized is not None
        return normalized

    @field_validator("registry_path")
    @classmethod
    def _validate_registry_path(cls, value: str) -> str:
        return validate_repository_relative_path(value, field="registry_path")

    @field_validator("committed_paths")
    @classmethod
    def _validate_committed_paths(cls, value: Sequence[str]) -> tuple[str, ...]:
        return _normalize_exact_paths(value, field="committed_paths")

    def _hash_payload(self) -> dict[str, object]:
        return {
            "step_id": self.step_id,
            "operation_id": self.operation_id,
            "repository_identity": self.repository_identity,
            "runtime_repository": self.runtime_repository,
            "runtime_commit": self.runtime_commit,
            "repository_plan_hash": self.repository_plan_hash,
            "review_hash": self.review_hash,
            "approval_hash": self.approval_hash,
            "base_commit": self.base_commit,
            "branch_name": self.branch_name,
            "commit_sha": self.commit_sha,
            "tree_sha": self.tree_sha,
            "commit_message": self.commit_message,
            "registry_path": self.registry_path,
            "registry_sha256": self.registry_sha256,
            "committed_paths": list(self.committed_paths),
            "profile_hashes": [item.model_dump(mode="json") for item in self.profile_hashes],
            "agent_hashes": [item.model_dump(mode="json") for item in self.agent_hashes],
        }

    @classmethod
    def create(cls, **values: object) -> "LocalCommitReceipt":
        payload = _jsonable(dict(values))
        if "step_id" not in payload:
            payload["step_id"] = _STEP_ID
        validated = cls.model_validate({**payload, "receipt_hash": "0" * 64})
        return cls.model_validate(
            {
                **validated.model_dump(mode="json", exclude={"receipt_hash"}),
                "receipt_hash": canonical_sha256(validated._hash_payload()),
            }
        )

    @model_validator(mode="after")
    def _validate_hash(self) -> Self:
        if self.receipt_hash == "0" * 64:
            return self
        if self.receipt_hash != canonical_sha256(self._hash_payload()):
            raise BootstrapApplyError("local commit receipt hash does not match the canonical payload")
        return self


class LocalCommitStatus(BootstrapDocument):
    step_id: Literal["bootstrap-local-commit"] = _STEP_ID
    operation_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    repository_identity: RepositoryIdentity
    runtime_repository: RepositoryUrl
    runtime_commit: GitCommit
    repository_plan_hash: Sha256
    review_hash: Sha256
    approval_hash: Sha256 | None = None
    overall_state: LocalCommitLifecycleState
    summary: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    branch_name: str
    base_commit: GitCommit
    commit_sha: GitCommit | None = None
    resumable: bool
    rollback_ready: bool

    @field_validator("branch_name")
    @classmethod
    def _validate_branch_name(cls, value: str) -> str:
        normalized = _normalize_branch_name(value)
        assert normalized is not None
        return normalized


class LocalCommitStatePayload(BootstrapDocument):
    generation: int = Field(ge=0)
    lifecycle_state: LocalCommitLifecycleState
    review: LocalCommitReview
    approval: LocalCommitApproval | None = None
    receipt: LocalCommitReceipt | None = None
    rollback_snapshots: tuple[LocalCommitRollbackSnapshot, ...] = ()


class LocalCommitStateEnvelope(BootstrapDocument):
    payload: LocalCommitStatePayload
    generation_hash: Sha256

    @property
    def generation(self) -> int:
        return self.payload.generation

    @property
    def lifecycle_state(self) -> LocalCommitLifecycleState:
        return self.payload.lifecycle_state

    @property
    def review(self) -> LocalCommitReview:
        return self.payload.review

    @property
    def approval(self) -> LocalCommitApproval | None:
        return self.payload.approval

    @property
    def receipt(self) -> LocalCommitReceipt | None:
        return self.payload.receipt

    @property
    def rollback_snapshots(self) -> tuple[LocalCommitRollbackSnapshot, ...]:
        return self.payload.rollback_snapshots

    @classmethod
    def create(cls, **values: object) -> "LocalCommitStateEnvelope":
        payload = LocalCommitStatePayload.model_validate(values)
        payload_json = payload.model_dump(mode="json")
        digest = canonical_sha256({"payload": payload_json})
        return cls.model_validate({"payload": payload_json, "generation_hash": digest})

    @model_validator(mode="after")
    def _validate_hash(self) -> Self:
        payload_json = self.payload.model_dump(mode="json")
        if self.generation_hash != canonical_sha256({"payload": payload_json}):
            raise BootstrapApplyError("local commit state hash does not match the state payload")
        return self


def default_local_commit_state_root() -> Path:
    return default_state_root() / "local-commit"


def operation_directory(
    repository_identity: str,
    operation_id: str,
    *,
    state_root: Path | None = None,
) -> Path:
    root = (state_root or default_local_commit_state_root()).resolve()
    repo_segment = canonical_sha256({"repository_identity": repository_identity})
    target = (root / repo_segment / operation_id).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise BootstrapApplyError("local commit state escapes the state root") from exc
    return target


def state_file_path(
    repository_identity: str,
    operation_id: str,
    *,
    state_root: Path | None = None,
) -> Path:
    return operation_directory(repository_identity, operation_id, state_root=state_root) / _STATE_FILE_NAME


def lock_file_path(
    repository_identity: str,
    operation_id: str,
    *,
    state_root: Path | None = None,
) -> Path:
    return operation_directory(repository_identity, operation_id, state_root=state_root) / _LOCK_FILE_NAME


def write_local_commit_state(
    envelope: LocalCommitStateEnvelope,
    *,
    expected_generation: int | None = None,
    expected_generation_hash: str | None = None,
    state_root: Path | None = None,
) -> Path:
    review = envelope.review
    path = state_file_path(review.repository_identity, review.operation_id, state_root=state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = lock_file_path(review.repository_identity, review.operation_id, state_root=state_root)
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise BootstrapApplyError("local commit state is locked by another writer") from exc
    try:
        if expected_generation is None:
            if path.exists():
                raise BootstrapApplyError("local commit state already exists")
        else:
            current = read_local_commit_state(
                review.repository_identity,
                review.operation_id,
                state_root=state_root,
            )
            if (
                current.generation != expected_generation
                or current.generation_hash != expected_generation_hash
            ):
                raise BootstrapApplyError("local commit state generation conflict")
        data = canonical_json_bytes(envelope.model_dump(mode="json")) + b"\n"
        if len(data) > _MAX_STATE_BYTES:
            raise BootstrapApplyError("local commit state exceeds size limit")
        temp = path.with_name(f"{path.stem}.{envelope.generation_hash}.tmp")
        with open(temp, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        os.close(lock_fd)
        os.unlink(lock_path)
    return path


def read_local_commit_state(
    repository_identity: str,
    operation_id: str,
    *,
    state_root: Path | None = None,
) -> LocalCommitStateEnvelope:
    path = state_file_path(repository_identity, operation_id, state_root=state_root)
    data = path.read_bytes()
    if len(data) > _MAX_STATE_BYTES:
        raise BootstrapApplyError("local commit state exceeds size limit")
    try:
        return LocalCommitStateEnvelope.model_validate_json(data)
    except Exception as exc:
        raise BootstrapApplyError("local commit state is invalid or tampered") from exc


def next_local_commit_generation(
    envelope: LocalCommitStateEnvelope,
    **updates: object,
) -> LocalCommitStateEnvelope:
    payload = envelope.payload.model_dump(mode="python")
    payload.update(updates)
    payload["generation"] = envelope.generation + 1
    return LocalCommitStateEnvelope.create(**payload)


def selected_local_commit_agents_from_registry(
    repository_root: Path,
    *,
    repo_agent_ids: Sequence[str],
    registry_path: str = _DEFAULT_REGISTRY_PATH,
) -> tuple[LocalCommitSelectedAgent, ...]:
    normalized_registry_path = validate_repository_relative_path(registry_path, field="registry_path")
    registry = RootRegistry.from_document((repository_root / normalized_registry_path).read_text(encoding="utf-8"))
    by_id = {agent.agent_id.casefold(): agent for agent in registry.agents}
    selected: list[LocalCommitSelectedAgent] = []
    seen: set[str] = set()
    for repo_agent_id in repo_agent_ids:
        key = repo_agent_id.casefold()
        if key in seen:
            continue
        seen.add(key)
        agent = by_id.get(key)
        if agent is None or not agent.enabled:
            raise BootstrapApplyError("local commit review requires every selected agent to exist and remain enabled in the registry")
        selected.append(
            LocalCommitSelectedAgent(
                repo_agent_id=agent.agent_id,
                root=agent.root,
                profile_path=agent.config_path,
            )
        )
    return tuple(selected)


class LocalGitCommitCoordinator:
    def __init__(
        self,
        *,
        git: GitCommandAdapterProtocol | None = None,
        state_root: Path | None = None,
    ) -> None:
        self._git = git or SubprocessGitCommandAdapter()
        self._state_root = Path(state_root) if state_root is not None else default_local_commit_state_root()

    def build_review(
        self,
        repository: str | Path,
        *,
        operation_id: str,
        repository_identity: str,
        runtime_repository: str,
        runtime_commit: str,
        repository_plan_hash: str,
        managed_paths: Sequence[str],
        reviewed_existing_paths: Sequence[str] = (),
        selected_agents: Sequence[LocalCommitSelectedAgent] | None = None,
        selected_agent_ids: Sequence[str] = (),
        commit_message: str | None = None,
        registry_path: str = _DEFAULT_REGISTRY_PATH,
    ) -> LocalCommitReview:
        repository_root = self._repository_root(Path(repository))
        normalized_managed = _normalize_exact_paths(managed_paths, field="managed_paths")
        normalized_existing = _normalize_exact_paths(
            reviewed_existing_paths,
            field="reviewed_existing_paths",
        )
        normalized_registry_path = validate_repository_relative_path(
            registry_path,
            field="registry_path",
        )
        normalized_agents = self._resolve_selected_agents(
            repository_root,
            selected_agents=selected_agents,
            selected_agent_ids=selected_agent_ids,
            registry_path=normalized_registry_path,
        )
        proposed_message = commit_message or default_local_commit_message(operation_id)
        existing_state = self._try_read_state(
            repository_identity,
            operation_id,
        )
        if existing_state is not None:
            self._validate_existing_request(
                existing_state.review,
                repository_root=repository_root,
                repository_identity=repository_identity,
                runtime_repository=runtime_repository,
                runtime_commit=runtime_commit,
                repository_plan_hash=repository_plan_hash,
                managed_paths=normalized_managed,
                reviewed_existing_paths=normalized_existing,
                selected_agents=normalized_agents,
                commit_message=proposed_message,
                registry_path=normalized_registry_path,
            )
            self._validate_state_against_worktree(existing_state)
            return existing_state.review
        observed_identity = self._repository_identity(repository_root)
        if observed_identity != repository_identity:
            raise BootstrapApplyError("local commit review requires the exact repository identity")
        review_entries, rollback_snapshots = self._capture_review_entries(
            repository_root,
            managed_paths=normalized_managed,
            reviewed_existing_paths=normalized_existing,
        )
        review = LocalCommitReview.create(
            operation_id=operation_id,
            repository_root=str(repository_root),
            repository_identity=repository_identity,
            runtime_repository=runtime_repository,
            runtime_commit=runtime_commit,
            repository_plan_hash=repository_plan_hash,
            base_commit=self._head_commit(repository_root),
            original_branch=self._current_branch(repository_root),
            branch_name=bootstrap_branch_name(operation_id),
            proposed_message=proposed_message,
            registry_path=normalized_registry_path,
            managed_paths=normalized_managed,
            reviewed_existing_paths=normalized_existing,
            selected_agents=normalized_agents,
            entries=review_entries,
        )
        envelope = LocalCommitStateEnvelope.create(
            generation=0,
            lifecycle_state="reviewed",
            review=review,
            rollback_snapshots=rollback_snapshots,
        )
        write_local_commit_state(envelope, state_root=self._state_root)
        return review

    def create_approval(
        self,
        review: LocalCommitReview,
        *,
        actor: str,
        summary: str,
    ) -> LocalCommitApproval:
        return LocalCommitApproval.create(
            repository_identity=review.repository_identity,
            operation_id=review.operation_id,
            runtime_repository=review.runtime_repository,
            runtime_commit=review.runtime_commit,
            repository_plan_hash=review.repository_plan_hash,
            review_hash=review.review_hash,
            actor=actor,
            summary=summary,
        )

    def apply(
        self,
        review: LocalCommitReview,
        approval: LocalCommitApproval,
    ) -> LocalCommitReceipt:
        envelope = self._load_bound_state(review)
        self._validate_approval(review, approval)
        if envelope.approval is not None and envelope.approval.approval_hash != approval.approval_hash:
            raise BootstrapApplyError("local commit approval does not match the recorded approval binding")
        if envelope.lifecycle_state == "rolled_back":
            raise BootstrapApplyError("local commit apply cannot resume a rolled-back commit stage")
        repository_root = Path(review.repository_root)
        if envelope.receipt is not None:
            self._validate_committed_resume(repository_root, review, envelope.receipt)
            return envelope.receipt
        self._validate_review_snapshot(repository_root, review)
        if not review.entries:
            raise BootstrapApplyError("local commit refuses to create an empty commit")
        branch_head = self._branch_head(repository_root, review.branch_name)
        if branch_head is not None and branch_head != review.base_commit:
            raise BootstrapApplyError("bootstrap branch already exists at an unexpected commit")
        current_branch = self._current_branch(repository_root)
        current_head = self._head_commit(repository_root)
        if current_branch != review.branch_name:
            if branch_head is None:
                self._run_checked(repository_root, "checkout", "-b", review.branch_name)
            else:
                self._run_checked(repository_root, "checkout", review.branch_name)
        elif current_head != review.base_commit:
            raise BootstrapApplyError("local commit apply requires the bootstrap branch to still point at the reviewed base commit")
        reviewed_paths = tuple(entry.path for entry in review.entries)
        self._run_checked(repository_root, "add", "--all", "--", *reviewed_paths)
        staged_paths = self._staged_paths(repository_root)
        staged_keys = {path.casefold() for path in staged_paths}
        reviewed_keys = {path.casefold() for path in reviewed_paths}
        if any(key not in reviewed_keys for key in staged_keys):
            raise BootstrapApplyError("local commit staged an unexpected path outside the reviewed scope")
        if not staged_paths:
            raise BootstrapApplyError("local commit refuses to create an empty commit")
        self._run_checked(
            repository_root,
            "commit",
            "--no-gpg-sign",
            "-m",
            review.proposed_message,
        )
        commit_sha = self._head_commit(repository_root)
        tree_sha = self._tree_sha(repository_root, commit_sha)
        receipt = self._build_receipt(
            repository_root,
            review=review,
            approval=approval,
            commit_sha=commit_sha,
            tree_sha=tree_sha,
        )
        updated = next_local_commit_generation(
            envelope,
            lifecycle_state="committed",
            approval=approval,
            receipt=receipt,
        )
        write_local_commit_state(
            updated,
            expected_generation=envelope.generation,
            expected_generation_hash=envelope.generation_hash,
            state_root=self._state_root,
        )
        return receipt

    def rollback(
        self,
        review: LocalCommitReview,
        approval: LocalCommitApproval,
    ) -> LocalCommitStatus:
        envelope = self._load_bound_state(review)
        self._validate_approval(review, approval)
        if envelope.approval is None or envelope.approval.approval_hash != approval.approval_hash:
            raise BootstrapApplyError("local commit rollback requires the exact recorded approval binding")
        if envelope.receipt is None:
            raise BootstrapApplyError("local commit rollback requires an exact committed local source commit")
        repository_root = Path(review.repository_root)
        if envelope.lifecycle_state == "rolled_back":
            return self.status(review)
        self._validate_committed_resume(repository_root, review, envelope.receipt)
        if review.original_branch is not None:
            original_head = self._branch_head(repository_root, review.original_branch)
            if original_head != review.base_commit:
                raise BootstrapApplyError("local commit rollback requires the original branch to remain at the reviewed base commit")
            self._run_checked(repository_root, "checkout", review.original_branch)
        else:
            self._run_checked(repository_root, "checkout", "--detach", review.base_commit)
        self._restore_review_snapshot(repository_root, envelope.rollback_snapshots)
        updated = next_local_commit_generation(
            envelope,
            lifecycle_state="rolled_back",
        )
        write_local_commit_state(
            updated,
            expected_generation=envelope.generation,
            expected_generation_hash=envelope.generation_hash,
            state_root=self._state_root,
        )
        return self.status(review)

    def status(self, review: LocalCommitReview) -> LocalCommitStatus:
        envelope = self._load_bound_state(review)
        repository_root = Path(review.repository_root)
        if envelope.lifecycle_state == "committed":
            assert envelope.receipt is not None
            self._validate_committed_resume(repository_root, review, envelope.receipt)
            return LocalCommitStatus(
                operation_id=review.operation_id,
                repository_identity=review.repository_identity,
                runtime_repository=review.runtime_repository,
                runtime_commit=review.runtime_commit,
                repository_plan_hash=review.repository_plan_hash,
                review_hash=review.review_hash,
                approval_hash=None if envelope.approval is None else envelope.approval.approval_hash,
                overall_state="committed",
                summary=f"local commit {envelope.receipt.commit_sha[:12]} is ready on {envelope.receipt.branch_name}",
                branch_name=envelope.receipt.branch_name,
                base_commit=review.base_commit,
                commit_sha=envelope.receipt.commit_sha,
                resumable=True,
                rollback_ready=True,
            )
        self._validate_review_snapshot(repository_root, review)
        overall_state: LocalCommitLifecycleState = envelope.lifecycle_state
        summary = "local commit review is ready for approval"
        resumable = True
        rollback_ready = False
        if overall_state == "rolled_back":
            summary = "local commit was rolled back to the reviewed branch and index snapshot"
        return LocalCommitStatus(
            operation_id=review.operation_id,
            repository_identity=review.repository_identity,
            runtime_repository=review.runtime_repository,
            runtime_commit=review.runtime_commit,
            repository_plan_hash=review.repository_plan_hash,
            review_hash=review.review_hash,
            approval_hash=None if envelope.approval is None else envelope.approval.approval_hash,
            overall_state=overall_state,
            summary=summary,
            branch_name=review.branch_name,
            base_commit=review.base_commit,
            commit_sha=None,
            resumable=resumable,
            rollback_ready=rollback_ready,
        )

    def load_state(
        self,
        *,
        repository_identity: str,
        operation_id: str,
        runtime_commit: str,
    ) -> LocalCommitStateEnvelope:
        envelope = read_local_commit_state(
            repository_identity,
            operation_id,
            state_root=self._state_root,
        )
        if envelope.review.runtime_commit != runtime_commit:
            raise BootstrapApplyError("local commit resume requires the exact runtime commit")
        return envelope

    def _load_bound_state(self, review: LocalCommitReview) -> LocalCommitStateEnvelope:
        envelope = self.load_state(
            repository_identity=review.repository_identity,
            operation_id=review.operation_id,
            runtime_commit=review.runtime_commit,
        )
        if envelope.review.review_hash != review.review_hash:
            raise BootstrapApplyError("local commit review does not match the recorded review binding")
        if envelope.review != review:
            raise BootstrapApplyError("local commit review payload does not match the recorded review payload")
        return envelope

    def _try_read_state(
        self,
        repository_identity: str,
        operation_id: str,
    ) -> LocalCommitStateEnvelope | None:
        try:
            return read_local_commit_state(
                repository_identity,
                operation_id,
                state_root=self._state_root,
            )
        except FileNotFoundError:
            return None

    def _validate_existing_request(
        self,
        review: LocalCommitReview,
        *,
        repository_root: Path,
        repository_identity: str,
        runtime_repository: str,
        runtime_commit: str,
        repository_plan_hash: str,
        managed_paths: Sequence[str],
        reviewed_existing_paths: Sequence[str],
        selected_agents: Sequence[LocalCommitSelectedAgent],
        commit_message: str,
        registry_path: str,
    ) -> None:
        expected = LocalCommitReview.create(
            operation_id=review.operation_id,
            repository_root=str(repository_root),
            repository_identity=repository_identity,
            runtime_repository=runtime_repository,
            runtime_commit=runtime_commit,
            repository_plan_hash=repository_plan_hash,
            base_commit=review.base_commit,
            original_branch=review.original_branch,
            branch_name=review.branch_name,
            proposed_message=commit_message,
            registry_path=registry_path,
            managed_paths=managed_paths,
            reviewed_existing_paths=reviewed_existing_paths,
            selected_agents=selected_agents,
            entries=review.entries,
        )
        if expected.model_dump(mode="json", exclude={"review_hash"}) != review.model_dump(mode="json", exclude={"review_hash"}):
            raise BootstrapApplyError("local commit request does not match the recorded review context")

    def _validate_state_against_worktree(
        self,
        envelope: LocalCommitStateEnvelope,
    ) -> None:
        repository_root = Path(envelope.review.repository_root)
        if envelope.lifecycle_state == "committed":
            assert envelope.receipt is not None
            self._validate_committed_resume(repository_root, envelope.review, envelope.receipt)
            return
        self._validate_review_snapshot(repository_root, envelope.review)

    def _validate_review_snapshot(
        self,
        repository_root: Path,
        review: LocalCommitReview,
    ) -> None:
        observed_root = self._repository_root(repository_root)
        if observed_root != repository_root.resolve():
            raise BootstrapApplyError("local commit resume requires the exact repository root")
        observed_identity = self._repository_identity(repository_root)
        if observed_identity != review.repository_identity:
            raise BootstrapApplyError("local commit resume requires the exact repository identity")
        if self._head_commit(repository_root) != review.base_commit:
            raise BootstrapApplyError("local commit resume requires the exact reviewed base commit")
        if self._current_branch(repository_root) != review.original_branch:
            raise BootstrapApplyError("local commit resume requires the exact reviewed branch")
        current_entries, _ = self._capture_review_entries(
            repository_root,
            managed_paths=review.managed_paths,
            reviewed_existing_paths=review.reviewed_existing_paths,
        )
        if current_entries != review.entries:
            raise BootstrapApplyError("local commit review drifted after repository plan review")

    def _validate_committed_resume(
        self,
        repository_root: Path,
        review: LocalCommitReview,
        receipt: LocalCommitReceipt,
    ) -> None:
        observed_identity = self._repository_identity(repository_root)
        if observed_identity != review.repository_identity:
            raise BootstrapApplyError("local commit resume requires the exact repository identity")
        if self._current_branch(repository_root) != receipt.branch_name:
            raise BootstrapApplyError("local commit resume requires the exact bootstrap branch")
        if self._head_commit(repository_root) != receipt.commit_sha:
            raise BootstrapApplyError("local commit resume requires the exact reviewed commit SHA")
        if self._branch_head(repository_root, receipt.branch_name) != receipt.commit_sha:
            raise BootstrapApplyError("local commit resume requires the bootstrap branch ref to remain unchanged")
        if self._status_entries(repository_root):
            raise BootstrapApplyError("local commit resume requires a clean committed worktree and index")

    def _validate_approval(
        self,
        review: LocalCommitReview,
        approval: LocalCommitApproval,
    ) -> None:
        if (
            approval.repository_identity != review.repository_identity
            or approval.operation_id != review.operation_id
            or approval.runtime_repository != review.runtime_repository
            or approval.runtime_commit != review.runtime_commit
            or approval.repository_plan_hash != review.repository_plan_hash
            or approval.review_hash != review.review_hash
        ):
            raise BootstrapApplyError("local commit approval does not match the exact review, runtime, and repository plan")

    def _resolve_selected_agents(
        self,
        repository_root: Path,
        *,
        selected_agents: Sequence[LocalCommitSelectedAgent] | None,
        selected_agent_ids: Sequence[str],
        registry_path: str,
    ) -> tuple[LocalCommitSelectedAgent, ...]:
        if selected_agents is not None:
            payload = tuple(LocalCommitSelectedAgent.model_validate(item) for item in selected_agents)
            if selected_agent_ids:
                ids = {item.repo_agent_id.casefold() for item in payload}
                expected = {item.casefold() for item in selected_agent_ids}
                if ids != expected:
                    raise BootstrapApplyError("selected_agents does not match selected_agent_ids")
        else:
            payload = selected_local_commit_agents_from_registry(
                repository_root,
                repo_agent_ids=selected_agent_ids,
                registry_path=registry_path,
            )
        for agent in payload:
            profile_path = repository_root / agent.profile_path
            if not profile_path.is_file():
                raise BootstrapApplyError("local commit review requires every selected agent profile to exist")
        registry_file = repository_root / registry_path
        if not registry_file.is_file():
            raise BootstrapApplyError("local commit review requires a committed registry file")
        return payload

    def _build_receipt(
        self,
        repository_root: Path,
        *,
        review: LocalCommitReview,
        approval: LocalCommitApproval,
        commit_sha: str,
        tree_sha: str,
    ) -> LocalCommitReceipt:
        registry_sha256 = self._file_sha256(repository_root / review.registry_path)
        profile_hashes = tuple(
            LocalCommitProfileHash(
                repo_agent_id=agent.repo_agent_id,
                profile_path=agent.profile_path,
                sha256=self._file_sha256(repository_root / agent.profile_path),
            )
            for agent in review.selected_agents
        )
        cache = _ScanCache(repository_root)
        agent_hashes = []
        for agent in review.selected_agents:
            sidecar = BootstrapSidecar.from_document(
                (repository_root / agent.profile_path).read_text(encoding="utf-8")
            )
            if sidecar.repo_agent_id != agent.repo_agent_id:
                raise BootstrapApplyError("selected agent profile does not match the reviewed repoAgentId")
            agent_hashes.append(
                LocalCommitAgentHash(
                    repo_agent_id=agent.repo_agent_id,
                    source_root=sidecar.source_root,
                    source_sha256=_fingerprint_root(cache, sidecar.source_root),
                    package_root=sidecar.package_root,
                    package_sha256=_fingerprint_root(cache, sidecar.package_root),
                )
            )
        return LocalCommitReceipt.create(
            operation_id=review.operation_id,
            repository_identity=review.repository_identity,
            runtime_repository=review.runtime_repository,
            runtime_commit=review.runtime_commit,
            repository_plan_hash=review.repository_plan_hash,
            review_hash=review.review_hash,
            approval_hash=approval.approval_hash,
            base_commit=review.base_commit,
            branch_name=review.branch_name,
            commit_sha=commit_sha,
            tree_sha=tree_sha,
            commit_message=review.proposed_message,
            registry_path=review.registry_path,
            registry_sha256=registry_sha256,
            committed_paths=tuple(entry.path for entry in review.entries),
            profile_hashes=profile_hashes,
            agent_hashes=tuple(agent_hashes),
        )

    def _capture_review_entries(
        self,
        repository_root: Path,
        *,
        managed_paths: Sequence[str],
        reviewed_existing_paths: Sequence[str],
    ) -> tuple[tuple[LocalCommitWorktreeEntry, ...], tuple[LocalCommitRollbackSnapshot, ...]]:
        managed = {path.casefold(): path for path in managed_paths}
        reviewed_existing = {path.casefold(): path for path in reviewed_existing_paths}
        allowed = {**managed, **reviewed_existing}
        raw_entries = self._status_entries(repository_root)
        current_dirty = {entry.path.casefold(): entry.path for entry in raw_entries}
        unexpected = sorted(
            path
            for key, path in current_dirty.items()
            if key not in allowed
        )
        if unexpected:
            raise BootstrapApplyError(
                f"local commit review refuses unrelated dirty paths: {unexpected[0]}"
            )
        for key, path in reviewed_existing.items():
            if key not in current_dirty:
                raise BootstrapApplyError(
                    f"reviewed existing path is not currently dirty: {path}"
                )
        entries: list[LocalCommitWorktreeEntry] = []
        rollback_snapshots: list[LocalCommitRollbackSnapshot] = []
        for raw in raw_entries:
            worktree_path = repository_root / raw.path
            exists = worktree_path.exists()
            worktree_sha = None if not exists else self._file_sha256(worktree_path)
            index_object_id = self._index_object_id(repository_root, raw.path)
            review_kind: LocalCommitReviewKind = "managed"
            if raw.path.casefold() in reviewed_existing and raw.path.casefold() not in managed:
                review_kind = "existing"
            entry = LocalCommitWorktreeEntry(
                review_kind=review_kind,
                path=raw.path,
                index_status=raw.index_status,
                worktree_status=raw.worktree_status,
                index_object_id=index_object_id,
                worktree_sha256=worktree_sha,
            )
            entries.append(entry)
            rollback_snapshots.append(
                LocalCommitRollbackSnapshot(
                    path=raw.path,
                    exists=exists,
                    bytes_b64=(
                        None
                        if not exists
                        else base64.b64encode(worktree_path.read_bytes()).decode("ascii")
                    ),
                    index_status=raw.index_status,
                    worktree_status=raw.worktree_status,
                )
            )
        return (
            tuple(sorted(entries, key=lambda item: (item.path.casefold(), item.path))),
            tuple(sorted(rollback_snapshots, key=lambda item: (item.path.casefold(), item.path))),
        )

    def _restore_review_snapshot(
        self,
        repository_root: Path,
        snapshots: Sequence[LocalCommitRollbackSnapshot],
    ) -> None:
        staged_paths: list[str] = []
        for snapshot in sorted(snapshots, key=lambda item: (item.path.casefold(), item.path)):
            target = repository_root / snapshot.path
            target.parent.mkdir(parents=True, exist_ok=True)
            if snapshot.exists:
                target.write_bytes(snapshot.decoded_content() or b"")
            elif target.exists():
                if target.is_dir():
                    raise BootstrapApplyError("local commit rollback cannot replace a directory with a reviewed file path")
                target.unlink()
            if snapshot.index_status not in {" ", "?"}:
                staged_paths.append(snapshot.path)
        if staged_paths:
            self._run_checked(repository_root, "add", "--all", "--", *staged_paths)

    def _repository_root(self, repository: Path) -> Path:
        return Path(self._run_text(repository, "rev-parse", "--show-toplevel")).resolve()

    def _repository_identity(self, repository_root: Path) -> str:
        remote = self._run_text(repository_root, "remote", "get-url", "origin")
        owner, repo = _github_remote_identity(remote)
        return f"{owner}/{repo}"

    def _head_commit(self, repository_root: Path) -> str:
        return self._run_text(repository_root, "rev-parse", "HEAD")

    def _current_branch(self, repository_root: Path) -> str | None:
        result = self._git.run(repository_root, "symbolic-ref", "--quiet", "--short", "HEAD")
        if result.returncode == 1:
            return None
        if result.returncode != 0:
            message = result.stderr_text.strip() or result.stdout_text.strip() or "git symbolic-ref failed"
            raise BootstrapApplyError(message)
        return _normalize_branch_name(result.stdout_text.strip())

    def _branch_head(self, repository_root: Path, branch_name: str) -> str | None:
        result = self._git.run(repository_root, "rev-parse", "--verify", f"refs/heads/{branch_name}")
        if result.returncode != 0:
            return None
        return result.stdout_text.strip()

    def _tree_sha(self, repository_root: Path, commit_sha: str) -> str:
        return self._run_text(repository_root, "rev-parse", f"{commit_sha}^{{tree}}")

    def _status_entries(self, repository_root: Path) -> tuple["_RawStatusEntry", ...]:
        result = self._run_checked(repository_root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
        data = result.stdout
        if not data:
            return ()
        entries: list[_RawStatusEntry] = []
        index = 0
        while index < len(data):
            if index + 3 > len(data):
                raise BootstrapApplyError("git status output is truncated")
            index_status = chr(data[index])
            worktree_status = chr(data[index + 1])
            if data[index + 2] != 32:
                raise BootstrapApplyError("git status output is malformed")
            index += 3
            terminator = data.find(b"\0", index)
            if terminator < 0:
                raise BootstrapApplyError("git status output is truncated")
            path = data[index:terminator].decode("utf-8", errors="replace").replace("\\", "/")
            index = terminator + 1
            if any(status in {"R", "C", "U"} for status in (index_status, worktree_status)):
                raise BootstrapApplyError("local commit review does not support renamed, copied, or unmerged paths")
            entries.append(
                _RawStatusEntry(
                    path=validate_repository_relative_path(path, field="dirty_path"),
                    index_status=index_status,
                    worktree_status=worktree_status,
                )
            )
        return tuple(sorted(entries, key=lambda item: (item.path.casefold(), item.path)))

    def _staged_paths(self, repository_root: Path) -> tuple[str, ...]:
        result = self._run_checked(repository_root, "diff", "--cached", "--name-only", "-z")
        if not result.stdout:
            return ()
        values = [
            validate_repository_relative_path(item.decode("utf-8", errors="replace").replace("\\", "/"), field="staged_path")
            for item in result.stdout.split(b"\0")
            if item
        ]
        return tuple(sorted(values, key=lambda item: (item.casefold(), item)))

    def _index_object_id(self, repository_root: Path, repo_path: str) -> str | None:
        result = self._git.run(repository_root, "ls-files", "--stage", "--", repo_path)
        if result.returncode != 0:
            message = result.stderr_text.strip()
            if message:
                raise BootstrapApplyError(message)
            return None
        lines = [line.strip() for line in result.stdout_text.splitlines() if line.strip()]
        if not lines:
            return None
        if len(lines) != 1:
            raise BootstrapApplyError("local commit review does not support conflicted index entries")
        fields = lines[0].split()
        if len(fields) < 3:
            raise BootstrapApplyError("git ls-files --stage returned malformed output")
        return fields[1]

    def _file_sha256(self, path: Path) -> str:
        if not path.is_file():
            raise BootstrapApplyError(f"required local commit input path is missing: {path}")
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _run_text(self, repository_root: Path, *args: str) -> str:
        result = self._run_checked(repository_root, *args)
        return result.stdout_text.strip()

    def _run_checked(self, repository_root: Path, *args: str) -> GitCommandResult:
        result = self._git.run(repository_root, *args)
        if result.returncode != 0:
            message = result.stderr_text.strip() or result.stdout_text.strip() or "git command failed"
            raise BootstrapApplyError(message)
        return result


@dataclass(frozen=True, slots=True)
class _RawStatusEntry:
    path: str
    index_status: str
    worktree_status: str


class BootstrapLocalCommitHandler:
    def __init__(
        self,
        *,
        coordinator: LocalGitCommitCoordinator | None = None,
    ) -> None:
        self._coordinator = coordinator or LocalGitCommitCoordinator()

    def review(self, *, operation) -> LocalCommitReview:
        context = self._context(operation)
        commit_agent_ids = context.get("commit_agent_ids")
        selected_agent_ids = (
            operation.selection_plan.selected_agent_ids
            if commit_agent_ids is None
            else cast(Sequence[str], commit_agent_ids)
        )
        return self._coordinator.build_review(
            operation.repository_binding.repository_root,
            operation_id=operation.operation_id,
            repository_identity=operation.repository_binding.repository_id,
            runtime_repository=operation.runtime_binding.runtime_repository,
            runtime_commit=operation.runtime_binding.runtime_commit,
            repository_plan_hash=str(context["repository_plan_hash"]),
            managed_paths=cast(Sequence[str], context["managed_paths"]),
            reviewed_existing_paths=cast(Sequence[str], context.get("reviewed_existing_paths", ())),
            selected_agent_ids=selected_agent_ids,
            commit_message=cast(str | None, context.get("commit_summary")),
        )

    def validate_resume(self, *, operation) -> None:
        review = self.review(operation=operation)
        self._coordinator.status(review)

    def approve(self, *, operation, approval) -> object:
        from foundry_opt.bootstrap.runner import BootstrapChildReference, BootstrapStageOutcome

        review = self.review(operation=operation)
        local_approval = self._coordinator.create_approval(
            review,
            actor=approval.actor,
            summary=approval.summary,
        )
        receipt = self._coordinator.apply(review, local_approval)
        next_stage = self._next_stage(operation)
        child_refs = tuple(item for item in operation.child_refs if item.step != "commit")
        return BootstrapStageOutcome(
            stage=next_stage,
            note=f"Created reviewed local commit {receipt.commit_sha[:12]} on {receipt.branch_name}.",
            child_refs=(
                *child_refs,
                BootstrapChildReference(
                    step="commit",
                    kind="local-reviewed-commit",
                    identifier=receipt.commit_sha,
                    summary=f"{receipt.branch_name} @ {receipt.commit_sha[:12]}",
                ),
            ),
            repository_binding=operation.repository_binding.model_copy(
                update={
                    "head_commit": receipt.commit_sha,
                    "branch_name": receipt.branch_name,
                }
            ),
        )

    def rollback(self, *, operation, step, child_ref) -> object:
        from foundry_opt.bootstrap.runner import BootstrapStageOutcome

        if step != "commit" or child_ref.step != "commit":
            raise BootstrapApplyError("local commit handler can only roll back the commit step")
        review = self.review(operation=operation)
        state = self._coordinator.load_state(
            repository_identity=review.repository_identity,
            operation_id=review.operation_id,
            runtime_commit=review.runtime_commit,
        )
        if state.approval is None:
            raise BootstrapApplyError("local commit rollback requires a recorded approval")
        self._coordinator.rollback(review, state.approval)
        remaining = tuple(item for item in operation.child_refs if item.step != "commit")
        return BootstrapStageOutcome(
            stage="rolled_back",
            note="Rolled back the reviewed local commit and restored the original branch/index snapshot.",
            child_refs=remaining,
            repository_binding=operation.repository_binding.model_copy(
                update={
                    "head_commit": review.base_commit,
                    "branch_name": review.original_branch,
                }
            ),
        )

    def _context(self, operation) -> Mapping[str, object]:
        context = operation.handler_context.get(LOCAL_COMMIT_CONTEXT_KEY)
        if not isinstance(context, Mapping):
            raise BootstrapApplyError("local commit handler requires a recorded local_commit handler_context")
        if "repository_plan_hash" not in context or "managed_paths" not in context:
            raise BootstrapApplyError("local commit handler_context must include repository_plan_hash and managed_paths")
        if not operation.selection_plan.selected_agent_ids:
            raise BootstrapApplyError("local commit review requires selected_agent_ids")
        return context

    def _next_stage(self, operation) -> LocalCommitNextStage:
        value = self._context(operation).get("next_stage", "deployment_approval")
        if value not in {"deployment_approval", "final_handoff"}:
            raise BootstrapApplyError("local commit handler_context carries an unsupported next_stage")
        return cast(LocalCommitNextStage, value)


__all__ = [
    "BootstrapLocalCommitHandler",
    "GitCommandAdapterProtocol",
    "GitCommandResult",
    "LOCAL_COMMIT_CONTEXT_KEY",
    "LocalCommitAgentHash",
    "LocalCommitApproval",
    "LocalCommitLifecycleState",
    "LocalCommitNextStage",
    "LocalCommitProfileHash",
    "LocalCommitReceipt",
    "LocalCommitReview",
    "LocalCommitRollbackSnapshot",
    "LocalCommitSelectedAgent",
    "LocalCommitStateEnvelope",
    "LocalCommitStatus",
    "LocalCommitWorktreeEntry",
    "LocalGitCommitCoordinator",
    "SubprocessGitCommandAdapter",
    "bootstrap_branch_name",
    "build_local_commit_context",
    "default_local_commit_message",
    "default_local_commit_state_root",
    "load_local_commit_state",
    "lock_file_path",
    "next_local_commit_generation",
    "operation_directory",
    "read_local_commit_state",
    "selected_local_commit_agents_from_registry",
    "state_file_path",
    "write_local_commit_state",
]


def load_local_commit_state(
    repository_identity: str,
    operation_id: str,
    *,
    state_root: Path | None = None,
) -> LocalCommitStateEnvelope:
    return read_local_commit_state(
        repository_identity,
        operation_id,
        state_root=state_root,
    )
