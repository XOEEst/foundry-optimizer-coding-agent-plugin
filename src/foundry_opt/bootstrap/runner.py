from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Protocol, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from foundry_opt.bootstrap.canonical import canonical_json_bytes, canonical_sha256, safe_persisted_document
from foundry_opt.bootstrap.contracts import (
    AgentId,
    BootstrapDocument,
    BootstrapSidecar,
    ReviewedFoundryTarget,
    RootRegistry,
)
from foundry_opt.bootstrap.discovery import DiscoveryResult, discover_repository_agents
from foundry_opt.bootstrap.errors import BootstrapApplyError, BootstrapConfigError
from foundry_opt.bootstrap.operation_state import DiscoveredAgentRecord, DiscoveryBlockerRecord, SelectionPlan
from foundry_opt.bootstrap.owner_review import ResourceLinksReview, build_discovery_review, build_resource_links
from foundry_opt.bootstrap.shared import github_remote_identity, require_safe_operation_id, resolve_state_child_directory, resolve_state_root, runtime_commit_from_environment, runtime_repository_from_environment, scoped_state_root
from foundry_opt.bootstrap.state_lock import (
    atomic_replace_state,
    state_file_lock,
)
from foundry_opt.models import FrozenModel

BootstrapQuestionKind = Literal[
    "agent_selection",
    "register_enable",
    "foundry_target",
    "verification_policy",
    "repository_approval",
    "connection_approval",
    "commit_approval",
    "deployment_approval",
]
BootstrapApprovalStep = Literal["repository", "connection", "commit", "deployment"]
BootstrapLifecycleStage = Literal[
    "preflight",
    "discovery_review",
    "blocked",
    "agent_selection",
    "foundry_target_resolution",
    "register_enable",
    "verification_policy",
    "repository_approval",
    "connection_approval",
    "commit_approval",
    "deployment_approval",
    "final_handoff",
    "rolled_back",
]
BootstrapActionName = Literal["answer", "approve", "status", "rollback"]
BootstrapRegistrationIntentKind = Literal[
    "ignore",
    "register_disabled",
    "register_enabled",
]
BootstrapVerificationChoiceKind = Literal[
    "preserve_existing",
    "defer_to_issue",
    "repository_checks",
    "no_evidence",
]

_STATE_FILE_NAME = "state.json"
_LOCK_FILE_NAME = "state.lock"
_REPOSITORY_INDEX_DIRECTORY_NAME = "repositories"
_REPOSITORY_INDEX_FILE_NAME = "active-operation.json"
_REPOSITORY_INDEX_LOCK_FILE_NAME = "active-operation.lock"
_MAX_STATE_BYTES = 1024 * 1024
_MAX_REPOSITORY_INDEX_BYTES = 16 * 1024
_TERMINAL_LIFECYCLE_STAGES = frozenset({"final_handoff", "rolled_back"})
_MAX_PARENT_CAS_ATTEMPTS = 8
_QUESTION_KIND_BY_STAGE: dict[BootstrapLifecycleStage, BootstrapQuestionKind] = {
    "agent_selection": "agent_selection",
    "foundry_target_resolution": "foundry_target",
    "register_enable": "register_enable",
    "verification_policy": "verification_policy",
    "repository_approval": "repository_approval",
    "connection_approval": "connection_approval",
    "commit_approval": "commit_approval",
    "deployment_approval": "deployment_approval",
}
_APPROVAL_STAGE_BY_STEP: dict[BootstrapApprovalStep, BootstrapLifecycleStage] = {
    "repository": "repository_approval",
    "connection": "connection_approval",
    "commit": "commit_approval",
    "deployment": "deployment_approval",
}
_ALLOWED_NEXT_STAGES: dict[BootstrapLifecycleStage, frozenset[BootstrapLifecycleStage]] = {
    "preflight": frozenset({"discovery_review", "blocked"}),
    "discovery_review": frozenset({"agent_selection", "blocked"}),
    "agent_selection": frozenset(
        {"agent_selection", "register_enable", "foundry_target_resolution", "blocked"}
    ),
    "foundry_target_resolution": frozenset(
        {
            "foundry_target_resolution",
            "register_enable",
            "verification_policy",
            "repository_approval",
            "final_handoff",
            "blocked",
        }
    ),
    "register_enable": frozenset(
        {
            "register_enable",
            "foundry_target_resolution",
            "verification_policy",
            "repository_approval",
            "final_handoff",
            "blocked",
        }
    ),
    "verification_policy": frozenset(
        {
            "verification_policy",
            "repository_approval",
            "blocked",
        }
    ),
    "repository_approval": frozenset(
        {
            "repository_approval",
            "connection_approval",
            "commit_approval",
            "final_handoff",
            "blocked",
            "rolled_back",
        }
    ),
    "connection_approval": frozenset(
        {
            "connection_approval",
            "commit_approval",
            "deployment_approval",
            "final_handoff",
            "blocked",
            "rolled_back",
        }
    ),
    "commit_approval": frozenset(
        {
            "commit_approval",
            "deployment_approval",
            "final_handoff",
            "blocked",
            "rolled_back",
        }
    ),
    "deployment_approval": frozenset(
        {
            "deployment_approval",
            "final_handoff",
            "blocked",
            "rolled_back",
        }
    ),
    "final_handoff": frozenset({"final_handoff", "rolled_back"}),
    "blocked": frozenset({"blocked"}),
    "rolled_back": frozenset({"rolled_back"}),
}


class BootstrapQuestionChoice(FrozenModel):
    value: str
    label: str
    detail: str | None = None


class BootstrapQuestion(FrozenModel):
    question_id: str
    kind: BootstrapQuestionKind
    title: str
    details_markdown: str
    required_fields: tuple[str, ...] = ()
    allow_multiple: bool = False
    choices: tuple[BootstrapQuestionChoice, ...] = ()


class BootstrapAvailableAction(FrozenModel):
    name: BootstrapActionName
    step: BootstrapApprovalStep | None = None


class BootstrapTurn(FrozenModel):
    owner_markdown: str
    next_question: BootstrapQuestion | None = None
    available_actions: tuple[BootstrapAvailableAction, ...]
    operation_id: str
    state: BootstrapLifecycleStage
    resource_links: ResourceLinksReview


@dataclass(frozen=True, slots=True)
class _ExistingSidecarInspection:
    path: str
    profile: BootstrapSidecar | None = None
    error: str | None = None


