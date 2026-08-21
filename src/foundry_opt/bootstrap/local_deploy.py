from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from foundry_opt.bootstrap.canonical import (
    canonical_json_bytes,
    canonical_sha256,
    safe_persisted_document,
)
from foundry_opt.bootstrap.contracts import (
    AgentId,
    BootstrapDocument,
    FoundryTargetState,
    GitCommit,
    RepositoryIdentity,
    RepositoryUrl,
    Sha256,
)
from foundry_opt.bootstrap.errors import BootstrapApplyError, BootstrapConfigError
from foundry_opt.bootstrap.foundry_targets import build_local_user_credential
from foundry_opt.bootstrap.local_commit import (
    LocalCommitReceipt,
    LocalCommitReview,
    LocalCommitStatus,
    LocalGitCommitCoordinator,
)
from foundry_opt.bootstrap.operation_state import default_state_root
from foundry_opt.bootstrap.owner_review import ResourceLink, ResourceLinksReview
from foundry_opt.bootstrap.shared import require_safe_operation_id
from foundry_opt.bootstrap.workflow_integration import resolve_registry_selection
from foundry_opt.poc.config import validate_repository_relative_path
from foundry_opt.poc.deploy import (
    DeploymentReceipt,
    DeploymentService,
    PACKAGE_FINGERPRINT_METADATA_KEY,
    PROFILE_FINGERPRINT_METADATA_KEY,
    REGISTRY_FINGERPRINT_METADATA_KEY,
    REPO_AGENT_ID_METADATA_KEY,
    SOURCE_FINGERPRINT_METADATA_KEY,
    TARGET_FINGERPRINT_METADATA_KEY,
    build_deployment_agent_metadata,
    build_repository_policy_from_registry_selection,
)
from foundry_opt.poc.foundry import AzureProjectsEvaluationBackend, FoundryPocClient
from foundry_opt.poc.source import package_git_source
from foundry_opt.poc.verification import resolve_deployment_verification

LocalDeploymentLifecycleState = Literal[
    "awaiting_approval",
    "applying",
    "applied",
    "failed",
]
LocalDeploymentAgentStatus = Literal["published", "reconciled"]

_STATE_FILE_NAME = "state.json"
_LOCK_FILE_NAME = "state.lock"
_MAX_STATE_BYTES = 1024 * 1024


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


class LocalDeploymentAgentPlan(BootstrapDocument):
    repo_agent_id: AgentId
    repository_identity: RepositoryIdentity
    config_path: str
    commit_sha: GitCommit
    project_endpoint: str
    agent_name: AgentId
    target_state: FoundryTargetState
    previous_version: str | None = Field(default=None, max_length=64)
    package_root: str
    source_sha256: Sha256
    package_sha256: Sha256
    profile_sha256: Sha256
    registry_sha256: Sha256
    target_sha256: Sha256
    verification_mode: Literal["foundry_evaluation", "repository_checks", "none"]
    verification_warning: str | None = Field(default=None, max_length=1024)

    @field_validator("config_path")
    @classmethod
    def _validate_config_path(cls, value: str) -> str:
        return validate_repository_relative_path(value, field="config_path")

    @field_validator("package_root")
    @classmethod
    def _validate_package_root(cls, value: str) -> str:
        if value == ".":
            return value
        return validate_repository_relative_path(value, field="package_root")

    @model_validator(mode="after")
    def _validate_target(self) -> Self:
        if self.target_state not in {
            "new_target",
            "existing_aligned",
            "existing_diverged",
        }:
            raise BootstrapConfigError(
                "local deployment requires a new, aligned, or explicitly reviewed diverged target"
            )
        safe_persisted_document(self.model_dump(mode="json"))
        return self


