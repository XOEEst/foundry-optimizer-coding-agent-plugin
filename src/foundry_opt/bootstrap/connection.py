from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, field_validator, model_validator

from foundry_opt.bootstrap.canonical import canonical_json_bytes, canonical_sha256, safe_persisted_document
from foundry_opt.bootstrap.contracts import (
    BootstrapDocument,
    BootstrapPlan,
    BootstrapReceipt,
    FingerprintRecord,
    GitCommit,
    RedactedStatusInfo,
    RepositoryIdentity,
    RepositoryUrl,
    Sha256,
)
from foundry_opt.bootstrap.errors import BootstrapApplyError, BootstrapConfigError
from foundry_opt.bootstrap.operation_state import default_state_root
from foundry_opt.bootstrap.orchestrator import PhaseDriver
from foundry_opt.bootstrap.receipts import PhaseReceipt, failure_receipt, summarize_receipt

ConnectionPhaseName = Literal["github", "azure"]
ConnectionPhaseStateName = Literal[
    "pending",
    "applying",
    "applied",
    "failed",
    "compensation_required",
    "rolled_back",
]
ConnectionOverallState = Literal[
    "awaiting_approval",
    "pending",
    "partial",
    "applying",
    "applied",
    "failed",
    "rolled_back",
]

_STEP_ID = "connect-github-to-azure"
_STATE_FILE_NAME = "state.json"
_LOCK_FILE_NAME = "state.lock"
_MAX_STATE_BYTES = 512 * 1024
_PHASES: tuple[ConnectionPhaseName, ...] = ("github", "azure")
_SANITIZED_ERROR_CODES = {
    BootstrapApplyError: "apply-invalid",
    ValueError: "provider-invalid",
    RuntimeError: "provider-runtime",
    Exception: "provider-failed",
}

def _jsonable(value: object) -> object:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")  # type: ignore[no-any-return]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _sorted_fingerprints(values: Sequence[FingerprintRecord]) -> tuple[FingerprintRecord, ...]:
    return tuple(sorted(values, key=lambda item: (item.label, item.sha256)))


def _failure_details(exc: BaseException) -> tuple[BootstrapReceipt | None, Mapping[str, object]]:
    receipt = getattr(exc, "compensation_receipt", None)
    provider_state = getattr(exc, "provider_state", None)
    if isinstance(receipt, BootstrapReceipt) and isinstance(provider_state, Mapping):
        return receipt, dict(provider_state)
    return None, {}


class ConnectionPhasePlan(BootstrapDocument):
    phase: ConnectionPhaseName
    plan: BootstrapPlan
    live_fingerprints: tuple[FingerprintRecord, ...] = ()

    @field_validator("live_fingerprints")
    @classmethod
    def _validate_live_fingerprints(
        cls,
        value: Sequence[FingerprintRecord],
    ) -> tuple[FingerprintRecord, ...]:
        payload = tuple(value)
        safe_persisted_document([item.model_dump(mode="json") for item in payload])
        return _sorted_fingerprints(payload)

    @model_validator(mode="after")
    def _validate_phase_plan(self) -> Self:
        if any(action.phase != self.phase for action in self.plan.actions):
            raise BootstrapApplyError("connection phase plan contains an action for the wrong phase")
        return self


class ConnectionPlan(BootstrapDocument):
    step_id: Literal["connect-github-to-azure"] = _STEP_ID
    operation_id: str
    runtime_repository: RepositoryUrl
    runtime_commit: GitCommit
    repository_identity: RepositoryIdentity
    phase_plans: tuple[ConnectionPhasePlan, ...]
    plan_hash: Sha256

    def _hash_payload(self) -> dict[str, object]:
        return {
            "step_id": self.step_id,
            "operation_id": self.operation_id,
            "runtime_repository": self.runtime_repository,
            "runtime_commit": self.runtime_commit,
            "repository_identity": self.repository_identity,
            "phase_plans": [item.model_dump(mode="json") for item in self.phase_plans],
        }

    @classmethod
    def create(cls, **values: object) -> "ConnectionPlan":
        payload = _jsonable(dict(values))
        if "step_id" not in payload:
            payload["step_id"] = _STEP_ID
        hash_payload = {
            "step_id": payload["step_id"],
            "operation_id": payload["operation_id"],
            "runtime_repository": payload["runtime_repository"],
            "runtime_commit": payload["runtime_commit"],
            "repository_identity": payload["repository_identity"],
            "phase_plans": payload["phase_plans"],
        }
        return cls.model_validate({**payload, "plan_hash": canonical_sha256(hash_payload)})

    def phase_plan(self, phase: ConnectionPhaseName) -> ConnectionPhasePlan:
        for item in self.phase_plans:
            if item.phase == phase:
                return item
        raise BootstrapApplyError(f"connection plan does not contain the {phase} phase")

    @model_validator(mode="after")
    def _validate_plan(self) -> Self:
        if tuple(item.phase for item in self.phase_plans) != _PHASES:
            raise BootstrapApplyError("connection plan must contain github then azure phase plans")
        for item in self.phase_plans:
            if (
                item.plan.operation_id != self.operation_id
                or item.plan.runtime_repository != self.runtime_repository
                or item.plan.runtime_commit != self.runtime_commit
                or item.plan.repository_identity != self.repository_identity
            ):
                raise BootstrapApplyError("connection child phase plan does not match the composite identity")
        if self.plan_hash != canonical_sha256(self._hash_payload()):
            raise BootstrapApplyError("connection plan hash does not match the canonical payload")
        return self


