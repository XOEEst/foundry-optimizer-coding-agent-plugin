from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from collections.abc import Mapping
from decimal import Decimal
from math import isfinite
from pathlib import Path
from typing import Any, Callable, Final, Literal, Protocol, runtime_checkable

import typer
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from foundry_opt.poc.candidate import (
    CandidateWorkspace,
    FinalizedCandidate,
    PreparedCandidate,
)
from foundry_opt.optimizer_adapters import (
    EVALUATION_ADAPTER_ID,
    EVALUATION_ADAPTER_VERSION,
    EVALUATION_IMPLEMENTATION_ID,
    EVALUATION_IMPLEMENTATION_VERSION,
    PROMPT_TARGET_ADAPTER_ID,
    PROMPT_TARGET_ADAPTER_VERSION,
    PROMPT_TARGET_IMPLEMENTATION_ID,
    PROMPT_TARGET_IMPLEMENTATION_VERSION,
    TARGET_ADAPTER_ID,
    TARGET_ADAPTER_VERSION,
    TARGET_IMPLEMENTATION_ID,
    TARGET_IMPLEMENTATION_VERSION,
    CandidateTargetHandle,
    EvaluationProjection,
    FoundryEvaluationAdapter,
    FoundryHostedTargetAdapter,
    FoundryPromptTargetAdapter,
    TargetSnapshot,
)
from foundry_opt.poc.foundry import FoundryPocClient


RUNTIME_INTERFACE_ID: Final = "agent-optimizer-runtime"
RUNTIME_INTERFACE_VERSION: Final = "1.0.0"
BACKEND_ID: Final = "foundry-opt"
BACKEND_VERSION: Final = "1.0.0"
WORKSPACE_IMPLEMENTATION_ID: Final = "foundry-opt.git-worktree"
WORKSPACE_IMPLEMENTATION_VERSION: Final = "1.0.0"
RUNTIME_CAPABILITIES: Final = {
    "frozen_contract_verification": True,
    "external_trusted_state": True,
    "agent_scoped_storage": True,
    "public_report_path_derivation": True,
    "adapter_identity_verification": True,
    "candidate_worktree_lifecycle": True,
    "target_adapter_dispatch": True,
    "evaluation_adapter_dispatch": True,
}

_RUN_ID_PATTERN: Final = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
_SHA256_PATTERN: Final = re.compile(r"^sha256:[0-9a-f]{64}$")


class OptimizerRuntimeError(RuntimeError):
    """A frozen optimizer run contract cannot be executed safely."""


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RuntimeBackendIdentity(_FrozenModel):
    interface_id: str = RUNTIME_INTERFACE_ID
    interface_version: str = RUNTIME_INTERFACE_VERSION
    backend_id: str = BACKEND_ID
    backend_version: str = BACKEND_VERSION
    implementation_id: str = "foundry_opt.optimizer_runtime"
    implementation_version: str = BACKEND_VERSION
    implementation_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")


class RuntimeSelection(_FrozenModel):
    interface_id: str = Field(min_length=1)
    interface_version: str = Field(min_length=1)
    backend_id: str = Field(min_length=1)
    backend_version: str = Field(min_length=1)
    manifest_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    implementation_id: str = Field(min_length=1)
    implementation_version: str = Field(min_length=1)
    implementation_hash: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    capabilities: dict[str, bool]


class RuntimeRunPaths(_FrozenModel):
    repository_root: Path
    agent_root: Path
    agent_scope_id: str
    trusted_state_root: Path
    trusted_agent_root: Path
    trusted_run_root: Path
    public_run_root: Path
    contract_path: Path

    @field_validator(
        "repository_root",
        "agent_root",
        "trusted_state_root",
        "trusted_agent_root",
        "trusted_run_root",
        "public_run_root",
        "contract_path",
    )
    @classmethod
    def require_absolute_path(cls, value: Path) -> Path:
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("runtime paths must be absolute")
        return path