class LocalDeploymentPlan(BootstrapDocument):
    operation_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    repository_identity: RepositoryIdentity
    runtime_repository: RepositoryUrl
    runtime_commit: GitCommit
    commit_receipt_hash: Sha256
    commit_sha: GitCommit
    agents: tuple[LocalDeploymentAgentPlan, ...]
    plan_hash: Sha256

    def _hash_payload(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "repository_identity": self.repository_identity,
            "runtime_repository": self.runtime_repository,
            "runtime_commit": self.runtime_commit,
            "commit_receipt_hash": self.commit_receipt_hash,
            "commit_sha": self.commit_sha,
            "agents": [item.model_dump(mode="json") for item in self.agents],
        }

    @classmethod
    def create(cls, **values: object) -> "LocalDeploymentPlan":
        payload = _jsonable(dict(values))
        validated = cls.model_validate({**payload, "plan_hash": "0" * 64})
        return cls.model_validate(
            {
                **validated.model_dump(mode="json", exclude={"plan_hash"}),
                "plan_hash": canonical_sha256(validated._hash_payload()),
            }
        )

    @model_validator(mode="after")
    def _validate_plan(self) -> Self:
        if not self.agents:
            raise BootstrapConfigError(
                "local deployment plan requires at least one enabled agent"
            )
        ids = [item.repo_agent_id.casefold() for item in self.agents]
        if len(ids) != len(set(ids)):
            raise BootstrapConfigError(
                "local deployment plan contains duplicate repoAgentId values"
            )
        if any(
            item.repository_identity != self.repository_identity
            or item.commit_sha != self.commit_sha
            for item in self.agents
        ):
            raise BootstrapConfigError(
                "local deployment agent plans must match the parent repository and commit"
            )
        if self.plan_hash != "0" * 64 and self.plan_hash != canonical_sha256(
            self._hash_payload()
        ):
            raise BootstrapApplyError(
                "local deployment plan hash does not match the canonical payload"
            )
        return self

    def render_markdown(self) -> str:
        lines = [
            "## Deployment review",
            f"- Exact commit: `{self.commit_sha}`",
            f"- Agents: {len(self.agents)}",
            "- Authentication: the current local Azure identity",
            "- Route behavior: publish a regular immutable version; never set an explicit version selector",
        ]
        for agent in self.agents:
            action = (
                "create the first regular version"
                if agent.target_state == "new_target"
                else "reconcile identical code or publish a new regular version"
            )
            lines.extend(
                (
                    f"- `{agent.repo_agent_id}`",
                    f"  - Foundry project: {agent.project_endpoint}",
                    f"  - Foundry agent: {agent.agent_name}",
                    f"  - Target state: {agent.target_state.replace('_', ' ')}",
                    f"  - Verification: {agent.verification_mode.replace('_', ' ')}",
                    f"  - Action: {action}",
                )
            )
            if agent.target_state == "existing_diverged":
                lines.append(
                    "  - Warning: the current Foundry version differs from this exact commit"
                )
            if agent.verification_warning:
                lines.append(f"  - Warning: {agent.verification_warning}")
        lines.append(
            "- Approval scope: deploy only these agents from this exact commit and reviewed targets"
        )
        return "\n".join(lines)


class LocalDeploymentApproval(BootstrapDocument):
    repository_identity: RepositoryIdentity
    operation_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    runtime_commit: GitCommit
    plan_hash: Sha256
    actor: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    summary: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    approval_hash: Sha256

    def _hash_payload(self) -> dict[str, object]:
        return {
            "repository_identity": self.repository_identity,
            "operation_id": self.operation_id,
            "runtime_commit": self.runtime_commit,
            "plan_hash": self.plan_hash,
            "actor": self.actor,
            "summary": self.summary,
        }

    @classmethod
    def create(
        cls,
        *,
        plan: LocalDeploymentPlan,
        actor: str,
        summary: str,
    ) -> "LocalDeploymentApproval":
        payload = {
            "repository_identity": plan.repository_identity,
            "operation_id": plan.operation_id,
            "runtime_commit": plan.runtime_commit,
            "plan_hash": plan.plan_hash,
            "actor": actor,
            "summary": summary,
        }
        safe_persisted_document(payload)
        return cls.model_validate(
            {**payload, "approval_hash": canonical_sha256(payload)}
        )

    @model_validator(mode="after")
    def _validate_hash(self) -> Self:
        if self.approval_hash != canonical_sha256(self._hash_payload()):
            raise BootstrapApplyError(
                "local deployment approval hash does not match the approval payload"
            )
        return self


