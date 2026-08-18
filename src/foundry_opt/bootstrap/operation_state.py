from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from foundry_opt.bootstrap.canonical import canonical_json_bytes, canonical_sha256
from foundry_opt.bootstrap.contracts import AgentId, BindingAssessment, BindingClassification, BootstrapDocument, BootstrapPlan, FingerprintRecord, Sha256
from foundry_opt.bootstrap.errors import BootstrapApplyError, BootstrapConfigError
from foundry_opt.bootstrap.receipts import ApplyPhaseName, ApprovalRecord, EvaluationReplacementRecord, PhaseReceipt, merge_phase_receipts

GenerationStatus = Literal["draft", "applied", "blocked"]

_MAX_STATE_BYTES = 512 * 1024
_STATE_FILE_NAME = "state.json"
_LOCK_FILE_NAME = "state.lock"


class DiscoveryBlockerRecord(BootstrapDocument):
    code: str
    detail: str


class DiscoveredAgentRecord(BootstrapDocument):
    """Local discovery facts the skill needs to build and review binding evidence.

    Fingerprints are the repository-side digests observed evidence must reproduce, so they are
    persisted with the operation and echoed by `bootstrap discover`.
    """

    repo_agent_id: AgentId
    root: str
    config_path: str | None = None
    source_root: str
    package_root: str
    source_fingerprint: Sha256
    package_fingerprint: Sha256
    classification: BindingClassification
    detail: str | None = None
    confidence: float = 0.0
    blockers: tuple[DiscoveryBlockerRecord, ...] = ()
    approved_shared_source_repo_agent_ids: tuple[str, ...] = ()

    def to_discovery_payload(self) -> dict[str, object]:
        """Discovery-native view, matching `discovery_result_json` field names."""

        return {
            "repoAgentId": self.repo_agent_id,
            "root": self.root,
            "configPath": self.config_path,
            "sourceRoot": self.source_root,
            "packageRoot": self.package_root,
            "sourceFingerprint": self.source_fingerprint,
            "packageFingerprint": self.package_fingerprint,
            "classification": self.classification,
            "detail": self.detail,
            "confidence": self.confidence,
            "blockers": [{"code": item.code, "detail": item.detail} for item in self.blockers],
            "approvedSharedSourceRepoAgentIds": list(self.approved_shared_source_repo_agent_ids),
        }


class SelectionPlan(BootstrapDocument):
    repository_root: str
    selected_agent_ids: tuple[str, ...]
    binding_assessments: tuple[BindingAssessment, ...]
    discovery_fingerprints: tuple[FingerprintRecord, ...]
    blockers: tuple[str, ...] = ()
    discovered_agents: tuple[DiscoveredAgentRecord, ...] = ()


class EvaluatorLineage(BootstrapDocument):
    objective_hash: str | None = None
    lineage_hash: str | None = None
    active_bundle_id: str | None = None
    preserved_bundle_id: str | None = None


class AgentEvaluatorLineage(EvaluatorLineage):
    """Per-agent evaluator bundle lineage.

    A repository may onboard several agents in one operation, each with its own bundle and
    replacement lineage, so lineage is recorded per agent. The legacy single
    `evaluator_replacement` field remains as a compatibility projection of the first agent.
    """

    repo_agent_id: AgentId
    candidate_bundle_id: str | None = None
    status: Literal["planned", "activated", "failed"] = "planned"
    detail: str | None = None


class EvaluationAgentReplacement(BootstrapDocument):
    repo_agent_id: AgentId
    active_bundle_id: str = Field(min_length=1, max_length=256)
    candidate_bundle_id: str = Field(min_length=1, max_length=256)
    preserved_bundle_id: str = Field(min_length=1, max_length=256)
    lineage_hash: str = Field(min_length=1, max_length=128)
    status: Literal["planned", "activated", "failed"]
    detail: str | None = Field(default=None, max_length=4096)

    def as_legacy_record(self) -> EvaluationReplacementRecord:
        return EvaluationReplacementRecord(
            active_bundle_id=self.active_bundle_id,
            candidate_bundle_id=self.candidate_bundle_id,
            preserved_bundle_id=self.preserved_bundle_id,
            lineage_hash=self.lineage_hash,
            status=self.status,
            detail=self.detail,
        )

    def as_lineage(self) -> AgentEvaluatorLineage:
        return AgentEvaluatorLineage(
            repo_agent_id=self.repo_agent_id,
            lineage_hash=self.lineage_hash,
            active_bundle_id=self.candidate_bundle_id if self.status == "activated" else self.active_bundle_id,
            preserved_bundle_id=self.preserved_bundle_id,
            candidate_bundle_id=self.candidate_bundle_id,
            status=self.status,
            detail=self.detail,
        )