@runtime_checkable
class OptimizerRuntimeSession(Protocol):
    @property
    def paths(self) -> RuntimeRunPaths: ...

    def prepare_candidate(
        self,
        candidate_id: str,
        *,
        model: str,
        hypothesis: str,
        parent_id: str | None = None,
    ) -> PreparedCandidate: ...

    def finalize_candidate(self, candidate_id: str) -> FinalizedCandidate: ...

    def cleanup_candidate(
        self,
        candidate_id: str,
        *,
        remove_artifacts: bool = False,
    ) -> None: ...

    def inspect_target(self) -> TargetSnapshot: ...

    def materialize_candidate_target(
        self,
        candidate_id: str,
    ) -> CandidateTargetHandle: ...

    def verify_candidate_target(
        self,
        candidate_id: str,
    ) -> CandidateTargetHandle: ...

    def cleanup_candidate_target(self, candidate_id: str) -> str: ...

    def evaluate(
        self,
        *,
        subject_kind: Literal["baseline", "candidate"],
        subject_id: str,
        split_role: Literal["search", "confirmation", "validation"],
        split_fingerprint: str,
        opaque_role_path: Path,
        expected_count: int,
    ) -> EvaluationProjection: ...


@runtime_checkable
class OptimizerRuntime(Protocol):
    @property
    def identity(self) -> RuntimeBackendIdentity: ...

    def open_run(
        self,
        contract: Mapping[str, object],
    ) -> OptimizerRuntimeSession: ...


class FoundryOptRuntimeSession:
    def __init__(
        self,
        *,
        paths: RuntimeRunPaths,
        workspace: CandidateWorkspace,
        target_adapter: (
            FoundryHostedTargetAdapter | FoundryPromptTargetAdapter | None
        ) = None,
        evaluation_adapter: FoundryEvaluationAdapter | None = None,
    ) -> None:
        self._paths = paths
        self._workspace = workspace
        self._target_adapter = target_adapter
        self._evaluation_adapter = evaluation_adapter

    @property
    def paths(self) -> RuntimeRunPaths:
        return self._paths

    def prepare_candidate(
        self,
        candidate_id: str,
        *,
        model: str,
        hypothesis: str,
        parent_id: str | None = None,
    ) -> PreparedCandidate:
        return self._workspace.prepare(
            candidate_id,
            model=model,
            hypothesis=hypothesis,
            parent_id=parent_id,
        )

    def finalize_candidate(self, candidate_id: str) -> FinalizedCandidate:
        return self._workspace.finalize(candidate_id)

    def cleanup_candidate(
        self,
        candidate_id: str,
        *,
        remove_artifacts: bool = False,
    ) -> None:
        self._workspace.cleanup(
            candidate_id,
            remove_artifacts=remove_artifacts,
        )

    @property
    def target_adapter(
        self,
    ) -> FoundryHostedTargetAdapter | FoundryPromptTargetAdapter:
        if self._target_adapter is None:
            raise OptimizerRuntimeError(
                "the frozen contract does not select an implemented target adapter"
            )
        return self._target_adapter

    @property
    def evaluation_adapter(self) -> FoundryEvaluationAdapter:
        if self._evaluation_adapter is None:
            raise OptimizerRuntimeError(
                "the frozen contract does not select an implemented evaluation adapter"
            )
        return self._evaluation_adapter

    def inspect_target(self) -> TargetSnapshot:
        return self.target_adapter.inspect_target()

    def materialize_candidate_target(
        self,
        candidate_id: str,
    ) -> CandidateTargetHandle:
        return self.target_adapter.materialize_candidate(
            self._workspace.finalized(candidate_id)
        )

    def verify_candidate_target(
        self,
        candidate_id: str,
    ) -> CandidateTargetHandle:
        return self.target_adapter.verify_candidate(candidate_id)

    def cleanup_candidate_target(self, candidate_id: str) -> str:
        return self.target_adapter.cleanup_candidate(candidate_id)

    def evaluate(
        self,
        *,
        subject_kind: Literal["baseline", "candidate"],
        subject_id: str,
        split_role: Literal["search", "confirmation", "validation"],
        split_fingerprint: str,
        opaque_role_path: Path,
        expected_count: int,
    ) -> EvaluationProjection:
        if subject_kind == "baseline":
            if subject_id != "baseline":
                raise OptimizerRuntimeError(
                    "baseline evaluations must use subject_id 'baseline'"
                )
            target = self.target_adapter.baseline_target()
        else:
            target = self.target_adapter.candidate_target(subject_id)
        return self.evaluation_adapter.evaluate(
            target,
            split_role=split_role,
            split_fingerprint=split_fingerprint,
            opaque_role_path=opaque_role_path,
            expected_count=expected_count,
        )