class RepositoryBinding(BootstrapDocument):
    repository_root: str
    repository_id: str
    repository_url: str
    head_commit: str
    branch_name: str | None = None

    @field_validator("repository_root")
    @classmethod
    def _validate_repository_root(cls, value: str) -> str:
        if not value:
            raise BootstrapConfigError("repository_root is required")
        return value

    @field_validator("repository_id")
    @classmethod
    def _validate_repository_id(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value) is None:
            raise BootstrapConfigError("repository_id must be owner/repo")
        return value

    @field_validator("repository_url")
    @classmethod
    def _validate_repository_url(cls, value: str) -> str:
        if re.fullmatch(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?", value) is None:
            raise BootstrapConfigError("repository_url must be a canonical https GitHub repository URL")
        return value

    @field_validator("head_commit")
    @classmethod
    def _validate_head_commit(cls, value: str) -> str:
        if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is None:
            raise BootstrapConfigError("head_commit must be a git commit SHA")
        return value

    @field_validator("branch_name")
    @classmethod
    def _validate_branch_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise BootstrapConfigError("branch_name must not be empty")
        if any(ord(ch) < 32 for ch in normalized) or "\x7f" in normalized:
            raise BootstrapConfigError("branch_name contains control characters")
        return normalized


class RuntimeBinding(BootstrapDocument):
    runtime_repository: str
    runtime_commit: str

    @field_validator("runtime_repository")
    @classmethod
    def _validate_runtime_repository(cls, value: str) -> str:
        if not value:
            raise BootstrapConfigError("runtime_repository is required")
        return value

    @field_validator("runtime_commit")
    @classmethod
    def _validate_runtime_commit(cls, value: str) -> str:
        if re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is None:
            raise BootstrapConfigError("runtime_commit must be a git commit SHA")
        return value


class BootstrapAnswerRecord(BootstrapDocument):
    question_id: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    kind: BootstrapQuestionKind
    value: str | bool | tuple[str, ...] | Mapping[str, str]
    answered_at: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    state_generation: int = Field(ge=0)
    state_generation_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

    @field_validator("value")
    @classmethod
    def _validate_value(cls, value: object) -> object:
        safe_persisted_document({"value": value})
        return value


class BootstrapApprovalRecord(BootstrapDocument):
    step: BootstrapApprovalStep
    actor: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    summary: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    approved_at: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    state_generation: int = Field(ge=0)
    state_generation_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    approval_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

    @classmethod
    def create(
        cls,
        *,
        step: BootstrapApprovalStep,
        actor: str,
        summary: str,
        approved_at: str,
        state_generation: int,
        state_generation_hash: str,
    ) -> "BootstrapApprovalRecord":
        payload = {
            "step": step,
            "actor": actor,
            "summary": summary,
            "approved_at": approved_at,
            "state_generation": state_generation,
            "state_generation_hash": state_generation_hash,
        }
        safe_persisted_document(payload)
        return cls.model_validate({**payload, "approval_hash": canonical_sha256(payload)})

    @model_validator(mode="after")
    def _validate_hash(self) -> Self:
        payload = {
            "step": self.step,
            "actor": self.actor,
            "summary": self.summary,
            "approved_at": self.approved_at,
            "state_generation": self.state_generation,
            "state_generation_hash": self.state_generation_hash,
        }
        if self.approval_hash != canonical_sha256(payload):
            raise BootstrapApplyError("approval_hash does not match the approval payload")
        return self


class BootstrapFoundryTargetRecord(BootstrapDocument):
    repo_agent_id: AgentId
    root: Annotated[str, StringConstraints(min_length=1, max_length=240)]
    reviewed_target: ReviewedFoundryTarget

    @model_validator(mode="after")
    def _validate_safe(self) -> Self:
        safe_persisted_document(
            {
                "repo_agent_id": self.repo_agent_id,
                "root": self.root,
                "reviewed_target": self.reviewed_target.model_dump(mode="json"),
            }
        )
        return self


class BootstrapRegistrationIntent(BootstrapDocument):
    repo_agent_id: AgentId
    intent: BootstrapRegistrationIntentKind


class BootstrapVerificationChoice(BootstrapDocument):
    repo_agent_id: AgentId
    choice: BootstrapVerificationChoiceKind


class BootstrapChildReference(BootstrapDocument):
    step: BootstrapApprovalStep
    kind: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    identifier: Annotated[str, StringConstraints(min_length=1, max_length=512)]
    summary: Annotated[str, StringConstraints(min_length=1, max_length=512)] | None = None

    @model_validator(mode="after")
    def _validate_safe(self) -> Self:
        safe_persisted_document(
            {
                "step": self.step,
                "kind": self.kind,
                "identifier": self.identifier,
                "summary": self.summary,
            }
        )
        return self


class BootstrapStageOutcome(FrozenModel):
    stage: BootstrapLifecycleStage
    note: str | None = None
    child_refs: tuple[BootstrapChildReference, ...] | None = None
    foundry_targets: tuple[BootstrapFoundryTargetRecord, ...] | None = None
    repository_binding: RepositoryBinding | None = None
    handler_context: Mapping[str, object] | None = None


class BootstrapRunnerStatePayload(BootstrapDocument):
    generation: int = Field(ge=0)
    operation_id: str
    lifecycle_stage: BootstrapLifecycleStage
    started_at: str
    updated_at: str
    repository_binding: RepositoryBinding
    runtime_binding: RuntimeBinding
    selection_plan: SelectionPlan
    answers: tuple[BootstrapAnswerRecord, ...] = ()
    approvals: tuple[BootstrapApprovalRecord, ...] = ()
    foundry_targets: tuple[BootstrapFoundryTargetRecord, ...] = ()
    registration_intents: tuple[BootstrapRegistrationIntent, ...] = ()
    verification_choices: tuple[BootstrapVerificationChoice, ...] = ()
    child_refs: tuple[BootstrapChildReference, ...] = ()
    note: str | None = Field(default=None, max_length=4096)
    handler_context: Mapping[str, object] = Field(default_factory=dict)

    @field_validator("operation_id")
    @classmethod
    def _validate_operation_id(cls, value: str) -> str:
        return require_safe_operation_id(
            value,
            message="operation_id is invalid",
            error_factory=BootstrapConfigError,
        )

    @field_validator("child_refs")
    @classmethod
    def _validate_child_refs(
        cls,
        value: Sequence[BootstrapChildReference],
    ) -> tuple[BootstrapChildReference, ...]:
        refs = tuple(value)
        seen: set[str] = set()
        for item in refs:
            key = item.step.casefold()
            if key in seen:
                raise BootstrapConfigError("child_refs must not contain duplicate steps")
            seen.add(key)
        return refs

    @field_validator("foundry_targets")
    @classmethod
    def _validate_foundry_targets(
        cls,
        value: Sequence[BootstrapFoundryTargetRecord],
    ) -> tuple[BootstrapFoundryTargetRecord, ...]:
        records = tuple(value)
        seen: set[str] = set()
        for item in records:
            key = item.repo_agent_id.casefold()
            if key in seen:
                raise BootstrapConfigError("foundry_targets must not contain duplicate repo_agent_id values")
            seen.add(key)
        return tuple(sorted(records, key=lambda item: item.repo_agent_id.casefold()))

    @field_validator("registration_intents")
    @classmethod
    def _validate_registration_intents(
        cls,
        value: Sequence[BootstrapRegistrationIntent],
    ) -> tuple[BootstrapRegistrationIntent, ...]:
        records = tuple(value)
        keys = [item.repo_agent_id.casefold() for item in records]
        if len(keys) != len(set(keys)):
            raise BootstrapConfigError(
                "registration_intents must not contain duplicate repo_agent_id values"
            )
        return tuple(
            sorted(records, key=lambda item: item.repo_agent_id.casefold())
        )

    @field_validator("verification_choices")
    @classmethod
    def _validate_verification_choices(
        cls,
        value: Sequence[BootstrapVerificationChoice],
    ) -> tuple[BootstrapVerificationChoice, ...]:
        records = tuple(value)
        keys = [item.repo_agent_id.casefold() for item in records]
        if len(keys) != len(set(keys)):
            raise BootstrapConfigError(
                "verification_choices must not contain duplicate repo_agent_id values"
            )
        return tuple(
            sorted(records, key=lambda item: item.repo_agent_id.casefold())
        )

    @field_validator("handler_context")
    @classmethod
    def _validate_handler_context(cls, value: Mapping[str, object]) -> Mapping[str, object]:
        normalized = dict(value)
        safe_persisted_document(normalized)
        return normalized


class BootstrapRunnerStateEnvelope(BootstrapDocument):
    payload: BootstrapRunnerStatePayload
    generation_hash: str

    @property
    def generation(self) -> int:
        return self.payload.generation

    @property
    def operation_id(self) -> str:
        return self.payload.operation_id

    @property
    def lifecycle_stage(self) -> BootstrapLifecycleStage:
        return self.payload.lifecycle_stage

    @property
    def repository_binding(self) -> RepositoryBinding:
        return self.payload.repository_binding

    @property
    def runtime_binding(self) -> RuntimeBinding:
        return self.payload.runtime_binding

    @property
    def selection_plan(self) -> SelectionPlan:
        return self.payload.selection_plan

    @property
    def answers(self) -> tuple[BootstrapAnswerRecord, ...]:
        return self.payload.answers

    @property
    def approvals(self) -> tuple[BootstrapApprovalRecord, ...]:
        return self.payload.approvals

    @property
    def foundry_targets(self) -> tuple[BootstrapFoundryTargetRecord, ...]:
        return self.payload.foundry_targets

    @property
    def registration_intents(self) -> tuple[BootstrapRegistrationIntent, ...]:
        return self.payload.registration_intents

    @property
    def verification_choices(self) -> tuple[BootstrapVerificationChoice, ...]:
        return self.payload.verification_choices

    @property
    def child_refs(self) -> tuple[BootstrapChildReference, ...]:
        return self.payload.child_refs

    @property
    def note(self) -> str | None:
        return self.payload.note

    @property
    def handler_context(self) -> Mapping[str, object]:
        return self.payload.handler_context

    @classmethod
    def create(cls, **values: object) -> "BootstrapRunnerStateEnvelope":
        payload = BootstrapRunnerStatePayload.model_validate(values)
        payload_json = payload.model_dump(mode="json")
        digest = canonical_sha256({"payload": payload_json})
        return cls.model_validate({"payload": payload_json, "generation_hash": digest})

    @model_validator(mode="after")
    def _validate_hash(self) -> Self:
        if len(self.generation_hash) != 64 or any(ch not in "0123456789abcdef" for ch in self.generation_hash):
            raise BootstrapApplyError("generation_hash must be a lowercase sha256 digest")
        if self.generation_hash != canonical_sha256({"payload": self.payload.model_dump(mode="json")}):
            raise BootstrapApplyError("generation_hash does not match the state payload")
        return self


class BootstrapRepositoryOperationIndex(BootstrapDocument):
    repository_id: str
    operation_id: str
    index_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]

    @field_validator("repository_id")
    @classmethod
    def _validate_repository_id(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value) is None:
            raise BootstrapConfigError(
                "repository operation index has an invalid repository_id"
            )
        return value

    @field_validator("operation_id")
    @classmethod
    def _validate_operation_id(cls, value: str) -> str:
        return require_safe_operation_id(
            value,
            message="repository operation index has an invalid operation_id",
            error_factory=BootstrapConfigError,
        )

    @classmethod
    def create(
        cls,
        *,
        repository_id: str,
        operation_id: str,
    ) -> "BootstrapRepositoryOperationIndex":
        payload = {
            "repository_id": repository_id,
            "operation_id": operation_id,
        }
        return cls.model_validate(
            {
                **payload,
                "index_hash": canonical_sha256(payload),
            }
        )

    @model_validator(mode="after")
    def _validate_hash(self) -> Self:
        payload = {
            "repository_id": self.repository_id,
            "operation_id": self.operation_id,
        }
        if self.index_hash != canonical_sha256(payload):
            raise BootstrapApplyError(
                "repository operation index hash does not match its payload"
            )
        return self


class FilesystemProtocol(Protocol):
    def resolve_directory(self, value: str | Path) -> Path: ...


class GitProtocol(Protocol):
    def repository_root(self, value: Path) -> Path: ...
    def repository_url(self, value: Path) -> str: ...
    def repository_id(self, repository_url: str) -> str: ...
    def head_commit(self, value: Path) -> str: ...
    def current_branch(self, value: Path) -> str | None: ...


class RuntimeBindingProtocol(Protocol):
    def runtime_repository(self) -> str: ...
    def runtime_commit(self) -> str: ...


class ClockProtocol(Protocol):
    def now(self) -> datetime: ...


class BootstrapRunnerStateStoreProtocol(Protocol):
    def load(self, operation_id: str) -> BootstrapRunnerStateEnvelope: ...
    def find_active(
        self,
        repository_id: str,
    ) -> BootstrapRunnerStateEnvelope | None: ...
    def start_or_resume(
        self,
        envelope: BootstrapRunnerStateEnvelope,
    ) -> BootstrapRunnerStateEnvelope: ...
    def save(
        self,
        envelope: BootstrapRunnerStateEnvelope,
        *,
        expected_generation: int | None = None,
        expected_generation_hash: str | None = None,
    ) -> None: ...


class GitHubBridgeProtocol(Protocol):
    pass


class AzureBridgeProtocol(Protocol):
    pass


class FoundryBridgeProtocol(Protocol):
    pass


class FoundryTargetResolutionHandlerProtocol(Protocol):
    def prepare(
        self,
        *,
        operation: BootstrapRunnerStateEnvelope,
    ) -> BootstrapStageOutcome: ...

    def build_question(
        self,
        *,
        operation: BootstrapRunnerStateEnvelope,
        question_id: str,
    ) -> BootstrapQuestion | None: ...

    def render_owner_markdown(
        self,
        *,
        operation: BootstrapRunnerStateEnvelope,
    ) -> str | None: ...

    def build_resource_links(
        self,
        *,
        operation: BootstrapRunnerStateEnvelope,
    ) -> ResourceLinksReview | None: ...

    def persisted_answer_value(
        self,
        *,
        operation: BootstrapRunnerStateEnvelope,
        answer: object,
    ) -> Mapping[str, str]: ...

    def handle_answer(
        self,
        *,
        operation: BootstrapRunnerStateEnvelope,
        answer: object,
    ) -> BootstrapStageOutcome: ...


class BootstrapApprovalHandlerProtocol(Protocol):
    def approve(
        self,
        *,
        operation: BootstrapRunnerStateEnvelope,
        approval: BootstrapApprovalRecord,
    ) -> BootstrapStageOutcome: ...


class BootstrapRollbackHandlerProtocol(Protocol):
    def rollback(
        self,
        *,
        operation: BootstrapRunnerStateEnvelope,
        step: BootstrapApprovalStep,
        child_ref: BootstrapChildReference,
    ) -> BootstrapStageOutcome: ...


class RenderableReviewProtocol(Protocol):
    def render_markdown(self) -> str: ...


class BootstrapCommitHandlerProtocol(Protocol):
    def review(
        self,
        *,
        operation: BootstrapRunnerStateEnvelope,
    ) -> RenderableReviewProtocol: ...

    def approve(
        self,
        *,
        operation: BootstrapRunnerStateEnvelope,
        approval: BootstrapApprovalRecord,
    ) -> BootstrapStageOutcome: ...

    def rollback(
        self,
        *,
        operation: BootstrapRunnerStateEnvelope,
        step: BootstrapApprovalStep,
        child_ref: BootstrapChildReference,
    ) -> BootstrapStageOutcome: ...

    def validate_resume(
        self,
        *,
        operation: BootstrapRunnerStateEnvelope,
    ) -> None: ...