class OperationStatus(BootstrapDocument):
    operation_id: str
    repository_id: str
    runtime_repository: str
    runtime_commit: str
    plan_hash: str
    phase_states: tuple[PhaseReceipt, ...]
    blockers: tuple[str, ...]
    binding_assessments: tuple[BindingAssessment, ...]
    evaluator_lineage: EvaluatorLineage
    deployment_eligible: bool
    evaluator_lineages: tuple[AgentEvaluatorLineage, ...] = ()


class OperationStatePayload(BootstrapDocument):
    generation: int = Field(ge=0)
    repository_id: str
    operation_id: str
    runtime_repository: str
    runtime_commit: str
    selection_plan: SelectionPlan
    bootstrap_plan: BootstrapPlan
    discovery_fingerprints: tuple[FingerprintRecord, ...]
    resource_fingerprints: tuple[FingerprintRecord, ...] = ()
    required_phases: tuple[ApplyPhaseName, ...] = ()
    approvals: tuple[ApprovalRecord, ...] = ()
    phase_receipts: tuple[PhaseReceipt, ...] = ()
    evaluator_replacement: EvaluationReplacementRecord | None = None
    evaluator_replacements: tuple[EvaluationAgentReplacement, ...] = ()


class OperationStateEnvelope(BootstrapDocument):
    payload: OperationStatePayload
    generation_hash: str

    @property
    def generation(self) -> int:
        return self.payload.generation

    @property
    def repository_id(self) -> str:
        return self.payload.repository_id

    @property
    def operation_id(self) -> str:
        return self.payload.operation_id

    @property
    def runtime_repository(self) -> str:
        return self.payload.runtime_repository

    @property
    def runtime_commit(self) -> str:
        return self.payload.runtime_commit

    @property
    def selection_plan(self) -> SelectionPlan:
        return self.payload.selection_plan

    @property
    def bootstrap_plan(self) -> BootstrapPlan:
        return self.payload.bootstrap_plan

    @property
    def discovery_fingerprints(self) -> tuple[FingerprintRecord, ...]:
        return self.payload.discovery_fingerprints

    @property
    def resource_fingerprints(self) -> tuple[FingerprintRecord, ...]:
        return self.payload.resource_fingerprints

    @property
    def required_phases(self) -> tuple[ApplyPhaseName, ...]:
        return self.payload.required_phases

    @property
    def approvals(self) -> tuple[ApprovalRecord, ...]:
        return self.payload.approvals

    @property
    def phase_receipts(self) -> tuple[PhaseReceipt, ...]:
        return self.payload.phase_receipts

    @property
    def evaluator_replacement(self) -> EvaluationReplacementRecord | None:
        return self.payload.evaluator_replacement

    @property
    def evaluator_replacements(self) -> tuple[EvaluationAgentReplacement, ...]:
        return self.payload.evaluator_replacements

    @field_validator("payload")
    @classmethod
    def _validate_segment(cls, value: OperationStatePayload) -> OperationStatePayload:
        if not value.operation_id or any(sep in value.operation_id for sep in ("/", "\\", "..")):
            raise BootstrapConfigError("state path segment is invalid")
        return value

    @model_validator(mode="after")
    def _validate_hash(self) -> "OperationStateEnvelope":
        if len(self.generation_hash) != 64 or any(ch not in "0123456789abcdef" for ch in self.generation_hash):
            raise BootstrapApplyError("generation_hash must be a 64-character lowercase sha256 hex digest")
        payload = {"payload": self.payload.model_dump(mode="json")}
        if self.generation_hash != canonical_sha256(payload):
            raise BootstrapApplyError("generation_hash does not match state payload")
        return self

    @classmethod
    def create(cls, **values: object) -> "OperationStateEnvelope":
        payload = _jsonable(dict(values))
        validated_payload = OperationStatePayload.model_validate(payload)
        payload_json = validated_payload.model_dump(mode="json")
        digest = canonical_sha256({"payload": payload_json})
        return cls.model_validate({"payload": payload_json, "generation_hash": digest})


def default_state_root() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "foundry-opt" / "bootstrap"
    home = Path.home()
    return home / ".foundry-opt" / "bootstrap"


def operation_directory(repository_id: str, operation_id: str, *, state_root: Path | None = None) -> Path:
    root = (state_root or default_state_root()).resolve()
    repo_segment = canonical_sha256({"repository_id": repository_id})
    target = (root / repo_segment / operation_id).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise BootstrapApplyError("operation state escapes the state root") from exc
    return target


def state_file_path(repository_id: str, operation_id: str, *, state_root: Path | None = None) -> Path:
    return operation_directory(repository_id, operation_id, state_root=state_root) / _STATE_FILE_NAME