class FoundryOptRuntime:
    """Reference runtime backend for frozen agent-optimizer-v2 contracts."""

    def __init__(
        self,
        *,
        git_executable: str = "git",
        target_client_factory: (
            Callable[[Mapping[str, object]], FoundryPocClient] | None
        ) = None,
        evaluation_client_factory: (
            Callable[[Mapping[str, object]], object] | None
        ) = None,
        credential_factory: Callable[[], object] | None = None,
    ) -> None:
        self._git_executable = git_executable
        self._target_client_factory = target_client_factory
        self._evaluation_client_factory = evaluation_client_factory
        self._credential_factory = credential_factory
        self._identity = RuntimeBackendIdentity(
            implementation_hash=_implementation_hash()
        )

    @property
    def identity(self) -> RuntimeBackendIdentity:
        return self._identity

    def open_run(
        self,
        contract: Mapping[str, object],
    ) -> FoundryOptRuntimeSession:
        document = _object(contract, "run contract")
        _verify_contract_hash(document)
        selection = RuntimeSelection.model_validate(
            _object(document.get("runtime"), "runtime")
        )
        self._verify_runtime_selection(selection)

        run_id = _required_string(document, "run_id")
        if _RUN_ID_PATTERN.fullmatch(run_id) is None:
            raise OptimizerRuntimeError("run_id is not a safe path component")
        storage = _object(document.get("storage"), "storage")
        layout = _object(storage.get("layout"), "storage.layout")
        repository_root = Path(
            _required_string(storage, "repository_root")
        ).resolve(strict=True)
        agent_root = Path(
            _required_string(storage, "agent_root")
        ).resolve(strict=True)
        agent_scope_id = _required_string(storage, "agent_scope_id")
        if _RUN_ID_PATTERN.fullmatch(agent_scope_id) is None:
            raise OptimizerRuntimeError(
                "agent_scope_id is not a safe path component"
            )
        trusted_state_root = Path(
            _required_string(storage, "trusted_state_root")
        ).resolve(strict=False)
        _verify_primary_checkout(repository_root)
        _verify_agent_root(repository_root, agent_root)
        _verify_separate_roots(repository_root, trusted_state_root)

        public_tracking = _required_string(
            layout,
            "public_tracking_directory",
        )
        public_runs = _required_string(layout, "public_runs_directory")
        contract_filename = _required_string(layout, "contract_filename")
        workspace_artifacts = _required_string(
            layout,
            "workspace_artifact_directory",
        )
        _verify_fixed_segment(public_tracking, "experiment_tracking")
        _verify_fixed_segment(public_runs, "runs")
        _verify_fixed_segment(contract_filename, "contract.json")
        _verify_fixed_segment(workspace_artifacts, "workspace-artifacts")
        _verify_tracking_is_ignored(
            repository_root,
            (
                agent_root
                / public_tracking
                / public_runs
                / run_id
                / ".runtime-probe"
            ).relative_to(repository_root),
            git_executable=self._git_executable,
        )

        trusted_agent_root = (
            trusted_state_root / agent_scope_id
        ).resolve(strict=False)
        trusted_run_root = (trusted_agent_root / run_id).resolve(strict=False)
        public_run_root = (
            agent_root / public_tracking / public_runs / run_id
        ).resolve(strict=False)
        _verify_separate_roots(repository_root, trusted_run_root)
        trusted_agent_root.mkdir(parents=True, exist_ok=True)
        _bind_agent_scope(
            trusted_agent_root / "scope.json",
            repository_root=repository_root,
            agent_root=agent_root,
            agent_scope_id=agent_scope_id,
        )
        trusted_run_root.mkdir(parents=True, exist_ok=True)
        contract_path = trusted_run_root / contract_filename

        adapters = _object(document.get("adapters"), "adapters")
        workspace_adapter = _object(
            adapters.get("workspace"),
            "adapters.workspace",
        )
        self._verify_workspace_adapter(workspace_adapter)
        config = _object(
            workspace_adapter.get("resolved_config"),
            "adapters.workspace.resolved_config",
        )
        configured_repository = Path(
            _required_string(config, "repository_root")
        ).resolve(strict=True)
        configured_agent_root = Path(
            _required_string(config, "agent_root")
        ).resolve(strict=True)
        configured_agent_scope_id = _required_string(
            config,
            "agent_scope_id",
        )
        configured_trusted_root = Path(
            _required_string(config, "trusted_state_root")
        ).resolve(strict=False)
        if configured_repository != repository_root:
            raise OptimizerRuntimeError(
                "workspace repository_root does not match storage.repository_root"
            )
        if configured_agent_root != agent_root:
            raise OptimizerRuntimeError(
                "workspace agent_root does not match storage.agent_root"
            )
        if configured_agent_scope_id != agent_scope_id:
            raise OptimizerRuntimeError(
                "workspace agent_scope_id does not match storage.agent_scope_id"
            )
        if configured_trusted_root != trusted_state_root:
            raise OptimizerRuntimeError(
                "workspace trusted_state_root does not match storage.trusted_state_root"
            )

        source_root = _required_string(config, "source_root")
        _verify_source_root_scope(
            repository_root,
            agent_root,
            source_root,
        )
        workspace = CandidateWorkspace(
            repository_root,
            trusted_run_root,
            _required_string(config, "base_commit"),
            editable_patterns=_string_sequence(config, "editable_patterns"),
            protected_patterns=_string_sequence(
                config,
                "protected_patterns",
                required=False,
            ),
            source_root=source_root,
            artifact_directory=workspace_artifacts,
            git_executable=self._git_executable,
        )
        target_adapter = self._construct_target_adapter(
            adapters.get("target"),
            paths=(
                repository_root,
                agent_root,
                trusted_run_root,
            ),
            run_id=run_id,
            contract_hash=_required_string(document, "contract_hash"),
        )
        evaluation_adapter = self._construct_evaluation_adapter(
            adapters.get("evaluation"),
            trusted_run_root=trusted_run_root,
            run_id=run_id,
            contract_hash=_required_string(document, "contract_hash"),
            objective=document.get("objective"),
        )
        _persist_frozen_contract(contract_path, document)
        return FoundryOptRuntimeSession(
            paths=RuntimeRunPaths(
                repository_root=repository_root,
                agent_root=agent_root,
                agent_scope_id=agent_scope_id,
                trusted_state_root=trusted_state_root,
                trusted_agent_root=trusted_agent_root,
                trusted_run_root=trusted_run_root,
                public_run_root=public_run_root,
                contract_path=contract_path,
            ),
            workspace=workspace,
            target_adapter=target_adapter,
            evaluation_adapter=evaluation_adapter,
        )

    def _verify_runtime_selection(self, selection: RuntimeSelection) -> None:
        expected = self.identity
        for field_name in (
            "interface_id",
            "interface_version",
            "backend_id",
            "backend_version",
            "implementation_id",
            "implementation_version",
            "implementation_hash",
        ):
            if getattr(selection, field_name) != getattr(expected, field_name):
                raise OptimizerRuntimeError(
                    f"runtime {field_name} does not match the loaded backend"
                )
        if selection.manifest_hash != _runtime_manifest_hash():
            raise OptimizerRuntimeError(
                "runtime manifest_hash does not match the loaded backend"
            )
        if selection.capabilities != RUNTIME_CAPABILITIES:
            raise OptimizerRuntimeError(
                "runtime capabilities do not match the loaded backend"
            )

    @staticmethod
    def _verify_workspace_adapter(adapter: Mapping[str, object]) -> None:
        expected = {
            "id": "git-worktree",
            "version": "1.0.0",
            "manifest_status": "implemented",
            "implementation_id": WORKSPACE_IMPLEMENTATION_ID,
            "implementation_version": WORKSPACE_IMPLEMENTATION_VERSION,
        }
        for key, value in expected.items():
            if adapter.get(key) != value:
                raise OptimizerRuntimeError(
                    f"workspace adapter {key} is not supported by foundry-opt"
                )

    def _construct_target_adapter(
        self,
        value: object,
        *,
        paths: tuple[Path, Path, Path],
        run_id: str,
        contract_hash: str,
    ) -> FoundryHostedTargetAdapter | FoundryPromptTargetAdapter | None:
        if value is None:
            return None
        adapter = _object(value, "adapters.target")
        adapter_id = adapter.get("id")
        if not isinstance(adapter_id, str):
            raise OptimizerRuntimeError(
                "target adapter id must be an exact string identity"
            )
        implementations = {
            TARGET_ADAPTER_ID: (
                TARGET_ADAPTER_VERSION,
                TARGET_IMPLEMENTATION_ID,
                TARGET_IMPLEMENTATION_VERSION,
                FoundryHostedTargetAdapter,
            ),
            PROMPT_TARGET_ADAPTER_ID: (
                PROMPT_TARGET_ADAPTER_VERSION,
                PROMPT_TARGET_IMPLEMENTATION_ID,
                PROMPT_TARGET_IMPLEMENTATION_VERSION,
                FoundryPromptTargetAdapter,
            ),
        }
        selected = implementations.get(adapter_id)
        if selected is None:
            raise OptimizerRuntimeError(
                f"target adapter id {adapter_id!r} is not supported by foundry-opt"
            )
        (
            adapter_version,
            implementation_id,
            implementation_version,
            adapter_type,
        ) = selected
        self._verify_implemented_adapter(
            adapter,
            kind="targets",
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            implementation_id=implementation_id,
            implementation_version=implementation_version,
        )
        config = _object(
            adapter.get("resolved_config"),
            "adapters.target.resolved_config",
        )
        self._verify_config_hash(adapter, config, "target")
        client = (
            None
            if self._target_client_factory is None
            else self._target_client_factory(config)
        )
        repository_root, agent_root, trusted_run_root = paths
        return adapter_type(
            config,
            repository_root=repository_root,
            agent_root=agent_root,
            trusted_run_root=trusted_run_root,
            run_id=run_id,
            contract_hash=contract_hash,
            client=client,
            credential_factory=self._credential_factory,
        )

    def _construct_evaluation_adapter(
        self,
        value: object,
        *,
        trusted_run_root: Path,
        run_id: str,
        contract_hash: str,
        objective: object,
    ) -> FoundryEvaluationAdapter | None:
        if value is None:
            return None
        adapter = _object(value, "adapters.evaluation")
        self._verify_implemented_adapter(
            adapter,
            kind="evaluators",
            adapter_id=EVALUATION_ADAPTER_ID,
            adapter_version=EVALUATION_ADAPTER_VERSION,
            implementation_id=EVALUATION_IMPLEMENTATION_ID,
            implementation_version=EVALUATION_IMPLEMENTATION_VERSION,
        )
        config = _object(
            adapter.get("resolved_config"),
            "adapters.evaluation.resolved_config",
        )
        self._verify_config_hash(adapter, config, "evaluation")
        client = (
            None
            if self._evaluation_client_factory is None
            else self._evaluation_client_factory(config)
        )
        return FoundryEvaluationAdapter(
            config,
            trusted_run_root=trusted_run_root,
            run_id=run_id,
            contract_hash=contract_hash,
            objective=_object(objective, "objective"),
            openai_client=client,
            credential_factory=self._credential_factory,
        )

    @staticmethod
    def _verify_config_hash(
        adapter: Mapping[str, object],
        config: Mapping[str, object],
        kind: str,
    ) -> None:
        expected = adapter.get("config_hash")
        if not isinstance(expected, str) or _SHA256_PATTERN.fullmatch(expected) is None:
            raise OptimizerRuntimeError(f"{kind} adapter config_hash is invalid")
        actual = f"sha256:{hashlib.sha256(_rfc8785_bytes(config)).hexdigest()}"
        if actual != expected:
            raise OptimizerRuntimeError(
                f"{kind} adapter config_hash does not match resolved_config"
            )

    @staticmethod
    def _verify_implemented_adapter(
        adapter: Mapping[str, object],
        *,
        kind: str,
        adapter_id: str,
        adapter_version: str,
        implementation_id: str,
        implementation_version: str,
    ) -> None:
        manifest = _adapter_manifest(kind, adapter_id)
        expected = {
            "id": adapter_id,
            "version": adapter_version,
            "manifest_status": "implemented",
            "implementation_id": implementation_id,
            "implementation_version": implementation_version,
            "manifest_hash": _adapter_manifest_hash(kind, adapter_id),
            "capabilities": manifest.get("capabilities"),
        }
        for key, expected_value in expected.items():
            if adapter.get(key) != expected_value:
                raise OptimizerRuntimeError(
                    f"{adapter_id} adapter {key} does not match the loaded implementation"
                )