class LocalDeploymentAgentReceipt(BootstrapDocument):
    repo_agent_id: AgentId
    commit_sha: GitCommit
    project_endpoint: str
    agent_name: AgentId
    status: LocalDeploymentAgentStatus
    published_version: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    previous_version: str | None = Field(default=None, max_length=64)
    source_tree_sha256: Sha256
    source_zip_sha256: Sha256
    package_sha256: Sha256
    profile_sha256: Sha256
    registry_sha256: Sha256
    target_sha256: Sha256
    verification_mode: Literal["foundry_evaluation", "repository_checks", "none"]
    verification_status: str
    verification_warning: str | None = Field(default=None, max_length=1024)
    evaluation_link: str | None = Field(default=None, max_length=2048)
    draft_cleanup_complete: Literal[True] = True
    route_mutated: Literal[False] = False
    latest_verified: Literal[True] = True


class LocalDeploymentReceipt(BootstrapDocument):
    operation_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    repository_identity: RepositoryIdentity
    runtime_repository: RepositoryUrl
    runtime_commit: GitCommit
    plan_hash: Sha256
    approval_hash: Sha256
    commit_sha: GitCommit
    agents: tuple[LocalDeploymentAgentReceipt, ...]
    receipt_hash: Sha256

    def _hash_payload(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "repository_identity": self.repository_identity,
            "runtime_repository": self.runtime_repository,
            "runtime_commit": self.runtime_commit,
            "plan_hash": self.plan_hash,
            "approval_hash": self.approval_hash,
            "commit_sha": self.commit_sha,
            "agents": [item.model_dump(mode="json") for item in self.agents],
        }

    @classmethod
    def create(cls, **values: object) -> "LocalDeploymentReceipt":
        payload = _jsonable(dict(values))
        validated = cls.model_validate({**payload, "receipt_hash": "0" * 64})
        return cls.model_validate(
            {
                **validated.model_dump(mode="json", exclude={"receipt_hash"}),
                "receipt_hash": canonical_sha256(validated._hash_payload()),
            }
        )

    @model_validator(mode="after")
    def _validate_hash(self) -> Self:
        if self.receipt_hash != "0" * 64 and self.receipt_hash != canonical_sha256(
            self._hash_payload()
        ):
            raise BootstrapApplyError(
                "local deployment receipt hash does not match the canonical payload"
            )
        return self


class LocalDeploymentStatePayload(BootstrapDocument):
    generation: int = Field(ge=0)
    lifecycle_state: LocalDeploymentLifecycleState
    plan: LocalDeploymentPlan
    approval: LocalDeploymentApproval | None = None
    completed_agents: tuple[LocalDeploymentAgentReceipt, ...] = ()
    error_summary: str | None = Field(default=None, max_length=1024)


class LocalDeploymentStateEnvelope(BootstrapDocument):
    payload: LocalDeploymentStatePayload
    generation_hash: Sha256

    @property
    def generation(self) -> int:
        return self.payload.generation

    @property
    def lifecycle_state(self) -> LocalDeploymentLifecycleState:
        return self.payload.lifecycle_state

    @property
    def plan(self) -> LocalDeploymentPlan:
        return self.payload.plan

    @property
    def approval(self) -> LocalDeploymentApproval | None:
        return self.payload.approval

    @property
    def completed_agents(self) -> tuple[LocalDeploymentAgentReceipt, ...]:
        return self.payload.completed_agents

    @classmethod
    def create(cls, **values: object) -> "LocalDeploymentStateEnvelope":
        payload = LocalDeploymentStatePayload.model_validate(values)
        body = payload.model_dump(mode="json")
        return cls.model_validate(
            {
                "payload": body,
                "generation_hash": canonical_sha256({"payload": body}),
            }
        )

    @model_validator(mode="after")
    def _validate_hash(self) -> Self:
        if self.generation_hash != canonical_sha256(
            {"payload": self.payload.model_dump(mode="json")}
        ):
            raise BootstrapApplyError(
                "local deployment state hash does not match the payload"
            )
        return self


