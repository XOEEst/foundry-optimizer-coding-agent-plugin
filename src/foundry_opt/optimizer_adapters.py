from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
import subprocess
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal
from urllib.parse import urlsplit, urlunsplit

from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential
from pydantic import BaseModel, ConfigDict, Field, field_validator

from foundry_opt.poc.candidate import FinalizedCandidate
from foundry_opt.poc.foundry import (
    EvaluationContract,
    FoundryPocClient,
    HostedDefinition,
    ImageHostedDraftReference,
    PromptDefinition,
    PromptDraftReference,
    RouteDriftError,
    RouteFingerprint,
    _resolve_contract_evaluator_id,
    _validate_evaluator_contract,
)


TARGET_ADAPTER_ID: Final = "foundry-hosted"
TARGET_ADAPTER_VERSION: Final = "1.0.0"
TARGET_IMPLEMENTATION_ID: Final = "foundry-opt.foundry-hosted-image-definition"
TARGET_IMPLEMENTATION_VERSION: Final = "1.0.0"
PROMPT_TARGET_ADAPTER_ID: Final = "foundry-prompt"
PROMPT_TARGET_ADAPTER_VERSION: Final = "1.0.0"
PROMPT_TARGET_IMPLEMENTATION_ID: Final = "foundry-opt.foundry-prompt-definition"
PROMPT_TARGET_IMPLEMENTATION_VERSION: Final = "1.0.0"
EVALUATION_ADAPTER_ID: Final = "foundry-evaluation"
EVALUATION_ADAPTER_VERSION: Final = "1.0.0"
EVALUATION_IMPLEMENTATION_ID: Final = "foundry-opt.foundry-evaluation"
EVALUATION_IMPLEMENTATION_VERSION: Final = "1.0.0"

_SHA256_PATTERN: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
_TOOL_SCHEMAS_NAME: Final = "TOOL_SCHEMAS"