def register_optimizer_runtime_commands(parent: typer.Typer) -> None:
    @parent.command("inspect")
    def inspect_command(
        contract: Path = typer.Option(..., "--contract"),
    ) -> None:
        runtime = FoundryOptRuntime()
        session = runtime.open_run(_read_contract(contract))
        _echo(
            {
                "runtime": runtime.identity.model_dump(mode="json"),
                "paths": session.paths.model_dump(mode="json"),
                "status": "ready",
            }
        )

    @parent.command("candidate-create")
    def candidate_create_command(
        contract: Path = typer.Option(..., "--contract"),
        candidate_id: str = typer.Option(..., "--candidate"),
        model: str = typer.Option(..., "--model"),
        hypothesis: str = typer.Option(..., "--hypothesis"),
        parent_id: str | None = typer.Option(None, "--parent"),
    ) -> None:
        session = FoundryOptRuntime().open_run(_read_contract(contract))
        prepared = session.prepare_candidate(
            candidate_id,
            model=model,
            hypothesis=hypothesis,
            parent_id=parent_id,
        )
        _echo(prepared.model_dump(mode="json"))

    @parent.command("candidate-finalize")
    def candidate_finalize_command(
        contract: Path = typer.Option(..., "--contract"),
        candidate_id: str = typer.Option(..., "--candidate"),
    ) -> None:
        session = FoundryOptRuntime().open_run(_read_contract(contract))
        finalized = session.finalize_candidate(candidate_id)
        _echo(finalized.model_dump(mode="json"))

    @parent.command("candidate-cleanup")
    def candidate_cleanup_command(
        contract: Path = typer.Option(..., "--contract"),
        candidate_id: str = typer.Option(..., "--candidate"),
        remove_artifacts: bool = typer.Option(False, "--remove-artifacts"),
    ) -> None:
        session = FoundryOptRuntime().open_run(_read_contract(contract))
        session.cleanup_candidate(
            candidate_id,
            remove_artifacts=remove_artifacts,
        )
        _echo({"candidate_id": candidate_id, "status": "cleaned"})