class BootstrapRepositoryHandlerProtocol(Protocol):
    def review(
        self,
        *,
        operation: BootstrapRunnerStateEnvelope,
    ) -> RenderableReviewProtocol: ...

    def approve(
        self,
        *,
        operation: BootstrapRunnerStateEnvelope,
        approval: BootstrapApprovalRecord,
    ) -> BootstrapStageOutcome: ...

    def rollback(
        self,
        *,
        operation: BootstrapRunnerStateEnvelope,
        step: BootstrapApprovalStep,
        child_ref: BootstrapChildReference,
    ) -> BootstrapStageOutcome: ...


class BootstrapConnectionHandlerProtocol(Protocol):
    def review(
        self,
        *,
        operation: BootstrapRunnerStateEnvelope,
    ) -> RenderableReviewProtocol: ...

    def approve(
        self,
        *,
        operation: BootstrapRunnerStateEnvelope,
        approval: BootstrapApprovalRecord,
    ) -> BootstrapStageOutcome: ...

    def rollback(
        self,
        *,
        operation: BootstrapRunnerStateEnvelope,
        step: BootstrapApprovalStep,
        child_ref: BootstrapChildReference,
    ) -> BootstrapStageOutcome: ...

    def build_resource_links(
        self,
        *,
        operation: BootstrapRunnerStateEnvelope,
    ) -> ResourceLinksReview: ...


class BootstrapDeploymentHandlerProtocol(Protocol):
    def review(
        self,
        *,
        operation: BootstrapRunnerStateEnvelope,
    ) -> RenderableReviewProtocol: ...

    def approve(
        self,
        *,
        operation: BootstrapRunnerStateEnvelope,
        approval: BootstrapApprovalRecord,
    ) -> BootstrapStageOutcome: ...

    def validate_resume(
        self,
        *,
        operation: BootstrapRunnerStateEnvelope,
    ) -> None: ...

    def build_resource_links(
        self,
        *,
        operation: BootstrapRunnerStateEnvelope,
    ) -> ResourceLinksReview: ...


class LocalFilesystem(FilesystemProtocol):
    def resolve_directory(self, value: str | Path) -> Path:
        target = Path(value).expanduser().resolve()
        if not target.exists():
            raise BootstrapConfigError(f"repository path does not exist: {target}")
        if not target.is_dir():
            raise BootstrapConfigError(f"repository path is not a directory: {target}")
        return target


class SubprocessGitProtocol(GitProtocol):
    def repository_root(self, value: Path) -> Path:
        return Path(self._run(value, "rev-parse", "--show-toplevel")).resolve()

    def repository_url(self, value: Path) -> str:
        remote = self._run(value, "remote", "get-url", "origin")
        owner, repo = github_remote_identity(remote)
        return f"https://github.com/{owner}/{repo}.git"

    def repository_id(self, repository_url: str) -> str:
        owner, repo = github_remote_identity(repository_url)
        return f"{owner}/{repo}"

    def head_commit(self, value: Path) -> str:
        return self._run(value, "rev-parse", "HEAD")

    def current_branch(self, value: Path) -> str | None:
        completed = self._run_result(value, "symbolic-ref", "--quiet", "--short", "HEAD")
        if completed.returncode == 1:
            return None
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip() or "git command failed"
            raise BootstrapConfigError(message)
        branch = completed.stdout.strip()
        return branch or None

    @staticmethod
    def _run(value: Path, *args: str) -> str:
        completed = SubprocessGitProtocol._run_result(value, *args)
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip() or "git command failed"
            raise BootstrapConfigError(message)
        return completed.stdout.strip()

    @staticmethod
    def _run_result(value: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(value), *args],
            check=False,
            capture_output=True,
            text=True,
        )


class EnvironmentRuntimeBinding(RuntimeBindingProtocol):
    def runtime_repository(self) -> str:
        return runtime_repository_from_environment()

    def runtime_commit(self) -> str:
        return runtime_commit_from_environment()


class UtcClock(ClockProtocol):
    def now(self) -> datetime:
        return datetime.now(UTC)


def default_runner_state_root() -> Path:
    return scoped_state_root("runner")


def operation_directory(operation_id: str, *, state_root: Path | None = None) -> Path:
    root = resolve_state_root(state_root) if state_root is not None else default_runner_state_root()
    operation_segment = require_safe_operation_id(
        operation_id,
        message="operation state path is invalid",
        error_factory=BootstrapApplyError,
    )
    return resolve_state_child_directory(
        root,
        operation_segment,
        escape_message="operation state escapes the state root",
    )


def state_file_path(operation_id: str, *, state_root: Path | None = None) -> Path:
    return operation_directory(operation_id, state_root=state_root) / _STATE_FILE_NAME


def lock_file_path(operation_id: str, *, state_root: Path | None = None) -> Path:
    return operation_directory(operation_id, state_root=state_root) / _LOCK_FILE_NAME


def repository_index_directory(
    repository_id: str,
    *,
    state_root: Path | None = None,
) -> Path:
    root = (
        resolve_state_root(state_root)
        if state_root is not None
        else default_runner_state_root()
    )
    repository_segment = canonical_sha256({"repository_id": repository_id})
    return resolve_state_child_directory(
        root,
        _REPOSITORY_INDEX_DIRECTORY_NAME,
        repository_segment,
        escape_message="repository operation index escapes the state root",
    )


def repository_index_file_path(
    repository_id: str,
    *,
    state_root: Path | None = None,
) -> Path:
    return (
        repository_index_directory(repository_id, state_root=state_root)
        / _REPOSITORY_INDEX_FILE_NAME
    )


def repository_index_lock_file_path(
    repository_id: str,
    *,
    state_root: Path | None = None,
) -> Path:
    return (
        repository_index_directory(repository_id, state_root=state_root)
        / _REPOSITORY_INDEX_LOCK_FILE_NAME
    )