class LocalDeploymentAdapterProtocol(Protocol):
    def deploy(
        self,
        repository_root: Path,
        plan: LocalDeploymentAgentPlan,
    ) -> LocalDeploymentAgentReceipt: ...


class LocalCommitStateProtocol(Protocol):
    lifecycle_state: str
    review: LocalCommitReview
    receipt: LocalCommitReceipt | None


class LocalCommitCoordinatorProtocol(Protocol):
    def load_state(
        self,
        *,
        repository_identity: str,
        operation_id: str,
        runtime_commit: str,
    ) -> LocalCommitStateProtocol: ...

    def status(self, review: LocalCommitReview) -> LocalCommitStatus: ...


class DefaultLocalDeploymentAdapter(LocalDeploymentAdapterProtocol):
    def __init__(self, *, deadline_seconds: float = 1800.0) -> None:
        self._deadline_seconds = deadline_seconds

    def deploy(
        self,
        repository_root: Path,
        plan: LocalDeploymentAgentPlan,
    ) -> LocalDeploymentAgentReceipt:
        selection = resolve_registry_selection(
            repository_root,
            repo_agent_id=plan.repo_agent_id,
        )
        if (
            selection.config_path != plan.config_path
            or selection.registry_hash != plan.registry_sha256
            or selection.sidecar_hash != plan.profile_sha256
            or selection.sidecar.package_root != plan.package_root
            or selection.sidecar.foundry_project.project_endpoint
            != plan.project_endpoint
            or selection.sidecar.foundry_project.agent_name != plan.agent_name
        ):
            raise BootstrapApplyError(
                "local deployment inputs drifted from the approved agent plan"
            )
        verification = resolve_deployment_verification(profile=selection.sidecar)
        if verification.mode != plan.verification_mode:
            raise BootstrapApplyError(
                "local deployment verification mode drifted from the approved plan"
            )
        packaged = package_git_source(
            repository_root,
            commit=plan.commit_sha,
            source_root=plan.package_root,
        )
        credential = build_local_user_credential()
        client: FoundryPocClient | None = None
        try:
            backend = AzureProjectsEvaluationBackend(
                project_endpoint=plan.project_endpoint,
                credential=credential,
            )
            client = FoundryPocClient(
                plan.project_endpoint,
                credential,
                evaluation_backend=backend,
            )
            service = DeploymentService(
                client=client,
                policy=build_repository_policy_from_registry_selection(selection),
                metadata=build_deployment_agent_metadata(
                    selection,
                    verification=verification,
                ),
                deadline_seconds=self._deadline_seconds,
                allow_missing_target=plan.target_state == "new_target",
            )
            receipt: DeploymentReceipt = service.publish(
                repository=plan.repository_identity,
                release_commit=plan.commit_sha,
                packaged=packaged,
                repository_root=repository_root,
                verification=verification,
                reconciliation_metadata={
                    REPO_AGENT_ID_METADATA_KEY: plan.repo_agent_id,
                    SOURCE_FINGERPRINT_METADATA_KEY: plan.source_sha256,
                    PACKAGE_FINGERPRINT_METADATA_KEY: plan.package_sha256,
                    PROFILE_FINGERPRINT_METADATA_KEY: plan.profile_sha256,
                    REGISTRY_FINGERPRINT_METADATA_KEY: plan.registry_sha256,
                    TARGET_FINGERPRINT_METADATA_KEY: plan.target_sha256,
                },
            )
        finally:
            if client is not None:
                client.close()
            closer = getattr(credential, "close", None)
            if callable(closer):
                closer()
        warning = (
            None
            if receipt.verification.warning is None
            else receipt.verification.warning.message
        )
        return LocalDeploymentAgentReceipt(
            repo_agent_id=plan.repo_agent_id,
            commit_sha=plan.commit_sha,
            project_endpoint=receipt.project_endpoint,
            agent_name=receipt.agent_name,
            status="reconciled" if receipt.reconciled else "published",
            published_version=receipt.published_version,
            previous_version=receipt.previous_version,
            source_tree_sha256=receipt.source_tree_sha256,
            source_zip_sha256=receipt.source_zip_sha256,
            package_sha256=plan.package_sha256,
            profile_sha256=plan.profile_sha256,
            registry_sha256=plan.registry_sha256,
            target_sha256=plan.target_sha256,
            verification_mode=receipt.verification.mode,
            verification_status=receipt.verification.status,
            verification_warning=warning,
            evaluation_link=receipt.evaluation_link,
        )