class OptimizerAdapterError(RuntimeError):
    """A frozen target or evaluation adapter contract could not be executed."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FoundryHostedTargetConfig(_FrozenModel):
    endpoint_allowlist: tuple[str, ...]
    project_endpoint: str
    agent_name: str = Field(min_length=1)
    baseline_version: str = Field(min_length=1)
    baseline_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    draft_mode: Literal["image_definition"]
    credential_provider: Literal["azure_cli"]
    instruction_file: str = Field(min_length=1)
    tool_schema_file: str = Field(min_length=1)
    tool_config_key: str = Field(default="tool_schemas", min_length=1)
    config_environment_variable: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    model: str = Field(min_length=1)
    request_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    operation_timeout_seconds: float = Field(default=300.0, gt=0, le=3600)
    poll_interval_seconds: float = Field(default=5.0, ge=0, le=60)

    @field_validator("project_endpoint")
    @classmethod
    def validate_endpoint(cls, value: str, info: Any) -> str:
        return _allowed_endpoint(value, info.data.get("endpoint_allowlist", ()))

    @field_validator("endpoint_allowlist")
    @classmethod
    def validate_allowlist(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("endpoint_allowlist must contain unique HTTPS origins")
        return tuple(_normalized_origin(item, require_origin=True) for item in value)


class FoundryPromptTargetConfig(_FrozenModel):
    endpoint_allowlist: tuple[str, ...]
    project_endpoint: str
    agent_name: str = Field(min_length=1)
    baseline_version: str = Field(min_length=1)
    baseline_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    credential_provider: Literal["azure_cli"]
    instruction_file: str = Field(min_length=1)
    model: str = Field(min_length=1)
    request_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    operation_timeout_seconds: float = Field(default=300.0, gt=0, le=3600)
    poll_interval_seconds: float = Field(default=5.0, ge=0, le=60)

    @field_validator("project_endpoint")
    @classmethod
    def validate_endpoint(cls, value: str, info: Any) -> str:
        return _allowed_endpoint(value, info.data.get("endpoint_allowlist", ()))

    @field_validator("endpoint_allowlist")
    @classmethod
    def validate_allowlist(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("endpoint_allowlist must contain unique HTTPS origins")
        return tuple(_normalized_origin(item, require_origin=True) for item in value)


class FoundryEvaluationConfig(_FrozenModel):
    endpoint_allowlist: tuple[str, ...]
    project_endpoint: str
    evaluation_id: str = Field(min_length=1)
    evaluator_references: tuple[str, ...]
    dataset_sources: dict[str, str]
    data_mapping: dict[str, object]
    credential_provider: Literal["azure_cli"]
    request_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    operation_timeout_seconds: float = Field(default=1800.0, gt=0, le=7200)
    poll_interval_seconds: float = Field(default=5.0, ge=0, le=60)

    @field_validator("endpoint_allowlist")
    @classmethod
    def validate_allowlist(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) != len(set(value)):
            raise ValueError("endpoint_allowlist must contain unique HTTPS origins")
        return tuple(_normalized_origin(item, require_origin=True) for item in value)

    @field_validator("project_endpoint")
    @classmethod
    def validate_endpoint(cls, value: str, info: Any) -> str:
        return _allowed_endpoint(value, info.data.get("endpoint_allowlist", ()))

    @field_validator("evaluator_references")
    @classmethod
    def validate_evaluators(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not item for item in value):
            raise ValueError("evaluator_references must be nonempty")
        if len(value) != len(set(value)):
            raise ValueError("evaluator_references must be unique")
        return value

    @field_validator("dataset_sources")
    @classmethod
    def validate_dataset_sources(
        cls,
        value: dict[str, str],
    ) -> dict[str, str]:
        if set(value) != {"search_pool", "validation"} or any(
            not item for item in value.values()
        ):
            raise ValueError(
                "dataset_sources must freeze search_pool and validation references"
            )
        return value

    @field_validator("data_mapping")
    @classmethod
    def validate_data_mapping(
        cls,
        value: dict[str, object],
    ) -> dict[str, object]:
        if not isinstance(value.get("query"), str) or not value["query"]:
            raise ValueError("data_mapping.query must be a nonempty string")
        if any(not isinstance(item, str) or not item for item in value.values()):
            raise ValueError("data_mapping values must be nonempty strings")
        return value


class TargetSnapshot(_FrozenModel):
    agent_name: str
    baseline_version: str
    definition_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    route_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    config_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    receipt_reference: str


class CandidateTargetHandle(_FrozenModel):
    candidate_id: str
    invoke_handle: str
    artifact_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    workspace_artifact_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    receipt_reference: str


class EvaluationTarget(_FrozenModel):
    subject_kind: Literal["baseline", "candidate"]
    subject_id: str
    agent_name: str
    agent_version: str


class TaskScore(_FrozenModel):
    task_id: str
    score: float
    passed: bool


class EvaluationProjection(_FrozenModel):
    subject_kind: Literal["baseline", "candidate"]
    subject_id: str
    split_role: Literal["search", "confirmation", "validation"]
    split_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    avgScore: float
    total: int
    passed: int
    failed: int
    complete: bool
    task_scores: tuple[TaskScore, ...] | None = None
    receipt_reference: str


@dataclass(frozen=True, slots=True)
class _BaselineState:
    definition: HostedDefinition
    route: RouteFingerprint
    config: dict[str, object]
    instruction: str
    tool_schemas: object
    tool_source_guard: str


@dataclass(frozen=True, slots=True)
class _PromptBaselineState:
    definition: PromptDefinition
    route: RouteFingerprint
    instruction: str


class FoundryHostedTargetAdapter:
    def __init__(
        self,
        config: Mapping[str, object],
        *,
        repository_root: Path,
        agent_root: Path,
        trusted_run_root: Path,
        run_id: str,
        contract_hash: str,
        client: FoundryPocClient | None = None,
        credential_factory: Callable[[], object] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = FoundryHostedTargetConfig.model_validate(config)
        self._repository_root = Path(repository_root).resolve(strict=True)
        self._agent_root = Path(agent_root).resolve(strict=True)
        self._trusted_run_root = Path(trusted_run_root).resolve(strict=False)
        self._run_id = run_id
        self._contract_hash = _require_sha256(contract_hash)
        self._monotonic = monotonic
        credential = None
        if client is None:
            credential = (credential_factory or _azure_cli_credential)()
        self._client = client or FoundryPocClient(
            self.config.project_endpoint,
            credential,
            timeout=self.config.request_timeout_seconds,
        )
        self._baseline: _BaselineState | None = None
        self._handles: dict[str, ImageHostedDraftReference] = {}
        self._target_receipts = self._trusted_run_root / "target-receipts"
        self._restricted = self._trusted_run_root / "restricted-evidence" / "targets"
        self._target_receipts.mkdir(parents=True, exist_ok=True)
        self._restricted.mkdir(parents=True, exist_ok=True)

    def inspect_target(self) -> TargetSnapshot:
        started = _now()
        deadline = self._deadline(self.config.operation_timeout_seconds)
        definition, _, _ = self._client.get_image_hosted_version(
            self.config.agent_name,
            self.config.baseline_version,
            deadline_monotonic=deadline,
        )
        route = self._client.route_fingerprint(
            self.config.agent_name,
            deadline_monotonic=deadline,
        )
        environment = definition.as_payload().get("environment_variables")
        if not isinstance(environment, Mapping):
            raise OptimizerAdapterError(
                "baseline hosted definition omitted environment_variables"
            )
        raw_config = environment.get(self.config.config_environment_variable)
        if not isinstance(raw_config, str):
            raise OptimizerAdapterError(
                "baseline hosted definition omitted the frozen config environment variable"
            )
        baseline_config = _strict_json_object(raw_config, "hosted target config")
        if baseline_config.get("model") != self.config.model:
            raise OptimizerAdapterError("baseline model differs from the frozen model")
        instruction_path = _scoped_file(
            self._repository_root,
            self._agent_root,
            self.config.instruction_file,
        )
        tool_path = _scoped_file(
            self._repository_root,
            self._agent_root,
            self.config.tool_schema_file,
        )
        instruction = _git_file_at_commit(
            self._repository_root,
            self.config.baseline_commit,
            instruction_path.relative_to(self._repository_root).as_posix(),
        )
        tool_source = _git_file_at_commit(
            self._repository_root,
            self.config.baseline_commit,
            tool_path.relative_to(self._repository_root).as_posix(),
        )
        tool_schemas, tool_source_guard = _load_tool_schemas_source(
            tool_source,
            subject=str(tool_path),
        )
        if baseline_config.get("instructions") != instruction:
            raise OptimizerAdapterError(
                "instruction_file does not exactly match the hosted baseline config"
            )
        if _json_plain(
            baseline_config.get(self.config.tool_config_key)
        ) != _json_plain(tool_schemas):
            raise OptimizerAdapterError(
                "tool_schema_file does not exactly match the hosted baseline config"
            )
        self._baseline = _BaselineState(
            definition=definition,
            route=route,
            config=baseline_config,
            instruction=instruction,
            tool_schemas=tool_schemas,
            tool_source_guard=tool_source_guard,
        )
        restricted_reference = self._persist_restricted(
            "baseline",
            {
                "agent_name": self.config.agent_name,
                "agent_version": self.config.baseline_version,
                "definition": definition.as_payload(),
                "route": _route_document(route),
                "config": baseline_config,
            },
        )
        receipt_reference = self._write_target_receipt(
            operation_type="inspect",
            started_at=started,
            artifact_hash=f"sha256:{definition.sha256}",
            restricted_provider_reference=restricted_reference,
        )
        return TargetSnapshot(
            agent_name=self.config.agent_name,
            baseline_version=self.config.baseline_version,
            definition_hash=f"sha256:{definition.sha256}",
            route_hash=f"sha256:{route.sha256}",
            config_hash=_sha256_json(baseline_config),
            receipt_reference=receipt_reference,
        )

    def baseline_target(self) -> EvaluationTarget:
        if self._baseline is None:
            self.inspect_target()
        return EvaluationTarget(
            subject_kind="baseline",
            subject_id="baseline",
            agent_name=self.config.agent_name,
            agent_version=self.config.baseline_version,
        )

    def materialize_candidate(
        self,
        candidate: FinalizedCandidate,
    ) -> CandidateTargetHandle:
        baseline = self._require_baseline()
        started = _now()
        instruction_relative = _repository_relative_config_path(
            self._repository_root,
            self._agent_root,
            self.config.instruction_file,
        )
        tool_relative = _repository_relative_config_path(
            self._repository_root,
            self._agent_root,
            self.config.tool_schema_file,
        )
        allowed_paths = {instruction_relative, tool_relative}
        unexpected = sorted(set(candidate.incremental_changed_paths) - allowed_paths)
        if unexpected:
            raise OptimizerAdapterError(
                "candidate changed paths outside instruction and tool descriptions: "
                + ", ".join(unexpected)
            )
        candidate_instruction = (
            candidate.workspace_path / Path(instruction_relative)
        ).read_text(encoding="utf-8")
        candidate_tools, source_guard = _load_tool_schemas(
            candidate.workspace_path / Path(tool_relative)
        )
        if source_guard != baseline.tool_source_guard:
            raise OptimizerAdapterError(
                "candidate changed executable code around TOOL_SCHEMAS"
            )
        _require_description_only_change(
            baseline.tool_schemas,
            candidate_tools,
        )
        candidate_config = deepcopy(baseline.config)
        candidate_config["instructions"] = candidate_instruction
        candidate_config[self.config.tool_config_key] = _json_plain(candidate_tools)
        if candidate_config.get("model") != self.config.model:
            raise OptimizerAdapterError("candidate attempted to change the frozen model")
        payload = deepcopy(baseline.definition.as_payload())
        environment = payload.get("environment_variables")
        if not isinstance(environment, Mapping):
            raise OptimizerAdapterError(
                "baseline hosted definition omitted environment_variables"
            )
        candidate_environment = dict(environment)
        candidate_environment[self.config.config_environment_variable] = (
            _canonical_json(candidate_config)
        )
        payload["environment_variables"] = candidate_environment
        definition = HostedDefinition.coerce(payload)
        workspace_hash = f"sha256:{candidate.hashes.source_tree_sha256}"
        ownership_token = uuid.uuid4().hex
        deadline = self._deadline(self.config.operation_timeout_seconds)
        current_route = self._client.route_fingerprint(
            self.config.agent_name,
            deadline_monotonic=deadline,
        )
        if current_route.sha256 != baseline.route.sha256:
            raise RouteDriftError(
                "Foundry route changed after the target baseline was frozen",
                expected=baseline.route,
                actual=current_route,
            )
        reference = self._client.create_image_hosted_draft(
            self.config.agent_name,
            definition,
            deadline_monotonic=deadline,
            ownership_token=ownership_token,
        )
        if reference.route.sha256 != baseline.route.sha256:
            self._client.delete_owned_image_hosted_version(
                reference,
                deadline_monotonic=deadline,
            )
            raise RouteDriftError(
                "Foundry route changed while the candidate draft was created",
                expected=baseline.route,
                actual=reference.route,
            )
        self._handles[candidate.candidate_id] = reference
        restricted_reference = self._persist_restricted(
            candidate.candidate_id,
            {
                "candidate_id": candidate.candidate_id,
                "workspace_artifact_hash": workspace_hash,
                "reference": _image_reference_document(reference),
            },
        )
        receipt_reference = self._write_target_receipt(
            operation_type="materialize",
            started_at=started,
            candidate_id=candidate.candidate_id,
            workspace_artifact_hash=workspace_hash,
            ownership_token=ownership_token,
            artifact_hash=f"sha256:{reference.definition_sha256}",
            restricted_provider_reference=restricted_reference,
        )
        return CandidateTargetHandle(
            candidate_id=candidate.candidate_id,
            invoke_handle=f"opaque:foundry-target:{candidate.candidate_id}",
            artifact_hash=f"sha256:{reference.definition_sha256}",
            workspace_artifact_hash=workspace_hash,
            receipt_reference=receipt_reference,
        )

    def verify_candidate(self, candidate_id: str) -> CandidateTargetHandle:
        started = _now()
        reference = self._reference(candidate_id)
        workspace_hash = self._workspace_hash(candidate_id)
        verified = self._client.verify_image_hosted_draft(
            reference,
            deadline_monotonic=self._deadline(
                self.config.operation_timeout_seconds
            ),
        )
        self._handles[candidate_id] = verified
        restricted_reference = self._persist_restricted(
            candidate_id,
            {
                "candidate_id": candidate_id,
                "reference": _image_reference_document(verified),
                "workspace_artifact_hash": workspace_hash,
            },
        )
        receipt_reference = self._write_target_receipt(
            operation_type="verify",
            started_at=started,
            candidate_id=candidate_id,
            workspace_artifact_hash=workspace_hash,
            ownership_token=verified.ownership_token,
            artifact_hash=f"sha256:{verified.definition_sha256}",
            restricted_provider_reference=restricted_reference,
        )
        return CandidateTargetHandle(
            candidate_id=candidate_id,
            invoke_handle=f"opaque:foundry-target:{candidate_id}",
            artifact_hash=f"sha256:{verified.definition_sha256}",
            workspace_artifact_hash=workspace_hash,
            receipt_reference=receipt_reference,
        )

    def candidate_target(self, candidate_id: str) -> EvaluationTarget:
        reference = self._reference(candidate_id)
        return EvaluationTarget(
            subject_kind="candidate",
            subject_id=candidate_id,
            agent_name=reference.agent_name,
            agent_version=reference.version,
        )

    def cleanup_candidate(self, candidate_id: str) -> str:
        started = _now()
        reference = self._reference(candidate_id)
        self._client.delete_owned_image_hosted_version(
            reference,
            deadline_monotonic=self._deadline(
                self.config.operation_timeout_seconds
            ),
        )
        restricted_reference = self._restricted_reference(candidate_id)
        receipt = self._write_target_receipt(
            operation_type="cleanup",
            started_at=started,
            candidate_id=candidate_id,
            ownership_token=reference.ownership_token,
            restricted_provider_reference=restricted_reference,
        )
        self._handles.pop(candidate_id, None)
        return receipt

    def _require_baseline(self) -> _BaselineState:
        if self._baseline is None:
            self.inspect_target()
        assert self._baseline is not None
        return self._baseline

    def _reference(self, candidate_id: str) -> ImageHostedDraftReference:
        reference = self._handles.get(candidate_id)
        if reference is not None:
            return reference
        path = self._restricted_path(candidate_id)
        if not path.is_file():
            raise OptimizerAdapterError(
                f"candidate target {candidate_id!r} is not materialized"
            )
        document = _read_json_object(path)
        raw_reference = document.get("reference")
        if not isinstance(raw_reference, Mapping):
            raise OptimizerAdapterError("restricted target reference is invalid")
        reference = _image_reference_from_document(raw_reference)
        self._handles[candidate_id] = reference
        return reference

    def _workspace_hash(self, candidate_id: str) -> str:
        document = _read_json_object(self._restricted_path(candidate_id))
        value = document.get("workspace_artifact_hash")
        if not isinstance(value, str):
            raise OptimizerAdapterError(
                "restricted target reference omitted workspace artifact hash"
            )
        return _require_sha256(value)

    def _restricted_path(self, name: str) -> Path:
        return self._restricted / f"{name}.json"

    def _restricted_reference(self, name: str) -> str:
        return f"restricted:targets/{name}.json"

    def _persist_restricted(self, name: str, value: Mapping[str, object]) -> str:
        _atomic_json(self._restricted_path(name), value)
        return self._restricted_reference(name)

    def _write_target_receipt(
        self,
        *,
        operation_type: str,
        started_at: str,
        candidate_id: str | None = None,
        workspace_artifact_hash: str | None = None,
        ownership_token: str | None = None,
        artifact_hash: str | None = None,
        restricted_provider_reference: str | None = None,
    ) -> str:
        operation_id = uuid.uuid4().hex
        receipt = {
            "schema_version": 1,
            "receipt_id": operation_id,
            "run_id": self._run_id,
            "operation_id": operation_id,
            "operation_type": operation_type,
            "adapter_id": TARGET_ADAPTER_ID,
            "adapter_version": TARGET_ADAPTER_VERSION,
            "input_contract_hash": self._contract_hash,
            "candidate_id": candidate_id,
            "workspace_artifact_hash": workspace_artifact_hash,
            "ownership_token": ownership_token,
            "artifact_hash": artifact_hash,
            "restricted_provider_reference": restricted_provider_reference,
            "approval_reference": None,
            "status": "complete",
            "started_at": started_at,
            "completed_at": _now(),
            "error": None,
        }
        path = self._target_receipts / f"{operation_id}.json"
        _atomic_json(path, receipt)
        return f"restricted:target-receipts/{path.name}"

    def _deadline(self, seconds: float) -> float:
        return self._monotonic() + float(seconds)


class FoundryPromptTargetAdapter:
    def __init__(
        self,
        config: Mapping[str, object],
        *,
        repository_root: Path,
        agent_root: Path,
        trusted_run_root: Path,
        run_id: str,
        contract_hash: str,
        client: FoundryPocClient | None = None,
        credential_factory: Callable[[], object] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = FoundryPromptTargetConfig.model_validate(config)
        self._repository_root = Path(repository_root).resolve(strict=True)
        self._agent_root = Path(agent_root).resolve(strict=True)
        self._trusted_run_root = Path(trusted_run_root).resolve(strict=False)
        self._run_id = run_id
        self._contract_hash = _require_sha256(contract_hash)
        self._monotonic = monotonic
        credential = None
        if client is None:
            credential = (credential_factory or _azure_cli_credential)()
        self._client = client or FoundryPocClient(
            self.config.project_endpoint,
            credential,
            timeout=self.config.request_timeout_seconds,
        )
        self._baseline: _PromptBaselineState | None = None
        self._handles: dict[str, PromptDraftReference] = {}
        self._target_receipts = self._trusted_run_root / "target-receipts"
        self._restricted = self._trusted_run_root / "restricted-evidence" / "targets"
        self._target_receipts.mkdir(parents=True, exist_ok=True)
        self._restricted.mkdir(parents=True, exist_ok=True)

    def inspect_target(self) -> TargetSnapshot:
        started = _now()
        deadline = self._deadline(self.config.operation_timeout_seconds)
        definition, _, _ = self._client.get_prompt_version(
            self.config.agent_name,
            self.config.baseline_version,
            deadline_monotonic=deadline,
        )
        route = self._client.route_fingerprint(
            self.config.agent_name,
            deadline_monotonic=deadline,
        )
        if definition.model != self.config.model:
            raise OptimizerAdapterError("baseline model differs from the frozen model")
        instruction_path = _scoped_file(
            self._repository_root,
            self._agent_root,
            self.config.instruction_file,
        )
        instruction = _git_file_at_commit(
            self._repository_root,
            self.config.baseline_commit,
            instruction_path.relative_to(self._repository_root).as_posix(),
        )
        if definition.instructions != instruction:
            raise OptimizerAdapterError(
                "instruction_file does not exactly match the prompt baseline definition"
            )
        self._baseline = _PromptBaselineState(
            definition=definition,
            route=route,
            instruction=instruction,
        )
        restricted_reference = self._persist_restricted(
            "baseline",
            {
                "agent_name": self.config.agent_name,
                "agent_version": self.config.baseline_version,
                "definition": definition.as_payload(),
                "route": _route_document(route),
            },
        )
        receipt_reference = self._write_target_receipt(
            operation_type="inspect",
            started_at=started,
            artifact_hash=f"sha256:{definition.sha256}",
            restricted_provider_reference=restricted_reference,
        )
        return TargetSnapshot(
            agent_name=self.config.agent_name,
            baseline_version=self.config.baseline_version,
            definition_hash=f"sha256:{definition.sha256}",
            route_hash=f"sha256:{route.sha256}",
            config_hash=_sha256_json(definition.as_payload()),
            receipt_reference=receipt_reference,
        )

    def baseline_target(self) -> EvaluationTarget:
        if self._baseline is None:
            self.inspect_target()
        return EvaluationTarget(
            subject_kind="baseline",
            subject_id="baseline",
            agent_name=self.config.agent_name,
            agent_version=self.config.baseline_version,
        )

    def materialize_candidate(
        self,
        candidate: FinalizedCandidate,
    ) -> CandidateTargetHandle:
        baseline = self._require_baseline()
        started = _now()
        instruction_relative = _repository_relative_config_path(
            self._repository_root,
            self._agent_root,
            self.config.instruction_file,
        )
        expected_paths = {instruction_relative}
        if (
            set(candidate.incremental_changed_paths) != expected_paths
            or set(candidate.changed_paths) != expected_paths
        ):
            raise OptimizerAdapterError(
                "candidate changed path must be exactly the configured instruction_file"
            )
        candidate_instruction = (
            candidate.workspace_path / Path(instruction_relative)
        ).read_text(encoding="utf-8")
        definition = PromptDefinition(
            model=baseline.definition.model,
            instructions=candidate_instruction,
            payload=deepcopy(baseline.definition.payload),
        )
        if definition.model != self.config.model:
            raise OptimizerAdapterError("candidate attempted to change the frozen model")
        if _json_plain(definition.payload) != _json_plain(
            baseline.definition.payload
        ):
            raise OptimizerAdapterError(
                "candidate attempted to change the frozen prompt payload"
            )
        workspace_hash = f"sha256:{candidate.hashes.source_tree_sha256}"
        ownership_token = uuid.uuid4().hex
        deadline = self._deadline(self.config.operation_timeout_seconds)
        current_route = self._client.route_fingerprint(
            self.config.agent_name,
            deadline_monotonic=deadline,
        )
        if current_route.sha256 != baseline.route.sha256:
            raise RouteDriftError(
                "Foundry route changed after the target baseline was frozen",
                expected=baseline.route,
                actual=current_route,
            )
        reference = self._client.create_prompt_draft(
            self.config.agent_name,
            definition,
            deadline_monotonic=deadline,
            ownership_token=ownership_token,
        )
        if reference.route.sha256 != baseline.route.sha256:
            self._client.delete_owned_prompt_version(
                reference,
                deadline_monotonic=deadline,
            )
            raise RouteDriftError(
                "Foundry route changed while the candidate draft was created",
                expected=baseline.route,
                actual=reference.route,
            )
        self._handles[candidate.candidate_id] = reference
        restricted_reference = self._persist_restricted(
            candidate.candidate_id,
            {
                "candidate_id": candidate.candidate_id,
                "workspace_artifact_hash": workspace_hash,
                "reference": _prompt_reference_document(reference),
            },
        )
        receipt_reference = self._write_target_receipt(
            operation_type="materialize",
            started_at=started,
            candidate_id=candidate.candidate_id,
            workspace_artifact_hash=workspace_hash,
            ownership_token=ownership_token,
            artifact_hash=f"sha256:{reference.definition_sha256}",
            restricted_provider_reference=restricted_reference,
        )
        return CandidateTargetHandle(
            candidate_id=candidate.candidate_id,
            invoke_handle=f"opaque:foundry-target:{candidate.candidate_id}",
            artifact_hash=f"sha256:{reference.definition_sha256}",
            workspace_artifact_hash=workspace_hash,
            receipt_reference=receipt_reference,
        )

    def verify_candidate(self, candidate_id: str) -> CandidateTargetHandle:
        started = _now()
        reference = self._reference(candidate_id)
        workspace_hash = self._workspace_hash(candidate_id)
        verified = self._client.verify_prompt_draft(
            reference,
            deadline_monotonic=self._deadline(
                self.config.operation_timeout_seconds
            ),
        )
        if verified.definition_sha256 != reference.definition_sha256:
            raise OptimizerAdapterError(
                "verified prompt draft differs from the materialized definition"
            )
        self._handles[candidate_id] = verified
        restricted_reference = self._persist_restricted(
            candidate_id,
            {
                "candidate_id": candidate_id,
                "reference": _prompt_reference_document(verified),
                "workspace_artifact_hash": workspace_hash,
            },
        )
        receipt_reference = self._write_target_receipt(
            operation_type="verify",
            started_at=started,
            candidate_id=candidate_id,
            workspace_artifact_hash=workspace_hash,
            ownership_token=verified.ownership_token,
            artifact_hash=f"sha256:{verified.definition_sha256}",
            restricted_provider_reference=restricted_reference,
        )
        return CandidateTargetHandle(
            candidate_id=candidate_id,
            invoke_handle=f"opaque:foundry-target:{candidate_id}",
            artifact_hash=f"sha256:{verified.definition_sha256}",
            workspace_artifact_hash=workspace_hash,
            receipt_reference=receipt_reference,
        )

    def candidate_target(self, candidate_id: str) -> EvaluationTarget:
        reference = self._reference(candidate_id)
        return EvaluationTarget(
            subject_kind="candidate",
            subject_id=candidate_id,
            agent_name=reference.agent_name,
            agent_version=reference.version,
        )

    def cleanup_candidate(self, candidate_id: str) -> str:
        started = _now()
        reference = self._reference(candidate_id)
        self._client.delete_owned_prompt_version(
            reference,
            deadline_monotonic=self._deadline(
                self.config.operation_timeout_seconds
            ),
        )
        receipt = self._write_target_receipt(
            operation_type="cleanup",
            started_at=started,
            candidate_id=candidate_id,
            ownership_token=reference.ownership_token,
            restricted_provider_reference=self._restricted_reference(candidate_id),
        )
        self._handles.pop(candidate_id, None)
        return receipt

    def _require_baseline(self) -> _PromptBaselineState:
        if self._baseline is None:
            self.inspect_target()
        assert self._baseline is not None
        return self._baseline

    def _reference(self, candidate_id: str) -> PromptDraftReference:
        reference = self._handles.get(candidate_id)
        if reference is not None:
            return reference
        path = self._restricted_path(candidate_id)
        if not path.is_file():
            raise OptimizerAdapterError(
                f"candidate target {candidate_id!r} is not materialized"
            )
        document = _read_json_object(path)
        raw_reference = document.get("reference")
        if not isinstance(raw_reference, Mapping):
            raise OptimizerAdapterError("restricted target reference is invalid")
        reference = _prompt_reference_from_document(raw_reference)
        self._handles[candidate_id] = reference
        return reference

    def _workspace_hash(self, candidate_id: str) -> str:
        document = _read_json_object(self._restricted_path(candidate_id))
        value = document.get("workspace_artifact_hash")
        if not isinstance(value, str):
            raise OptimizerAdapterError(
                "restricted target reference omitted workspace artifact hash"
            )
        return _require_sha256(value)

    def _restricted_path(self, name: str) -> Path:
        return self._restricted / f"{name}.json"

    def _restricted_reference(self, name: str) -> str:
        return f"restricted:targets/{name}.json"

    def _persist_restricted(self, name: str, value: Mapping[str, object]) -> str:
        _atomic_json(self._restricted_path(name), value)
        return self._restricted_reference(name)

    def _write_target_receipt(
        self,
        *,
        operation_type: str,
        started_at: str,
        candidate_id: str | None = None,
        workspace_artifact_hash: str | None = None,
        ownership_token: str | None = None,
        artifact_hash: str | None = None,
        restricted_provider_reference: str | None = None,
    ) -> str:
        operation_id = uuid.uuid4().hex
        receipt = {
            "schema_version": 1,
            "receipt_id": operation_id,
            "run_id": self._run_id,
            "operation_id": operation_id,
            "operation_type": operation_type,
            "adapter_id": PROMPT_TARGET_ADAPTER_ID,
            "adapter_version": PROMPT_TARGET_ADAPTER_VERSION,
            "input_contract_hash": self._contract_hash,
            "candidate_id": candidate_id,
            "workspace_artifact_hash": workspace_artifact_hash,
            "ownership_token": ownership_token,
            "artifact_hash": artifact_hash,
            "restricted_provider_reference": restricted_provider_reference,
            "approval_reference": None,
            "status": "complete",
            "started_at": started_at,
            "completed_at": _now(),
            "error": None,
        }
        path = self._target_receipts / f"{operation_id}.json"
        _atomic_json(path, receipt)
        return f"restricted:target-receipts/{path.name}"

    def _deadline(self, seconds: float) -> float:
        return self._monotonic() + float(seconds)


class FoundryEvaluationAdapter:
    def __init__(
        self,
        config: Mapping[str, object],
        *,
        trusted_run_root: Path,
        run_id: str,
        contract_hash: str,
        objective: Mapping[str, object],
        openai_client: object | None = None,
        credential_factory: Callable[[], object] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = FoundryEvaluationConfig.model_validate(config)
        self._trusted_run_root = Path(trusted_run_root).resolve(strict=False)
        self._run_id = run_id
        self._contract_hash = _require_sha256(contract_hash)
        self._objective = _freeze_objective(
            objective,
            self.config.evaluator_references,
        )
        if openai_client is None:
            credential = (credential_factory or _azure_cli_credential)()
            project = AIProjectClient(
                endpoint=self.config.project_endpoint,
                credential=credential,
            )
            openai_client = project.get_openai_client()
        self._openai = openai_client
        self._monotonic = monotonic
        self._sleep = sleep
        self._receipts = self._trusted_run_root / "evaluation-receipts"
        self._restricted = (
            self._trusted_run_root / "restricted-evidence" / "evaluations"
        )
        self._receipts.mkdir(parents=True, exist_ok=True)
        self._restricted.mkdir(parents=True, exist_ok=True)

    def evaluate(
        self,
        target: EvaluationTarget,
        *,
        split_role: Literal["search", "confirmation", "validation"],
        split_fingerprint: str,
        opaque_role_path: Path,
        expected_count: int,
    ) -> EvaluationProjection:
        if split_role not in {"search", "confirmation", "validation"}:
            raise OptimizerAdapterError("unsupported evaluation split role")
        if expected_count <= 0:
            raise OptimizerAdapterError("expected_count must be positive")
        fingerprint = _require_sha256(split_fingerprint)
        rows = _read_jsonl(opaque_role_path)
        if len(rows) != expected_count:
            raise OptimizerAdapterError(
                "opaque role row count does not match the frozen denominator"
            )
        evals = getattr(self._openai, "evals", None)
        runs = getattr(evals, "runs", None)
        if evals is None or runs is None:
            raise OptimizerAdapterError("Foundry OpenAI evals client is unavailable")
        definition = evals.retrieve(self.config.evaluation_id)
        criterion_aliases = _validate_evaluator_contract(
            definition,
            EvaluationContract(
                evaluation_id=self.config.evaluation_id,
                dataset_id="inline-jsonl",
                evaluator_ids=self.config.evaluator_references,
            ),
        )
        started = _now()
        operation_id = uuid.uuid4().hex
        created = runs.create(
            self.config.evaluation_id,
            name=f"{self._run_id}-{target.subject_id}-{split_role}",
            metadata={
                "foundry_opt_run_id": self._run_id,
                "foundry_opt_subject_kind": target.subject_kind,
                "foundry_opt_subject_id": target.subject_id,
                "foundry_opt_split_role": split_role,
                "foundry_opt_split_fingerprint": fingerprint,
            },
            data_source={
                "type": "azure_ai_target_completions",
                "source": {
                    "type": "file_content",
                    "content": [{"item": row} for row in rows],
                },
                "input_messages": {
                    "type": "template",
                    "template": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": {
                                "type": "input_text",
                                "text": self.config.data_mapping.get(
                                    "query",
                                    "{{item.query}}",
                                ),
                            },
                        }
                    ],
                },
                "target": {
                    "type": "azure_ai_agent",
                    "name": target.agent_name,
                    "version": target.agent_version,
                },
            },
        )
        run_id = _required_field(created, "id")
        deadline = self._monotonic() + self.config.operation_timeout_seconds
        current: object
        while True:
            current = runs.retrieve(run_id, eval_id=self.config.evaluation_id)
            status = _required_field(current, "status").lower()
            if status == "completed":
                break
            if status in {"failed", "canceled", "cancelled"}:
                raise OptimizerAdapterError(
                    f"Foundry evaluation ended with terminal status {status!r}"
                )
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise OptimizerAdapterError("Foundry evaluation deadline exhausted")
            self._sleep(min(self.config.poll_interval_seconds, remaining))
        output_api = getattr(runs, "output_items", None)
        if output_api is None:
            raise OptimizerAdapterError(
                "Foundry evaluation output-items API is unavailable"
            )
        while True:
            page = output_api.list(
                run_id,
                eval_id=self.config.evaluation_id,
                limit=100,
            )
            output_items = _all_output_items(page)
            if len(output_items) >= expected_count:
                break
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                raise OptimizerAdapterError(
                    "Foundry evaluation output items did not reach the frozen "
                    "denominator before the deadline"
                )
            self._sleep(min(self.config.poll_interval_seconds, remaining))
        task_results = _canonical_task_results(
            output_items,
            expected_count=expected_count,
            evaluator_references=self.config.evaluator_references,
            criterion_aliases=criterion_aliases,
            objective=self._objective,
        )
        avg_score = sum(item["score"] for item in task_results) / expected_count
        evidence_name = f"{operation_id}.json"
        evidence_path = self._restricted / evidence_name
        report_url = _optional_field(current, "report_url")
        _atomic_json(
            evidence_path,
            {
                "schema_version": 1,
                "evaluation_id": self.config.evaluation_id,
                "run_id": run_id,
                "provider_report_url": report_url,
                "subject": target.model_dump(mode="json"),
                "split_role": split_role,
                "split_fingerprint": fingerprint,
                "rows_sha256": _sha256_json(rows),
                "task_results": task_results,
                "canonical_avgScore": avg_score,
            },
        )
        evaluator_hash = _sha256_json(
            {
                "evaluation_id": self.config.evaluation_id,
                "evaluators": self.config.evaluator_references,
                "objective": self._objective,
            }
        )
        receipt = {
            "schema_version": 1,
            "receipt_id": operation_id,
            "run_id": self._run_id,
            "operation_id": operation_id,
            "operation_type": "evaluate",
            "adapter_id": EVALUATION_ADAPTER_ID,
            "adapter_version": EVALUATION_ADAPTER_VERSION,
            "input_contract_hash": self._contract_hash,
            "subject_kind": target.subject_kind,
            "subject_id": target.subject_id,
            "split_role": split_role,
            "split_fingerprint": fingerprint,
            "evaluator_contract_hash": evaluator_hash,
            "expected_count": expected_count,
            "received_count": len(task_results),
            "status": "complete",
            "task_results_artifact": (
                f"restricted:evaluations/{evidence_name}"
            ),
            "provider_report_reference": (
                f"restricted:foundry-evaluation-runs/{operation_id}"
            ),
            "ownership_token": operation_id,
            "started_at": started,
            "completed_at": _now(),
            "error": None,
        }
        receipt_path = self._receipts / f"{operation_id}.json"
        _atomic_json(receipt_path, receipt)
        public_tasks = None
        if split_role == "search":
            public_tasks = tuple(
                TaskScore(
                    task_id=str(item["task_id"]),
                    score=float(item["score"]),
                    passed=bool(item["passed"]),
                )
                for item in task_results
            )
        return EvaluationProjection(
            subject_kind=target.subject_kind,
            subject_id=target.subject_id,
            split_role=split_role,
            split_fingerprint=fingerprint,
            avgScore=avg_score,
            total=expected_count,
            passed=sum(bool(item["passed"]) for item in task_results),
            failed=sum(not bool(item["passed"]) for item in task_results),
            complete=True,
            task_scores=public_tasks,
            receipt_reference=f"restricted:evaluation-receipts/{receipt_path.name}",
        )


def _canonical_task_results(
    output_items: Sequence[object],
    *,
    expected_count: int,
    evaluator_references: tuple[str, ...],
    criterion_aliases: Mapping[str, object],
    objective: tuple[dict[str, object], ...],
) -> list[dict[str, object]]:
    if len(output_items) != expected_count:
        raise OptimizerAdapterError(
            "evaluation output items did not match the frozen denominator"
        )
    task_results: list[dict[str, object]] = []
    task_ids: set[str] = set()
    objective_by_reference = {
        str(item["reference"]): item for item in objective
    }
    for index, output in enumerate(output_items):
        raw_task_id = _field(output, "datasource_item_id")
        task_id = str(index if raw_task_id is None else raw_task_id)
        if task_id in task_ids:
            raise OptimizerAdapterError("evaluation returned duplicate task results")
        task_ids.add(task_id)
        results = _field(output, "results")
        if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
            raise OptimizerAdapterError("evaluation output item omitted evaluator results")
        scores: dict[str, float] = {}
        passes: dict[str, bool] = {}
        for result in results:
            binding = _resolve_contract_evaluator_id(
                result,
                criterion_aliases=criterion_aliases,
            )
            reference = binding.contract_id
            if reference in scores:
                raise OptimizerAdapterError(
                    "evaluation task contained a duplicate evaluator result"
                )
            score = _field(result, "score")
            if (
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(float(score))
            ):
                raise OptimizerAdapterError(
                    "evaluation task contained an unscorable evaluator result"
                )
            scores[reference] = float(score)
            passed = _field(result, "passed")
            if not isinstance(passed, bool):
                raise OptimizerAdapterError(
                    "evaluation task result omitted its pass status"
                )
            passes[reference] = passed
        if set(scores) != set(evaluator_references):
            raise OptimizerAdapterError(
                "evaluation task did not contain the exact evaluator contract"
            )
        task_score = 0.0
        for reference in evaluator_references:
            item = objective_by_reference[reference]
            normalization = item["normalization"]
            assert isinstance(normalization, Mapping)
            minimum = float(normalization["minimum"])
            maximum = float(normalization["maximum"])
            normalized = (scores[reference] - minimum) / (maximum - minimum)
            task_score += float(item["weight"]) * normalized
        task_results.append(
            {
                "task_id": task_id,
                "score": task_score,
                "passed": all(passes.values()),
                "evaluator_scores": scores,
            }
        )
    return task_results


def _freeze_objective(
    objective: Mapping[str, object],
    evaluator_references: tuple[str, ...],
) -> tuple[dict[str, object], ...]:
    raw_evaluators = objective.get("evaluators")
    if not isinstance(raw_evaluators, Sequence):
        raise OptimizerAdapterError("objective omitted evaluators")
    frozen: list[dict[str, object]] = []
    for raw in raw_evaluators:
        if not isinstance(raw, Mapping):
            raise OptimizerAdapterError("objective evaluator must be an object")
        reference = raw.get("reference")
        weight = raw.get("weight")
        normalization = raw.get("normalization")
        if reference not in evaluator_references:
            raise OptimizerAdapterError(
                "objective evaluator differs from the adapter contract"
            )
        if (
            isinstance(weight, bool)
            or not isinstance(weight, (int, float))
            or float(weight) <= 0
        ):
            raise OptimizerAdapterError("objective evaluator weight must be positive")
        if not isinstance(normalization, Mapping):
            raise OptimizerAdapterError("objective evaluator omitted normalization")
        minimum = normalization.get("minimum")
        maximum = normalization.get("maximum")
        if (
            isinstance(minimum, bool)
            or isinstance(maximum, bool)
            or not isinstance(minimum, (int, float))
            or not isinstance(maximum, (int, float))
            or float(minimum) >= float(maximum)
        ):
            raise OptimizerAdapterError("objective normalization range is invalid")
        frozen.append(
            {
                "reference": reference,
                "weight": float(weight),
                "normalization": {
                    "type": "linear",
                    "minimum": float(minimum),
                    "maximum": float(maximum),
                },
            }
        )
    if {str(item["reference"]) for item in frozen} != set(evaluator_references):
        raise OptimizerAdapterError(
            "objective does not contain the exact evaluator contract"
        )
    total_weight = sum(float(item["weight"]) for item in frozen)
    return tuple(
        {**item, "weight": float(item["weight"]) / total_weight}
        for item in frozen
    )


def _load_tool_schemas(path: Path) -> tuple[object, str]:
    return _load_tool_schemas_source(
        path.read_text(encoding="utf-8"),
        subject=str(path),
    )


def _load_tool_schemas_source(
    source: str,
    *,
    subject: str,
) -> tuple[object, str]:
    try:
        module = ast.parse(source, filename=subject)
    except SyntaxError as error:
        raise OptimizerAdapterError("tool_schema_file is not valid Python") from error
    assignments: list[ast.AST] = []
    value_node: ast.AST | None = None
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == _TOOL_SCHEMAS_NAME
            for target in node.targets
        ):
            assignments.append(node)
            value_node = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == _TOOL_SCHEMAS_NAME
        ):
            assignments.append(node)
            value_node = node.value
    if len(assignments) != 1 or value_node is None:
        raise OptimizerAdapterError(
            "tool_schema_file must define exactly one literal TOOL_SCHEMAS"
        )
    try:
        value = ast.literal_eval(value_node)
    except (ValueError, TypeError, SyntaxError) as error:
        raise OptimizerAdapterError(
            "TOOL_SCHEMAS must be a Python literal"
        ) from error
    _json_plain(value)
    guard_module = deepcopy(module)
    for node in guard_module.body:
        if node.lineno == assignments[0].lineno:
            if isinstance(node, ast.Assign):
                node.value = ast.Constant(value="<frozen-tool-schemas>")
            elif isinstance(node, ast.AnnAssign):
                node.value = ast.Constant(value="<frozen-tool-schemas>")
    guard = ast.dump(guard_module, include_attributes=False)
    return value, hashlib.sha256(guard.encode("utf-8")).hexdigest()


def _git_file_at_commit(
    repository_root: Path,
    commit: str,
    relative_path: str,
) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository_root), "show", f"{commit}:{relative_path}"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise OptimizerAdapterError(
            f"baseline file {relative_path!r} is unavailable at the frozen commit"
        )
    return completed.stdout


def _require_description_only_change(baseline: object, candidate: object) -> None:
    if isinstance(baseline, Mapping):
        if not isinstance(candidate, Mapping) or set(candidate) != set(baseline):
            raise OptimizerAdapterError(
                "candidate changed frozen tool schema structure"
            )
        for key in baseline:
            if key == "description":
                if not isinstance(baseline[key], str) or not isinstance(
                    candidate[key], str
                ):
                    raise OptimizerAdapterError(
                        "tool descriptions must remain strings"
                    )
                continue
            _require_description_only_change(baseline[key], candidate[key])
        return
    if isinstance(baseline, (list, tuple)):
        if not isinstance(candidate, (list, tuple)) or len(candidate) != len(
            baseline
        ):
            raise OptimizerAdapterError(
                "candidate changed frozen tool schema structure"
            )
        for old, new in zip(baseline, candidate, strict=True):
            _require_description_only_change(old, new)
        return
    if baseline != candidate:
        raise OptimizerAdapterError(
            "candidate changed frozen tool parameters or handlers"
        )


def _all_output_items(first_page: object) -> list[object]:
    if hasattr(first_page, "data"):
        items: list[object] = []
        page = first_page
        while True:
            data = getattr(page, "data", None)
            if not isinstance(data, Sequence):
                raise OptimizerAdapterError(
                    "evaluation output-items page omitted data"
                )
            items.extend(data)
            has_next = getattr(page, "has_next_page", None)
            if not callable(has_next) or not has_next():
                return items
            get_next = getattr(page, "get_next_page", None)
            if not callable(get_next):
                raise OptimizerAdapterError(
                    "evaluation output-items pagination is incomplete"
                )
            page = get_next()
    try:
        return list(first_page)  # type: ignore[arg-type]
    except TypeError as error:
        raise OptimizerAdapterError(
            "evaluation output-items response is not iterable"
        ) from error


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    resolved = Path(path).resolve(strict=True)
    rows: list[dict[str, object]] = []
    for line_number, line in enumerate(
        resolved.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise OptimizerAdapterError(
                f"opaque role JSONL line {line_number} is invalid"
            ) from error
        if not isinstance(value, dict):
            raise OptimizerAdapterError(
                f"opaque role JSONL line {line_number} must be an object"
            )
        rows.append(value)
    return rows


def _allowed_endpoint(value: str, allowlist: Sequence[str]) -> str:
    endpoint = _normalized_endpoint(value)
    origin = _normalized_origin(endpoint)
    normalized_allowlist = {
        _normalized_origin(item, require_origin=True) for item in allowlist
    }
    if origin not in normalized_allowlist:
        raise ValueError("project_endpoint origin is not in endpoint_allowlist")
    return endpoint


def _normalized_endpoint(value: str) -> str:
    parsed = urlsplit(value.rstrip("/"))
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("project_endpoint must be an HTTPS URL")
    return urlunsplit(("https", _normalized_netloc(parsed), parsed.path, "", ""))


def _normalized_origin(value: str, *, require_origin: bool = False) -> str:
    parsed = urlsplit(value.rstrip("/"))
    if (
        parsed.scheme.lower() != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or (require_origin and parsed.path not in {"", "/"})
    ):
        raise ValueError("endpoint allowlist entries must be HTTPS origins")
    return urlunsplit(("https", _normalized_netloc(parsed), "", "", ""))


def _normalized_netloc(parsed: Any) -> str:
    hostname = parsed.hostname
    if not isinstance(hostname, str) or not hostname:
        raise ValueError("endpoint must contain a hostname")
    hostname = hostname.rstrip(".").lower()
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("endpoint port is invalid") from error
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    if port is None or port == 443:
        return rendered_host
    return f"{rendered_host}:{port}"


def _scoped_file(repository: Path, agent_root: Path, value: str) -> Path:
    relative = _repository_relative_config_path(repository, agent_root, value)
    path = (repository / Path(relative)).resolve(strict=True)
    if not path.is_file():
        raise OptimizerAdapterError(f"configured file is not a file: {value}")
    return path


def _repository_relative_config_path(
    repository: Path,
    agent_root: Path,
    value: str,
) -> str:
    configured = Path(value)
    path = configured.resolve(strict=False) if configured.is_absolute() else (
        repository / configured
    ).resolve(strict=False)
    if path != agent_root and not path.is_relative_to(agent_root):
        raise OptimizerAdapterError(
            "configured target files must remain within agent_root"
        )
    return path.relative_to(repository).as_posix()


def _strict_json_object(value: str, subject: str) -> dict[str, object]:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = item
        return result

    try:
        parsed = json.loads(
            value,
            object_pairs_hook=reject_duplicates,
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
        )
    except (json.JSONDecodeError, ValueError) as error:
        raise OptimizerAdapterError(f"{subject} is not strict JSON") from error
    if not isinstance(parsed, dict):
        raise OptimizerAdapterError(f"{subject} must be a JSON object")
    return parsed


def _json_plain(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise OptimizerAdapterError("configuration contains a non-finite number")
        return value
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise OptimizerAdapterError(
                    "configuration object keys must be strings"
                )
            result[key] = _json_plain(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_json_plain(item) for item in value]
    raise OptimizerAdapterError("configuration is not JSON-compatible")


def _canonical_json(value: object) -> str:
    return json.dumps(
        _json_plain(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def _sha256_json(value: object) -> str:
    return f"sha256:{hashlib.sha256(_canonical_json(value).encode('utf-8')).hexdigest()}"


def _require_sha256(value: str) -> str:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise OptimizerAdapterError("expected a sha256: digest")
    return value


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    serialized = json.dumps(
        value,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    ) + "\n"
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(serialized, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OptimizerAdapterError("restricted adapter state is unreadable") from error
    if not isinstance(value, dict):
        raise OptimizerAdapterError("restricted adapter state must be an object")
    return value


def _route_document(route: RouteFingerprint) -> dict[str, object]:
    return {
        "agent_name": route.agent_name,
        "latest_version": route.latest_version,
        "selector": _json_plain(route.selector),
        "endpoint_configuration": _json_plain(route.endpoint_configuration),
        "sha256": route.sha256,
    }


def _image_reference_document(
    reference: ImageHostedDraftReference,
) -> dict[str, object]:
    return {
        "agent_name": reference.agent_name,
        "version": reference.version,
        "ownership_token": reference.ownership_token,
        "definition_sha256": reference.definition_sha256,
        "route": _route_document(reference.route),
        "definition": reference.definition.as_payload(),
        "service_id": reference.service_id,
        "status": reference.status,
    }


def _image_reference_from_document(
    value: Mapping[str, object],
) -> ImageHostedDraftReference:
    route_value = value.get("route")
    definition_value = value.get("definition")
    if not isinstance(route_value, Mapping) or not isinstance(
        definition_value, Mapping
    ):
        raise OptimizerAdapterError("restricted target reference is incomplete")
    return ImageHostedDraftReference(
        agent_name=str(value["agent_name"]),
        version=str(value["version"]),
        ownership_token=str(value["ownership_token"]),
        definition_sha256=str(value["definition_sha256"]),
        route=RouteFingerprint(
            agent_name=str(route_value["agent_name"]),
            latest_version=(
                None
                if route_value.get("latest_version") is None
                else str(route_value["latest_version"])
            ),
            selector=route_value.get("selector"),  # type: ignore[arg-type]
            endpoint_configuration=route_value.get(  # type: ignore[arg-type]
                "endpoint_configuration"
            ),
            sha256=str(route_value["sha256"]),
        ),
        definition=HostedDefinition.coerce(definition_value),
        service_id=(
            None if value.get("service_id") is None else str(value["service_id"])
        ),
        status=None if value.get("status") is None else str(value["status"]),
    )


def _prompt_reference_document(
    reference: PromptDraftReference,
) -> dict[str, object]:
    return {
        "agent_name": reference.agent_name,
        "version": reference.version,
        "ownership_token": reference.ownership_token,
        "definition_sha256": reference.definition_sha256,
        "route": _route_document(reference.route),
        "definition": reference.definition.as_payload(),
        "service_id": reference.service_id,
        "status": reference.status,
    }


def _prompt_reference_from_document(
    value: Mapping[str, object],
) -> PromptDraftReference:
    route_value = value.get("route")
    definition_value = value.get("definition")
    if not isinstance(route_value, Mapping) or not isinstance(
        definition_value, Mapping
    ):
        raise OptimizerAdapterError("restricted target reference is incomplete")
    return PromptDraftReference(
        agent_name=str(value["agent_name"]),
        version=str(value["version"]),
        ownership_token=str(value["ownership_token"]),
        definition_sha256=str(value["definition_sha256"]),
        route=RouteFingerprint(
            agent_name=str(route_value["agent_name"]),
            latest_version=(
                None
                if route_value.get("latest_version") is None
                else str(route_value["latest_version"])
            ),
            selector=route_value.get("selector"),  # type: ignore[arg-type]
            endpoint_configuration=route_value.get(  # type: ignore[arg-type]
                "endpoint_configuration"
            ),
            sha256=str(route_value["sha256"]),
        ),
        definition=PromptDefinition.coerce(definition_value),
        service_id=(
            None if value.get("service_id") is None else str(value["service_id"])
        ),
        status=None if value.get("status") is None else str(value["status"]),
    )


def _field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _required_field(value: object, name: str) -> str:
    result = _field(value, name)
    if not isinstance(result, str) or not result:
        raise OptimizerAdapterError(f"Foundry response omitted {name}")
    return result


def _optional_field(value: object, name: str) -> str | None:
    result = _field(value, name)
    return result if isinstance(result, str) and result else None


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _azure_cli_credential() -> AzureCliCredential:
    return AzureCliCredential(process_timeout=60)


__all__ = [
    "CandidateTargetHandle",
    "EVALUATION_ADAPTER_ID",
    "EVALUATION_ADAPTER_VERSION",
    "EVALUATION_IMPLEMENTATION_ID",
    "EVALUATION_IMPLEMENTATION_VERSION",
    "EvaluationProjection",
    "EvaluationTarget",
    "FoundryEvaluationAdapter",
    "FoundryEvaluationConfig",
    "FoundryHostedTargetAdapter",
    "FoundryHostedTargetConfig",
    "FoundryPromptTargetAdapter",
    "FoundryPromptTargetConfig",
    "OptimizerAdapterError",
    "PROMPT_TARGET_ADAPTER_ID",
    "PROMPT_TARGET_ADAPTER_VERSION",
    "PROMPT_TARGET_IMPLEMENTATION_ID",
    "PROMPT_TARGET_IMPLEMENTATION_VERSION",
    "TARGET_ADAPTER_ID",
    "TARGET_ADAPTER_VERSION",
    "TARGET_IMPLEMENTATION_ID",
    "TARGET_IMPLEMENTATION_VERSION",
    "TargetSnapshot",
    "TaskScore",
]