class FileBootstrapRunnerStateStore(BootstrapRunnerStateStoreProtocol):
    def __init__(self, *, state_root: Path | None = None) -> None:
        self._state_root = (
            resolve_state_root(state_root)
            if state_root is not None
            else default_runner_state_root()
        )

    def load(self, operation_id: str) -> BootstrapRunnerStateEnvelope:
        data = state_file_path(operation_id, state_root=self._state_root).read_bytes()
        if len(data) > _MAX_STATE_BYTES:
            raise BootstrapApplyError("bootstrap runner state exceeds size limit")
        try:
            return BootstrapRunnerStateEnvelope.model_validate_json(data)
        except Exception as exc:
            raise BootstrapApplyError("bootstrap runner state is invalid or tampered") from exc

    def start_or_resume(
        self,
        envelope: BootstrapRunnerStateEnvelope,
    ) -> BootstrapRunnerStateEnvelope:
        repository_id = envelope.repository_binding.repository_id
        with self._repository_lock(repository_id):
            active = self._find_active(repository_id)
            if active is not None:
                self._write_repository_index(active)
                return active
            self._save_state(envelope)
            self._write_repository_index(envelope)
            return envelope

    def find_active(
        self,
        repository_id: str,
    ) -> BootstrapRunnerStateEnvelope | None:
        with self._repository_lock(repository_id):
            active = self._find_active(repository_id)
            if active is not None:
                self._write_repository_index(active)
            return active

    def save(
        self,
        envelope: BootstrapRunnerStateEnvelope,
        *,
        expected_generation: int | None = None,
        expected_generation_hash: str | None = None,
    ) -> None:
        repository_id = envelope.repository_binding.repository_id
        with self._repository_lock(repository_id):
            if (
                expected_generation is None
                and envelope.lifecycle_stage not in _TERMINAL_LIFECYCLE_STAGES
            ):
                active = [
                    item
                    for item in self._load_repository_operations(repository_id)
                    if item.lifecycle_stage not in _TERMINAL_LIFECYCLE_STAGES
                    and item.operation_id != envelope.operation_id
                ]
                if active:
                    raise BootstrapApplyError(
                        "an active bootstrap operation already exists for repository "
                        f"{repository_id}: {active[0].operation_id}"
                    )
            self._save_state(
                envelope,
                expected_generation=expected_generation,
                expected_generation_hash=expected_generation_hash,
            )
            current_index = self._load_repository_index(repository_id)
            if (
                current_index is None
                or current_index.operation_id == envelope.operation_id
            ):
                self._write_repository_index(envelope)
                return
            indexed = self._load_indexed_operation(current_index)
            if (
                indexed.lifecycle_stage in _TERMINAL_LIFECYCLE_STAGES
                and envelope.lifecycle_stage not in _TERMINAL_LIFECYCLE_STAGES
            ):
                self._write_repository_index(envelope)

    def _save_state(
        self,
        envelope: BootstrapRunnerStateEnvelope,
        *,
        expected_generation: int | None = None,
        expected_generation_hash: str | None = None,
    ) -> None:
        path = state_file_path(envelope.operation_id, state_root=self._state_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = lock_file_path(envelope.operation_id, state_root=self._state_root)
        with state_file_lock(
            lock,
            locked_message=(
                "bootstrap runner state is locked by another writer"
            ),
        ):
            if expected_generation is None:
                if path.exists():
                    raise BootstrapApplyError("bootstrap runner state already exists")
            else:
                current = self.load(envelope.operation_id)
                if current.generation != expected_generation or current.generation_hash != expected_generation_hash:
                    raise BootstrapApplyError("bootstrap runner state generation conflict")
            data = canonical_json_bytes(envelope.model_dump(mode="json")) + b"\n"
            if len(data) > _MAX_STATE_BYTES:
                raise BootstrapApplyError("bootstrap runner state exceeds size limit")
            atomic_replace_state(
                path,
                data,
                generation_hash=envelope.generation_hash,
            )

    @contextmanager
    def _repository_lock(self, repository_id: str) -> Iterator[None]:
        lock = repository_index_lock_file_path(
            repository_id,
            state_root=self._state_root,
        )
        lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise BootstrapApplyError(
                "repository operation index is locked by another writer"
            ) from exc
        try:
            yield
        finally:
            os.close(lock_fd)
            os.unlink(lock)

    def _load_repository_operations(
        self,
        repository_id: str,
    ) -> tuple[BootstrapRunnerStateEnvelope, ...]:
        operation_ids: set[str] = set()
        index = self._load_repository_index(repository_id)
        if index is not None:
            operation_ids.add(index.operation_id)
        if self._state_root.is_dir():
            for child in self._state_root.iterdir():
                if (
                    not child.is_dir()
                    or child.name == _REPOSITORY_INDEX_DIRECTORY_NAME
                ):
                    continue
                path = child / _STATE_FILE_NAME
                if not path.is_file():
                    continue
                try:
                    data = path.read_bytes()
                    if len(data) > _MAX_STATE_BYTES:
                        continue
                    raw = json.loads(data)
                    payload = raw.get("payload")
                    binding = (
                        payload.get("repository_binding")
                        if isinstance(payload, dict)
                        else None
                    )
                    indexed_repository_id = (
                        binding.get("repository_id")
                        if isinstance(binding, dict)
                        else None
                    )
                except (OSError, ValueError):
                    continue
                if indexed_repository_id == repository_id:
                    operation_ids.add(child.name)
        operations: list[BootstrapRunnerStateEnvelope] = []
        for operation_id in sorted(operation_ids):
            try:
                operation = self.load(operation_id)
            except FileNotFoundError as exc:
                raise BootstrapApplyError(
                    "repository operation state is missing"
                ) from exc
            if operation.operation_id != operation_id:
                raise BootstrapApplyError(
                    "bootstrap runner state path does not match its operation_id"
                )
            if operation.repository_binding.repository_id != repository_id:
                raise BootstrapApplyError(
                    "repository operation index points to a different repository"
                )
            operations.append(operation)
        return tuple(operations)

    def _find_active(
        self,
        repository_id: str,
    ) -> BootstrapRunnerStateEnvelope | None:
        active = [
            item
            for item in self._load_repository_operations(repository_id)
            if item.lifecycle_stage not in _TERMINAL_LIFECYCLE_STAGES
        ]
        if len(active) > 1:
            operation_ids = ", ".join(
                sorted(item.operation_id for item in active)
            )
            raise BootstrapApplyError(
                "multiple active bootstrap operations exist for repository "
                f"{repository_id}: {operation_ids}"
            )
        return active[0] if active else None

    def _load_repository_index(
        self,
        repository_id: str,
    ) -> BootstrapRepositoryOperationIndex | None:
        path = repository_index_file_path(
            repository_id,
            state_root=self._state_root,
        )
        if not path.exists():
            return None
        data = path.read_bytes()
        if len(data) > _MAX_REPOSITORY_INDEX_BYTES:
            raise BootstrapApplyError("repository operation index exceeds size limit")
        try:
            index = BootstrapRepositoryOperationIndex.model_validate_json(data)
        except Exception as exc:
            raise BootstrapApplyError(
                "repository operation index is invalid or tampered"
            ) from exc
        if index.repository_id != repository_id:
            raise BootstrapApplyError(
                "repository operation index identity does not match its path"
            )
        return index

    def _load_indexed_operation(
        self,
        index: BootstrapRepositoryOperationIndex,
    ) -> BootstrapRunnerStateEnvelope:
        try:
            operation = self.load(index.operation_id)
        except FileNotFoundError as exc:
            raise BootstrapApplyError(
                "repository operation index references missing state"
            ) from exc
        if operation.repository_binding.repository_id != index.repository_id:
            raise BootstrapApplyError(
                "repository operation index points to a different repository"
            )
        return operation

    def _write_repository_index(
        self,
        envelope: BootstrapRunnerStateEnvelope,
    ) -> None:
        index = BootstrapRepositoryOperationIndex.create(
            repository_id=envelope.repository_binding.repository_id,
            operation_id=envelope.operation_id,
        )
        path = repository_index_file_path(
            index.repository_id,
            state_root=self._state_root,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        data = canonical_json_bytes(index.model_dump(mode="json")) + b"\n"
        if len(data) > _MAX_REPOSITORY_INDEX_BYTES:
            raise BootstrapApplyError("repository operation index exceeds size limit")
        temp = path.with_name(f"{path.stem}.{index.index_hash}.tmp")
        try:
            with open(temp, "xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def next_runner_generation(
    envelope: BootstrapRunnerStateEnvelope,
    *,
    now: datetime,
    **updates: object,
) -> BootstrapRunnerStateEnvelope:
    payload = envelope.payload.model_dump(mode="python")
    payload.update(updates)
    payload["generation"] = envelope.generation + 1
    payload["updated_at"] = _isoformat(now)
    return BootstrapRunnerStateEnvelope.create(**payload)


class BootstrapRunner:
    def __init__(
        self,
        *,
        filesystem: FilesystemProtocol | None = None,
        github: GitHubBridgeProtocol | None = None,
        azure: AzureBridgeProtocol | None = None,
        foundry: FoundryBridgeProtocol | None = None,
        git: GitProtocol | None = None,
        runtime: RuntimeBindingProtocol | None = None,
        clock: ClockProtocol | None = None,
        state_store: BootstrapRunnerStateStoreProtocol | None = None,
        target_resolution_handler: FoundryTargetResolutionHandlerProtocol | None = None,
        approval_handlers: Mapping[BootstrapApprovalStep, BootstrapApprovalHandlerProtocol] | None = None,
        repository_handler: BootstrapRepositoryHandlerProtocol | None = None,
        connection_handler: BootstrapConnectionHandlerProtocol | None = None,
        commit_handler: BootstrapCommitHandlerProtocol | None = None,
        deployment_handler: BootstrapDeploymentHandlerProtocol | None = None,
        rollback_handler: BootstrapRollbackHandlerProtocol | None = None,
    ) -> None:
        self._filesystem = filesystem or LocalFilesystem()
        self._github = github
        self._azure = azure
        self._foundry = foundry
        self._git = git or SubprocessGitProtocol()
        self._runtime = runtime or EnvironmentRuntimeBinding()
        self._clock = clock or UtcClock()
        self._state_store = state_store or FileBootstrapRunnerStateStore()
        if target_resolution_handler is None:
            from foundry_opt.bootstrap.foundry_targets import (
                DefaultFoundryTargetResolutionHandler,
            )

            target_resolution_handler = DefaultFoundryTargetResolutionHandler()
        self._target_resolution_handler = target_resolution_handler
        self._approval_handlers = dict(approval_handlers or {})
        self._repository_handler = repository_handler
        if (
            self._repository_handler is not None
            and "repository" not in self._approval_handlers
        ):
            self._approval_handlers["repository"] = self._repository_handler
        self._connection_handler = connection_handler
        if (
            self._connection_handler is not None
            and "connection" not in self._approval_handlers
        ):
            self._approval_handlers["connection"] = self._connection_handler
        self._commit_handler = commit_handler
        if self._commit_handler is not None and "commit" not in self._approval_handlers:
            self._approval_handlers["commit"] = self._commit_handler
        self._deployment_handler = deployment_handler
        if (
            self._deployment_handler is not None
            and "deployment" not in self._approval_handlers
        ):
            self._approval_handlers["deployment"] = self._deployment_handler
        self._rollback_handler = rollback_handler

    def start(self, repository: str | Path) -> BootstrapTurn:
        repository_path = self._filesystem.resolve_directory(repository)
        repository_root = self._git.repository_root(repository_path)
        repository_url = self._git.repository_url(repository_root)
        repository_id = self._git.repository_id(repository_url)
        repository_head = self._git.head_commit(repository_root)
        repository_branch = self._git.current_branch(repository_root)
        repository_binding = RepositoryBinding(
            repository_root=str(repository_root),
            repository_id=repository_id,
            repository_url=repository_url,
            head_commit=repository_head,
            branch_name=repository_branch,
        )
        runtime_binding = RuntimeBinding(
            runtime_repository=self._runtime.runtime_repository(),
            runtime_commit=self._runtime.runtime_commit(),
        )
        find_active = getattr(self._state_store, "find_active", None)
        if find_active is not None:
            active = find_active(repository_id)
            if active is not None:
                active = self._validate_resume(
                    active,
                    current_runtime=runtime_binding,
                    observed_repository=repository_binding,
                )
                return self._build_turn(active)
        discovery = discover_repository_agents(repository_root)
        selection = _selection_plan_from_discovery(
            discovery,
            registry=_load_repository_registry(repository_root),
            repository_root=repository_root,
        )
        stage: BootstrapLifecycleStage = "agent_selection"
        note = "Preflight and discovery completed."
        if not selection.discovered_agents:
            stage = "blocked"
            note = "No bootstrap-ready agents were discovered in the repository."
        now = self._clock.now()
        envelope = BootstrapRunnerStateEnvelope.create(
            generation=0,
            operation_id=f"bootstrap-{uuid.uuid4().hex[:12]}",
            lifecycle_stage=stage,
            started_at=_isoformat(now),
            updated_at=_isoformat(now),
            repository_binding=repository_binding,
            runtime_binding=runtime_binding,
            selection_plan=selection,
            note=note,
        )
        start_or_resume = getattr(self._state_store, "start_or_resume", None)
        if start_or_resume is None:
            self._state_store.save(envelope)
            selected = envelope
        else:
            selected = start_or_resume(envelope)
        if selected.operation_id != envelope.operation_id:
            selected = self._validate_resume(
                selected,
                current_runtime=runtime_binding,
                observed_repository=repository_binding,
            )
        return self._build_turn(selected)

    def answer(
        self,
        operation_id: str,
        question_id: str,
        answer: object,
    ) -> BootstrapTurn:
        envelope = self._load_validated(operation_id)
        current_question = self._build_question(envelope)
        if current_question is None:
            raise BootstrapApplyError("the current bootstrap stage does not accept answers")
        if current_question.question_id != question_id:
            raise BootstrapApplyError("stale question id")
        answer_record = BootstrapAnswerRecord(
            question_id=question_id,
            kind=current_question.kind,
            value=self._persisted_answer_value(current_question.kind, answer, envelope),
            answered_at=_isoformat(self._clock.now()),
            state_generation=envelope.generation,
            state_generation_hash=envelope.generation_hash,
        )
        if current_question.kind == "agent_selection":
            selected_ids = self._validate_selection_answer(answer, envelope)
            updated_selection = SelectionPlan.model_validate(
                {
                    **envelope.selection_plan.model_dump(mode="json"),
                    "selected_agent_ids": selected_ids,
                }
            )
            _validate_stage_transition(envelope.lifecycle_stage, "register_enable")
            updated = next_runner_generation(
                envelope,
                now=self._clock.now(),
                lifecycle_stage="register_enable",
                selection_plan=updated_selection,
                answers=(*envelope.answers, answer_record),
                note=(
                    "Selection recorded. Choose whether each selected candidate "
                    "should be ignored, registered disabled, or registered enabled."
                ),
            )
        elif current_question.kind == "foundry_target":
            updated = self._apply_stage_outcome(
                envelope,
                answer_record=answer_record,
                outcome=self._require_target_resolution_handler().handle_answer(
                    operation=envelope,
                    answer=answer,
                ),
            )
        elif current_question.kind == "register_enable":
            updated = self._handle_registration_answer(
                envelope,
                answer_record=answer_record,
                answer=answer,
            )
        elif current_question.kind == "verification_policy":
            updated = self._handle_verification_answer(
                envelope,
                answer_record=answer_record,
                answer=answer,
            )
        else:
            raise BootstrapApplyError(
                f"{current_question.kind} requires approve() instead of answer()"
            )
        self._state_store.save(
            updated,
            expected_generation=envelope.generation,
            expected_generation_hash=envelope.generation_hash,
        )
        return self._build_turn(updated)

    def approve(
        self,
        operation_id: str,
        step: BootstrapApprovalStep,
        actor: str,
        summary: str,
    ) -> BootstrapTurn:
        envelope = self._load_validated(operation_id)
        expected_stage = _APPROVAL_STAGE_BY_STEP[step]
        if envelope.lifecycle_stage != expected_stage:
            raise BootstrapApplyError("stale approval step")
        handler = self._approval_handlers.get(step)
        if handler is None:
            raise BootstrapApplyError(f"{step} approval handler is not configured")
        approval = BootstrapApprovalRecord.create(
            step=step,
            actor=actor,
            summary=summary,
            approved_at=_isoformat(self._clock.now()),
            state_generation=envelope.generation,
            state_generation_hash=envelope.generation_hash,
        )
        updated = self._apply_stage_outcome(
            envelope,
            approval=approval,
            outcome=handler.approve(operation=envelope, approval=approval),
        )
        self._state_store.save(
            updated,
            expected_generation=envelope.generation,
            expected_generation_hash=envelope.generation_hash,
        )
        return self._build_turn(updated)

    def status(self, operation_id: str) -> BootstrapTurn:
        return self._build_turn(self._load_validated(operation_id))

    def rollback(
        self,
        operation_id: str,
        step: BootstrapApprovalStep,
    ) -> BootstrapTurn:
        child_completed = False
        for _ in range(_MAX_PARENT_CAS_ATTEMPTS):
            envelope = self._state_store.load(operation_id)
            self._validate_runtime_binding(envelope)
            self._validate_repository_identity(envelope)
            permitted_step = _next_rollback_step(envelope.child_refs)
            if permitted_step != step:
                if (
                    child_completed
                    and all(item.step != step for item in envelope.child_refs)
                ):
                    return self._build_turn(
                        self._validate_loaded(envelope)
                    )
                if permitted_step is None:
                    raise BootstrapApplyError(
                        "rollback has no permitted recorded child step"
                    )
                raise BootstrapApplyError(
                    "next permitted rollback step is "
                    f"{permitted_step}"
                )
            child_ref = next(
                item for item in envelope.child_refs if item.step == step
            )
            handler = self._rollback_handler_for_step(step)
            outcome = self._reconcile_handler_rollback(
                handler,
                operation=envelope,
                step=step,
                child_ref=child_ref,
            )
            if outcome is None:
                self._validate_loaded(envelope)
                outcome = handler.rollback(
                    operation=envelope,
                    step=step,
                    child_ref=child_ref,
                )
            child_completed = True
            updated = self._apply_stage_outcome(
                envelope,
                outcome=outcome,
            )
            if self._save_after_parent_cas(envelope, updated):
                return self._build_turn(updated)
        raise BootstrapApplyError(
            "bootstrap runner state kept changing during rollback"
        )

    def _load_validated(self, operation_id: str) -> BootstrapRunnerStateEnvelope:
        envelope = self._reconcile_rolled_back_children(operation_id)
        current_runtime = RuntimeBinding(
            runtime_repository=self._runtime.runtime_repository(),
            runtime_commit=self._runtime.runtime_commit(),
        )
        root = self._git.repository_root(Path(envelope.repository_binding.repository_root))
        current_url = self._git.repository_url(root)
        current_id = self._git.repository_id(current_url)
        current_head = self._git.head_commit(root)
        current_branch = self._git.current_branch(root)
        observed = RepositoryBinding(
            repository_root=str(root),
            repository_id=current_id,
            repository_url=current_url,
            head_commit=current_head,
            branch_name=current_branch,
        )
        return self._validate_resume(
            envelope,
            current_runtime=current_runtime,
            observed_repository=observed,
        )

    def _validate_resume(
        self,
        envelope: BootstrapRunnerStateEnvelope,
        *,
        current_runtime: RuntimeBinding,
        observed_repository: RepositoryBinding,
    ) -> BootstrapRunnerStateEnvelope:
        if current_runtime != envelope.runtime_binding:
            raise BootstrapApplyError(
                "bootstrap resume requires the exact runtime repository and commit"
            )
        if observed_repository != envelope.repository_binding:
            raise BootstrapApplyError("bootstrap resume requires the exact repository root, identity, and commit")
        if self._commit_handler is not None and (
            envelope.lifecycle_stage == "commit_approval"
            or any(item.step == "commit" for item in envelope.child_refs)
        ):
            self._commit_handler.validate_resume(operation=envelope)
        if self._deployment_handler is not None and (
            envelope.lifecycle_stage != "rolled_back"
            and (
                envelope.lifecycle_stage == "deployment_approval"
                or any(
                    item.step == "deployment"
                    for item in envelope.child_refs
                )
            )
        ):
            self._deployment_handler.validate_resume(operation=envelope)
        return envelope

    def _validate_loaded(
        self,
        envelope: BootstrapRunnerStateEnvelope,
    ) -> BootstrapRunnerStateEnvelope:
        return self._validate_resume(
            envelope,
            current_runtime=RuntimeBinding(
                runtime_repository=self._runtime.runtime_repository(),
                runtime_commit=self._runtime.runtime_commit(),
            ),
            observed_repository=self._observed_repository_binding(envelope),
        )

    def _validate_runtime_binding(
        self,
        envelope: BootstrapRunnerStateEnvelope,
    ) -> None:
        current_runtime = RuntimeBinding(
            runtime_repository=self._runtime.runtime_repository(),
            runtime_commit=self._runtime.runtime_commit(),
        )
        if current_runtime != envelope.runtime_binding:
            raise BootstrapApplyError("bootstrap resume requires the exact runtime repository and commit")

    def _observed_repository_binding(
        self,
        envelope: BootstrapRunnerStateEnvelope,
    ) -> RepositoryBinding:
        root = self._git.repository_root(Path(envelope.repository_binding.repository_root))
        current_url = self._git.repository_url(root)
        current_id = self._git.repository_id(current_url)
        current_head = self._git.head_commit(root)
        current_branch = self._git.current_branch(root)
        return RepositoryBinding(
            repository_root=str(root),
            repository_id=current_id,
            repository_url=current_url,
            head_commit=current_head,
            branch_name=current_branch,
        )

    def _validate_repository_identity(
        self,
        envelope: BootstrapRunnerStateEnvelope,
    ) -> None:
        observed = self._observed_repository_binding(envelope)
        expected = envelope.repository_binding
        if (
            observed.repository_root != expected.repository_root
            or observed.repository_id != expected.repository_id
            or observed.repository_url != expected.repository_url
        ):
            raise BootstrapApplyError("bootstrap resume requires the exact repository root, identity, and commit")

    def _reconcile_rolled_back_children(
        self,
        operation_id: str,
    ) -> BootstrapRunnerStateEnvelope:
        for _ in range(_MAX_PARENT_CAS_ATTEMPTS):
            envelope = self._state_store.load(operation_id)
            self._validate_runtime_binding(envelope)
            self._validate_repository_identity(envelope)
            step = _next_rollback_step(envelope.child_refs)
            if step is None:
                return envelope
            handler = self._rollback_handler_for_step(
                step,
                required=False,
            )
            if handler is None:
                return envelope
            child_ref = next(
                item for item in envelope.child_refs if item.step == step
            )
            outcome = self._reconcile_handler_rollback(
                handler,
                operation=envelope,
                step=step,
                child_ref=child_ref,
            )
            if outcome is None:
                return envelope
            updated = self._apply_stage_outcome(
                envelope,
                outcome=outcome,
            )
            if self._save_after_parent_cas(envelope, updated):
                continue
        raise BootstrapApplyError(
            "bootstrap runner state kept changing during rollback reconciliation"
        )

    def _rollback_handler_for_step(
        self,
        step: BootstrapApprovalStep,
        *,
        required: bool = True,
    ) -> BootstrapRollbackHandlerProtocol | None:
        handler: BootstrapRollbackHandlerProtocol | None = None
        if step == "repository" and self._repository_handler is not None:
            handler = self._repository_handler
        elif step == "connection" and self._connection_handler is not None:
            handler = self._connection_handler
        elif step == "commit" and self._commit_handler is not None:
            handler = self._commit_handler
        elif self._rollback_handler is not None:
            handler = self._rollback_handler
        if handler is None and required:
            raise BootstrapApplyError("rollback handler is not configured")
        return handler

    @staticmethod
    def _reconcile_handler_rollback(
        handler: BootstrapRollbackHandlerProtocol,
        *,
        operation: BootstrapRunnerStateEnvelope,
        step: BootstrapApprovalStep,
        child_ref: BootstrapChildReference,
    ) -> BootstrapStageOutcome | None:
        reconcile = getattr(handler, "reconcile_rollback", None)
        if not callable(reconcile):
            return None
        outcome = reconcile(
            operation=operation,
            step=step,
            child_ref=child_ref,
        )
        if outcome is not None and not isinstance(
            outcome,
            BootstrapStageOutcome,
        ):
            raise BootstrapApplyError(
                "rollback reconciliation returned an invalid outcome"
            )
        return outcome

    def _save_after_parent_cas(
        self,
        expected: BootstrapRunnerStateEnvelope,
        updated: BootstrapRunnerStateEnvelope,
    ) -> bool:
        try:
            self._state_store.save(
                updated,
                expected_generation=expected.generation,
                expected_generation_hash=expected.generation_hash,
            )
        except BootstrapApplyError:
            latest = self._state_store.load(expected.operation_id)
            if (
                latest.generation == expected.generation
                and latest.generation_hash == expected.generation_hash
            ):
                raise
            return False
        return True

    def _apply_stage_outcome(
        self,
        envelope: BootstrapRunnerStateEnvelope,
        *,
        answer_record: BootstrapAnswerRecord | None = None,
        approval: BootstrapApprovalRecord | None = None,
        outcome: BootstrapStageOutcome,
    ) -> BootstrapRunnerStateEnvelope:
        answers = envelope.answers
        if answer_record is not None:
            answers = (*answers, answer_record)
        approvals = envelope.approvals
        if approval is not None:
            approvals = (*approvals, approval)
        foundry_targets = envelope.foundry_targets
        if outcome.foundry_targets is not None:
            foundry_targets = outcome.foundry_targets
        child_refs = outcome.child_refs if outcome.child_refs is not None else envelope.child_refs
        repository_binding = outcome.repository_binding or envelope.repository_binding
        handler_context = dict(envelope.handler_context)
        if outcome.handler_context is not None:
            handler_context.update(outcome.handler_context)
        _validate_stage_transition(envelope.lifecycle_stage, outcome.stage)
        updated = next_runner_generation(
            envelope,
            now=self._clock.now(),
            lifecycle_stage=outcome.stage,
            repository_binding=repository_binding,
            answers=answers,
            approvals=approvals,
            foundry_targets=foundry_targets,
            child_refs=child_refs,
            note=outcome.note,
            handler_context=handler_context,
        )
        return self._advance_existing_verification(updated)

    def _build_turn(self, envelope: BootstrapRunnerStateEnvelope) -> BootstrapTurn:
        resource_links = build_resource_links(repository_id=envelope.repository_binding.repository_id)
        if self._target_resolution_handler is not None:
            extra_links = self._target_resolution_handler.build_resource_links(operation=envelope)
            if extra_links is not None:
                resource_links = _merge_resource_links(resource_links, extra_links)
        if self._deployment_handler is not None and (
            envelope.lifecycle_stage != "rolled_back"
            and (
                envelope.lifecycle_stage == "deployment_approval"
                or any(
                    item.step == "deployment"
                    for item in envelope.child_refs
                )
            )
        ):
            resource_links = _merge_resource_links(
                resource_links,
                self._deployment_handler.build_resource_links(
                    operation=envelope
                ),
            )
        if self._connection_handler is not None and (
            envelope.lifecycle_stage == "connection_approval"
            or any(item.step == "connection" for item in envelope.child_refs)
        ):
            resource_links = _merge_resource_links(
                resource_links,
                self._connection_handler.build_resource_links(
                    operation=envelope
                ),
            )
        return BootstrapTurn(
            owner_markdown=self._render_owner_markdown(envelope),
            next_question=self._build_question(envelope),
            available_actions=self._available_actions(envelope),
            operation_id=envelope.operation_id,
            state=envelope.lifecycle_stage,
            resource_links=resource_links,
        )

    def _build_question(
        self,
        envelope: BootstrapRunnerStateEnvelope,
    ) -> BootstrapQuestion | None:
        kind = _QUESTION_KIND_BY_STAGE.get(envelope.lifecycle_stage)
        if kind is None:
            return None
        question_id = self._question_id(envelope, kind)
        if kind == "agent_selection":
            review = build_discovery_review(envelope.selection_plan)
            choices = tuple(
                BootstrapQuestionChoice(
                    value=item.repo_agent_id,
                    label=f"{item.repo_agent_id} ({item.root})",
                    detail=item.summary,
                )
                for item in review.agents
            )
            return BootstrapQuestion(
                question_id=question_id,
                kind=kind,
                title="Select the repository agents to bootstrap",
                details_markdown=(
                    "Choose one or more discovered `repoAgentId` values. "
                    "Only ready candidates should move forward."
                ),
                allow_multiple=True,
                choices=choices,
            )
        if kind == "foundry_target":
            if self._target_resolution_handler is not None:
                question = self._target_resolution_handler.build_question(
                    operation=envelope,
                    question_id=question_id,
                )
                if question is not None:
                    return question
            selected = ", ".join(envelope.selection_plan.selected_agent_ids) or "none"
            return BootstrapQuestion(
                question_id=question_id,
                kind=kind,
                title="Resolve the reviewed Foundry target",
                details_markdown=(
                    "The next bridge step resolves the reviewed Foundry project endpoint "
                    f"and deployed agent name for `{selected}`. Live resolution hooks are "
                    "intentionally injected and land in a dependent task."
                ),
            )
        if kind == "register_enable":
            pending = self._pending_registration_agent(envelope)
            if pending is None:
                raise BootstrapApplyError(
                    "registration question has no unresolved agent"
                )
            return BootstrapQuestion(
                question_id=question_id,
                kind=kind,
                title=f"Choose how to register {pending.repo_agent_id}",
                details_markdown=(
                    f"Folder: `{pending.root}`. Register enabled to include it in "
                    "bootstrap deployment, register disabled to record it without "
                    "deploying, or ignore it."
                ),
                choices=(
                    BootstrapQuestionChoice(
                        value="register_enabled",
                        label="Register and enable",
                        detail="Manage the agent and include it in deployment review.",
                    ),
                    BootstrapQuestionChoice(
                        value="register_disabled",
                        label="Register disabled",
                        detail="Record the agent but do not require a target or deploy it.",
                    ),
                    BootstrapQuestionChoice(
                        value="ignore",
                        label="Ignore",
                        detail="Leave the discovered folder unmanaged.",
                    ),
                ),
            )
        if kind == "verification_policy":
            pending_id = self._pending_verification_agent_id(envelope)
            if pending_id is None:
                raise BootstrapApplyError(
                    "verification question has no unresolved enabled agent"
                )
            choices = self._verification_question_choices(
                envelope,
                pending_id,
            )
            return BootstrapQuestion(
                question_id=question_id,
                kind=kind,
                title=f"Choose verification for {pending_id}",
                details_markdown=(
                    "Verification is optional. Preserve an existing profile gate, "
                    "defer dataset/evaluator selection to an issue, use existing "
                    "repository checks, or start with no evidence and a visible warning."
                ),
                choices=choices,
            )
        step = _approval_step_for_question(kind)
        detail = "Bridge handlers populate the reviewed step details."
        if step == "repository":
            detail = (
                "The repository handler shows registry, profile, instruction, "
                "issue-form, workflow, preserve, and conflict intent."
            )
        if step == "commit":
            detail = (
                "The local commit handler populates the reviewed paths, diff summary, "
                "base commit, and proposed message."
            )
        return BootstrapQuestion(
            question_id=question_id,
            kind=kind,
            title=f"Approve the {step} step",
            details_markdown=(
                f"Use `approve(..., step={step!r}, ...)` to continue. "
                f"{detail}"
            ),
        )

    def _verification_question_choices(
        self,
        envelope: BootstrapRunnerStateEnvelope,
        repo_agent_id: str,
    ) -> tuple[BootstrapQuestionChoice, ...]:
        choices: list[BootstrapQuestionChoice] = []
        profile = self._existing_profile(envelope, repo_agent_id)
        if profile is not None:
            choices.append(
                BootstrapQuestionChoice(
                    value="preserve_existing",
                    label="Preserve existing verification",
                )
            )
        choices.append(
            BootstrapQuestionChoice(
                value="defer_to_issue",
                label="Defer dataset and evaluators to an issue",
            )
        )
        if profile is not None and profile.verification.repository_checks:
            choices.append(
                BootstrapQuestionChoice(
                    value="repository_checks",
                    label="Use existing repository checks",
                )
            )
        choices.append(
            BootstrapQuestionChoice(
                value="no_evidence",
                label="Start with no evidence",
            )
        )
        return tuple(choices)

    def _existing_profile(
        self,
        envelope: BootstrapRunnerStateEnvelope,
        repo_agent_id: str,
    ) -> BootstrapSidecar | None:
        inspection = self._inspect_existing_sidecar(
            envelope,
            repo_agent_id=repo_agent_id,
        )
        if inspection is None or inspection.error is not None:
            return None
        return inspection.profile

    def _advance_existing_verification(
        self,
        envelope: BootstrapRunnerStateEnvelope,
    ) -> BootstrapRunnerStateEnvelope:
        if envelope.lifecycle_stage != "verification_policy":
            return envelope
        choices = list(envelope.verification_choices)
        preserved: list[str] = []
        while True:
            resolved = {
                item.repo_agent_id.casefold()
                for item in choices
            }
            pending_id = next(
                (
                    item
                    for item in self._enabled_agent_ids(envelope)
                    if item.casefold() not in resolved
                ),
                None,
            )
            if pending_id is None:
                break
            if self._existing_profile(envelope, pending_id) is None:
                break
            choices.append(
                BootstrapVerificationChoice(
                    repo_agent_id=pending_id,
                    choice="preserve_existing",
                )
            )
            preserved.append(pending_id)
        if not preserved:
            return envelope
        enabled = self._enabled_agent_ids(envelope)
        complete = len(choices) == len(enabled)
        return next_runner_generation(
            envelope,
            now=self._clock.now(),
            lifecycle_stage=(
                "repository_approval" if complete else "verification_policy"
            ),
            verification_choices=tuple(choices),
            note=(
                "Preserved existing sidecar verification and prepared the repository review."
                if complete
                else "Preserved existing sidecar verification for "
                + ", ".join(preserved)
                + "."
            ),
        )

    def _render_existing_sidecars(
        self,
        envelope: BootstrapRunnerStateEnvelope,
    ) -> str | None:
        inspections = tuple(
            (
                record.repo_agent_id,
                self._inspect_existing_sidecar(
                    envelope,
                    repo_agent_id=record.repo_agent_id,
                ),
            )
            for record in envelope.selection_plan.discovered_agents
        )
        existing = tuple(
            (repo_agent_id, inspection)
            for repo_agent_id, inspection in inspections
            if inspection is not None
        )
        if not existing:
            return None
        lines = ["## Existing sidecars"]
        for repo_agent_id, inspection in existing:
            assert inspection is not None
            lines.append(f"- {repo_agent_id}: `{inspection.path}`")
            if inspection.error is not None:
                lines.append(f"  - Status: invalid ({inspection.error})")
                continue
            profile = inspection.profile
            assert profile is not None
            lines.extend(
                (
                    f"  - Profile agent ID: `{profile.repo_agent_id}`",
                    (
                        "  - Foundry target: "
                        f"`{profile.foundry_project.project_endpoint}` / "
                        f"`{profile.foundry_project.agent_name}`"
                    ),
                    f"  - Baseline model: `{profile.baseline_model}`",
                    (
                        "  - Deployment: "
                        f"{'enabled' if profile.deployment.enabled else 'disabled'}"
                    ),
                )
            )
            verification = profile.verification
            bundle = verification.bundle
            if bundle is not None:
                evaluator_ids = tuple(
                    item.reference.evaluator_id
                    for item in bundle.default_evaluator_bundle.objective.evaluators
                )
                lines.extend(
                    (
                        f"  - Verification: `{verification.mode}` with a default evaluator bundle",
                        f"  - Development dataset: `{bundle.development_dataset.dataset_id}`",
                        f"  - Validating dataset: `{bundle.validating_dataset.dataset_id}`",
                        (
                            "  - Default evaluators: "
                            + ", ".join(f"`{item}`" for item in evaluator_ids)
                        ),
                    )
                )
            elif verification.repository_checks:
                lines.append(
                    f"  - Verification: `{verification.mode}` with "
                    f"{len(verification.repository_checks)} repository check(s)"
                )
            else:
                lines.append(
                    f"  - Verification: `{verification.mode}`; no default evaluator bundle"
                )
        return "\n".join(lines)

    @staticmethod
    def _inspect_existing_sidecar(
        envelope: BootstrapRunnerStateEnvelope,
        *,
        repo_agent_id: str,
    ) -> _ExistingSidecarInspection | None:
        candidate = next(
            (
                item
                for item in envelope.selection_plan.discovered_agents
                if item.repo_agent_id.casefold() == repo_agent_id.casefold()
            ),
            None,
        )
        if candidate is None:
            return None
        candidate_paths: list[str] = []
        registry = _load_repository_registry(
            Path(envelope.repository_binding.repository_root),
        )
        if registry is not None:
            registry_entry = _matching_registry_entry(
                registry,
                repo_agent_id=repo_agent_id,
                root=candidate.root,
                source_root=candidate.source_root,
            )
            if registry_entry is not None:
                candidate_paths.append(registry_entry.config_path)
        if (
            candidate.config_path is not None
            and PurePosixPath(candidate.config_path).name == "foundry-opt.yaml"
        ):
            candidate_paths.append(candidate.config_path)
        managed_root = (
            candidate.root
            if candidate.root != "."
            else candidate.source_root
        )
        conventional_path = (
            ".foundry/foundry-opt.yaml"
            if managed_root == "."
            else f"{managed_root}/.foundry/foundry-opt.yaml"
        )
        if conventional_path not in candidate_paths:
            candidate_paths.append(conventional_path)
        repository_root = Path(envelope.repository_binding.repository_root)
        for relative in candidate_paths:
            path = repository_root.joinpath(*PurePosixPath(relative).parts)
            if not path.is_file():
                continue
            try:
                profile = BootstrapSidecar.from_document(
                    path.read_text(encoding="utf-8")
                )
            except (OSError, BootstrapConfigError) as exc:
                return _ExistingSidecarInspection(
                    path=relative,
                    error=str(exc),
                )
            if profile.repo_agent_id != repo_agent_id:
                return _ExistingSidecarInspection(
                    path=relative,
                    error=(
                        "profile repo_agent_id does not match the discovered "
                        f"agent ID {repo_agent_id!r}"
                    ),
                )
            return _ExistingSidecarInspection(
                path=relative,
                profile=profile,
            )
        return None

    def _available_actions(
        self,
        envelope: BootstrapRunnerStateEnvelope,
    ) -> tuple[BootstrapAvailableAction, ...]:
        stage = envelope.lifecycle_stage
        if stage in {"agent_selection", "foundry_target_resolution", "register_enable", "verification_policy"}:
            return (
                BootstrapAvailableAction(name="answer"),
                BootstrapAvailableAction(name="status"),
            )
        if stage in {"repository_approval", "connection_approval", "commit_approval", "deployment_approval"}:
            step = next(key for key, value in _APPROVAL_STAGE_BY_STEP.items() if value == stage)
            actions: list[BootstrapAvailableAction] = [
                BootstrapAvailableAction(name="approve", step=step),
                BootstrapAvailableAction(name="status"),
            ]
            actions.extend(_next_rollback_actions(envelope.child_refs))
            return tuple(actions)
        if stage in {"final_handoff", "rolled_back"}:
            actions = [BootstrapAvailableAction(name="status")]
            actions.extend(_next_rollback_actions(envelope.child_refs))
            return tuple(actions)
        return (BootstrapAvailableAction(name="status"),)

    def _render_owner_markdown(self, envelope: BootstrapRunnerStateEnvelope) -> str:
        registry = _load_repository_registry(
            Path(envelope.repository_binding.repository_root),
        )
        lines = [
            "## Bootstrap preflight",
            f"- Repository: {envelope.repository_binding.repository_id}",
            f"- Repository branch: {envelope.repository_binding.branch_name or '(detached)'}",
            f"- Repository commit: {envelope.repository_binding.head_commit[:12]}",
            f"- Runtime: {envelope.runtime_binding.runtime_commit[:12]}",
            "",
            build_discovery_review(
                envelope.selection_plan,
                registry=registry,
            ).render_markdown(),
        ]
        existing_sidecars = self._render_existing_sidecars(envelope)
        if existing_sidecars is not None:
            lines.extend(("", existing_sidecars))
        if self._target_resolution_handler is not None:
            rendered_targets = self._target_resolution_handler.render_owner_markdown(
                operation=envelope,
            )
            if rendered_targets:
                lines.extend(("", rendered_targets))
        if (
            self._repository_handler is not None
            and envelope.lifecycle_stage == "repository_approval"
        ):
            lines.extend(
                (
                    "",
                    self._repository_handler.review(
                        operation=envelope
                    ).render_markdown(),
                )
            )
        if (
            self._connection_handler is not None
            and envelope.lifecycle_stage == "connection_approval"
        ):
            lines.extend(
                (
                    "",
                    self._connection_handler.review(
                        operation=envelope
                    ).render_markdown(),
                )
            )
        if self._commit_handler is not None and (
            envelope.lifecycle_stage == "commit_approval"
            or any(item.step == "commit" for item in envelope.child_refs)
        ):
            lines.extend(("", self._commit_handler.review(operation=envelope).render_markdown()))
        if self._deployment_handler is not None and (
            envelope.lifecycle_stage != "rolled_back"
            and (
                envelope.lifecycle_stage == "deployment_approval"
                or any(
                    item.step == "deployment"
                    for item in envelope.child_refs
                )
            )
        ):
            lines.extend(
                (
                    "",
                    self._deployment_handler.review(
                        operation=envelope
                    ).render_markdown(),
                )
            )
        if envelope.note:
            lines.extend(("", "## Bridge state", f"- {envelope.note}"))
        if envelope.lifecycle_stage == "agent_selection":
            lines.extend(
                (
                    "",
                    "## Next action",
                    "- Select the `repoAgentId` values to bootstrap.",
                )
            )
        elif envelope.lifecycle_stage == "foundry_target_resolution":
            selected = ", ".join(envelope.selection_plan.selected_agent_ids) or "none"
            lines.extend(
                (
                    "",
                    "## Next action",
                    f"- Resolve the reviewed Foundry project endpoint and deployed agent name for {selected}.",
                )
            )
        elif envelope.lifecycle_stage == "blocked":
            lines.extend(("", "## Status", "- Bootstrap is blocked until discovery finds a ready agent."))
        elif envelope.lifecycle_stage == "rolled_back":
            lines.extend(("", "## Status", "- Recorded child work was rolled back through the bridge handler."))
        elif envelope.lifecycle_stage == "final_handoff":
            lines.extend(("", "## Status", "- Bootstrap reached the final bridge handoff stage."))
        else:
            lines.extend(
                (
                    "",
                    "## Next action",
                    f"- Continue the `{envelope.lifecycle_stage}` bridge stage.",
                )
            )
        return "\n".join(lines)

    def _persisted_answer_value(
        self,
        kind: BootstrapQuestionKind,
        answer: object,
        envelope: BootstrapRunnerStateEnvelope,
    ) -> str | bool | tuple[str, ...] | Mapping[str, str]:
        if kind == "agent_selection":
            return self._validate_selection_answer(answer, envelope)
        if kind == "foundry_target" and self._target_resolution_handler is not None:
            normalized = self._target_resolution_handler.persisted_answer_value(
                operation=envelope,
                answer=answer,
            )
            safe_persisted_document({"value": normalized})
            return normalized
        if kind in {"register_enable", "verification_policy"}:
            return _coerce_single_choice(answer)
        if isinstance(answer, bool):
            return answer
        if isinstance(answer, str):
            safe_persisted_document({"value": answer})
            return answer
        if isinstance(answer, Mapping):
            normalized = {str(key): str(value) for key, value in answer.items()}
            safe_persisted_document({"value": normalized})
            return normalized
        if isinstance(answer, Sequence) and not isinstance(answer, (str, bytes, bytearray)):
            normalized = tuple(str(item) for item in answer)
            safe_persisted_document({"value": normalized})
            return normalized
        raise BootstrapApplyError("answer value is unsupported")

    def _validate_selection_answer(
        self,
        answer: object,
        envelope: BootstrapRunnerStateEnvelope,
    ) -> tuple[str, ...]:
        selected = _coerce_selection_answer(answer)
        if not selected:
            raise BootstrapApplyError("selection answer must choose at least one repoAgentId")
        review = build_discovery_review(envelope.selection_plan)
        by_id = {item.repo_agent_id.casefold(): item for item in review.agents}
        canonical: list[str] = []
        seen: set[str] = set()
        for value in selected:
            key = value.casefold()
            if key in seen:
                continue
            seen.add(key)
            agent = by_id.get(key)
            if agent is None:
                raise BootstrapApplyError(f"unknown repoAgentId: {value}")
            if agent.readiness != "ready":
                raise BootstrapApplyError(f"selected repoAgentId is not ready: {agent.repo_agent_id}")
            canonical.append(agent.repo_agent_id)
        return tuple(sorted(canonical, key=str.casefold))

    def _require_target_resolution_handler(self) -> FoundryTargetResolutionHandlerProtocol:
        if self._target_resolution_handler is None:
            raise BootstrapApplyError("foundry target resolution handler is not configured")
        return self._target_resolution_handler

    def _handle_registration_answer(
        self,
        envelope: BootstrapRunnerStateEnvelope,
        *,
        answer_record: BootstrapAnswerRecord,
        answer: object,
    ) -> BootstrapRunnerStateEnvelope:
        pending = self._pending_registration_agent(envelope)
        if pending is None:
            raise BootstrapApplyError(
                "there is no unresolved registration decision"
            )
        choice = _coerce_single_choice(answer)
        if choice not in {
            "ignore",
            "register_disabled",
            "register_enabled",
        }:
            raise BootstrapApplyError("registration answer is not an offered choice")
        intents = (
            *envelope.registration_intents,
            BootstrapRegistrationIntent(
                repo_agent_id=pending.repo_agent_id,
                intent=choice,
            ),
        )
        remaining = len(intents) < len(envelope.selection_plan.selected_agent_ids)
        if remaining:
            stage: BootstrapLifecycleStage = "register_enable"
            note = f"Recorded registration intent for {pending.repo_agent_id}."
        else:
            enabled = tuple(
                item.repo_agent_id
                for item in intents
                if item.intent == "register_enabled"
            )
            registered = tuple(
                item.repo_agent_id
                for item in intents
                if item.intent != "ignore"
            )
            if not registered:
                stage = "final_handoff"
                note = "All selected candidates were ignored; no repository changes are planned."
            elif enabled:
                stage = "foundry_target_resolution"
                note = (
                    "Registration choices are complete. Resolve Foundry targets "
                    "for enabled agents."
                )
            else:
                stage = "repository_approval"
                note = "Registration choices are complete. No agents are enabled for deployment."
        _validate_stage_transition(envelope.lifecycle_stage, stage)
        updated = next_runner_generation(
            envelope,
            now=self._clock.now(),
            lifecycle_stage=stage,
            answers=(*envelope.answers, answer_record),
            registration_intents=intents,
            note=note,
        )
        if stage == "foundry_target_resolution":
            updated = self._apply_stage_outcome(
                updated,
                outcome=self._require_target_resolution_handler().prepare(
                    operation=updated
                ),
            )
        return updated

    def _handle_verification_answer(
        self,
        envelope: BootstrapRunnerStateEnvelope,
        *,
        answer_record: BootstrapAnswerRecord,
        answer: object,
    ) -> BootstrapRunnerStateEnvelope:
        pending_id = self._pending_verification_agent_id(envelope)
        if pending_id is None:
            raise BootstrapApplyError(
                "there is no unresolved verification decision"
            )
        choice = _coerce_single_choice(answer)
        offered = {
            item.value
            for item in self._verification_question_choices(
                envelope,
                pending_id,
            )
        }
        if choice not in offered:
            raise BootstrapApplyError("verification answer is not an offered choice")
        choices = (
            *envelope.verification_choices,
            BootstrapVerificationChoice(
                repo_agent_id=pending_id,
                choice=choice,
            ),
        )
        remaining = self._enabled_agent_ids(
            envelope,
            registration_intents=envelope.registration_intents,
        )
        complete = len(choices) == len(remaining)
        stage: BootstrapLifecycleStage = (
            "repository_approval" if complete else "verification_policy"
        )
        note = (
            "Verification choices are complete. Review the repository plan."
            if complete
            else f"Recorded verification choice for {pending_id}."
        )
        _validate_stage_transition(envelope.lifecycle_stage, stage)
        return next_runner_generation(
            envelope,
            now=self._clock.now(),
            lifecycle_stage=stage,
            answers=(*envelope.answers, answer_record),
            verification_choices=choices,
            note=note,
        )

    def _pending_registration_agent(
        self,
        envelope: BootstrapRunnerStateEnvelope,
    ) -> DiscoveredAgentRecord | None:
        resolved = {
            item.repo_agent_id.casefold()
            for item in envelope.registration_intents
        }
        by_id = {
            item.repo_agent_id.casefold(): item
            for item in envelope.selection_plan.discovered_agents
        }
        for repo_agent_id in envelope.selection_plan.selected_agent_ids:
            if repo_agent_id.casefold() not in resolved:
                return by_id[repo_agent_id.casefold()]
        return None

    def _pending_verification_agent_id(
        self,
        envelope: BootstrapRunnerStateEnvelope,
    ) -> str | None:
        resolved = {
            item.repo_agent_id.casefold()
            for item in envelope.verification_choices
        }
        for repo_agent_id in self._enabled_agent_ids(envelope):
            if repo_agent_id.casefold() not in resolved:
                return repo_agent_id
        return None

    @staticmethod
    def _enabled_agent_ids(
        envelope: BootstrapRunnerStateEnvelope,
        *,
        registration_intents: Sequence[BootstrapRegistrationIntent] | None = None,
    ) -> tuple[str, ...]:
        intents = (
            envelope.registration_intents
            if registration_intents is None
            else tuple(registration_intents)
        )
        return tuple(
            item.repo_agent_id
            for item in intents
            if item.intent == "register_enabled"
        )

    @staticmethod
    def _question_id(
        envelope: BootstrapRunnerStateEnvelope,
        kind: BootstrapQuestionKind,
    ) -> str:
        return f"{kind}:{envelope.generation}:{envelope.generation_hash[:12]}"


def _selection_plan_from_discovery(
    result: DiscoveryResult,
    *,
    registry: RootRegistry | None = None,
    repository_root: Path | None = None,
) -> SelectionPlan:
    discovered_agents: list[DiscoveredAgentRecord] = []
    binding_assessments = []
    seen_ids: set[str] = set()
    for agent in result.agents:
        registry_entry = (
            None
            if registry is None
            else _matching_registry_entry(
                registry,
                repo_agent_id=agent.repoAgentId,
                root=agent.root,
                source_root=agent.sourceRoot,
            )
        )
        existing_sidecar = (
            None
            if repository_root is None
            else _existing_sidecar_for_discovered(
                repository_root,
                root=agent.root,
                source_root=agent.sourceRoot,
            )
        )
        repo_agent_id = (
            registry_entry.agent_id
            if registry_entry is not None
            else (
                existing_sidecar[1].repo_agent_id
                if existing_sidecar is not None
                else agent.repoAgentId
            )
        )
        key = repo_agent_id.casefold()
        if key in seen_ids:
            raise BootstrapConfigError(
                f"existing registry maps multiple discovered agents to {repo_agent_id!r}"
            )
        seen_ids.add(key)
        assessment = agent.bindingAssessment.model_copy(
            update={"agent_id": repo_agent_id}
        )
        binding_assessments.append(assessment)
        discovered_agents.append(
            DiscoveredAgentRecord(
            repo_agent_id=repo_agent_id,
            root=agent.root,
            config_path=(
                registry_entry.config_path
                if registry_entry is not None
                else (
                    existing_sidecar[0]
                    if existing_sidecar is not None
                    else agent.configPath
                )
            ),
            source_root=agent.sourceRoot,
            package_root=agent.packageRoot,
            source_fingerprint=agent.sourceFingerprint,
            package_fingerprint=agent.packageFingerprint,
            classification=assessment.classification,
            detail=assessment.detail,
            confidence=agent.confidence,
            blockers=tuple(
                DiscoveryBlockerRecord(code=item.code, detail=item.detail)
                for item in agent.blockers
            ),
            approved_shared_source_repo_agent_ids=agent.approvedSharedSourceRepoAgentIds,
        )
        )
    discovered_records = tuple(discovered_agents)
    blockers = tuple(
        sorted(
            {
                blocker.detail
                for agent in discovered_records
                for blocker in agent.blockers
                if blocker.detail
            }
        )
    )
    return SelectionPlan(
        repository_root=result.repositoryRoot,
        selected_agent_ids=(),
        binding_assessments=tuple(binding_assessments),
        discovery_fingerprints=(),
        blockers=blockers,
        discovered_agents=discovered_records,
    )


def _load_repository_registry(
    repository_root: Path,
) -> RootRegistry | None:
    path = repository_root / ".foundry-opt" / "registry.yaml"
    if not path.is_file():
        return None
    try:
        return RootRegistry.from_document(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BootstrapConfigError(
            "existing repository registry could not be read"
        ) from exc


def _existing_sidecar_for_discovered(
    repository_root: Path,
    *,
    root: str,
    source_root: str,
) -> tuple[str, BootstrapSidecar] | None:
    managed_root = root if root != "." else source_root
    relative = (
        ".foundry/foundry-opt.yaml"
        if managed_root == "."
        else f"{managed_root}/.foundry/foundry-opt.yaml"
    )
    path = repository_root.joinpath(*PurePosixPath(relative).parts)
    if not path.is_file():
        return None
    try:
        return (
            relative,
            BootstrapSidecar.from_document(path.read_text(encoding="utf-8")),
        )
    except OSError as exc:
        raise BootstrapConfigError(
            f"existing sidecar could not be read: {relative}"
        ) from exc


def _matching_registry_entry(
    registry: RootRegistry,
    *,
    repo_agent_id: str,
    root: str,
    source_root: str,
):
    exact = [
        item
        for item in registry.agents
        if item.agent_id.casefold() == repo_agent_id.casefold()
    ]
    if len(exact) == 1:
        return exact[0]
    matching_roots = [
        item
        for item in registry.agents
        if item.root.casefold() in {root.casefold(), source_root.casefold()}
    ]
    if len(matching_roots) > 1:
        raise BootstrapConfigError(
            f"existing registry has ambiguous entries for discovered root {root!r}"
        )
    return matching_roots[0] if matching_roots else None


def _coerce_selection_answer(answer: object) -> tuple[str, ...]:
    if isinstance(answer, str):
        return (answer,)
    if isinstance(answer, Mapping):
        payload = answer.get("selected_agent_ids")
        if payload is None:
            payload = answer.get("selected")
        if isinstance(payload, str):
            return (payload,)
        if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
            return tuple(str(item) for item in payload)
        raise BootstrapApplyError("selection answer must include selected_agent_ids")
    if isinstance(answer, Sequence) and not isinstance(answer, (str, bytes, bytearray)):
        return tuple(str(item) for item in answer)
    raise BootstrapApplyError("selection answer must be a repoAgentId or a list of repoAgentIds")


def _coerce_single_choice(answer: object) -> str:
    if isinstance(answer, str):
        return answer
    if isinstance(answer, Mapping):
        value = answer.get("choice")
        if value is None:
            value = answer.get("value")
        if isinstance(value, str):
            return value
    if isinstance(answer, Sequence) and not isinstance(
        answer,
        (str, bytes, bytearray),
    ):
        values = tuple(str(item) for item in answer)
        if len(values) == 1:
            return values[0]
    raise BootstrapApplyError("answer must select exactly one offered choice")


def _approval_step_for_question(kind: BootstrapQuestionKind) -> BootstrapApprovalStep:
    mapping: dict[BootstrapQuestionKind, BootstrapApprovalStep] = {
        "repository_approval": "repository",
        "connection_approval": "connection",
        "commit_approval": "commit",
        "deployment_approval": "deployment",
    }
    try:
        return mapping[kind]
    except KeyError as exc:
        raise BootstrapApplyError("question kind does not map to an approval step") from exc


def _next_rollback_actions(
    child_refs: Sequence[BootstrapChildReference],
) -> tuple[BootstrapAvailableAction, ...]:
    step = _next_rollback_step(child_refs)
    if step is not None:
        return (
            BootstrapAvailableAction(
                name="rollback",
                step=step,
            ),
        )
    return ()


def _next_rollback_step(
    child_refs: Sequence[BootstrapChildReference],
) -> BootstrapApprovalStep | None:
    recorded = {item.step for item in child_refs}
    for step in ("commit", "connection", "repository"):
        if step in recorded:
            return step
    return None


def _isoformat(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _validate_stage_transition(
    current: BootstrapLifecycleStage,
    target: BootstrapLifecycleStage,
) -> None:
    allowed = _ALLOWED_NEXT_STAGES.get(current, frozenset())
    if target not in allowed:
        raise BootstrapApplyError(f"invalid bootstrap stage transition: {current} -> {target}")


def _merge_resource_links(
    current: ResourceLinksReview,
    extra: ResourceLinksReview,
) -> ResourceLinksReview:
    def _merge(bucket: tuple[object, ...], incoming: tuple[object, ...]) -> tuple[object, ...]:
        merged: dict[tuple[str, str, str], object] = {}
        for item in (*bucket, *incoming):
            label = str(getattr(item, "label", ""))
            target = str(getattr(item, "target", ""))
            url = str(getattr(item, "url", "") or "")
            merged[(label.casefold(), target.casefold(), url)] = item
        return tuple(
            merged[key]
            for key in sorted(merged, key=lambda item: item)
        )

    return ResourceLinksReview(
        github=tuple(_merge(current.github, extra.github)),
        azure=tuple(_merge(current.azure, extra.azure)),
        foundry=tuple(_merge(current.foundry, extra.foundry)),
    )


__all__ = [
    "BootstrapApprovalHandlerProtocol",
    "BootstrapApprovalRecord",
    "BootstrapApprovalStep",
    "BootstrapAvailableAction",
    "BootstrapFoundryTargetRecord",
    "BootstrapRegistrationIntent",
    "BootstrapRegistrationIntentKind",
    "BootstrapVerificationChoice",
    "BootstrapVerificationChoiceKind",
    "BootstrapCommitHandlerProtocol",
    "BootstrapConnectionHandlerProtocol",
    "BootstrapDeploymentHandlerProtocol",
    "BootstrapChildReference",
    "BootstrapLifecycleStage",
    "BootstrapQuestion",
    "BootstrapQuestionChoice",
    "BootstrapQuestionKind",
    "BootstrapRepositoryHandlerProtocol",
    "BootstrapRollbackHandlerProtocol",
    "BootstrapRunner",
    "BootstrapRunnerStateEnvelope",
    "BootstrapRunnerStatePayload",
    "BootstrapRunnerStateStoreProtocol",
    "BootstrapStageOutcome",
    "BootstrapTurn",
    "FileBootstrapRunnerStateStore",
    "FoundryTargetResolutionHandlerProtocol",
    "RepositoryBinding",
    "RuntimeBinding",
    "default_runner_state_root",
    "lock_file_path",
    "next_runner_generation",
    "operation_directory",
    "repository_index_directory",
    "repository_index_file_path",
    "repository_index_lock_file_path",
    "state_file_path",
]