def default_local_deployment_state_root() -> Path:
    return default_state_root() / "local-deployment"


def _operation_directory(
    repository_identity: str,
    operation_id: str,
    *,
    state_root: Path,
) -> Path:
    root = state_root.resolve()
    operation_segment = require_safe_operation_id(
        operation_id,
        message="local deployment operation id is invalid",
        error_factory=BootstrapApplyError,
    )
    target = (
        root
        / canonical_sha256({"repository_identity": repository_identity})
        / operation_segment
    ).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise BootstrapApplyError(
            "local deployment state escapes the state root"
        ) from exc
    return target


def _state_path(
    repository_identity: str,
    operation_id: str,
    *,
    state_root: Path,
) -> Path:
    return (
        _operation_directory(
            repository_identity,
            operation_id,
            state_root=state_root,
        )
        / _STATE_FILE_NAME
    )


class LocalDeploymentCoordinator:
    def __init__(
        self,
        *,
        adapter: LocalDeploymentAdapterProtocol | None = None,
        commit_coordinator: LocalCommitCoordinatorProtocol | None = None,
        state_root: Path | None = None,
    ) -> None:
        self._adapter = adapter or DefaultLocalDeploymentAdapter()
        self._commit_coordinator = commit_coordinator or LocalGitCommitCoordinator()
        self._state_root = (
            Path(state_root)
            if state_root is not None
            else default_local_deployment_state_root()
        )

    def build_plan(self, operation) -> LocalDeploymentPlan:
        existing = self._try_load(
            operation.repository_binding.repository_id,
            operation.operation_id,
        )
        if existing is not None:
            self._validate_operation(existing.plan, operation)
            return existing.plan
        commit_state = self._commit_coordinator.load_state(
            repository_identity=operation.repository_binding.repository_id,
            operation_id=operation.operation_id,
            runtime_commit=operation.runtime_binding.runtime_commit,
        )
        if commit_state.lifecycle_state != "committed" or commit_state.receipt is None:
            raise BootstrapApplyError(
                "local deployment requires the reviewed local commit receipt"
            )
        self._commit_coordinator.status(commit_state.review)
        commit_receipt = commit_state.receipt
        if commit_receipt.commit_sha != operation.repository_binding.head_commit:
            raise BootstrapApplyError(
                "local deployment commit does not match the runner repository binding"
            )
        targets = {
            item.repo_agent_id.casefold(): item.reviewed_target
            for item in operation.foundry_targets
        }
        profile_hashes = {
            item.repo_agent_id.casefold(): item
            for item in commit_receipt.profile_hashes
        }
        agent_hashes = {
            item.repo_agent_id.casefold(): item
            for item in commit_receipt.agent_hashes
        }
        repository_root = Path(operation.repository_binding.repository_root)
        agents: list[LocalDeploymentAgentPlan] = []
        for repo_agent_id in sorted(
            operation.selection_plan.selected_agent_ids,
            key=str.casefold,
        ):
            key = repo_agent_id.casefold()
            target = targets.get(key)
            profile_hash = profile_hashes.get(key)
            agent_hash = agent_hashes.get(key)
            if target is None or profile_hash is None or agent_hash is None:
                raise BootstrapApplyError(
                    "local deployment is missing a reviewed target or exact commit fingerprint"
                )
            if (
                target.state not in {
                    "new_target",
                    "existing_aligned",
                    "existing_diverged",
                }
                or not target.deployment_ready
                or target.project_endpoint is None
                or target.agent_name is None
            ):
                raise BootstrapApplyError(
                    f"local deployment target is not ready: {repo_agent_id}"
                )
            selection = resolve_registry_selection(
                repository_root,
                repo_agent_id=repo_agent_id,
            )
            sidecar_target = selection.sidecar.foundry_target
            if sidecar_target is None or sidecar_target != target:
                raise BootstrapApplyError(
                    "committed profile does not contain the exact reviewed Foundry target"
                )
            if (
                selection.registry_hash != commit_receipt.registry_sha256
                or selection.sidecar_hash != profile_hash.sha256
                or selection.config_path != profile_hash.profile_path
                or selection.sidecar.package_root != agent_hash.package_root
            ):
                raise BootstrapApplyError(
                    "committed registry/profile fingerprints do not match deployment inputs"
                )
            verification = resolve_deployment_verification(
                profile=selection.sidecar
            )
            warning = (
                None
                if verification.warning is None
                else verification.warning.message
            )
            agents.append(
                LocalDeploymentAgentPlan(
                    repo_agent_id=repo_agent_id,
                    repository_identity=operation.repository_binding.repository_id,
                    config_path=selection.config_path,
                    commit_sha=commit_receipt.commit_sha,
                    project_endpoint=target.project_endpoint,
                    agent_name=target.agent_name,
                    target_state=target.state,
                    previous_version=target.latest_agent_version,
                    package_root=agent_hash.package_root,
                    source_sha256=agent_hash.source_sha256,
                    package_sha256=agent_hash.package_sha256,
                    profile_sha256=profile_hash.sha256,
                    registry_sha256=commit_receipt.registry_sha256,
                    target_sha256=canonical_sha256(
                        target.model_dump(mode="json")
                    ),
                    verification_mode=verification.mode,
                    verification_warning=warning,
                )
            )
        plan = LocalDeploymentPlan.create(
            operation_id=operation.operation_id,
            repository_identity=operation.repository_binding.repository_id,
            runtime_repository=operation.runtime_binding.runtime_repository,
            runtime_commit=operation.runtime_binding.runtime_commit,
            commit_receipt_hash=commit_receipt.receipt_hash,
            commit_sha=commit_receipt.commit_sha,
            agents=tuple(agents),
        )
        self._write(
            LocalDeploymentStateEnvelope.create(
                generation=0,
                lifecycle_state="awaiting_approval",
                plan=plan,
            )
        )
        return plan

    def create_approval(
        self,
        plan: LocalDeploymentPlan,
        *,
        actor: str,
        summary: str,
    ) -> LocalDeploymentApproval:
        return LocalDeploymentApproval.create(
            plan=plan,
            actor=actor,
            summary=summary,
        )

    def apply(
        self,
        operation,
        plan: LocalDeploymentPlan,
        approval: LocalDeploymentApproval,
    ) -> LocalDeploymentReceipt:
        self._validate_operation(plan, operation)
        envelope = self._load(plan.repository_identity, plan.operation_id)
        if envelope.plan != plan:
            raise BootstrapApplyError(
                "local deployment plan does not match persisted state"
            )
        self._validate_approval(plan, approval)
        if envelope.approval is not None and envelope.approval != approval:
            raise BootstrapApplyError(
                "local deployment approval does not match the recorded approval"
            )
        completed = {
            item.repo_agent_id.casefold(): item
            for item in envelope.completed_agents
        }
        if envelope.lifecycle_state == "applied":
            return self._receipt(envelope)
        current = self._next(
            envelope,
            lifecycle_state="applying",
            approval=approval,
            error_summary=None,
        )
        self._write(current, expected=envelope)
        repository_root = Path(operation.repository_binding.repository_root)
        try:
            for agent in plan.agents:
                if agent.repo_agent_id.casefold() in completed:
                    continue
                result = self._adapter.deploy(repository_root, agent)
                completed[agent.repo_agent_id.casefold()] = result
                latest = self._load(plan.repository_identity, plan.operation_id)
                updated = self._next(
                    latest,
                    lifecycle_state="applying",
                    completed_agents=tuple(
                        completed[key] for key in sorted(completed)
                    ),
                )
                self._write(updated, expected=latest)
            latest = self._load(plan.repository_identity, plan.operation_id)
            applied = self._next(
                latest,
                lifecycle_state="applied",
                completed_agents=tuple(
                    completed[key] for key in sorted(completed)
                ),
            )
            self._write(applied, expected=latest)
            return self._receipt(applied)
        except Exception as exc:
            latest = self._load(plan.repository_identity, plan.operation_id)
            failed = self._next(
                latest,
                lifecycle_state="failed",
                completed_agents=tuple(
                    completed[key] for key in sorted(completed)
                ),
                error_summary=(
                    str(exc).strip() or type(exc).__name__
                )[:1024],
            )
            self._write(failed, expected=latest)
            raise BootstrapApplyError(
                "local deployment failed; the exact approved plan can be resumed"
            ) from exc

    def status(
        self,
        *,
        repository_identity: str,
        operation_id: str,
        runtime_commit: str,
    ) -> LocalDeploymentStateEnvelope:
        envelope = self._load(repository_identity, operation_id)
        if envelope.plan.runtime_commit != runtime_commit:
            raise BootstrapApplyError(
                "local deployment status requires the exact runtime commit"
            )
        return envelope

    def resource_links(self, plan: LocalDeploymentPlan) -> ResourceLinksReview:
        return ResourceLinksReview(
            foundry=tuple(
                ResourceLink(
                    label=f"Foundry agent: {agent.repo_agent_id}",
                    target=f"{agent.project_endpoint} / {agent.agent_name}",
                    url=agent.project_endpoint,
                )
                for agent in plan.agents
            )
        )

    def _validate_operation(self, plan: LocalDeploymentPlan, operation) -> None:
        if (
            plan.operation_id != operation.operation_id
            or plan.repository_identity
            != operation.repository_binding.repository_id
            or plan.runtime_repository
            != operation.runtime_binding.runtime_repository
            or plan.runtime_commit != operation.runtime_binding.runtime_commit
            or plan.commit_sha != operation.repository_binding.head_commit
        ):
            raise BootstrapApplyError(
                "local deployment plan does not match the active bootstrap operation"
            )

    @staticmethod
    def _validate_approval(
        plan: LocalDeploymentPlan,
        approval: LocalDeploymentApproval,
    ) -> None:
        if (
            approval.repository_identity != plan.repository_identity
            or approval.operation_id != plan.operation_id
            or approval.runtime_commit != plan.runtime_commit
            or approval.plan_hash != plan.plan_hash
        ):
            raise BootstrapApplyError(
                "local deployment approval does not match the exact plan"
            )

    @staticmethod
    def _receipt(
        envelope: LocalDeploymentStateEnvelope,
    ) -> LocalDeploymentReceipt:
        approval = envelope.approval
        if envelope.lifecycle_state != "applied" or approval is None:
            raise BootstrapApplyError(
                "local deployment receipt requires an applied state and approval"
            )
        return LocalDeploymentReceipt.create(
            operation_id=envelope.plan.operation_id,
            repository_identity=envelope.plan.repository_identity,
            runtime_repository=envelope.plan.runtime_repository,
            runtime_commit=envelope.plan.runtime_commit,
            plan_hash=envelope.plan.plan_hash,
            approval_hash=approval.approval_hash,
            commit_sha=envelope.plan.commit_sha,
            agents=envelope.completed_agents,
        )

    def _try_load(
        self,
        repository_identity: str,
        operation_id: str,
    ) -> LocalDeploymentStateEnvelope | None:
        try:
            return self._load(repository_identity, operation_id)
        except FileNotFoundError:
            return None

    def _load(
        self,
        repository_identity: str,
        operation_id: str,
    ) -> LocalDeploymentStateEnvelope:
        path = _state_path(
            repository_identity,
            operation_id,
            state_root=self._state_root,
        )
        data = path.read_bytes()
        if len(data) > _MAX_STATE_BYTES:
            raise BootstrapApplyError(
                "local deployment state exceeds the size limit"
            )
        try:
            return LocalDeploymentStateEnvelope.model_validate_json(data)
        except Exception as exc:
            raise BootstrapApplyError(
                "local deployment state is invalid or tampered"
            ) from exc

    def _write(
        self,
        envelope: LocalDeploymentStateEnvelope,
        *,
        expected: LocalDeploymentStateEnvelope | None = None,
    ) -> None:
        path = _state_path(
            envelope.plan.repository_identity,
            envelope.plan.operation_id,
            state_root=self._state_root,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = path.parent / _LOCK_FILE_NAME
        try:
            lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise BootstrapApplyError(
                "local deployment state is locked by another writer"
            ) from exc
        try:
            if expected is None:
                if path.exists():
                    raise BootstrapApplyError(
                        "local deployment state already exists"
                    )
            else:
                current = self._load(
                    envelope.plan.repository_identity,
                    envelope.plan.operation_id,
                )
                if (
                    current.generation != expected.generation
                    or current.generation_hash != expected.generation_hash
                ):
                    raise BootstrapApplyError(
                        "local deployment state generation conflict"
                    )
            data = canonical_json_bytes(envelope.model_dump(mode="json")) + b"\n"
            temp = path.with_name(
                f"{path.stem}.{envelope.generation_hash}.tmp"
            )
            with open(temp, "xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            os.close(lock_fd)
            os.unlink(lock)

    @staticmethod
    def _next(
        envelope: LocalDeploymentStateEnvelope,
        **updates: object,
    ) -> LocalDeploymentStateEnvelope:
        payload = envelope.payload.model_dump(mode="python")
        payload.update(updates)
        payload["generation"] = envelope.generation + 1
        return LocalDeploymentStateEnvelope.create(**payload)


class BootstrapLocalDeploymentHandler:
    def __init__(
        self,
        *,
        coordinator: LocalDeploymentCoordinator | None = None,
    ) -> None:
        self._coordinator = coordinator or LocalDeploymentCoordinator()

    def review(self, *, operation) -> LocalDeploymentPlan:
        return self._coordinator.build_plan(operation)

    def approve(self, *, operation, approval) -> object:
        from foundry_opt.bootstrap.runner import (
            BootstrapChildReference,
            BootstrapStageOutcome,
        )

        plan = self.review(operation=operation)
        local_approval = self._coordinator.create_approval(
            plan,
            actor=approval.actor,
            summary=approval.summary,
        )
        receipt = self._coordinator.apply(
            operation,
            plan,
            local_approval,
        )
        child_refs = tuple(
            item for item in operation.child_refs if item.step != "deployment"
        )
        versions = ", ".join(
            f"{item.repo_agent_id}={item.published_version}"
            for item in receipt.agents
        )
        return BootstrapStageOutcome(
            stage="final_handoff",
            note=f"Local Foundry deployment completed from the exact commit: {versions}.",
            child_refs=(
                *child_refs,
                BootstrapChildReference(
                    step="deployment",
                    kind="local-foundry-deployment",
                    identifier=receipt.receipt_hash,
                    summary=versions,
                ),
            ),
        )

    def validate_resume(self, *, operation) -> None:
        try:
            envelope = self._coordinator.status(
                repository_identity=operation.repository_binding.repository_id,
                operation_id=operation.operation_id,
                runtime_commit=operation.runtime_binding.runtime_commit,
            )
        except FileNotFoundError:
            return
        if envelope.plan.commit_sha != operation.repository_binding.head_commit:
            raise BootstrapApplyError(
                "local deployment resume requires the exact reviewed commit"
            )

    def build_resource_links(self, *, operation) -> ResourceLinksReview:
        return self._coordinator.resource_links(self.review(operation=operation))


__all__ = [
    "BootstrapLocalDeploymentHandler",
    "DefaultLocalDeploymentAdapter",
    "LocalDeploymentAdapterProtocol",
    "LocalDeploymentAgentPlan",
    "LocalDeploymentAgentReceipt",
    "LocalDeploymentApproval",
    "LocalDeploymentCoordinator",
    "LocalDeploymentPlan",
    "LocalDeploymentReceipt",
    "LocalDeploymentStateEnvelope",
]
