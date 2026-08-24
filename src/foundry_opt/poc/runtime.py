from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol, Self

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from foundry_opt.distribution import optimizer_skill_paths_match
from foundry_opt.poc.auth import AuthError, GitHubActionsOidcConfig, build_client_assertion_credential
from foundry_opt.poc.bootstrap import BootstrapReceipt, load_shared_pin, read_bootstrap_receipt
from foundry_opt.poc.candidate import CandidateWorkspace, FinalizedCandidate
from foundry_opt.poc.checks import LocalRepositoryCheckRunner, RepositoryCheckRunnerProtocol
from foundry_opt.poc.config import (
    AgentMetadata,
    HostedRuntimeContract,
    ModelDeploymentContract,
    RepositoryPolicy,
    SharedPin,
    load_agent_metadata,
    load_repository_policy,
)
from foundry_opt.poc.controller import CleanupResult, OptimizeJobController, RunResult
from foundry_opt.poc.decision import DecisionRules as ControllerDecisionRules
from foundry_opt.poc.decision import EvaluationSummary, GuardrailResult, GuardrailRule
from foundry_opt.poc.evidence import RenderedComment
from foundry_opt.poc.foundry import (
    AzureProjectsEvaluationBackend,
    CleanupError,
    ContractError,
    DeadlineError,
    DraftReference,
    DraftUnavailableError,
    EvaluationContract as FoundryEvaluationContract,
    EvaluationEvidence,
    FoundryPocClient,
    HostedDefinition,
    RouteDriftError,
    RouteFingerprint,
    ServiceError,
)
from foundry_opt.poc.github import (
    BrokerRemoteError,
    BrokerUnavailableError,
    CommentReceipt,
    FinalDecision,
    FinalDecisionReceipt,
    PullRequestReceipt,
    UnixSocketBrokerClient,
)
from foundry_opt.poc.state import (
    JobIdentity,
    JobRuntimeDigests,
    JobStateStore,
    STATE_FILENAME,
    StateNotFoundError,
)
from foundry_opt.poc.source import SourcePackagingError, package_git_source
from foundry_opt.poc.verification import VerificationResolution
from foundry_opt.repository_selection import protected_editable_patterns_for_repository


DEFAULT_POLICY_PATH = Path(".github/foundry-optimizer.yaml")
DEFAULT_METADATA_PATH = Path(".foundry/agent-metadata.yaml")
DEFAULT_PIN_PATH = Path(".github/foundry-opt.lock.yml")
BOOTSTRAP_RECEIPT_ENV = "FOUNDRY_OPT_BOOTSTRAP_RECEIPT"
BROKER_SOCKET_ENV = "FOUNDRY_OPT_BROKER_SOCKET"
STATE_ROOT_ENV = "FOUNDRY_OPT_STATE_ROOT"
DEADLINE_SECONDS_ENV = "FOUNDRY_OPT_RUNTIME_DEADLINE_SECONDS"
DEFAULT_DEADLINE_SECONDS = 25.0 * 60.0
RUNTIME_SIDECAR_FILENAME = "optimize-job-poc-runtime-sidecars.json"
_COMMIT_PATTERN = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
_CANDIDATE_ID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
_LOGICAL_KIND_PATTERN = re.compile(
    r"^(?:baseline|final|candidate-[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)$"
)
_BASELINE_MARKER_PATTERN = re.compile(
    r"^foundry-opt-poc:[A-Za-z0-9._-]+:baseline$"
)
_FINAL_MARKER_PATTERN = re.compile(
    r"^foundry-opt-poc:[A-Za-z0-9._-]+:final$"
)
_CANDIDATE_MARKER_PATTERN = re.compile(
    r"^foundry-opt-poc:[A-Za-z0-9._-]+:candidate:(?P<candidate_id>[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)$"
)
_BROKER_ERROR_TOKEN_PATTERN = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9_]{8,}|ghs-[A-Za-z0-9_-]{8,}|github_pat_[A-Za-z0-9_]{20,})"
)
_BROKER_ERROR_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]+")


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RuntimeWiringError(RuntimeError):
    """The optimize-job runtime wiring could not be proven safe."""


class RuntimeIntegrationError(RuntimeWiringError):
    """A required runtime integration is missing or inconsistent."""


class RuntimeSidecarError(RuntimeWiringError):
    """The runtime sidecar document is missing, malformed, or stale."""


class RuntimePaths(_FrozenModel):
    repository_root: Path
    policy_path: Path
    metadata_path: Path
    pin_path: Path
    bootstrap_receipt_path: Path
    broker_socket_path: Path
    state_root: Path
    job_root: Path
    job_state_path: Path
    artifact_root: Path
    workspace_root: Path
    sidecar_path: Path

    @field_validator(
        "repository_root",
        "policy_path",
        "metadata_path",
        "pin_path",
        "bootstrap_receipt_path",
        "broker_socket_path",
        "state_root",
        "job_root",
        "job_state_path",
        "artifact_root",
        "workspace_root",
        "sidecar_path",
    )
    @classmethod
    def validate_absolute_paths(cls, value: Path) -> Path:
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("runtime paths must be absolute")
        return path


class RuntimeSettings(_FrozenModel):
    repository_root: Path
    policy: RepositoryPolicy
    metadata: AgentMetadata
    pin: SharedPin
    bootstrap_receipt: BootstrapReceipt
    repository_head: str = Field(pattern=_COMMIT_PATTERN.pattern)
    base_commit: str = Field(pattern=_COMMIT_PATTERN.pattern)
    deadline_seconds: float = Field(gt=0)

    @field_validator("repository_root")
    @classmethod
    def validate_repository_root(cls, value: Path) -> Path:
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("repository_root must be absolute")
        return path

    @field_validator("deadline_seconds", mode="before")
    @classmethod
    def validate_deadline_seconds(cls, value: object) -> float:
        if isinstance(value, bool):
            raise TypeError("deadline_seconds must be numeric")
        if isinstance(value, str):
            try:
                value = float(value)
            except ValueError as error:
                raise TypeError("deadline_seconds must be numeric") from error
        if not isinstance(value, (int, float)):
            raise TypeError("deadline_seconds must be numeric")
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise ValueError("deadline_seconds must be positive and finite")
        return number


class _StoredRouteFingerprint(_FrozenModel):
    agent_name: str = Field(min_length=1, max_length=128)
    latest_version: str | None = Field(default=None, min_length=1, max_length=256)
    selector: object | None = None
    endpoint_configuration: object | None = None
    sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @field_validator("selector", "endpoint_configuration", mode="before")
    @classmethod
    def validate_json_values(cls, value: object) -> object:
        if value is None:
            return None
        return _json_value(value, subject="route fingerprint")

    @classmethod
    def from_route(cls, route: RouteFingerprint) -> Self:
        return cls(
            agent_name=route.agent_name,
            latest_version=route.latest_version,
            selector=route.selector,
            endpoint_configuration=route.endpoint_configuration,
            sha256=route.sha256,
        )

    def to_route(self) -> RouteFingerprint:
        return RouteFingerprint(
            agent_name=self.agent_name,
            latest_version=self.latest_version,
            selector=self.selector,
            endpoint_configuration=self.endpoint_configuration,
            sha256=self.sha256,
        )


class _StoredHostedDefinition(_FrozenModel):
    kind: str = Field(pattern=r"^hosted$")
    payload: dict[str, object]

    @field_validator("payload", mode="before")
    @classmethod
    def validate_payload(cls, value: object) -> dict[str, object]:
        return _json_object(value, subject="hosted definition payload")

    @classmethod
    def from_definition(cls, definition: HostedDefinition) -> Self:
        return cls(kind=definition.kind, payload=definition.payload)

    def to_definition(self) -> HostedDefinition:
        return HostedDefinition(kind=self.kind, payload=self.payload)