class ConnectionApproval(BootstrapDocument):
    step_id: Literal["connect-github-to-azure"] = _STEP_ID
    repository_identity: RepositoryIdentity
    operation_id: str
    parent_plan_hash: Sha256
    runtime_commit: GitCommit
    actor: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    summary: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    approval_hash: Sha256

    def _hash_payload(self) -> dict[str, object]:
        return {
            "step_id": self.step_id,
            "repository_identity": self.repository_identity,
            "operation_id": self.operation_id,
            "parent_plan_hash": self.parent_plan_hash,
            "runtime_commit": self.runtime_commit,
            "actor": self.actor,
            "summary": self.summary,
        }

    @classmethod
    def create(
        cls,
        *,
        repository_identity: str,
        operation_id: str,
        parent_plan_hash: str,
        runtime_commit: str,
        actor: str,
        summary: str,
    ) -> "ConnectionApproval":
        payload = {
            "step_id": _STEP_ID,
            "repository_identity": repository_identity,
            "operation_id": operation_id,
            "parent_plan_hash": parent_plan_hash,
            "runtime_commit": runtime_commit,
            "actor": actor,
            "summary": summary,
        }
        safe_persisted_document(payload)
        return cls.model_validate(
            {
                **payload,
                "approval_hash": canonical_sha256(payload),
            }
        )

    @property
    def plan_hash(self) -> str:
        return self.parent_plan_hash

    @model_validator(mode="after")
    def _validate_hash(self) -> Self:
        if self.approval_hash != canonical_sha256(self._hash_payload()):
            raise BootstrapApplyError("connection approval hash does not match the approval payload")
        return self


class ConnectionPhaseState(BootstrapDocument):
    phase: ConnectionPhaseName
    state: ConnectionPhaseStateName
    plan_hash: Sha256
    receipt_hash: Sha256 | None = None
    approval_hash: Sha256 | None = None
    created_actions: tuple[str, ...] = ()
    adopted_actions: tuple[str, ...] = ()
    changed_actions: tuple[str, ...] = ()
    compensation_required_actions: tuple[str, ...] = ()
    summary: Annotated[str, StringConstraints(min_length=1, max_length=4096)]


class ConnectionStatus(BootstrapDocument):
    step_id: Literal["connect-github-to-azure"] = _STEP_ID
    operation_id: str
    repository_identity: RepositoryIdentity
    runtime_repository: RepositoryUrl
    runtime_commit: GitCommit
    plan_hash: Sha256
    approval_hash: Sha256 | None = None
    overall_state: ConnectionOverallState
    summary: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    phase_states: tuple[ConnectionPhaseState, ...]
    resumable: bool
    rollback_ready: bool
    next_action: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def _validate_phase_order(self) -> Self:
        if tuple(item.phase for item in self.phase_states) != _PHASES:
            raise BootstrapApplyError("connection status must report github then azure phase states")
        return self