def lock_file_path(repository_id: str, operation_id: str, *, state_root: Path | None = None) -> Path:
    return operation_directory(repository_id, operation_id, state_root=state_root) / _LOCK_FILE_NAME


def write_operation_state(envelope: OperationStateEnvelope, *, expected_generation: int | None = None, state_root: Path | None = None) -> Path:
    path = state_file_path(envelope.repository_id, envelope.operation_id, state_root=state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = lock_file_path(envelope.repository_id, envelope.operation_id, state_root=state_root)
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise BootstrapApplyError("operation state is locked by another writer") from exc
    try:
        if expected_generation is None and path.exists():
            raise BootstrapApplyError("operation state already exists")
        if expected_generation is not None and path.exists():
            current = read_operation_state(envelope.repository_id, envelope.operation_id, state_root=state_root)
            if current.generation != expected_generation:
                raise BootstrapApplyError("operation state generation conflict")
        data = canonical_json_bytes(envelope.model_dump(mode="json")) + b"\n"
        if len(data) > _MAX_STATE_BYTES:
            raise BootstrapApplyError("operation state exceeds size limit")
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


def read_operation_state(repository_id: str, operation_id: str, *, state_root: Path | None = None) -> OperationStateEnvelope:
    path = state_file_path(repository_id, operation_id, state_root=state_root)
    data = path.read_bytes()
    if len(data) > _MAX_STATE_BYTES:
        raise BootstrapApplyError("operation state exceeds size limit")
    try:
        return OperationStateEnvelope.model_validate_json(data)
    except Exception as exc:
        raise BootstrapApplyError("operation state is invalid or tampered") from exc


def next_generation(envelope: OperationStateEnvelope, **updates: object) -> OperationStateEnvelope:
    payload = envelope.payload.model_dump(mode="python")
    payload.update(updates)
    payload["generation"] = envelope.generation + 1
    return OperationStateEnvelope.create(**payload)


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


def status_from_state(envelope: OperationStateEnvelope) -> OperationStatus:
    phase_receipts = merge_phase_receipts({receipt.phase: receipt for receipt in envelope.phase_receipts})
    blockers = list(envelope.selection_plan.blockers)
    required_phases = set(envelope.required_phases)
    applied_phases = {receipt.phase for receipt in phase_receipts if receipt.state == "applied"}
    deployment_eligible = (
        required_phases == {"repository", "github", "azure", "evaluations"}
        and required_phases == applied_phases
    )
    selected_ids = {item.casefold() for item in envelope.selection_plan.selected_agent_ids}
    aligned_ids = {
        item.agent_id.casefold()
        for item in envelope.selection_plan.binding_assessments
        if item.classification == "bound-aligned"
    }
    for receipt in phase_receipts:
        if receipt.state != "applied":
            deployment_eligible = False
            blockers.append(f"phase:{receipt.phase}:{receipt.state}")
        if receipt.receipt.error_info is not None:
            blockers.append(f"error:{receipt.phase}:{receipt.receipt.error_info.code}")
            deployment_eligible = False
    if any(item.classification != "bound-aligned" for item in envelope.selection_plan.binding_assessments if item.agent_id.casefold() in selected_ids):
        deployment_eligible = False
    if selected_ids - aligned_ids:
        deployment_eligible = False
    lineage = EvaluatorLineage()
    if envelope.evaluator_replacement is not None:
        lineage = EvaluatorLineage(
            lineage_hash=envelope.evaluator_replacement.lineage_hash,
            active_bundle_id=envelope.evaluator_replacement.candidate_bundle_id if envelope.evaluator_replacement.status == "activated" else envelope.evaluator_replacement.active_bundle_id,
            preserved_bundle_id=envelope.evaluator_replacement.preserved_bundle_id,
        )
        if envelope.evaluator_replacement.status != "activated":
            deployment_eligible = False
    lineages = tuple(sorted((item.as_lineage() for item in envelope.evaluator_replacements), key=lambda item: item.repo_agent_id))
    if any(item.status != "activated" for item in envelope.evaluator_replacements):
        deployment_eligible = False
    return OperationStatus(
        operation_id=envelope.operation_id,
        repository_id=envelope.repository_id,
        runtime_repository=envelope.runtime_repository,
        runtime_commit=envelope.runtime_commit,
        plan_hash=envelope.bootstrap_plan.plan_hash,
        phase_states=phase_receipts,
        blockers=tuple(blockers),
        binding_assessments=envelope.selection_plan.binding_assessments,
        evaluator_lineage=lineage,
        deployment_eligible=deployment_eligible,
        evaluator_lineages=lineages,
    )