class _StoredDraftReference(_FrozenModel):
    agent_name: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=256)
    ownership_token: str = Field(min_length=1, max_length=512)
    code_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    route_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_fields(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        route = payload.pop("route", None)
        payload.pop("definition", None)
        payload.pop("service_id", None)
        payload.pop("status", None)
        if "route_sha256" not in payload and isinstance(route, Mapping):
            raw_route_sha256 = route.get("sha256")
            if isinstance(raw_route_sha256, str):
                payload["route_sha256"] = raw_route_sha256
        return payload

    @model_validator(mode="after")
    def validate_reference(self) -> Self:
        if not self.version.startswith("draft-"):
            raise ValueError("stored references must be draft versions")
        return self

    @classmethod
    def from_reference(cls, reference: DraftReference) -> Self:
        return cls(
            agent_name=reference.agent_name,
            version=reference.version,
            ownership_token=reference.ownership_token,
            code_sha256=reference.code_sha256,
            route_sha256=reference.route.sha256,
        )

    def to_reference(self) -> DraftReference:
        return DraftReference(
            agent_name=self.agent_name,
            version=self.version,
            ownership_token=self.ownership_token,
            code_sha256=self.code_sha256,
            route=RouteFingerprint(
                agent_name=self.agent_name,
                latest_version=None,
                selector=None,
                endpoint_configuration=None,
                sha256=self.route_sha256,
            ),
            definition=HostedDefinition(),
        )


class _BaselineMetricAggregate(_FrozenModel):
    name: str = Field(min_length=1, max_length=256)
    passed_cases: int = Field(ge=0)
    failed_cases: int = Field(ge=0)


class _BaselineSidecar(_FrozenModel):
    summary: EvaluationSummary | None = None
    metrics: tuple[_BaselineMetricAggregate, ...] = ()
    pending_reference: _StoredDraftReference | None = None
    reference_verified: bool = False
    cleanup_required: bool = False
    cleanup_receipt_id: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_baseline(self) -> Self:
        if self.summary is not None and self.summary.run_kind != "development":
            raise ValueError("baseline summary must use the development run_kind")
        if self.summary is None and self.metrics:
            raise ValueError("baseline metrics require a completed summary")
        if (
            self.summary is None
            and self.pending_reference is None
            and self.cleanup_receipt_id is None
        ):
            raise ValueError("baseline sidecar must retain a draft, cleanup receipt, or summary")
        if self.cleanup_receipt_id is not None and self.pending_reference is not None:
            raise ValueError("cleaned baselines must not retain a pending draft reference")
        if self.cleanup_required and self.pending_reference is None:
            raise ValueError("baseline cleanup obligations require a pending draft reference")
        if self.cleanup_required and self.cleanup_receipt_id is not None:
            raise ValueError("completed baseline cleanup must not retain an obligation")
        names = [metric.name.casefold() for metric in self.metrics]
        if len(names) != len(set(names)):
            raise ValueError("baseline metric aggregates must be unique by name")
        return self

    @property
    def has_verified_reference(self) -> bool:
        return self.reference_verified or self.summary is not None

    def metric(self, name: str) -> _BaselineMetricAggregate | None:
        key = name.casefold()
        for metric in self.metrics:
            if metric.name.casefold() == key:
                return metric
        return None


class _CandidateSidecar(_FrozenModel):
    candidate_id: str = Field(pattern=_CANDIDATE_ID_PATTERN.pattern)
    draft_id: str = Field(min_length=1, max_length=256)
    reference: _StoredDraftReference | None = None
    reference_verified: bool = False
    cleanup_required: bool = False
    retry_phase: Literal["candidate", "validating"] | None = None
    development_summary: EvaluationSummary | None = None
    validating_summary: EvaluationSummary | None = None
    cleanup_receipt_id: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def validate_candidate(self) -> Self:
        if self.reference is not None and self.reference.version != self.draft_id:
            raise ValueError("draft_id must match the stored draft reference version")
        if self.development_summary is not None and self.development_summary.run_kind != "development":
            raise ValueError("development_summary must use the development run_kind")
        if self.validating_summary is not None and self.validating_summary.run_kind != "validating":
            raise ValueError("validating_summary must use the validating run_kind")
        if self.validating_summary is not None and self.development_summary is None:
            raise ValueError("validating_summary requires a completed development_summary")
        if self.retry_phase == "candidate" and self.development_summary is not None:
            raise ValueError("candidate retries cannot retain development_summary")
        if self.retry_phase == "validating" and self.development_summary is None:
            raise ValueError("validating retries require a completed development_summary")
        if self.retry_phase is not None and self.validating_summary is not None:
            raise ValueError("retrying candidates cannot retain validating_summary")
        if self.reference is None and self.cleanup_receipt_id is None and self.development_summary is None:
            raise ValueError("candidate sidecar must retain a draft reference or cleanup receipt")
        if self.cleanup_receipt_id is not None and self.reference is not None:
            raise ValueError("cleaned candidates must not retain a draft reference")
        if self.cleanup_required and self.reference is None:
            raise ValueError("candidate cleanup obligations require a draft reference")
        if self.cleanup_required and self.cleanup_receipt_id is not None:
            raise ValueError("completed candidate cleanup must not retain an obligation")
        return self

    @property
    def has_verified_reference(self) -> bool:
        return (
            self.reference_verified
            or self.development_summary is not None
            or self.validating_summary is not None
        )


class _RuntimeSidecarState(_FrozenModel):
    schema_version: int = Field(default=1, ge=1)
    generation: int = Field(default=0, ge=0)
    integrity: JobRuntimeDigests | None = None
    baseline: _BaselineSidecar | None = None
    candidates: dict[str, _CandidateSidecar] = Field(default_factory=dict)
    comments: dict[str, CommentReceipt] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        draft_ids: set[str] = set()
        for candidate_id, record in self.candidates.items():
            if candidate_id != record.candidate_id:
                raise ValueError("candidate sidecar keys must match the stored candidate_id")
            if record.draft_id in draft_ids:
                raise ValueError("candidate drafts must be unique")
            draft_ids.add(record.draft_id)
        for logical_kind, receipt in self.comments.items():
            if _LOGICAL_KIND_PATTERN.fullmatch(logical_kind) is None:
                raise ValueError("comment logical kinds must be bounded identifiers")
            if receipt.logical_kind != logical_kind:
                raise ValueError("comment sidecar keys must match receipt.logical_kind")
        return self

    def candidate_by_draft_id(self, draft_id: str) -> _CandidateSidecar | None:
        for record in self.candidates.values():
            if record.draft_id == draft_id:
                return record
        return None

    def with_baseline(self, baseline: _BaselineSidecar) -> Self:
        return self.model_copy(update={"baseline": baseline})

    def with_candidate(self, candidate: _CandidateSidecar) -> Self:
        updated = dict(self.candidates)
        updated[candidate.candidate_id] = candidate
        return self.model_copy(update={"candidates": updated})

    def with_comment(self, logical_kind: str, receipt: CommentReceipt) -> Self:
        updated = dict(self.comments)
        updated[logical_kind] = receipt
        return self.model_copy(update={"comments": updated})

    def with_integrity(self, integrity: JobRuntimeDigests) -> Self:
        return self.model_copy(update={"integrity": integrity})


class RuntimeSidecarStore:
    """Atomic JSON sidecars for Foundry draft references and broker receipts."""

    def __init__(self, path: Path) -> None:
        resolved = Path(path)
        self.path = (
            resolved
            if resolved.suffix.lower() == ".json"
            else resolved / RUNTIME_SIDECAR_FILENAME
        )

    def load(self) -> _RuntimeSidecarState:
        if not self.path.is_file():
            return _RuntimeSidecarState()
        try:
            payload = _strict_json_object(self.path.read_bytes(), subject="runtime sidecar")
        except OSError as error:
            raise RuntimeSidecarError("runtime sidecar could not be read") from error
        if set(payload) != {"content_sha256", "state"}:
            raise RuntimeSidecarError("runtime sidecar envelope is invalid")
        digest = payload["content_sha256"]
        state_payload = payload["state"]
        if not isinstance(digest, str) or not isinstance(state_payload, dict):
            raise RuntimeSidecarError("runtime sidecar envelope types are invalid")
        content = _canonical_json_bytes(state_payload)
        if hashlib.sha256(content).hexdigest() != digest:
            raise RuntimeSidecarError("runtime sidecar digest does not match its content")
        try:
            return _RuntimeSidecarState.model_validate(state_payload)
        except ValidationError as error:
            raise RuntimeSidecarError("runtime sidecar does not match the trusted schema") from error

    def save(
        self,
        state: _RuntimeSidecarState,
        *,
        expected_generation: int | None = None,
    ) -> _RuntimeSidecarState:
        current = self.load()
        expected = current.generation if expected_generation is None else expected_generation
        if current.generation != expected:
            raise RuntimeSidecarError("runtime sidecar generation changed")
        if state.generation != current.generation:
            raise RuntimeSidecarError("runtime sidecar generation was mutated out of band")
        if state == current and self.path.is_file():
            return current
        persisted = _RuntimeSidecarState.model_validate(
            {
                **state.model_dump(mode="python"),
                "generation": state.generation + 1,
            }
        )
        self._write_envelope(persisted)
        return persisted

    def update(self, mutate: Callable[[_RuntimeSidecarState], _RuntimeSidecarState]) -> _RuntimeSidecarState:
        current = self.load()
        updated = mutate(current)
        if updated == current and self.path.is_file():
            return current
        if updated.generation != current.generation:
            raise RuntimeSidecarError("runtime sidecar generation was mutated out of band")
        return self.save(updated, expected_generation=current.generation)

    def _write_envelope(self, state: _RuntimeSidecarState) -> None:
        payload = state.model_dump(mode="json")
        content = _canonical_json_bytes(payload)
        envelope = {
            "content_sha256": hashlib.sha256(content).hexdigest(),
            "state": payload,
        }
        encoded = _canonical_json_bytes(envelope)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f"{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        except OSError as error:
            raise RuntimeSidecarError("runtime sidecar write did not complete") from error


def load_runtime_paths(
    repository: Path,
    *,
    environment: Mapping[str, str] | None = None,
    job_id: str | None = None,
    policy_path: Path | str | None = None,
    metadata_path: Path | str | None = None,
    pin_path: Path | str | None = None,
    bootstrap_receipt_path: Path | str | None = None,
    broker_socket_path: Path | str | None = None,
    state_root: Path | str | None = None,
) -> RuntimePaths:
    env = os.environ if environment is None else environment
    repository_root = _repository_root(repository)
    resolved_policy_path = _resolve_existing_file(
        repository_root / DEFAULT_POLICY_PATH if policy_path is None else Path(policy_path),
        field="policy_path",
    )
    resolved_metadata_path = _resolve_existing_file(
        repository_root / DEFAULT_METADATA_PATH if metadata_path is None else Path(metadata_path),
        field="metadata_path",
    )
    resolved_pin_path = _resolve_existing_file(
        repository_root / DEFAULT_PIN_PATH if pin_path is None else Path(pin_path),
        field="pin_path",
    )
    resolved_receipt_path = _resolve_existing_file(
        _environment_or_path(
            bootstrap_receipt_path,
            BOOTSTRAP_RECEIPT_ENV,
            environment=env,
        ),
        field="bootstrap_receipt_path",
    )
    resolved_broker_socket = _resolve_any_path(
        _environment_or_path(
            broker_socket_path,
            BROKER_SOCKET_ENV,
            environment=env,
        ),
        field="broker_socket_path",
    )
    resolved_state_root = _resolve_any_path(
        _environment_or_path(
            state_root,
            STATE_ROOT_ENV,
            environment=env,
        ),
        field="state_root",
    )
    resolved_state_root.mkdir(parents=True, exist_ok=True)
    job_component = _validate_job_id_component(job_id) if job_id is not None else None
    job_root = (
        resolved_state_root
        if job_component is None
        else (resolved_state_root / job_component).resolve(strict=False)
    )
    artifact_root = (job_root / "artifacts").resolve(strict=False)
    workspace_root = (artifact_root / "candidate-workspace").resolve(strict=False)
    sidecar_path = (job_root / RUNTIME_SIDECAR_FILENAME).resolve(strict=False)
    job_state_path = (job_root / STATE_FILENAME).resolve(strict=False)
    if artifact_root == repository_root or artifact_root.is_relative_to(repository_root):
        raise RuntimeIntegrationError("runtime artifact_root must live outside the repository checkout")
    for directory in (job_root, artifact_root, workspace_root):
        directory.mkdir(parents=True, exist_ok=True)
    return RuntimePaths(
        repository_root=repository_root,
        policy_path=resolved_policy_path,
        metadata_path=resolved_metadata_path,
        pin_path=resolved_pin_path,
        bootstrap_receipt_path=resolved_receipt_path,
        broker_socket_path=resolved_broker_socket,
        state_root=resolved_state_root,
        job_root=job_root,
        job_state_path=job_state_path,
        artifact_root=artifact_root,
        workspace_root=workspace_root,
        sidecar_path=sidecar_path,
    )


def load_deadline_seconds(
    *,
    environment: Mapping[str, str] | None = None,
    deadline_seconds: float | str | None = None,
) -> float:
    if deadline_seconds is not None:
        return _validate_deadline_seconds(deadline_seconds, subject="deadline_seconds")
    env = os.environ if environment is None else environment
    raw = env.get(DEADLINE_SECONDS_ENV)
    if raw is None:
        return DEFAULT_DEADLINE_SECONDS
    return _validate_deadline_seconds(raw, subject=DEADLINE_SECONDS_ENV)


def load_repository_head(repository: Path) -> str:
    head = _git_text(_repository_root(repository), "rev-parse", "HEAD")
    return _validate_commit(head, subject="repository HEAD")


def resolve_base_commit(
    repository: Path,
    *,
    base_commit: str | None = None,
) -> str:
    repository_root = _repository_root(repository)
    if base_commit is None:
        return load_repository_head(repository_root)
    normalized = _validate_commit(base_commit, subject="base_commit")
    resolved = _git_text(repository_root, "rev-parse", "--verify", normalized)
    return _validate_commit(resolved, subject="resolved base_commit")


def load_runtime_settings(
    paths: RuntimePaths,
    *,
    environment: Mapping[str, str] | None = None,
    base_commit: str | None = None,
    deadline_seconds: float | str | None = None,
    policy_loader: Callable[..., RepositoryPolicy] = load_repository_policy,
    metadata_loader: Callable[[Path | str], AgentMetadata] = load_agent_metadata,
    pin_loader: Callable[[Path | str], SharedPin] = load_shared_pin,
    bootstrap_receipt_reader: Callable[[Path | str], BootstrapReceipt] = read_bootstrap_receipt,
    repository_head_loader: Callable[[Path], str] = load_repository_head,
    base_commit_resolver: Callable[..., str] = resolve_base_commit,
) -> RuntimeSettings:
    policy = policy_loader(paths.policy_path, metadata_path=paths.metadata_path)
    metadata = metadata_loader(paths.metadata_path)
    pin = pin_loader(paths.pin_path)
    receipt = bootstrap_receipt_reader(paths.bootstrap_receipt_path)
    repository_head = repository_head_loader(paths.repository_root)
    resolved_base_commit = base_commit_resolver(
        paths.repository_root,
        base_commit=base_commit,
    )
    configured_deadline = load_deadline_seconds(
        environment=environment,
        deadline_seconds=deadline_seconds,
    )
    _validate_bootstrap_receipt(pin, receipt)
    expected_metadata_path = (paths.repository_root / policy.metadata_path).resolve(strict=False)
    if expected_metadata_path != paths.metadata_path:
        raise RuntimeIntegrationError("repository policy metadata_path does not match the loaded metadata file")
    _validate_policy_models(policy, metadata)
    return RuntimeSettings(
        repository_root=paths.repository_root,
        policy=policy,
        metadata=metadata,
        pin=pin,
        bootstrap_receipt=receipt,
        repository_head=repository_head,
        base_commit=resolved_base_commit,
        deadline_seconds=configured_deadline,
    )


class HostedDefinitionMetadataProtocol(Protocol):
    project_endpoint: str
    hosted_runtime: HostedRuntimeContract
    model_deployments: tuple[ModelDeploymentContract, ...]


def build_hosted_definition(
    metadata: HostedDefinitionMetadataProtocol,
    model: str,
) -> HostedDefinition:
    deployment = _resolve_model_deployment(metadata, model)
    runtime = metadata.hosted_runtime
    return HostedDefinition(
        kind=runtime.kind,
        payload={
            "cpu": runtime.cpu,
            "memory": runtime.memory,
            "environment_variables": {
                runtime.model_environment_variable: deployment.deployment_name,
                "AZURE_AI_PROJECT_ENDPOINT": metadata.project_endpoint,
            },
            "protocol_versions": [
                {
                    "protocol": runtime.protocol_name,
                    "version": runtime.protocol_version,
                }
            ],
            "container_protocol_versions": [],
            "code_configuration": {
                "runtime": runtime.runtime,
                "entry_point": list(runtime.entry_point),
                "dependency_resolution": runtime.dependency_resolution,
            },
        },
    )


class ControllerFoundryOperations:
    """Production Foundry implementation for the optimize-job controller."""

    def __init__(
        self,
        *,
        repository: Path,
        source_root: str,
        policy: RepositoryPolicy,
        metadata: AgentMetadata,
        client: FoundryPocClient,
        artifact_state_path: Path,
        route_fingerprint: RouteFingerprint | str,
        verification_resolution: VerificationResolution | None = None,
        sidecars: RuntimeSidecarStore | None = None,
        deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        repository_head_loader: Callable[[Path], str] = load_repository_head,
    ) -> None:
        self._repository = _repository_root(repository)
        if source_root != policy.source_root:
            raise RuntimeIntegrationError("source_root must match the repository policy")
        self._source_root = source_root
        self._policy = policy
        self._metadata = metadata
        self._client = client
        self._job_root = _resolve_any_path(artifact_state_path, field="artifact_state_path")
        self._job_root.mkdir(parents=True, exist_ok=True)
        self._job_state_path = (self._job_root / STATE_FILENAME).resolve(strict=False)
        self._sidecars = sidecars or RuntimeSidecarStore(self._job_root / RUNTIME_SIDECAR_FILENAME)
        self._deadline_seconds = load_deadline_seconds(deadline_seconds=deadline_seconds)
        self._monotonic = monotonic
        self._repository_head_loader = repository_head_loader
        self._verification_resolution = verification_resolution
        self._expected_route = (
            route_fingerprint
            if isinstance(route_fingerprint, RouteFingerprint)
            else RouteFingerprint(
                agent_name=metadata.agent_name,
                latest_version=None,
                selector=None,
                endpoint_configuration=None,
                sha256=_validate_sha256(route_fingerprint, subject="route_fingerprint"),
            )
        )
        if self._expected_route.agent_name != self._metadata.agent_name:
            raise RuntimeIntegrationError("route fingerprint agent_name does not match metadata.agent_name")
        _validate_policy_models(policy, metadata)
        self._runtime_digests = _runtime_contract_digests(policy=policy, metadata=metadata)

    def evaluate_baseline(self, identity: JobIdentity) -> RunResult:
        self._assert_identity(identity)
        state = self._load_sidecar_state()
        baseline = state.baseline
        if baseline is not None and baseline.cleanup_receipt_id is not None:
            if baseline.summary is None:
                baseline = None
            else:
                return RunResult(status="ok", evaluation=baseline.summary)

        reference: DraftReference | None = None
        baseline_zip: bytes | None = None
        if baseline is None or baseline.pending_reference is None:
            baseline_zip = self._package_base_source(identity.base_commit)
            try:
                reference = self._create_draft_reference(
                    model=self._policy.baseline_model,
                    code_zip=baseline_zip,
                )
            except RouteDriftError:
                raise
            except (AuthError, ContractError, DeadlineError, DraftUnavailableError, ServiceError) as error:
                return RunResult(status="platform_failure", reason=str(error))
            baseline = self._update_sidecars(
                lambda current: current.with_baseline(
                    _BaselineSidecar(
                        pending_reference=_StoredDraftReference.from_reference(reference),
                    )
                )
            ).baseline
        else:
            reference = baseline.pending_reference.to_reference()

        if baseline is None or reference is None:
            raise RuntimeIntegrationError("baseline draft reference is unavailable")

        if baseline.cleanup_required:
            return self._retry_verification_cleanup(
                draft_id=reference.version,
                cleanup=self._cleanup_baseline_reference,
                reason="baseline draft cleanup completed after verification failure; rerun baseline evaluation",
                retry_phase="baseline",
            )

        if not baseline.has_verified_reference:
            if baseline_zip is None:
                baseline_zip = self._package_base_source(identity.base_commit)
            try:
                reference = self._verify_existing_draft(reference, code_zip=baseline_zip)
            except RouteDriftError:
                raise
            except (AuthError, ContractError, DeadlineError, DraftUnavailableError, ServiceError) as error:
                return self._record_baseline_verification_failure(reference, error)
            baseline = self._update_sidecars(
                lambda current: current.with_baseline(
                    current.baseline.model_copy(
                        update={
                            "pending_reference": _StoredDraftReference.from_reference(reference),
                            "reference_verified": True,
                            "cleanup_required": False,
                            "cleanup_receipt_id": None,
                        }
                    )
                )
            ).baseline

        if baseline is None:
            raise RuntimeIntegrationError("baseline sidecar is unavailable")
        if baseline.summary is None:
            try:
                evidence = self._client.run_evaluation(
                    reference,
                    self._development_contract(run_name=f"baseline-{identity.job_id}"),
                    deadline_monotonic=self._deadline(),
                )
                summary, baseline_metrics = self._normalize_baseline_evidence(evidence)
            except RouteDriftError:
                raise
            except (AuthError, ContractError, DeadlineError, DraftUnavailableError, ServiceError) as error:
                return RunResult(
                    status="platform_failure",
                    reason=str(error),
                    draft_id=reference.version,
                )
            baseline = self._update_sidecars(
                lambda current: current.with_baseline(
                    current.baseline.model_copy(
                        update={
                            "summary": summary,
                            "metrics": baseline_metrics,
                        }
                    )
                )
            ).baseline

        if baseline.cleanup_receipt_id is None:
            try:
                cleanup_receipt_id = self._cleanup_reference(reference)
            except RouteDriftError:
                raise
            except (AuthError, CleanupError, ContractError, DeadlineError, DraftUnavailableError, ServiceError) as error:
                return RunResult(
                    status="platform_failure",
                    reason=str(error),
                    draft_id=reference.version,
                )
            baseline = self._update_sidecars(
                lambda current: current.with_baseline(
                    current.baseline.model_copy(
                        update={
                            "pending_reference": None,
                            "reference_verified": False,
                            "cleanup_required": False,
                            "cleanup_receipt_id": cleanup_receipt_id,
                        }
                    )
                )
            ).baseline

        if baseline is None or baseline.summary is None:
            raise RuntimeIntegrationError("baseline summary is unavailable")
        return RunResult(status="ok", evaluation=baseline.summary)

    def evaluate_candidate(self, candidate: FinalizedCandidate) -> RunResult:
        self._assert_candidate(candidate)
        current = self._load_sidecar_state().candidates.get(candidate.candidate_id)
        if current is not None and current.development_summary is not None:
            return RunResult(
                status="ok",
                evaluation=current.development_summary,
                draft_id=current.draft_id,
            )
        if (
            current is not None
            and current.reference is None
            and current.cleanup_receipt_id is not None
            and current.retry_phase != "candidate"
        ):
            raise RuntimeIntegrationError(
                "candidate draft was already cleaned after a terminal development failure"
            )
        code_zip: bytes | None = None
        reference: DraftReference | None = None
        if current is None or current.reference is None:
            code_zip = self._load_candidate_source_zip(candidate)
            try:
                reference = self._create_draft_reference(
                    model=candidate.model,
                    code_zip=code_zip,
                )
            except RouteDriftError:
                raise
            except (AuthError, ContractError, DeadlineError, DraftUnavailableError, ServiceError) as error:
                return RunResult(status="platform_failure", reason=str(error))
            current = self._update_sidecars(
                lambda state: state.with_candidate(
                    _CandidateSidecar(
                        candidate_id=candidate.candidate_id,
                        draft_id=reference.version,
                        reference=_StoredDraftReference.from_reference(reference),
                    )
                )
            ).candidates[candidate.candidate_id]
        else:
            reference = current.reference.to_reference()
        if reference is None:
            raise RuntimeIntegrationError("candidate draft reference is unavailable")
        if current.cleanup_required:
            return self._retry_verification_cleanup(
                draft_id=current.draft_id,
                cleanup=lambda: self._cleanup_candidate_reference(candidate.candidate_id),
                reason="candidate draft cleanup completed after verification failure; rerun candidate evaluation",
                retry_phase="candidate",
            )

        code_zip = code_zip or self._load_candidate_source_zip(candidate)
        if reference.code_sha256 != candidate.hashes.source_zip_sha256:
            raise RuntimeIntegrationError("candidate draft reference does not match the finalized source ZIP")
        if not current.has_verified_reference:
            try:
                reference = self._verify_existing_draft(reference, code_zip=code_zip)
            except RouteDriftError:
                raise
            except (AuthError, ContractError, DeadlineError, DraftUnavailableError, ServiceError) as error:
                return self._record_candidate_verification_failure(
                    current,
                    reference,
                    error,
                    retry_phase="candidate",
                )
            current = self._update_sidecars(
                lambda state: state.with_candidate(
                    state.candidates[candidate.candidate_id].model_copy(
                        update={
                            "reference": _StoredDraftReference.from_reference(reference),
                            "reference_verified": True,
                            "cleanup_required": False,
                            "retry_phase": None,
                            "cleanup_receipt_id": None,
                        }
                    )
                )
            ).candidates[candidate.candidate_id]
        try:
            evidence = self._client.run_evaluation(
                reference,
                self._development_contract(run_name=f"candidate-{candidate.candidate_id}"),
                deadline_monotonic=self._deadline(),
            )
            summary = self._normalize_evidence(evidence, run_kind="development")
        except RouteDriftError:
            raise
        except (AuthError, ContractError, DeadlineError, DraftUnavailableError, ServiceError) as error:
            return RunResult(
                status="platform_failure",
                reason=str(error),
                draft_id=current.draft_id,
            )
        updated = current.model_copy(update={"development_summary": summary})
        self._update_sidecars(lambda state: state.with_candidate(updated))
        return RunResult(status="ok", evaluation=summary, draft_id=current.draft_id)

    def evaluate_validating(self, candidate: FinalizedCandidate) -> RunResult:
        self._assert_candidate(candidate)
        current = self._load_sidecar_state().candidates.get(candidate.candidate_id)
        if current is None:
            raise RuntimeIntegrationError("candidate validating run requires a persisted draft reference")
        if current.validating_summary is not None:
            return RunResult(
                status="ok",
                evaluation=current.validating_summary,
                draft_id=current.draft_id,
            )
        if current.development_summary is None:
            raise RuntimeIntegrationError("candidate validating run requires a completed development evaluation")
        code_zip: bytes | None = None
        if current.reference is None:
            if current.cleanup_receipt_id is None:
                raise RuntimeIntegrationError("candidate validating run requires a persisted draft reference")
            if current.retry_phase != "validating":
                raise RuntimeIntegrationError(
                    "candidate draft was already cleaned before the validating dataset run"
                )
            code_zip = self._load_candidate_source_zip(candidate)
            try:
                reference = self._create_draft_reference(
                    model=candidate.model,
                    code_zip=code_zip,
                )
            except RouteDriftError:
                raise
            except (AuthError, ContractError, DeadlineError, DraftUnavailableError, ServiceError) as error:
                return RunResult(status="platform_failure", reason=str(error))
            current = self._update_sidecars(
                lambda state: state.with_candidate(
                    state.candidates[candidate.candidate_id].model_copy(
                        update={
                            "draft_id": reference.version,
                            "reference": _StoredDraftReference.from_reference(reference),
                            "reference_verified": False,
                            "cleanup_required": False,
                            "retry_phase": None,
                            "cleanup_receipt_id": None,
                        }
                    )
                )
            ).candidates[candidate.candidate_id]
        if current.cleanup_required:
            return self._retry_verification_cleanup(
                draft_id=current.draft_id,
                cleanup=lambda: self._cleanup_candidate_reference(candidate.candidate_id),
                reason="candidate draft cleanup completed after verification failure; rerun validating evaluation",
                retry_phase="validating",
            )
        code_zip = code_zip or self._load_candidate_source_zip(candidate)
        reference = current.reference.to_reference()
        if reference.code_sha256 != candidate.hashes.source_zip_sha256:
            raise RuntimeIntegrationError("candidate validating run does not match the finalized source ZIP")
        try:
            reference = self._verify_existing_draft(reference, code_zip=code_zip)
        except RouteDriftError:
            raise
        except (AuthError, ContractError, DeadlineError, DraftUnavailableError, ServiceError) as error:
            return self._record_candidate_verification_failure(
                current,
                reference,
                error,
                retry_phase="validating",
            )
        try:
            evidence = self._client.run_evaluation(
                reference,
                self._validating_contract(run_name=f"validating-{candidate.candidate_id}"),
                deadline_monotonic=self._deadline(),
            )
            summary = self._normalize_evidence(evidence, run_kind="validating")
        except RouteDriftError:
            raise
        except (AuthError, ContractError, DeadlineError, DraftUnavailableError, ServiceError) as error:
            return RunResult(
                status="platform_failure",
                reason=str(error),
                draft_id=current.draft_id,
            )
        updated = current.model_copy(
            update={
                "reference": _StoredDraftReference.from_reference(reference),
                "reference_verified": True,
                "cleanup_required": False,
                "retry_phase": None,
                "validating_summary": summary,
            }
        )
        self._update_sidecars(lambda state: state.with_candidate(updated))
        return RunResult(status="ok", evaluation=summary, draft_id=current.draft_id)

    def cleanup_draft(self, draft_id: str) -> CleanupResult:
        state = self._load_sidecar_state()
        current = state.candidate_by_draft_id(draft_id)
        if current is None:
            raise RuntimeIntegrationError("cleanup requires a persisted candidate draft reference")
        if current.cleanup_receipt_id is not None:
            return CleanupResult(
                success=True,
                receipt_id=current.cleanup_receipt_id,
                retry_phase=current.retry_phase,
            )
        if self._should_defer_cleanup(current.candidate_id):
            return CleanupResult(
                success=False,
                reason="provisional winner draft retained for the validating dataset",
            )
        if current.reference is None:
            raise RuntimeIntegrationError("cleanup requires a persisted candidate draft reference")
        reference = current.reference.to_reference()
        try:
            cleanup_receipt_id = self._cleanup_reference(reference)
        except RouteDriftError:
            raise
        except (AuthError, CleanupError, ContractError, DeadlineError, DraftUnavailableError, ServiceError) as error:
            return CleanupResult(success=False, reason=str(error))
        updated = current.model_copy(
            update={
                "reference": None,
                "reference_verified": False,
                "cleanup_required": False,
                "cleanup_receipt_id": cleanup_receipt_id,
            }
        )
        self._update_sidecars(lambda sidecars: sidecars.with_candidate(updated))
        return CleanupResult(
            success=True,
            receipt_id=cleanup_receipt_id,
            retry_phase=current.retry_phase,
        )

    def _load_sidecar_state(self) -> _RuntimeSidecarState:
        current = self._sidecars.load()
        if current.integrity is None:
            if current.baseline is None and not current.candidates and not current.comments:
                return current
            return self._sidecars.save(
                current.with_integrity(self._runtime_digests),
                expected_generation=current.generation,
            )
        _assert_runtime_digests(
            current.integrity,
            policy=self._policy,
            metadata=self._metadata,
            subject="runtime sidecar",
        )
        return current

    def _update_sidecars(
        self,
        mutate: Callable[[_RuntimeSidecarState], _RuntimeSidecarState],
    ) -> _RuntimeSidecarState:
        def bound(current: _RuntimeSidecarState) -> _RuntimeSidecarState:
            prepared = (
                current.with_integrity(self._runtime_digests)
                if current.integrity is None
                else current
            )
            if prepared.integrity is not None:
                _assert_runtime_digests(
                    prepared.integrity,
                    policy=self._policy,
                    metadata=self._metadata,
                    subject="runtime sidecar",
                )
            updated = mutate(prepared)
            return (
                updated
                if updated.integrity is not None
                else updated.with_integrity(self._runtime_digests)
            )

        return self._sidecars.update(bound)

    def _retry_verification_cleanup(
        self,
        *,
        draft_id: str,
        cleanup: Callable[[], str],
        reason: str,
        retry_phase: Literal["baseline", "candidate", "validating"] | None = None,
    ) -> RunResult:
        try:
            cleanup()
        except RouteDriftError:
            raise
        except (AuthError, CleanupError, ContractError, DeadlineError, DraftUnavailableError, ServiceError) as error:
            return RunResult(status="platform_failure", reason=str(error), draft_id=draft_id)
        if retry_phase is None:
            return RunResult(status="platform_failure", reason=reason, draft_id=draft_id)
        return RunResult(
            status="retry",
            reason=reason,
            draft_id=draft_id,
            retry_phase=retry_phase,
        )

    def _record_baseline_verification_failure(
        self,
        reference: DraftReference,
        error: Exception,
    ) -> RunResult:
        try:
            cleanup_receipt_id = self._cleanup_reference(reference)
        except RouteDriftError:
            raise
        except (AuthError, CleanupError, ContractError, DeadlineError, DraftUnavailableError, ServiceError) as cleanup_error:
            self._update_sidecars(
                lambda current: current.with_baseline(
                    current.baseline.model_copy(
                        update={
                            "pending_reference": _StoredDraftReference.from_reference(reference),
                            "reference_verified": False,
                            "cleanup_required": True,
                            "cleanup_receipt_id": None,
                        }
                    )
                )
            )
            return RunResult(
                status="platform_failure",
                reason=f"{error}; baseline cleanup failed: {cleanup_error}",
                draft_id=reference.version,
            )
        self._update_sidecars(
            lambda current: current.with_baseline(
                current.baseline.model_copy(
                    update={
                        "pending_reference": None,
                        "reference_verified": False,
                        "cleanup_required": False,
                        "cleanup_receipt_id": cleanup_receipt_id,
                    }
                )
            )
        )
        return RunResult(
            status="retry",
            reason="baseline draft cleanup completed after verification failure; rerun baseline evaluation",
            draft_id=reference.version,
            retry_phase="baseline",
        )

    def _record_candidate_verification_failure(
        self,
        current: _CandidateSidecar,
        reference: DraftReference,
        error: Exception,
        *,
        retry_phase: Literal["candidate", "validating"],
    ) -> RunResult:
        try:
            cleanup_receipt_id = self._cleanup_reference(reference)
        except RouteDriftError:
            raise
        except (AuthError, CleanupError, ContractError, DeadlineError, DraftUnavailableError, ServiceError) as cleanup_error:
            self._update_sidecars(
                lambda state: state.with_candidate(
                    state.candidates[current.candidate_id].model_copy(
                        update={
                            "reference": _StoredDraftReference.from_reference(reference),
                            "reference_verified": False,
                            "cleanup_required": True,
                            "retry_phase": retry_phase,
                            "cleanup_receipt_id": None,
                        }
                    )
                )
            )
            return RunResult(
                status="platform_failure",
                reason=f"{error}; candidate cleanup failed: {cleanup_error}",
                draft_id=current.draft_id,
            )
        self._update_sidecars(
            lambda state: state.with_candidate(
                state.candidates[current.candidate_id].model_copy(
                    update={
                        "reference": None,
                        "reference_verified": False,
                        "cleanup_required": False,
                        "retry_phase": retry_phase,
                        "cleanup_receipt_id": cleanup_receipt_id,
                    }
                )
            )
        )
        return RunResult(
            status="retry",
            reason=(
                "candidate draft cleanup completed after verification failure; "
                f"rerun {retry_phase} evaluation"
            ),
            draft_id=current.draft_id,
            retry_phase=retry_phase,
        )

    def _cleanup_baseline_reference(self) -> str:
        baseline = self._load_sidecar_state().baseline
        if baseline is None or baseline.pending_reference is None:
            raise RuntimeIntegrationError("baseline cleanup requires a persisted draft reference")
        cleanup_receipt_id = self._cleanup_reference(baseline.pending_reference.to_reference())
        self._update_sidecars(
            lambda current: current.with_baseline(
                current.baseline.model_copy(
                    update={
                        "pending_reference": None,
                        "reference_verified": False,
                        "cleanup_required": False,
                        "cleanup_receipt_id": cleanup_receipt_id,
                    }
                )
            )
        )
        return cleanup_receipt_id

    def _cleanup_candidate_reference(self, candidate_id: str) -> str:
        current = self._load_sidecar_state().candidates.get(candidate_id)
        if current is None or current.reference is None:
            raise RuntimeIntegrationError("candidate cleanup requires a persisted draft reference")
        cleanup_receipt_id = self._cleanup_reference(current.reference.to_reference())
        self._update_sidecars(
            lambda state: state.with_candidate(
                state.candidates[candidate_id].model_copy(
                    update={
                        "reference": None,
                        "reference_verified": False,
                        "cleanup_required": False,
                        "retry_phase": current.retry_phase,
                        "cleanup_receipt_id": cleanup_receipt_id,
                    }
                )
            )
        )
        return cleanup_receipt_id

    def _load_candidate_source_zip(self, candidate: FinalizedCandidate) -> bytes:
        code_zip = _read_file_bytes(candidate.source_zip_path, subject="candidate source ZIP")
        if hashlib.sha256(code_zip).hexdigest() != candidate.hashes.source_zip_sha256:
            raise RuntimeIntegrationError("candidate source ZIP hash does not match the finalized artifact")
        return code_zip

    def _assert_identity(self, identity: JobIdentity) -> None:
        if identity.repository != self._metadata.repository_identity:
            raise RuntimeIntegrationError("job identity repository does not match trusted metadata")
        if identity.source_root != self._source_root:
            raise RuntimeIntegrationError("job identity source_root does not match runtime settings")
        if identity.route_fingerprint != self._expected_route.sha256:
            raise RuntimeIntegrationError("job identity route fingerprint does not match runtime settings")
        _assert_runtime_digests(
            identity.runtime_digests,
            policy=self._policy,
            metadata=self._metadata,
            subject="job identity",
        )
        current_head = self._repository_head_loader(self._repository)
        if identity.base_commit != current_head:
            raise RuntimeIntegrationError("repository HEAD drifted from the optimize-job base commit")

    def _assert_candidate(self, candidate: FinalizedCandidate) -> None:
        if candidate.source_root != self._source_root:
            raise RuntimeIntegrationError("candidate source_root does not match runtime settings")
        if not _policy_allows_model(self._policy, self._metadata, candidate.model):
            raise RuntimeIntegrationError("candidate model is outside the repository policy")
        repository_head = self._repository_head_loader(self._repository)
        if candidate.base_commit != repository_head:
            raise RuntimeIntegrationError("candidate base_commit does not match the repository HEAD")

    def _create_draft_reference(self, *, model: str, code_zip: bytes) -> DraftReference:
        reference = self._client.create_draft(
            self._metadata.agent_name,
            build_hosted_definition(self._metadata, model),
            code_zip,
            deadline_monotonic=self._deadline(),
        )
        self._assert_route(reference.route)
        return reference

    def _verify_existing_draft(
        self,
        reference: DraftReference,
        *,
        code_zip: bytes,
    ) -> DraftReference:
        active = self._client.poll_version_active(
            reference,
            deadline_monotonic=self._deadline(),
        )
        downloaded = self._client.download_code(
            active,
            deadline_monotonic=self._deadline(),
        )
        if downloaded != code_zip:
            raise ContractError("Foundry draft code did not match the exact uploaded source ZIP")
        return active

    def _cleanup_reference(self, reference: DraftReference) -> str:
        self._client.delete_exact_owned_version(
            reference,
            deadline_monotonic=self._deadline(),
        )
        current = self._client.fingerprint_route(
            self._metadata.agent_name,
            deadline_monotonic=self._deadline(),
        )
        self._assert_route(current)
        return _cleanup_receipt_id(reference.version)

    def _assert_route(self, actual: RouteFingerprint) -> None:
        if actual.sha256 != self._expected_route.sha256:
            raise RouteDriftError(
                "Foundry route changed while the optimize job was operating on a draft",
                expected=self._expected_route,
                actual=actual,
            )

    def _development_contract(self, *, run_name: str) -> FoundryEvaluationContract:
        selection = (
            None
            if self._verification_resolution is None
            else self._verification_resolution.foundry_evaluation
        )
        if selection is None:
            contract = self._metadata.development_evaluation
            return FoundryEvaluationContract(
                evaluation_id=contract.resolved_evaluation_id,
                dataset_id=contract.dataset_id,
                evaluator_ids=contract.custom_evaluator_ids,
                run_name=run_name,
            )
        return FoundryEvaluationContract(
            evaluation_id=selection.defaults.development_definition_id,
            dataset_id=selection.development_dataset_id,
            evaluator_ids=selection.development_evaluator_ids,
            run_name=run_name,
        )

    def _validating_contract(self, *, run_name: str) -> FoundryEvaluationContract:
        selection = (
            None
            if self._verification_resolution is None
            else self._verification_resolution.foundry_evaluation
        )
        if selection is None:
            contract = self._metadata.validating_evaluation
            return FoundryEvaluationContract(
                evaluation_id=contract.resolved_evaluation_id,
                dataset_id=contract.dataset_id,
                evaluator_ids=contract.custom_evaluator_ids,
                run_name=run_name,
            )
        return FoundryEvaluationContract(
            evaluation_id=selection.defaults.validating_definition_id,
            dataset_id=selection.validating_dataset_id,
            evaluator_ids=selection.validating_evaluator_ids,
            run_name=run_name,
        )

    def _normalize_baseline_evidence(
        self,
        evidence: EvaluationEvidence,
    ) -> tuple[EvaluationSummary, tuple[_BaselineMetricAggregate, ...]]:
        primary_metric = _metric_by_name(evidence, self._policy.primary_metric)
        guardrails = _guardrail_results(evidence, self._policy)
        metrics = tuple(
            _BaselineMetricAggregate(
                name=metric.name,
                passed_cases=metric.passed_cases,
                failed_cases=metric.failed_cases,
            )
            for metric in sorted(evidence.metrics, key=lambda item: (item.name.casefold(), item.name))
        )
        return (
            EvaluationSummary(
                run_kind="development",
                successful=True,
                primary_score=primary_metric.score,
                focused_cases_improved=0,
                focused_cases_regressed=0,
                token_count=None,
                latency_ms=None,
                foundry_version=evidence.reference.agent_version,
                evaluation_link=evidence.report_url,
                guardrails=guardrails,
            ),
            metrics,
        )

    def _normalize_evidence(
        self,
        evidence: EvaluationEvidence,
        *,
        run_kind: str,
    ) -> EvaluationSummary:
        baseline = self._sidecars.load().baseline
        if baseline is None:
            raise RuntimeIntegrationError("baseline aggregates are required before candidate evaluation")
        primary_metric = _metric_by_name(evidence, self._policy.primary_metric)
        baseline_metric = baseline.metric(self._policy.primary_metric)
        if baseline_metric is None:
            raise RuntimeIntegrationError("baseline aggregates omitted the primary metric")
        improved = max(primary_metric.passed_cases - baseline_metric.passed_cases, 0)
        regressed = max(primary_metric.failed_cases - baseline_metric.failed_cases, 0)
        return EvaluationSummary(
            run_kind=run_kind,
            successful=True,
            primary_score=primary_metric.score,
            focused_cases_improved=improved,
            focused_cases_regressed=regressed,
            token_count=None,
            latency_ms=None,
            foundry_version=evidence.reference.agent_version,
            evaluation_link=evidence.report_url,
            guardrails=_guardrail_results(evidence, self._policy),
        )

    def _package_base_source(self, base_commit: str) -> bytes:
        commit = _validate_commit(base_commit, subject="baseline base_commit")
        try:
            packaged = package_git_source(
                self._repository,
                commit=commit,
                source_root=self._source_root,
                work_root=self._job_root,
                check_deadline=_noop_deadline,
            )
        except SourcePackagingError as error:
            raise RuntimeIntegrationError(str(error)) from error
        return packaged.archive_bytes

    def _should_defer_cleanup(self, candidate_id: str) -> bool:
        if not self._job_state_path.is_file():
            return False
        store = JobStateStore(self._job_state_path)
        try:
            state = store.load()
        except StateNotFoundError:
            return False
        if state.terminal_outcome is not None:
            return False
        return state.provisional_winner_id == candidate_id

    def _deadline(self) -> float:
        return self._monotonic() + self._deadline_seconds


class BrokerIssueComments:
    """Map comment markers to logical broker kinds and persist stable receipts."""

    def __init__(
        self,
        *,
        client: UnixSocketBrokerClient,
        sidecars: RuntimeSidecarStore,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._client = client
        self._sidecars = sidecars
        self._timeout_seconds = load_deadline_seconds(deadline_seconds=timeout_seconds)

    def upsert_comment(self, comment: RenderedComment) -> str:
        logical_kind = _logical_kind_from_marker(comment.marker_id)
        try:
            receipt = self._client.upsert_comment(
                request_id=_stable_request_id("comment", logical_kind),
                logical_kind=logical_kind,
                markdown=comment.body,
                timeout_seconds=self._timeout_seconds,
            )
        except (BrokerRemoteError, BrokerUnavailableError) as error:
            raise runtime_integration_error_from_broker_failure(
                error,
                fallback="issue comment write is unavailable",
            ) from error
        if receipt.logical_kind != logical_kind:
            raise RuntimeIntegrationError("broker comment receipt logical_kind did not match the trusted marker")
        self._sidecars.update(lambda state: state.with_comment(logical_kind, receipt))
        return _comment_receipt_id(receipt)


class BrokerClosure:
    """Use the broker's final comment receipt to close a no-winner pull request."""

    def __init__(
        self,
        *,
        client: UnixSocketBrokerClient,
        sidecars: RuntimeSidecarStore,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._client = client
        self._sidecars = sidecars
        self._timeout_seconds = load_deadline_seconds(deadline_seconds=timeout_seconds)

    def signal_no_winner(self, identity: JobIdentity) -> str:
        receipt = self._sidecars.load().comments.get("final")
        if receipt is None:
            raise RuntimeIntegrationError("final comment receipt is required before no-winner closure")
        try:
            pull_request = self._client.close_no_winner(
                request_id=_stable_request_id("close-no-winner", identity.job_id),
                final_decision_receipt=FinalDecisionReceipt(
                    decision=FinalDecision.NO_WINNER,
                    comment_receipt=receipt,
                ),
                timeout_seconds=self._timeout_seconds,
            )
        except (BrokerRemoteError, BrokerUnavailableError) as error:
            raise runtime_integration_error_from_broker_failure(
                error,
                fallback="no-winner closure is unavailable",
                pull_request_binding_message="pull request binding is required for no-winner closure",
            ) from error
        return _pull_request_receipt_id(pull_request)


def capture_route_fingerprint(
    *,
    repository: Path,
    environment: Mapping[str, str] | None = None,
    paths: RuntimePaths | None = None,
    settings: RuntimeSettings | None = None,
    policy_path: Path | str | None = None,
    metadata_path: Path | str | None = None,
    pin_path: Path | str | None = None,
    bootstrap_receipt_path: Path | str | None = None,
    broker_socket_path: Path | str | None = None,
    state_root: Path | str | None = None,
    deadline_seconds: float | str | None = None,
    credential_builder: Callable[..., object] = build_client_assertion_credential,
    foundry_client_factory: Callable[..., FoundryPocClient] = FoundryPocClient,
    policy_loader: Callable[..., RepositoryPolicy] = load_repository_policy,
    metadata_loader: Callable[[Path | str], AgentMetadata] = load_agent_metadata,
    pin_loader: Callable[[Path | str], SharedPin] = load_shared_pin,
    bootstrap_receipt_reader: Callable[[Path | str], BootstrapReceipt] = read_bootstrap_receipt,
    monotonic: Callable[[], float] = time.monotonic,
) -> RouteFingerprint:
    runtime_paths = paths or load_runtime_paths(
        repository,
        environment=environment,
        policy_path=policy_path,
        metadata_path=metadata_path,
        pin_path=pin_path,
        bootstrap_receipt_path=bootstrap_receipt_path,
        broker_socket_path=broker_socket_path,
        state_root=state_root,
    )
    runtime_settings = settings or load_runtime_settings(
        runtime_paths,
        environment=environment,
        deadline_seconds=deadline_seconds,
        policy_loader=policy_loader,
        metadata_loader=metadata_loader,
        pin_loader=pin_loader,
        bootstrap_receipt_reader=bootstrap_receipt_reader,
    )
    credential = credential_builder(
        build_oidc_config(runtime_settings.metadata, role="optimizer"),
        environment=environment,
    )
    client = foundry_client_factory(
        runtime_settings.metadata.project_endpoint,
        credential,
        evaluation_backend=None,
    )
    try:
        return client.fingerprint_route(
            runtime_settings.metadata.agent_name,
            deadline_monotonic=monotonic() + runtime_settings.deadline_seconds,
        )
    finally:
        _close_if_supported(client)
        _close_if_supported(credential)


def build_job_identity(
    *,
    settings: RuntimeSettings,
    issue_number: int,
    route_fingerprint: RouteFingerprint | str,
    job_id: str | None = None,
    min_candidates: int | None = None,
    base_commit: str | None = None,
) -> JobIdentity:
    if isinstance(route_fingerprint, RouteFingerprint):
        if route_fingerprint.agent_name != settings.metadata.agent_name:
            raise RuntimeIntegrationError("route fingerprint agent_name does not match trusted metadata")
        route_sha256 = route_fingerprint.sha256
    else:
        route_sha256 = _validate_sha256(route_fingerprint, subject="route_fingerprint")
    selected_job_id = _validate_job_id_component(
        f"optimize-{issue_number}" if job_id is None else job_id
    )
    selected_candidates = settings.policy.min_candidates if min_candidates is None else min_candidates
    if (
        selected_candidates < settings.policy.min_candidates
        or selected_candidates > settings.policy.max_candidates
    ):
        raise RuntimeIntegrationError("optimize job candidate budget is outside the repository policy")
    selected_base_commit = settings.base_commit if base_commit is None else _validate_commit(
        base_commit,
        subject="base_commit",
    )
    return JobIdentity(
        job_id=selected_job_id,
        repository=settings.metadata.repository_identity,
        issue_number=issue_number,
        shared_commit=settings.pin.commit,
        base_commit=selected_base_commit,
        source_root=settings.policy.source_root,
        route_fingerprint=route_sha256,
        min_candidates=selected_candidates,
        runtime_digests=_runtime_contract_digests(
            policy=settings.policy,
            metadata=settings.metadata,
        ),
    )


def build_runtime_controller(
    *,
    repository: Path,
    identity: JobIdentity,
    environment: Mapping[str, str] | None = None,
    paths: RuntimePaths | None = None,
    settings: RuntimeSettings | None = None,
    captured_route: RouteFingerprint | str | None = None,
    verification_resolution: VerificationResolution | None = None,
    policy_path: Path | str | None = None,
    metadata_path: Path | str | None = None,
    pin_path: Path | str | None = None,
    bootstrap_receipt_path: Path | str | None = None,
    broker_socket_path: Path | str | None = None,
    state_root: Path | str | None = None,
    deadline_seconds: float | str | None = None,
    credential_builder: Callable[..., object] = build_client_assertion_credential,
    evaluation_backend_factory: Callable[..., AzureProjectsEvaluationBackend] = AzureProjectsEvaluationBackend,
    foundry_client_factory: Callable[..., FoundryPocClient] = FoundryPocClient,
    candidate_workspace_factory: Callable[..., CandidateWorkspace] = CandidateWorkspace,
    state_store_factory: Callable[[Path], JobStateStore] = JobStateStore,
    broker_client_factory: Callable[..., UnixSocketBrokerClient] = UnixSocketBrokerClient,
    foundry_operations_factory: Callable[..., ControllerFoundryOperations] = ControllerFoundryOperations,
    check_runner_factory: Callable[..., RepositoryCheckRunnerProtocol] = LocalRepositoryCheckRunner,
    comments_factory: Callable[..., BrokerIssueComments] = BrokerIssueComments,
    closure_factory: Callable[..., BrokerClosure] = BrokerClosure,
    policy_loader: Callable[..., RepositoryPolicy] = load_repository_policy,
    metadata_loader: Callable[[Path | str], AgentMetadata] = load_agent_metadata,
    pin_loader: Callable[[Path | str], SharedPin] = load_shared_pin,
    bootstrap_receipt_reader: Callable[[Path | str], BootstrapReceipt] = read_bootstrap_receipt,
) -> OptimizeJobController:
    runtime_paths = paths or load_runtime_paths(
        repository,
        environment=environment,
        job_id=identity.job_id,
        policy_path=policy_path,
        metadata_path=metadata_path,
        pin_path=pin_path,
        bootstrap_receipt_path=bootstrap_receipt_path,
        broker_socket_path=broker_socket_path,
        state_root=state_root,
    )
    runtime_settings = settings or load_runtime_settings(
        runtime_paths,
        environment=environment,
        base_commit=identity.base_commit,
        deadline_seconds=deadline_seconds,
        policy_loader=policy_loader,
        metadata_loader=metadata_loader,
        pin_loader=pin_loader,
        bootstrap_receipt_reader=bootstrap_receipt_reader,
    )
    _assert_identity_matches_settings(identity, runtime_settings)
    expected_route = captured_route or identity.route_fingerprint
    if isinstance(expected_route, RouteFingerprint) and expected_route.sha256 != identity.route_fingerprint:
        raise RuntimeIntegrationError("captured route fingerprint does not match the job identity")
    credential = credential_builder(
        build_oidc_config(runtime_settings.metadata, role="optimizer"),
        environment=environment,
    )
    evaluation_backend = evaluation_backend_factory(
        project_endpoint=runtime_settings.metadata.project_endpoint,
        credential=credential,
    )
    client = foundry_client_factory(
        runtime_settings.metadata.project_endpoint,
        credential,
        evaluation_backend=evaluation_backend,
    )
    sidecars = RuntimeSidecarStore(runtime_paths.sidecar_path)
    workspace = candidate_workspace_factory(
        runtime_paths.repository_root,
        runtime_paths.workspace_root,
        runtime_settings.base_commit,
        editable_patterns=runtime_settings.policy.editable_paths,
        protected_patterns=_runtime_protected_patterns(
            runtime_paths.repository_root,
            runtime_settings.policy,
        ),
        source_root=runtime_settings.policy.source_root,
    )
    state_store = state_store_factory(runtime_paths.job_state_path)
    broker_client = broker_client_factory(socket_path=runtime_paths.broker_socket_path)
    foundry = foundry_operations_factory(
        repository=runtime_paths.repository_root,
        source_root=runtime_settings.policy.source_root,
        policy=runtime_settings.policy,
        metadata=runtime_settings.metadata,
        client=client,
        artifact_state_path=runtime_paths.job_root,
        route_fingerprint=expected_route,
        verification_resolution=verification_resolution,
        sidecars=sidecars,
        deadline_seconds=runtime_settings.deadline_seconds,
    )
    check_runner = check_runner_factory(
        timeout_seconds=min(runtime_settings.deadline_seconds, 300.0),
    )
    comments = comments_factory(
        client=broker_client,
        sidecars=sidecars,
    )
    closure = closure_factory(
        client=broker_client,
        sidecars=sidecars,
    )
    return OptimizeJobController(
        store=state_store,
        workspace=workspace,
        foundry=foundry,
        comments=comments,
        closure=closure,
        rules=_decision_rules(runtime_settings.policy),
        check_runner=check_runner,
    )


def _assert_identity_matches_settings(identity: JobIdentity, settings: RuntimeSettings) -> None:
    if identity.repository != settings.metadata.repository_identity:
        raise RuntimeIntegrationError("job identity repository does not match trusted metadata")
    if identity.shared_commit != settings.pin.commit:
        raise RuntimeIntegrationError("job identity shared_commit does not match the shared pin")
    if identity.base_commit != settings.base_commit:
        raise RuntimeIntegrationError("job identity base_commit does not match runtime settings")
    if identity.source_root != settings.policy.source_root:
        raise RuntimeIntegrationError("job identity source_root does not match the repository policy")
    if identity.route_fingerprint and _SHA256_PATTERN.fullmatch(identity.route_fingerprint) is None:
        raise RuntimeIntegrationError("job identity route_fingerprint is invalid")
    _assert_runtime_digests(
        identity.runtime_digests,
        policy=settings.policy,
        metadata=settings.metadata,
        subject="job identity",
    )
    if settings.repository_head != identity.base_commit:
        raise RuntimeIntegrationError("repository HEAD drifted from the optimize-job base commit")
    if (
        identity.min_candidates < settings.policy.min_candidates
        or identity.min_candidates > settings.policy.max_candidates
    ):
        raise RuntimeIntegrationError("job identity min_candidates is outside the repository policy")


def build_oidc_config(
    metadata: AgentMetadata,
    *,
    role: str,
) -> GitHubActionsOidcConfig:
    principal = select_oidc_principal(metadata, role=role)
    variables = {item.alias: item for item in metadata.oidc.workflow_variables}
    variable = variables.get(principal.client_id_variable)
    if variable is None:
        raise RuntimeIntegrationError(
            f"{role} OIDC principal does not reference a declared workflow variable"
        )
    if variable.value != principal.client_id:
        raise RuntimeIntegrationError(
            f"{role} OIDC principal client_id does not match its workflow variable"
        )
    subject = principal.direct_oidc_subject or principal.subject
    if subject is None and len(principal.subjects) == 1:
        subject = principal.subjects[0].subject
    if subject is None:
        raise RuntimeIntegrationError(
            f"{role} OIDC principal must declare one exact GitHub subject"
        )
    return GitHubActionsOidcConfig(
        tenant_id=metadata.oidc.tenant_id,
        client_id=principal.client_id,
        expected_subject=subject,
        expected_repository_id=metadata.oidc.repository_id_claim,
        audience=metadata.oidc.audience,
        issuer=metadata.oidc.issuer,
    )


def select_oidc_principal(
    metadata: AgentMetadata,
    *,
    role: str,
) -> object:
    normalized_role = role.casefold()
    principals = metadata.oidc.principals
    for principal in principals:
        if principal.role.casefold() == normalized_role:
            return principal
    if normalized_role == "optimizer" and len(principals) == 1:
        return principals[0]
    raise RuntimeIntegrationError(
        f"trusted metadata must declare one {role} OIDC principal"
    )


def _validate_bootstrap_receipt(pin: SharedPin, receipt: BootstrapReceipt) -> None:
    if receipt.repository != pin.repository_url:
        raise RuntimeIntegrationError("bootstrap receipt repository does not match the shared pin")
    if receipt.commit != pin.commit:
        raise RuntimeIntegrationError("bootstrap receipt commit does not match the shared pin")
    if receipt.package_path != pin.package_path:
        raise RuntimeIntegrationError("bootstrap receipt package_path does not match the shared pin")
    if not optimizer_skill_paths_match(receipt.skill_path, pin.skill_path):
        raise RuntimeIntegrationError("bootstrap receipt skill_path does not match the shared pin")
    if receipt.lock_sha256 != pin.uv_lock_sha256:
        raise RuntimeIntegrationError("bootstrap receipt lock_sha256 does not match the shared pin")


def _validate_policy_models(policy: RepositoryPolicy, metadata: AgentMetadata) -> None:
    _resolve_model_deployment(metadata, policy.baseline_model)
    for model in policy.allowed_models:
        _resolve_model_deployment(metadata, model)
    if not _policy_allows_model(policy, metadata, policy.baseline_model):
        raise RuntimeIntegrationError("repository policy baseline_model is not in allowed_models")


def _policy_allows_model(
    policy: RepositoryPolicy,
    metadata: AgentMetadata,
    model: str,
) -> bool:
    target = _resolve_model_deployment(metadata, model)
    for allowed in policy.allowed_models:
        candidate = _resolve_model_deployment(metadata, allowed)
        if candidate.deployment_name.casefold() == target.deployment_name.casefold():
            return True
    return False


def _resolve_model_deployment(
    metadata: AgentMetadata,
    model: str,
) -> ModelDeploymentContract:
    matches: list[ModelDeploymentContract] = []
    key = model.casefold()
    for deployment in metadata.model_deployments:
        if deployment.alias.casefold() == key or deployment.deployment_name.casefold() == key:
            matches.append(deployment)
    if not matches:
        raise RuntimeIntegrationError("selected model is not in trusted metadata model_deployments")
    if len(matches) != 1:
        raise RuntimeIntegrationError("selected model is ambiguous in trusted metadata model_deployments")
    return matches[0]


def _decision_rules(policy: RepositoryPolicy) -> ControllerDecisionRules:
    return ControllerDecisionRules(
        aggregate_min_delta=policy.decision_rules.minimum_aggregate_delta,
        min_focused_cases_improved=(1 if policy.decision_rules.focused_cases_required else 0),
        max_focused_regressions=policy.decision_rules.max_regressions,
        guardrails=tuple(
            GuardrailRule(
                name=guardrail.metric,
                minimum_score=guardrail.required_pass_rate,
                require_pass=True,
            )
            for guardrail in policy.hard_guardrails
        ),
    )


def _guardrail_results(
    evidence: EvaluationEvidence,
    policy: RepositoryPolicy,
) -> tuple[GuardrailResult, ...]:
    return tuple(
        GuardrailResult(
            name=guardrail.metric,
            passed=_metric_by_name(evidence, guardrail.metric).passed,
            score=_metric_by_name(evidence, guardrail.metric).score,
        )
        for guardrail in policy.hard_guardrails
    )


def _metric_by_name(evidence: EvaluationEvidence, name: str) -> object:
    key = name.casefold()
    for metric in evidence.metrics:
        if metric.name.casefold() == key:
            return metric
    raise ContractError("evaluation evidence omitted a policy-required metric")


def _protected_patterns(policy: RepositoryPolicy) -> tuple[str, ...]:
    protected = {
        ".git/**",
        ".foundry-opt/**",
        ".foundry-opt/registry.yaml",
        str(DEFAULT_POLICY_PATH).replace("\\", "/"),
        policy.metadata_path,
        str(DEFAULT_PIN_PATH).replace("\\", "/"),
        "uv.lock",
    }
    return tuple(sorted(protected))


def _runtime_protected_patterns(
    repository_root: Path,
    policy: RepositoryPolicy,
) -> tuple[str, ...]:
    protected = set(_protected_patterns(policy))
    registry_path = repository_root / ".foundry-opt" / "registry.yaml"
    if registry_path.is_file():
        protected.update(protected_editable_patterns_for_repository(repository_root))
    overlaps = [
        editable
        for editable in policy.editable_paths
        if any(
            editable == protected_path.removesuffix("/**")
            or editable.startswith(protected_path.removesuffix("/**"))
            or (
                editable.endswith("/**")
                and protected_path.startswith(editable.removesuffix("/**"))
            )
            for protected_path in protected
        )
    ]
    if overlaps:
        raise RuntimeIntegrationError(
            f"editable path overlaps protected workflow/config scope: {overlaps[0]}"
        )
    return tuple(sorted(protected))


def _stable_request_id(prefix: str, subject: str) -> str:
    digest = hashlib.sha256(subject.encode("ascii")).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _logical_kind_from_marker(marker_id: str) -> str:
    if _BASELINE_MARKER_PATTERN.fullmatch(marker_id) is not None:
        return "baseline"
    if _FINAL_MARKER_PATTERN.fullmatch(marker_id) is not None:
        return "final"
    match = _CANDIDATE_MARKER_PATTERN.fullmatch(marker_id)
    if match is None:
        raise RuntimeIntegrationError("rendered comment marker_id is outside the trusted broker mapping")
    candidate_id = match.group("candidate_id")
    return f"candidate-{candidate_id}"


def _comment_receipt_id(receipt: CommentReceipt) -> str:
    return f"comment:{receipt.comment_id}"


def _pull_request_receipt_id(receipt: PullRequestReceipt) -> str:
    return f"pull-request:{receipt.pull_request_number}"


def runtime_integration_error_from_broker_failure(
    error: BrokerRemoteError | BrokerUnavailableError,
    *,
    fallback: str,
    pull_request_binding_message: str | None = None,
) -> RuntimeIntegrationError:
    message = _redact_broker_error_text(str(error)) or fallback
    if (
        pull_request_binding_message is not None
        and "pull request binding" in message.casefold()
    ):
        message = pull_request_binding_message
    return RuntimeIntegrationError(message)


def _redact_broker_error_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = " ".join(value.split())
    text = _BROKER_ERROR_CONTROL_PATTERN.sub(" ", text)
    text = _BROKER_ERROR_TOKEN_PATTERN.sub("******", text)
    return text[:240]


def _cleanup_receipt_id(draft_id: str) -> str:
    return f"cleanup:{draft_id}"


def _environment_or_path(
    value: Path | str | None,
    env_name: str,
    *,
    environment: Mapping[str, str],
) -> Path:
    if value is not None:
        return Path(value)
    raw = environment.get(env_name)
    if raw is None or not raw.strip():
        raise RuntimeIntegrationError(f"{env_name} is required")
    return Path(raw)


def _resolve_existing_file(path: Path, *, field: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as error:
        raise RuntimeIntegrationError(f"{field} could not be resolved") from error
    if not resolved.is_file():
        raise RuntimeIntegrationError(f"{field} must be a file")
    return resolved


def _resolve_any_path(path: Path, *, field: str) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError as error:
        raise RuntimeIntegrationError(f"{field} could not be resolved") from error


def _repository_root(repository: Path) -> Path:
    resolved = Path(repository).resolve(strict=True)
    discovered = _git_text(resolved, "rev-parse", "--show-toplevel")
    root = Path(discovered).resolve(strict=True)
    if root != resolved:
        raise RuntimeIntegrationError("repository must be the Git worktree root")
    return root


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
        raise RuntimeIntegrationError("git command timed out") from error
    except OSError as error:
        raise RuntimeIntegrationError("git could not be executed") from error
    if completed.returncode != 0:
        if text:
            detail = completed.stderr.strip() or completed.stdout.strip()
        else:
            detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeIntegrationError(
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


def _validate_job_id_component(value: str) -> str:
    if _JOB_ID_PATTERN.fullmatch(value) is None:
        raise RuntimeIntegrationError("job_id must be a bounded filesystem-safe identifier")
    return value


def _validate_commit(value: str, *, subject: str) -> str:
    if _COMMIT_PATTERN.fullmatch(value) is None:
        raise RuntimeIntegrationError(f"{subject} must be a Git commit SHA")
    return value


def _validate_sha256(value: str, *, subject: str) -> str:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise RuntimeIntegrationError(f"{subject} must be a 64-character lowercase SHA-256")
    return value


def _validate_deadline_seconds(value: float | str, *, subject: str) -> float:
    try:
        number = float(value)
    except ValueError as error:
        raise RuntimeIntegrationError(f"{subject} must be numeric") from error
    if not math.isfinite(number) or number <= 0:
        raise RuntimeIntegrationError(f"{subject} must be positive and finite")
    return number


def _read_file_bytes(path: Path, *, subject: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as error:
        raise RuntimeIntegrationError(f"{subject} could not be read") from error


def _runtime_contract_digests(
    *,
    policy: RepositoryPolicy,
    metadata: AgentMetadata,
) -> JobRuntimeDigests:
    selected_models = sorted(
        {policy.baseline_model, *policy.allowed_models},
        key=lambda item: (item.casefold(), item),
    )
    return JobRuntimeDigests(
        policy_sha256=_sha256_json(policy.model_dump(mode="json")),
        metadata_sha256=_sha256_json(metadata.model_dump(mode="json")),
        hosted_contracts_sha256=_sha256_json(
            {
                "agent_name": metadata.agent_name,
                "models": [
                    {
                        "model": model,
                        "definition": build_hosted_definition(metadata, model).as_payload(),
                    }
                    for model in selected_models
                ],
            }
        ),
        development_evaluation_sha256=_sha256_json(
            _evaluation_contract_digest_payload(metadata.development_evaluation)
        ),
        validating_evaluation_sha256=_sha256_json(
            _evaluation_contract_digest_payload(metadata.validating_evaluation)
        ),
    )


def _evaluation_contract_digest_payload(contract: Any) -> dict[str, object]:
    return {
        "evaluation_id": contract.resolved_evaluation_id,
        "dataset_id": contract.dataset_id,
        "evaluator_ids": list(contract.custom_evaluator_ids),
    }


def _assert_runtime_digests(
    expected: JobRuntimeDigests | None,
    *,
    policy: RepositoryPolicy,
    metadata: AgentMetadata,
    subject: str,
) -> None:
    if expected is None:
        return
    current = _runtime_contract_digests(policy=policy, metadata=metadata)
    for field, label in (
        ("policy_sha256", "policy"),
        ("metadata_sha256", "metadata"),
        ("hosted_contracts_sha256", "hosted contract"),
        ("development_evaluation_sha256", "development evaluation"),
        ("validating_evaluation_sha256", "validating evaluation"),
    ):
        if getattr(expected, field) != getattr(current, field):
            raise RuntimeIntegrationError(
                f"{subject} {label} digest does not match current runtime settings"
            )


def _sha256_json(payload: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


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


def _strict_json_object(value: bytes, *, subject: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise RuntimeSidecarError(f"{subject} contains a duplicate JSON key")
            result[key] = item
        return result

    try:
        payload = json.loads(value.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except RuntimeSidecarError:
        raise
    except UnicodeDecodeError as error:
        raise RuntimeSidecarError(f"{subject} is not UTF-8 JSON") from error
    except json.JSONDecodeError as error:
        raise RuntimeSidecarError(f"{subject} is not valid JSON") from error
    if not isinstance(payload, dict):
        raise RuntimeSidecarError(f"{subject} must be a JSON object")
    return payload


def _json_object(value: object, *, subject: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{subject} must be a JSON object")
    normalized: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise TypeError(f"{subject} keys must be non-empty strings")
        normalized[key] = _json_value(item, subject=subject)
    return normalized


def _json_value(value: object, *, subject: str) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return _json_object(value, subject=subject)
    if isinstance(value, (list, tuple)):
        return [_json_value(item, subject=subject) for item in value]
    raise TypeError(f"{subject} contains a non-JSON value")


def _close_if_supported(value: object) -> None:
    closer = getattr(value, "close", None)
    if callable(closer):
        closer()


def _noop_deadline() -> None:
    return None


__all__ = [
    "BOOTSTRAP_RECEIPT_ENV",
    "BROKER_SOCKET_ENV",
    "BrokerClosure",
    "BrokerIssueComments",
    "ControllerFoundryOperations",
    "DEADLINE_SECONDS_ENV",
    "DEFAULT_DEADLINE_SECONDS",
    "DEFAULT_METADATA_PATH",
    "DEFAULT_PIN_PATH",
    "DEFAULT_POLICY_PATH",
    "RUNTIME_SIDECAR_FILENAME",
    "RuntimeIntegrationError",
    "RuntimePaths",
    "RuntimeSettings",
    "RuntimeSidecarStore",
    "RuntimeWiringError",
    "STATE_ROOT_ENV",
    "build_hosted_definition",
    "build_oidc_config",
    "build_job_identity",
    "build_runtime_controller",
    "capture_route_fingerprint",
    "load_deadline_seconds",
    "load_repository_head",
    "load_runtime_paths",
    "load_runtime_settings",
    "resolve_base_commit",
    "select_oidc_principal",
]