def _implementation_hash() -> str:
    return _normalized_file_hash(Path(__file__))


def _runtime_manifest_hash() -> str:
    path = (
        Path(__file__).parent
        / "legacy_runtime_assets"
        / "runtime-backend.yaml"
    )
    return _normalized_file_hash(path)


def _adapter_manifest(kind: str, adapter_id: str) -> dict[str, object]:
    path = _adapter_manifest_path(kind, adapter_id)
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise OptimizerRuntimeError(
            f"{adapter_id} adapter manifest is unreadable"
        ) from error
    return _object(value, f"{adapter_id} adapter manifest")


def _adapter_manifest_hash(kind: str, adapter_id: str) -> str:
    return _normalized_file_hash(_adapter_manifest_path(kind, adapter_id))


def _adapter_manifest_path(kind: str, adapter_id: str) -> Path:
    return (
        Path(__file__).parent
        / "legacy_runtime_assets"
        / "adapters"
        / kind
        / adapter_id
        / "adapter.yaml"
    )


def _normalized_file_hash(path: Path) -> str:
    content = path.read_text(encoding="utf-8")
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.endswith("\n"):
        normalized += "\n"
    return f"sha256:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"


def _verify_contract_hash(contract: Mapping[str, object]) -> None:
    if contract.get("canonicalization") != "rfc8785":
        raise OptimizerRuntimeError("run contract must use RFC 8785 canonicalization")
    expected = contract.get("contract_hash")
    if not isinstance(expected, str) or _SHA256_PATTERN.fullmatch(expected) is None:
        raise OptimizerRuntimeError("run contract has an invalid contract_hash")
    body = {key: value for key, value in contract.items() if key != "contract_hash"}
    try:
        canonical = _rfc8785_bytes(body)
    except (TypeError, ValueError) as error:
        raise OptimizerRuntimeError(
            "run contract contains a value that RFC 8785 cannot canonicalize"
        ) from error
    actual = f"sha256:{hashlib.sha256(canonical).hexdigest()}"
    if actual != expected:
        raise OptimizerRuntimeError("run contract hash verification failed")