class ConnectionReceipt(BootstrapDocument):
    step_id: Literal["connect-github-to-azure"] = _STEP_ID
    operation_id: str
    repository_identity: RepositoryIdentity
    runtime_repository: RepositoryUrl
    runtime_commit: GitCommit
    plan_hash: Sha256
    approval_hash: Sha256
    overall_state: ConnectionOverallState
    summary: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    phase_states: tuple[ConnectionPhaseState, ...]
    rolled_back_phases: tuple[ConnectionPhaseName, ...] = ()
    receipt_hash: Sha256

    def _hash_payload(self) -> dict[str, object]:
        return {
            "step_id": self.step_id,
            "operation_id": self.operation_id,
            "repository_identity": self.repository_identity,
            "runtime_repository": self.runtime_repository,
            "runtime_commit": self.runtime_commit,
            "plan_hash": self.plan_hash,
            "approval_hash": self.approval_hash,
            "overall_state": self.overall_state,
            "summary": self.summary,
            "phase_states": [item.model_dump(mode="json") for item in self.phase_states],
            "rolled_back_phases": list(self.rolled_back_phases),
        }

    @classmethod
    def create(cls, **values: object) -> "ConnectionReceipt":
        payload = _jsonable(dict(values))
        if "step_id" not in payload:
            payload["step_id"] = _STEP_ID
        if "rolled_back_phases" not in payload:
            payload["rolled_back_phases"] = ()
        hash_payload = {
            "step_id": payload["step_id"],
            "operation_id": payload["operation_id"],
            "repository_identity": payload["repository_identity"],
            "runtime_repository": payload["runtime_repository"],
            "runtime_commit": payload["runtime_commit"],
            "plan_hash": payload["plan_hash"],
            "approval_hash": payload["approval_hash"],
            "overall_state": payload["overall_state"],
            "summary": payload["summary"],
            "phase_states": payload["phase_states"],
            "rolled_back_phases": list(payload["rolled_back_phases"]),
        }
        return cls.model_validate({**payload, "receipt_hash": canonical_sha256(hash_payload)})

    @model_validator(mode="after")
    def _validate_receipt(self) -> Self:
        if tuple(item.phase for item in self.phase_states) != _PHASES:
            raise BootstrapApplyError("connection receipt must reference github then azure phases")
        expected_rolled = tuple(item.phase for item in self.phase_states if item.state == "rolled_back")
        if self.rolled_back_phases != expected_rolled:
            raise BootstrapApplyError("connection receipt rolled_back_phases does not match child phase states")
        if self.receipt_hash != canonical_sha256(self._hash_payload()):
            raise BootstrapApplyError("connection receipt hash does not match the canonical payload")
        return self


class ConnectionStatePayload(BootstrapDocument):
    generation: int = Field(ge=0)
    connection_plan: ConnectionPlan
    approval: ConnectionApproval | None = None
    phase_receipts: tuple[PhaseReceipt, ...] = ()

    @model_validator(mode="after")
    def _validate_state(self) -> Self:
        if self.approval is not None:
            if (
                self.approval.repository_identity != self.connection_plan.repository_identity
                or self.approval.operation_id != self.connection_plan.operation_id
                or self.approval.parent_plan_hash != self.connection_plan.plan_hash
                or self.approval.runtime_commit != self.connection_plan.runtime_commit
            ):
                raise BootstrapApplyError("connection approval does not match the active connection plan")
        seen: set[str] = set()
        for receipt in self.phase_receipts:
            if receipt.phase not in _PHASES:
                raise BootstrapApplyError("connection state contains an unsupported child phase")
            if receipt.phase in seen:
                raise BootstrapApplyError("connection state contains duplicate child phase receipts")
            seen.add(receipt.phase)
            phase_plan = self.connection_plan.phase_plan(receipt.phase)
            if receipt.parent_plan_hash != self.connection_plan.plan_hash:
                raise BootstrapApplyError("child phase receipt parent plan hash is stale")
            if receipt.phase_plan_hash != phase_plan.plan.plan_hash:
                raise BootstrapApplyError("child phase receipt plan hash does not match the active child plan")
            if (
                receipt.receipt.operation_id != self.connection_plan.operation_id
                or receipt.receipt.runtime_repository != self.connection_plan.runtime_repository
                or receipt.receipt.runtime_commit != self.connection_plan.runtime_commit
                or receipt.receipt.repository_identity != self.connection_plan.repository_identity
            ):
                raise BootstrapApplyError("child phase receipt does not match the active connection identity")
            if self.approval is not None and receipt.approval_hash != self.approval.approval_hash:
                raise BootstrapApplyError("child phase receipt approval is stale")
        return self


class ConnectionStateEnvelope(BootstrapDocument):
    payload: ConnectionStatePayload
    generation_hash: Sha256

    @property
    def generation(self) -> int:
        return self.payload.generation

    @property
    def connection_plan(self) -> ConnectionPlan:
        return self.payload.connection_plan

    @property
    def approval(self) -> ConnectionApproval | None:
        return self.payload.approval

    @property
    def phase_receipts(self) -> tuple[PhaseReceipt, ...]:
        return self.payload.phase_receipts

    @property
    def repository_identity(self) -> str:
        return self.connection_plan.repository_identity

    @property
    def operation_id(self) -> str:
        return self.connection_plan.operation_id

    @property
    def runtime_repository(self) -> str:
        return self.connection_plan.runtime_repository

    @property
    def runtime_commit(self) -> str:
        return self.connection_plan.runtime_commit

    @field_validator("payload")
    @classmethod
    def _validate_segment(cls, value: ConnectionStatePayload) -> ConnectionStatePayload:
        operation_id = value.connection_plan.operation_id
        if not operation_id or any(token in operation_id for token in ("/", "\\", "..")):
            raise BootstrapConfigError("connection operation id is not safe for state persistence")
        return value

    @model_validator(mode="after")
    def _validate_hash(self) -> Self:
        payload = {"payload": self.payload.model_dump(mode="json")}
        if self.generation_hash != canonical_sha256(payload):
            raise BootstrapApplyError("connection state generation hash does not match the payload")
        return self

    @classmethod
    def create(cls, **values: object) -> "ConnectionStateEnvelope":
        payload = _jsonable(dict(values))
        validated = ConnectionStatePayload.model_validate(payload)
        body = {"payload": validated.model_dump(mode="json")}
        return cls.model_validate(
            {
                "payload": body["payload"],
                "generation_hash": canonical_sha256(body),
            }
        )


def connection_state_root(state_root: Path | None = None) -> Path:
    return (state_root or default_state_root()).resolve() / "connection"


def connection_state_directory(
    repository_identity: str,
    operation_id: str,
    *,
    state_root: Path | None = None,
) -> Path:
    root = connection_state_root(state_root)
    repo_segment = canonical_sha256({"repository_id": repository_identity})
    target = (root / repo_segment / operation_id).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise BootstrapApplyError("connection state escapes the connection state root") from exc
    return target


def connection_state_file_path(
    repository_identity: str,
    operation_id: str,
    *,
    state_root: Path | None = None,
) -> Path:
    return connection_state_directory(repository_identity, operation_id, state_root=state_root) / _STATE_FILE_NAME


def connection_lock_file_path(
    repository_identity: str,
    operation_id: str,
    *,
    state_root: Path | None = None,
) -> Path:
    return connection_state_directory(repository_identity, operation_id, state_root=state_root) / _LOCK_FILE_NAME


def write_connection_state(
    envelope: ConnectionStateEnvelope,
    *,
    expected_generation: int | None = None,
    state_root: Path | None = None,
) -> Path:
    path = connection_state_file_path(
        envelope.repository_identity,
        envelope.operation_id,
        state_root=state_root,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = connection_lock_file_path(
        envelope.repository_identity,
        envelope.operation_id,
        state_root=state_root,
    )
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise BootstrapApplyError("connection state is locked by another writer") from exc
    try:
        if expected_generation is None and path.exists():
            raise BootstrapApplyError("connection state already exists")
        if expected_generation is not None and path.exists():
            current = read_connection_state(
                envelope.repository_identity,
                envelope.operation_id,
                state_root=state_root,
            )
            if current.generation != expected_generation:
                raise BootstrapApplyError("connection state generation conflict")
        data = canonical_json_bytes(envelope.model_dump(mode="json")) + b"\n"
        if len(data) > _MAX_STATE_BYTES:
            raise BootstrapApplyError("connection state exceeds the safe size limit")
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


def read_connection_state(
    repository_identity: str,
    operation_id: str,
    *,
    state_root: Path | None = None,
) -> ConnectionStateEnvelope:
    path = connection_state_file_path(repository_identity, operation_id, state_root=state_root)
    data = path.read_bytes()
    if len(data) > _MAX_STATE_BYTES:
        raise BootstrapApplyError("connection state exceeds the safe size limit")
    try:
        return ConnectionStateEnvelope.model_validate_json(data)
    except Exception as exc:
        raise BootstrapApplyError("connection state is invalid or tampered") from exc


def next_connection_generation(
    envelope: ConnectionStateEnvelope,
    **updates: object,
) -> ConnectionStateEnvelope:
    payload = envelope.payload.model_dump(mode="python")
    payload.update(updates)
    payload["generation"] = envelope.generation + 1
    return ConnectionStateEnvelope.create(**payload)


class GitHubAzureConnectionManager:
    def __init__(
        self,
        *,
        github_driver: PhaseDriver,
        azure_driver: PhaseDriver,
        state_root: Path | None = None,
    ) -> None:
        self._drivers: dict[ConnectionPhaseName, PhaseDriver] = {
            "github": github_driver,
            "azure": azure_driver,
        }
        self._state_root = Path(state_root) if state_root is not None else None

    def build_plan(
        self,
        *,
        repository_identity: str,
        operation_id: str,
        runtime_repository: str,
        runtime_commit: str,
        context: Mapping[str, object] | None = None,
    ) -> ConnectionPlan:
        phase_plans: list[ConnectionPhasePlan] = []
        for phase in _PHASES:
            phase_context = self._phase_context(
                repository_identity=repository_identity,
                operation_id=operation_id,
                runtime_repository=runtime_repository,
                runtime_commit=runtime_commit,
                phase=phase,
                context=context,
                parent_plan_hash=None,
            )
            live = _sorted_fingerprints(self._drivers[phase].live_fingerprints(phase_context))
            actions = tuple(self._drivers[phase].plan(phase_context))
            self._validate_phase_actions(phase, actions)
            phase_plan = BootstrapPlan.create(
                operation_id=operation_id,
                runtime_repository=runtime_repository,
                runtime_commit=runtime_commit,
                repository_identity=repository_identity,
                actions=actions,
            )
            phase_plans.append(
                ConnectionPhasePlan(
                    phase=phase,
                    plan=phase_plan,
                    live_fingerprints=live,
                )
            )
        plan = ConnectionPlan.create(
            operation_id=operation_id,
            runtime_repository=runtime_repository,
            runtime_commit=runtime_commit,
            repository_identity=repository_identity,
            phase_plans=tuple(phase_plans),
        )
        envelope = ConnectionStateEnvelope.create(
            generation=0,
            connection_plan=plan,
        )
        write_connection_state(envelope, state_root=self._state_root)
        return plan

    def create_approval(
        self,
        plan: ConnectionPlan,
        *,
        actor: str,
        summary: str,
    ) -> ConnectionApproval:
        return ConnectionApproval.create(
            repository_identity=plan.repository_identity,
            operation_id=plan.operation_id,
            parent_plan_hash=plan.plan_hash,
            runtime_commit=plan.runtime_commit,
            actor=actor,
            summary=summary,
        )

    def bind_approval(
        self,
        plan: ConnectionPlan,
        approval: ConnectionApproval,
    ) -> ConnectionApproval:
        envelope = self._load_bound_state(plan)
        self._validate_approval(plan, approval)
        if envelope.approval is not None:
            if envelope.approval.approval_hash != approval.approval_hash:
                raise BootstrapApplyError("connection approval does not match the recorded approval binding")
            return envelope.approval
        updated = next_connection_generation(envelope, approval=approval)
        write_connection_state(
            updated,
            expected_generation=envelope.generation,
            state_root=self._state_root,
        )
        return approval

    def apply(
        self,
        plan: ConnectionPlan,
        approval: ConnectionApproval,
        *,
        context: Mapping[str, object] | None = None,
    ) -> ConnectionReceipt:
        self.bind_approval(plan, approval)
        envelope = self._load_bound_state(plan)
        if any(item.state == "applying" for item in envelope.phase_receipts):
            raise BootstrapApplyError("connection cannot safely resume an interrupted in-flight child phase")
        existing = {item.phase: item for item in envelope.phase_receipts}
        if all(existing.get(phase) is not None and existing[phase].state == "applied" for phase in _PHASES):
            return self._build_receipt(envelope)
        if any(
            current is not None and current.state in {"failed", "compensation_required", "rolled_back"}
            for current in existing.values()
        ):
            raise BootstrapApplyError("connection apply cannot resume a failed or rolled-back connection step")
        latest = envelope
        for phase in _PHASES:
            current = {item.phase: item for item in latest.phase_receipts}.get(phase)
            if current is not None:
                continue
            live = self._validate_live_fingerprints(plan, phase, context=context)
            applying = self._applying_phase_receipt(plan, phase, approval, live)
            latest = self._write_phase_receipt(latest, applying)
            finalized = self._execute_phase(plan, phase, approval, live)
            latest = self._write_phase_receipt(latest, finalized)
            if finalized.state != "applied":
                if phase == "azure":
                    latest = self._compensate_prior_success(plan, latest)
                return self._build_receipt(latest)
        return self._build_receipt(latest)

    def rollback(
        self,
        plan: ConnectionPlan,
        approval: ConnectionApproval,
    ) -> ConnectionReceipt:
        envelope = self._load_bound_state(plan)
        self._validate_approval(plan, approval)
        if envelope.approval is None or envelope.approval.approval_hash != approval.approval_hash:
            raise BootstrapApplyError("connection rollback requires the exact recorded approval binding")
        if any(item.state == "applying" for item in envelope.phase_receipts):
            raise BootstrapApplyError("connection rollback cannot safely proceed while a child phase is marked applying")
        latest = envelope
        receipts = {item.phase: item for item in latest.phase_receipts}
        if not any(
            item is not None and item.state in {"applied", "compensation_required"}
            for item in receipts.values()
        ):
            raise BootstrapApplyError("connection rollback requires an applied or compensation-required child phase")
        for phase in reversed(_PHASES):
            current = {item.phase: item for item in latest.phase_receipts}.get(phase)
            if current is None or current.state not in {"applied", "compensation_required"}:
                continue
            rolled, success = self._rollback_child_phase(
                current,
                reason=f"rolled back {phase}",
            )
            latest = self._write_phase_receipt(latest, rolled)
            if not success:
                return self._build_receipt(latest)
        return self._build_receipt(latest)

    def status(
        self,
        *,
        repository_identity: str,
        operation_id: str,
        runtime_commit: str,
    ) -> ConnectionStatus:
        envelope = read_connection_state(
            repository_identity,
            operation_id,
            state_root=self._state_root,
        )
        if envelope.runtime_commit != runtime_commit or envelope.connection_plan.runtime_commit != runtime_commit:
            raise BootstrapApplyError("connection status requires the exact runtime commit")
        return self._build_status(envelope)

    def _load_bound_state(self, plan: ConnectionPlan) -> ConnectionStateEnvelope:
        envelope = read_connection_state(
            plan.repository_identity,
            plan.operation_id,
            state_root=self._state_root,
        )
        if envelope.runtime_commit != plan.runtime_commit:
            raise BootstrapApplyError("connection resume requires the exact runtime commit")
        if envelope.connection_plan.plan_hash != plan.plan_hash:
            raise BootstrapApplyError("connection resume requires the exact approved connection plan")
        if envelope.connection_plan != plan:
            raise BootstrapApplyError("connection plan does not match the recorded connection plan payload")
        return envelope

    def _validate_approval(
        self,
        plan: ConnectionPlan,
        approval: ConnectionApproval,
    ) -> None:
        if (
            approval.repository_identity != plan.repository_identity
            or approval.operation_id != plan.operation_id
            or approval.runtime_commit != plan.runtime_commit
            or approval.parent_plan_hash != plan.plan_hash
        ):
            raise BootstrapApplyError("connection approval does not match the exact plan and runtime")

    def _phase_context(
        self,
        *,
        repository_identity: str,
        operation_id: str,
        runtime_repository: str,
        runtime_commit: str,
        phase: ConnectionPhaseName,
        context: Mapping[str, object] | None,
        parent_plan_hash: str | None,
    ) -> dict[str, object]:
        payload = dict(context or {})
        payload.update(
            {
                "repository_id": repository_identity,
                "operation_id": operation_id,
                "runtime_repository": runtime_repository,
                "runtime_commit": runtime_commit,
                "phase": phase,
            }
        )
        if parent_plan_hash is not None:
            payload["parent_plan_hash"] = parent_plan_hash
        return payload

    def _validate_phase_actions(
        self,
        phase: ConnectionPhaseName,
        actions: Sequence[object],
    ) -> None:
        for action in actions:
            if not hasattr(action, "phase") or getattr(action, "phase") != phase:
                raise BootstrapApplyError("connection child driver returned an action for the wrong phase")

    def _validate_live_fingerprints(
        self,
        plan: ConnectionPlan,
        phase: ConnectionPhaseName,
        *,
        context: Mapping[str, object] | None,
    ) -> tuple[FingerprintRecord, ...]:
        phase_plan = plan.phase_plan(phase)
        live = _sorted_fingerprints(
            self._drivers[phase].live_fingerprints(
                self._phase_context(
                    repository_identity=plan.repository_identity,
                    operation_id=plan.operation_id,
                    runtime_repository=plan.runtime_repository,
                    runtime_commit=plan.runtime_commit,
                    phase=phase,
                    context=context,
                    parent_plan_hash=plan.plan_hash,
                )
            )
        )
        if live != phase_plan.live_fingerprints:
            raise BootstrapApplyError(f"{phase} live fingerprints drifted from the approved connection plan")
        return live

    def _applying_phase_receipt(
        self,
        plan: ConnectionPlan,
        phase: ConnectionPhaseName,
        approval: ConnectionApproval,
        live: Sequence[FingerprintRecord],
    ) -> PhaseReceipt:
        phase_plan = plan.phase_plan(phase)
        return PhaseReceipt(
            phase=phase,
            state="applying",
            provider=phase,
            receipt=BootstrapReceipt.create(
                operation_id=plan.operation_id,
                runtime_repository=plan.runtime_repository,
                runtime_commit=plan.runtime_commit,
                repository_identity=plan.repository_identity,
                plan_hash=phase_plan.plan.plan_hash,
                error_info=RedactedStatusInfo(
                    code="phase-applying",
                    summary="phase applying",
                ),
            ),
            parent_plan_hash=plan.plan_hash,
            phase_plan_hash=phase_plan.plan.plan_hash,
            approval_hash=approval.approval_hash,
            summary="phase applying",
            recorded_fingerprints=tuple(live),
        )

    def _execute_phase(
        self,
        plan: ConnectionPlan,
        phase: ConnectionPhaseName,
        approval: ConnectionApproval,
        live: Sequence[FingerprintRecord],
    ) -> PhaseReceipt:
        driver = self._drivers[phase]
        phase_plan = plan.phase_plan(phase)
        receipt: BootstrapReceipt | None = None
        try:
            receipt = driver.apply(phase_plan.plan)
            if receipt.plan_hash != phase_plan.plan.plan_hash:
                raise BootstrapApplyError("provider receipt does not match the child connection phase plan")
            provider_state = self._safe_provider_state(driver, receipt)
            if not driver.verify(receipt):
                raise BootstrapApplyError("connection child phase verification failed")
            return PhaseReceipt(
                phase=phase,
                state="applied",
                provider=phase,
                receipt=receipt,
                parent_plan_hash=plan.plan_hash,
                phase_plan_hash=phase_plan.plan.plan_hash,
                approval_hash=approval.approval_hash,
                summary=summarize_receipt(receipt),
                provider_state=provider_state,
                recorded_fingerprints=tuple(live),
            )
        except Exception as exc:
            original_receipt = receipt if isinstance(receipt, BootstrapReceipt) else None
            compensation_actions: tuple[str, ...] = ()
            provider_state: Mapping[str, object] = {}
            rollback_receipt, rollback_state = _failure_details(exc)
            if rollback_receipt is not None:
                original_receipt = rollback_receipt
                compensation_actions = rollback_receipt.compensation_required_actions
                provider_state = rollback_state
            elif original_receipt is not None:
                compensation_actions = original_receipt.compensation_required_actions
                provider_state = self._safe_provider_state(driver, original_receipt)
            code, summary = self._sanitize_error(exc)
            failure = failure_receipt(
                phase=phase,
                provider=phase,
                operation_id=plan.operation_id,
                runtime_repository=plan.runtime_repository,
                runtime_commit=plan.runtime_commit,
                repository_identity=plan.repository_identity,
                parent_plan_hash=plan.plan_hash,
                phase_plan_hash=phase_plan.plan.plan_hash,
                before_fingerprints=tuple(live),
                code=code,
                summary=summary,
                compensation_required_actions=compensation_actions,
            )
            return PhaseReceipt(
                phase=phase,
                state="compensation_required" if compensation_actions else "failed",
                provider=phase,
                receipt=original_receipt or failure.receipt,
                parent_plan_hash=plan.plan_hash,
                phase_plan_hash=phase_plan.plan.plan_hash,
                approval_hash=approval.approval_hash,
                summary=failure.summary,
                provider_state=provider_state,
                recorded_fingerprints=tuple(live),
            )

    def _rollback_child_phase(
        self,
        current: PhaseReceipt,
        *,
        reason: str,
    ) -> tuple[PhaseReceipt, bool]:
        driver = self._drivers[current.phase]  # type: ignore[index]
        try:
            driver.restore_provider_state(current.provider_state)
            driver.rollback(current.receipt)
            if not driver.verify_rollback(current.receipt):
                raise BootstrapApplyError("connection child phase rollback verification failed")
            return (
                current.model_copy(
                    update={
                        "state": "rolled_back",
                        "rollback_summary": reason,
                    }
                ),
                True,
            )
        except Exception as exc:
            _, summary = self._sanitize_error(exc)
            return (
                current.model_copy(
                    update={
                        "state": "compensation_required",
                        "rollback_summary": f"{current.phase} rollback failed: {summary}",
                    }
                ),
                False,
            )

    def _compensate_prior_success(
        self,
        plan: ConnectionPlan,
        envelope: ConnectionStateEnvelope,
    ) -> ConnectionStateEnvelope:
        latest = envelope
        github = {item.phase: item for item in latest.phase_receipts}.get("github")
        if github is None or github.state != "applied":
            return latest
        compensated, _ = self._rollback_child_phase(
            github,
            reason="rolled back github after azure failed",
        )
        return self._write_phase_receipt(latest, compensated)

    def _write_phase_receipt(
        self,
        envelope: ConnectionStateEnvelope,
        receipt: PhaseReceipt,
    ) -> ConnectionStateEnvelope:
        receipts = {item.phase: item for item in envelope.phase_receipts}
        receipts[receipt.phase] = receipt
        ordered = tuple(
            receipts[phase]
            for phase in _PHASES
            if phase in receipts
        )
        updated = next_connection_generation(envelope, phase_receipts=ordered)
        write_connection_state(
            updated,
            expected_generation=envelope.generation,
            state_root=self._state_root,
        )
        return updated

    def _phase_state(
        self,
        plan: ConnectionPlan,
        receipt: PhaseReceipt | None,
        *,
        phase: ConnectionPhaseName,
        approval_hash: str | None,
    ) -> ConnectionPhaseState:
        phase_plan = plan.phase_plan(phase)
        if receipt is None:
            return ConnectionPhaseState(
                phase=phase,
                state="pending",
                plan_hash=phase_plan.plan.plan_hash,
                approval_hash=approval_hash,
                summary="pending",
            )
        return ConnectionPhaseState(
            phase=phase,
            state=receipt.state,
            plan_hash=phase_plan.plan.plan_hash,
            receipt_hash=receipt.receipt.receipt_hash,
            approval_hash=receipt.approval_hash,
            created_actions=receipt.receipt.created_actions,
            adopted_actions=receipt.receipt.adopted_actions,
            changed_actions=receipt.receipt.changed_actions,
            compensation_required_actions=receipt.receipt.compensation_required_actions,
            summary=receipt.rollback_summary or receipt.summary,
        )

    def _build_status(self, envelope: ConnectionStateEnvelope) -> ConnectionStatus:
        approval_hash = envelope.approval.approval_hash if envelope.approval is not None else None
        receipt_map = {item.phase: item for item in envelope.phase_receipts}
        phase_states = tuple(
            self._phase_state(
                envelope.connection_plan,
                receipt_map.get(phase),
                phase=phase,
                approval_hash=approval_hash,
            )
            for phase in _PHASES
        )
        overall_state = self._overall_state(phase_states, approval_bound=envelope.approval is not None)
        resumable = (
            envelope.approval is not None
            and overall_state in {"pending", "partial"}
            and not any(item.state == "applying" for item in phase_states)
        )
        rollback_ready = any(item.state in {"applied", "compensation_required"} for item in phase_states)
        next_action: str | None
        if overall_state == "awaiting_approval":
            next_action = "bind-approval"
        elif overall_state in {"pending", "partial"}:
            next_action = "apply"
        elif overall_state == "applied":
            next_action = "rollback"
        elif overall_state == "failed" and rollback_ready:
            next_action = "rollback"
        elif overall_state == "failed":
            next_action = "rebuild-plan"
        elif overall_state == "applying":
            next_action = "inspect-interrupted-state"
        else:
            next_action = None
        return ConnectionStatus(
            operation_id=envelope.operation_id,
            repository_identity=envelope.repository_identity,
            runtime_repository=envelope.runtime_repository,
            runtime_commit=envelope.runtime_commit,
            plan_hash=envelope.connection_plan.plan_hash,
            approval_hash=approval_hash,
            overall_state=overall_state,
            summary=self._status_summary(phase_states, overall_state),
            phase_states=phase_states,
            resumable=resumable,
            rollback_ready=rollback_ready,
            next_action=next_action,
        )

    def _build_receipt(self, envelope: ConnectionStateEnvelope) -> ConnectionReceipt:
        status = self._build_status(envelope)
        if status.approval_hash is None:
            raise BootstrapApplyError("connection receipt requires a recorded approval binding")
        return ConnectionReceipt.create(
            operation_id=status.operation_id,
            repository_identity=status.repository_identity,
            runtime_repository=status.runtime_repository,
            runtime_commit=status.runtime_commit,
            plan_hash=status.plan_hash,
            approval_hash=status.approval_hash,
            overall_state=status.overall_state,
            summary=status.summary,
            phase_states=status.phase_states,
            rolled_back_phases=tuple(
                item.phase for item in status.phase_states if item.state == "rolled_back"
            ),
        )

    def _safe_provider_state(
        self,
        driver: PhaseDriver,
        receipt: BootstrapReceipt,
    ) -> Mapping[str, object]:
        try:
            state = driver.export_provider_state(receipt)
        except Exception:
            return {}
        safe_persisted_document(state)
        return state

    def _overall_state(
        self,
        phase_states: Sequence[ConnectionPhaseState],
        *,
        approval_bound: bool,
    ) -> ConnectionOverallState:
        states = tuple(item.state for item in phase_states)
        if not approval_bound:
            return "awaiting_approval"
        if all(state == "applied" for state in states):
            return "applied"
        if any(state in {"failed", "compensation_required"} for state in states):
            return "failed"
        if any(state == "applying" for state in states):
            return "applying"
        if any(state == "rolled_back" for state in states) and all(
            state in {"pending", "rolled_back"} for state in states
        ):
            return "rolled_back"
        if any(state == "applied" for state in states):
            return "partial"
        return "pending"

    def _status_summary(
        self,
        phase_states: Sequence[ConnectionPhaseState],
        overall_state: ConnectionOverallState,
    ) -> str:
        summary = ", ".join(f"{item.phase}={item.state}" for item in phase_states)
        return f"{_STEP_ID}:{overall_state} ({summary})"

    def _sanitize_error(self, exc: Exception) -> tuple[str, str]:
        for error_type, code in _SANITIZED_ERROR_CODES.items():
            if isinstance(exc, error_type):
                return code, type(exc).__name__[:256]
        return "provider-failed", "Exception"


__all__ = [
    "ConnectionApproval",
    "ConnectionPhasePlan",
    "ConnectionPhaseState",
    "ConnectionPlan",
    "ConnectionReceipt",
    "ConnectionStateEnvelope",
    "ConnectionStatus",
    "GitHubAzureConnectionManager",
    "connection_state_directory",
    "connection_state_file_path",
    "connection_state_root",
    "next_connection_generation",
    "read_connection_state",
    "write_connection_state",
]