def _persist_frozen_contract(
    path: Path,
    contract: Mapping[str, object],
) -> None:
    serialized = json.dumps(
        contract,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
    ) + "\n"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise OptimizerRuntimeError(
                "persisted run contract is unreadable"
            ) from error
        if existing != contract:
            raise OptimizerRuntimeError(
                "persisted run contract differs from the supplied contract"
            )
        return
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(serialized, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _bind_agent_scope(
    path: Path,
    *,
    repository_root: Path,
    agent_root: Path,
    agent_scope_id: str,
) -> None:
    document = {
        "schema_version": 1,
        "agent_scope_id": agent_scope_id,
        "repository_root": str(repository_root),
        "agent_root": str(agent_root),
    }
    serialized = json.dumps(
        document,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    ) + "\n"
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise OptimizerRuntimeError(
                "persisted agent scope binding is unreadable"
            ) from error
        if existing != document:
            raise OptimizerRuntimeError(
                "agent_scope_id is already bound to a different agent root"
            )
        return
    temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(serialized, encoding="utf-8", newline="\n")
        try:
            os.link(temporary, path)
        except FileExistsError:
            _bind_agent_scope(
                path,
                repository_root=repository_root,
                agent_root=agent_root,
                agent_scope_id=agent_scope_id,
            )
    finally:
        temporary.unlink(missing_ok=True)


def _verify_primary_checkout(repository: Path) -> None:
    if not (repository / ".git").is_dir():
        raise OptimizerRuntimeError(
            "repository_root must be a primary Git checkout, not a worktree"
        )


def _verify_agent_root(repository: Path, agent_root: Path) -> None:
    if agent_root != repository and not agent_root.is_relative_to(repository):
        raise OptimizerRuntimeError(
            "agent_root must be the repository root or a directory within it"
        )


def _verify_source_root_scope(
    repository: Path,
    agent_root: Path,
    source_root: str,
) -> None:
    source_path = (repository / source_root).resolve(strict=True)
    if source_path != agent_root and not source_path.is_relative_to(agent_root):
        raise OptimizerRuntimeError(
            "workspace source_root must remain within agent_root"
        )


def _verify_separate_roots(repository: Path, trusted_root: Path) -> None:
    if trusted_root == repository or trusted_root.is_relative_to(repository):
        raise OptimizerRuntimeError(
            "trusted_state_root must be outside repository_root"
        )
    if repository.is_relative_to(trusted_root):
        raise OptimizerRuntimeError(
            "repository_root must be outside trusted_state_root"
        )


def _verify_tracking_is_ignored(
    repository: Path,
    relative_probe: Path,
    *,
    git_executable: str,
) -> None:
    completed = subprocess.run(
        [
            git_executable,
            "-C",
            str(repository),
            "check-ignore",
            "--quiet",
            "--no-index",
            "--",
            str(relative_probe),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if completed.returncode != 0:
        raise OptimizerRuntimeError(
            "experiment_tracking must be ignored by the customer repository"
        )


def _verify_fixed_segment(value: str, expected: str) -> None:
    if value != expected:
        raise OptimizerRuntimeError(
            f"storage layout must use {expected!r}"
        )


def _object(value: object, subject: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise OptimizerRuntimeError(f"{subject} must be an object")
    return dict(value)


def _required_string(value: Mapping[str, object], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise OptimizerRuntimeError(f"{key} must be a nonempty string")
    return result


def _string_sequence(
    value: Mapping[str, object],
    key: str,
    *,
    required: bool = True,
) -> tuple[str, ...]:
    result = value.get(key)
    if result is None and not required:
        return ()
    if not isinstance(result, list) or any(
        not isinstance(item, str) or not item for item in result
    ):
        raise OptimizerRuntimeError(
            f"{key} must be a list of nonempty strings"
        )
    if len(result) != len(set(result)):
        raise OptimizerRuntimeError(f"{key} must contain unique values")
    return tuple(result)


def _read_contract(path: Path) -> dict[str, object]:
    try:
        value: Any = json.loads(path.resolve(strict=True).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise OptimizerRuntimeError("run contract is unreadable") from error
    return _object(value, "run contract")


def _echo(value: object) -> None:
    typer.echo(
        json.dumps(
            value,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )
    )


def _rfc8785_bytes(value: object) -> bytes:
    return _serialize_rfc8785(value).encode("utf-8")


def _serialize_rfc8785(value: object) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, int):
        if abs(value) > 9_007_199_254_740_991:
            raise ValueError("integer is outside the interoperable IEEE 754 range")
        return str(value)
    if isinstance(value, float):
        return _serialize_rfc8785_number(value)
    if isinstance(value, list):
        return "[" + ",".join(_serialize_rfc8785(item) for item in value) + "]"
    if isinstance(value, Mapping):
        entries: list[str] = []
        for key in sorted(value, key=_utf16_sort_key):
            if not isinstance(key, str):
                raise TypeError("object keys must be strings")
            entries.append(
                f"{_serialize_rfc8785(key)}:{_serialize_rfc8785(value[key])}"
            )
        return "{" + ",".join(entries) + "}"
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def _serialize_rfc8785_number(value: float) -> str:
    if not isfinite(value):
        raise ValueError("numbers must be finite")
    if value == 0:
        return "0"
    absolute = abs(value)
    shortest = repr(value).lower()
    if 1e-6 <= absolute < 1e21:
        if "e" in shortest:
            shortest = format(Decimal(shortest), "f")
        if "." in shortest:
            shortest = shortest.rstrip("0").rstrip(".")
        return shortest
    if "e" not in shortest:
        shortest = format(Decimal(shortest).normalize(), "e")
    mantissa, exponent = shortest.split("e", maxsplit=1)
    mantissa = mantissa.rstrip("0").rstrip(".")
    exponent_value = int(exponent)
    sign = "+" if exponent_value >= 0 else "-"
    return f"{mantissa}e{sign}{abs(exponent_value)}"


def _utf16_sort_key(value: object) -> bytes:
    if not isinstance(value, str):
        raise TypeError("object keys must be strings")
    return value.encode("utf-16-be", errors="surrogatepass")


__all__ = [
    "BACKEND_ID",
    "BACKEND_VERSION",
    "FoundryOptRuntime",
    "FoundryOptRuntimeSession",
    "OptimizerRuntime",
    "OptimizerRuntimeError",
    "OptimizerRuntimeSession",
    "RUNTIME_CAPABILITIES",
    "RUNTIME_INTERFACE_ID",
    "RUNTIME_INTERFACE_VERSION",
    "RuntimeBackendIdentity",
    "RuntimeRunPaths",
    "RuntimeSelection",
    "WORKSPACE_IMPLEMENTATION_ID",
    "WORKSPACE_IMPLEMENTATION_VERSION",
    "register_optimizer_runtime_commands",
]
