from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self

import yaml
from pydantic import Field, StringConstraints, model_validator

from foundry_opt.bootstrap.canonical import (
    canonical_json_bytes,
    canonical_sha256,
    safe_persisted_document,
)
from foundry_opt.bootstrap.contracts import (
    BootstrapDocument,
    BootstrapPlan,
    BootstrapReceipt,
    BootstrapSidecar,
    DecisionPolicy,
    DeploymentSettings,
    FoundryProjectSettings,
    HardGuardrail,
    RuntimeProtocolSettings,
    SelectedAgentProfile,
    VerificationSettings,
)
from foundry_opt.bootstrap.errors import BootstrapApplyError, BootstrapConfigError
from foundry_opt.bootstrap.input_contracts import (
    BootstrapPlanInput,
    TrustedTemplateManifest,
)
from foundry_opt.bootstrap.local_commit import build_local_commit_context
from foundry_opt.bootstrap.operation_state import default_state_root
from foundry_opt.bootstrap.owner_review import PlanReview, build_plan_review
from foundry_opt.bootstrap.plan_factory import load_trusted_manifest
from foundry_opt.bootstrap.repository.engine import (
    apply_repository,
    plan_repository,
    rollback_repository,
)
from foundry_opt.bootstrap.shared import require_safe_operation_id
from foundry_opt.bootstrap.state_lock import (
    atomic_replace_state,
    state_file_lock,
)

RepositorySetupLifecycleState = Literal[
    "awaiting_approval",
    "applied",
    "rolled_back",
]

_STATE_FILE_NAME = "state.json"
_LOCK_FILE_NAME = "state.lock"
_MAX_STATE_BYTES = 2 * 1024 * 1024
_DEFAULT_MODEL = "gpt-5-mini"


class RepositorySetupApproval(BootstrapDocument):
    repository_identity: str
    operation_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    runtime_commit: str
    plan_hash: str
    actor: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    summary: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    approval_hash: str

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
        plan: BootstrapPlan,
        actor: str,
        summary: str,
    ) -> "RepositorySetupApproval":
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
                "repository setup approval hash does not match the payload"
            )
        return self


class RepositorySetupStatePayload(BootstrapDocument):
    generation: int = Field(ge=0)
    lifecycle_state: RepositorySetupLifecycleState
    plan_input: BootstrapPlanInput
    plan: BootstrapPlan
    approval: RepositorySetupApproval | None = None
    receipt: BootstrapReceipt | None = None


class RepositorySetupStateEnvelope(BootstrapDocument):
    payload: RepositorySetupStatePayload
    generation_hash: str

    @property
    def generation(self) -> int:
        return self.payload.generation

    @property
    def lifecycle_state(self) -> RepositorySetupLifecycleState:
        return self.payload.lifecycle_state

    @property
    def plan_input(self) -> BootstrapPlanInput:
        return self.payload.plan_input

    @property
    def plan(self) -> BootstrapPlan:
        return self.payload.plan

    @property
    def approval(self) -> RepositorySetupApproval | None:
        return self.payload.approval

    @property
    def receipt(self) -> BootstrapReceipt | None:
        return self.payload.receipt

    @classmethod
    def create(cls, **values: object) -> "RepositorySetupStateEnvelope":
        payload = RepositorySetupStatePayload.model_validate(values)
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
                "repository setup state hash does not match the payload"
            )
        return self


class RepositorySetupReview(BootstrapDocument):
    plan_hash: str
    review: PlanReview
    assumptions: tuple[str, ...] = ()

    def render_markdown(self) -> str:
        lines = [self.review.render_markdown()]
        if self.assumptions:
            lines.extend(("", "### Detected defaults"))
            lines.extend(f"- {item}" for item in self.assumptions)
        lines.append(
            "- Approval applies only the repository files shown above; it does not connect Azure or deploy an agent."
        )
        return "\n".join(lines)


def default_repository_setup_state_root() -> Path:
    return default_state_root() / "repository-setup"


class RepositorySetupCoordinator:
    def __init__(self, *, state_root: Path | None = None) -> None:
        self._state_root = (
            Path(state_root)
            if state_root is not None
            else default_repository_setup_state_root()
        )

    def build(self, operation) -> RepositorySetupStateEnvelope:
        existing = self._try_load(
            operation.repository_binding.repository_id,
            operation.operation_id,
        )
        if existing is not None:
            self._validate_operation(existing.plan, operation)
            return existing
        plan_input, _ = build_repository_plan_input(operation)
        repository_root = Path(operation.repository_binding.repository_root)
        payloads = load_trusted_manifest(plan_input)
        plan = plan_repository(
            repository_root,
            operation_id=operation.operation_id,
            runtime_repository=operation.runtime_binding.runtime_repository,
            runtime_commit=operation.runtime_binding.runtime_commit,
            repository_identity=operation.repository_binding.repository_id,
            payloads=payloads,
        )
        envelope = RepositorySetupStateEnvelope.create(
            generation=0,
            lifecycle_state="awaiting_approval",
            plan_input=plan_input,
            plan=plan,
        )
        self._write(envelope)
        return envelope

    def review(self, operation) -> RepositorySetupReview:
        envelope = self.build(operation)
        _, assumptions = build_repository_plan_input(operation)
        return RepositorySetupReview(
            plan_hash=envelope.plan.plan_hash,
            review=build_plan_review(
                envelope.plan,
                plan_input=envelope.plan_input,
            ),
            assumptions=assumptions,
        )

    def approve(
        self,
        operation,
        *,
        actor: str,
        summary: str,
    ) -> tuple[RepositorySetupStateEnvelope, RepositorySetupApproval]:
        envelope = self.build(operation)
        approval = RepositorySetupApproval.create(
            plan=envelope.plan,
            actor=actor,
            summary=summary,
        )
        if envelope.lifecycle_state == "applied":
            if envelope.approval != approval:
                raise BootstrapApplyError(
                    "repository setup approval does not match the recorded approval"
                )
            return envelope, approval
        if envelope.lifecycle_state != "awaiting_approval":
            raise BootstrapApplyError(
                "repository setup cannot apply from the current state"
            )
        receipt, _ = apply_repository(
            Path(operation.repository_binding.repository_root),
            envelope.plan,
        )
        _install_local_state_excludes(
            Path(operation.repository_binding.repository_root)
        )
        applied = self._next(
            envelope,
            lifecycle_state="applied",
            approval=approval,
            receipt=receipt,
        )
        self._write(applied, expected=envelope)
        return applied, approval

    def rollback(self, operation) -> RepositorySetupStateEnvelope:
        envelope = self.build(operation)
        if envelope.lifecycle_state == "rolled_back":
            return envelope
        if envelope.lifecycle_state != "applied" or envelope.receipt is None:
            raise BootstrapApplyError(
                "repository setup rollback requires an applied receipt"
            )
        rollback_repository(
            Path(operation.repository_binding.repository_root),
            envelope.receipt,
        )
        rolled = self._next(envelope, lifecycle_state="rolled_back")
        self._write(rolled, expected=envelope)
        return rolled

    def status(self, operation) -> RepositorySetupStateEnvelope:
        envelope = self._load(
            operation.repository_binding.repository_id,
            operation.operation_id,
        )
        self._validate_operation(envelope.plan, operation)
        return envelope

    def _validate_operation(self, plan: BootstrapPlan, operation) -> None:
        if (
            plan.operation_id != operation.operation_id
            or plan.repository_identity
            != operation.repository_binding.repository_id
            or plan.runtime_repository
            != operation.runtime_binding.runtime_repository
            or plan.runtime_commit != operation.runtime_binding.runtime_commit
        ):
            raise BootstrapApplyError(
                "repository setup state does not match the active bootstrap operation"
            )

    def _operation_directory(
        self,
        repository_identity: str,
        operation_id: str,
    ) -> Path:
        root = self._state_root.resolve()
        operation_segment = require_safe_operation_id(
            operation_id,
            message="repository setup operation id is invalid",
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
                "repository setup state escapes the state root"
            ) from exc
        return target

    def _path(self, repository_identity: str, operation_id: str) -> Path:
        return (
            self._operation_directory(repository_identity, operation_id)
            / _STATE_FILE_NAME
        )

    def _try_load(
        self,
        repository_identity: str,
        operation_id: str,
    ) -> RepositorySetupStateEnvelope | None:
        try:
            return self._load(repository_identity, operation_id)
        except FileNotFoundError:
            return None

    def _load(
        self,
        repository_identity: str,
        operation_id: str,
    ) -> RepositorySetupStateEnvelope:
        data = self._path(repository_identity, operation_id).read_bytes()
        if len(data) > _MAX_STATE_BYTES:
            raise BootstrapApplyError(
                "repository setup state exceeds the size limit"
            )
        try:
            return RepositorySetupStateEnvelope.model_validate_json(data)
        except Exception as exc:
            raise BootstrapApplyError(
                "repository setup state is invalid or tampered"
            ) from exc

    def _write(
        self,
        envelope: RepositorySetupStateEnvelope,
        *,
        expected: RepositorySetupStateEnvelope | None = None,
    ) -> None:
        path = self._path(
            envelope.plan.repository_identity,
            envelope.plan.operation_id,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = path.parent / _LOCK_FILE_NAME
        with state_file_lock(
            lock,
            locked_message=(
                "repository setup state is locked by another writer"
            ),
        ):
            if expected is None:
                if path.exists():
                    raise BootstrapApplyError(
                        "repository setup state already exists"
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
                        "repository setup state generation conflict"
                    )
            data = canonical_json_bytes(envelope.model_dump(mode="json")) + b"\n"
            atomic_replace_state(
                path,
                data,
                generation_hash=envelope.generation_hash,
            )

    @staticmethod
    def _next(
        envelope: RepositorySetupStateEnvelope,
        **updates: object,
    ) -> RepositorySetupStateEnvelope:
        payload = envelope.payload.model_dump(mode="python")
        payload.update(updates)
        payload["generation"] = envelope.generation + 1
        return RepositorySetupStateEnvelope.create(**payload)


class BootstrapRepositorySetupHandler:
    def __init__(
        self,
        *,
        coordinator: RepositorySetupCoordinator | None = None,
    ) -> None:
        self._coordinator = coordinator or RepositorySetupCoordinator()

    def review(self, *, operation) -> RepositorySetupReview:
        return self._coordinator.review(operation)

    def approve(self, *, operation, approval) -> object:
        from foundry_opt.bootstrap.runner import (
            BootstrapChildReference,
            BootstrapStageOutcome,
        )

        state, repository_approval = self._coordinator.approve(
            operation,
            actor=approval.actor,
            summary=approval.summary,
        )
        assert state.receipt is not None
        enabled = _enabled_agent_ids(operation)
        deployable = _deployable_agent_ids(operation, enabled)
        commit_context = build_local_commit_context(
            state.plan,
            commit_agent_ids=enabled,
            next_stage=(
                "deployment_approval" if deployable else "final_handoff"
            ),
        )
        child_refs = tuple(
            item for item in operation.child_refs if item.step != "repository"
        )
        next_stage = "connection_approval" if enabled else "commit_approval"
        note = (
            "Applied the reviewed repository plan. Review the GitHub-to-Azure connection next."
            if enabled
            else "Applied the reviewed repository plan. No agents are enabled, so review the local commit next."
        )
        return BootstrapStageOutcome(
            stage=next_stage,
            note=note,
            child_refs=(
                *child_refs,
                BootstrapChildReference(
                    step="repository",
                    kind="repository-bootstrap",
                    identifier=state.receipt.receipt_hash,
                    summary=f"repository plan {state.plan.plan_hash[:12]}",
                ),
            ),
            handler_context={
                **commit_context,
                "repository_setup": {
                    "plan_hash": state.plan.plan_hash,
                    "approval_hash": repository_approval.approval_hash,
                    "enabled_agent_ids": list(enabled),
                    "deployable_agent_ids": list(deployable),
                },
            },
        )

    def rollback(self, *, operation, step, child_ref) -> object:
        if step != "repository" or child_ref.step != "repository":
            raise BootstrapApplyError(
                "repository setup handler can only roll back repository work"
            )
        state = self._coordinator.status(operation)
        if (
            state.receipt is None
            or child_ref.identifier != state.receipt.receipt_hash
        ):
            raise BootstrapApplyError(
                "repository rollback child reference does not match state"
            )
        self._coordinator.rollback(operation)
        return self._rollback_outcome(operation)

    def reconcile_rollback(
        self,
        *,
        operation,
        step,
        child_ref,
    ) -> object | None:
        if step != "repository" or child_ref.step != "repository":
            raise BootstrapApplyError(
                "repository setup handler can only reconcile repository work"
            )
        state = self._coordinator.status(operation)
        if state.lifecycle_state != "rolled_back":
            return None
        if (
            state.receipt is None
            or child_ref.identifier != state.receipt.receipt_hash
        ):
            raise BootstrapApplyError(
                "repository rollback child reference does not match state"
            )
        return self._rollback_outcome(operation)

    @staticmethod
    def _rollback_outcome(operation) -> object:
        from foundry_opt.bootstrap.runner import BootstrapStageOutcome

        remaining = tuple(
            item for item in operation.child_refs if item.step != "repository"
        )
        return BootstrapStageOutcome(
            stage="rolled_back",
            note="Rolled back the reviewed repository bootstrap changes.",
            child_refs=remaining,
        )


def build_repository_plan_input(
    operation,
) -> tuple[BootstrapPlanInput, tuple[str, ...]]:
    repository_root = Path(operation.repository_binding.repository_root)
    discovered = {
        item.repo_agent_id.casefold(): item
        for item in operation.selection_plan.discovered_agents
    }
    targets = {
        item.repo_agent_id.casefold(): item.reviewed_target
        for item in operation.foundry_targets
    }
    verification = {
        item.repo_agent_id.casefold(): item.choice
        for item in operation.verification_choices
    }
    intents = tuple(
        item
        for item in operation.registration_intents
        if item.intent != "ignore"
    )
    if not intents:
        raise BootstrapConfigError(
            "repository setup requires at least one registered agent"
        )
    selected_agents: list[dict[str, object]] = []
    render_contexts: list[dict[str, object]] = []
    assumptions: list[str] = []
    for intent in intents:
        candidate = discovered[intent.repo_agent_id.casefold()]
        managed_root = _managed_root(candidate.root, candidate.source_root)
        config_path = (
            candidate.config_path
            if (
                candidate.config_path is not None
                and PurePosixPath(candidate.config_path).name
                == "foundry-opt.yaml"
            )
            else f"{managed_root}/.foundry/foundry-opt.yaml"
        )
        target = targets.get(intent.repo_agent_id.casefold())
        profile = _selected_profile(
            repository_root,
            repo_agent_id=intent.repo_agent_id,
            managed_root=managed_root,
            config_path=config_path,
            package_root=candidate.package_root,
            target=target,
            verification_choice=verification.get(
                intent.repo_agent_id.casefold()
            ),
            enabled=intent.intent == "register_enabled",
            assumptions=assumptions,
        )
        selected: dict[str, object] = {
            "repo_agent_id": intent.repo_agent_id,
            "root": candidate.root,
            "discovery_root": candidate.root,
            "config_path": config_path,
            "editable_paths": (f"{managed_root}/**",),
            "enabled": intent.intent == "register_enabled",
        }
        if target is not None:
            selected["foundry_target"] = target.model_dump(mode="json")
        if profile is not None:
            selected["profile"] = profile.model_dump(mode="json")
        selected_agents.append(selected)
        render_contexts.append(
            {
                "repo_agent_id": intent.repo_agent_id,
                "values": (
                    {"key": "selectedRoot", "value": managed_root},
                ),
            }
        )
    manifest = TrustedTemplateManifest.load_pinned_manifest()
    lock_sha = _runtime_lock_sha256()
    plan_input = BootstrapPlanInput.model_validate(
        {
            "repository": {
                "repository_id": operation.repository_binding.repository_id,
                "repository_url": operation.repository_binding.repository_url,
                "default_branch": _default_branch(
                    repository_root,
                    operation.repository_binding.branch_name,
                ),
                "root": ".",
                "selected_agents": selected_agents,
            },
            "runtime_provenance": {
                "runtime_repository_url": operation.runtime_binding.runtime_repository,
                "runtime_commit": operation.runtime_binding.runtime_commit,
                "uv_lock_sha256": lock_sha,
            },
            "repository_phase": {
                "trusted_manifest_id": manifest.manifest_id,
                "trusted_manifest_version": manifest.manifest_version,
                "trusted_manifest_hash": manifest.manifest_hash,
                "agent_render_contexts": render_contexts,
            },
            "offline_plan": True,
            "required_phases": ("repository",),
        }
    )
    return plan_input, tuple(sorted(set(assumptions), key=str.casefold))


def _selected_profile(
    repository_root: Path,
    *,
    repo_agent_id: str,
    managed_root: str,
    config_path: str,
    package_root: str,
    target,
    verification_choice: str | None,
    enabled: bool,
    assumptions: list[str],
) -> SelectedAgentProfile | None:
    profile_path = repository_root / config_path
    existing: BootstrapSidecar | None = None
    if profile_path.is_file():
        existing = BootstrapSidecar.from_document(
            profile_path.read_text(encoding="utf-8")
        )
        if existing.repo_agent_id != repo_agent_id:
            raise BootstrapConfigError(
                "existing profile repo_agent_id does not match the selected agent"
            )
    if existing is None and not enabled:
        return None
    if existing is not None:
        payload = existing.model_dump(mode="json")
        for key in (
            "schema_version",
            "repo_agent_id",
            "source_root",
            "editable_paths",
        ):
            payload.pop(key, None)
        profile = SelectedAgentProfile.model_validate(payload)
    else:
        if (
            target is None
            or target.state == "blocked"
            or target.project_endpoint is None
            or target.agent_name is None
            or target.account_resource_id is None
        ):
            raise BootstrapConfigError(
                f"enabled agent {repo_agent_id} requires a resolved Foundry target before a quick profile can be created"
            )
        runtime, runtime_assumption = _infer_runtime(
            repository_root,
            package_root,
        )
        model = _infer_model(repository_root, managed_root)
        assumptions.extend((runtime_assumption, f"{repo_agent_id}: model deployment `{model}`"))
        profile = SelectedAgentProfile(
            package_root=package_root,
            runtime=runtime,
            foundry_project=FoundryProjectSettings(
                project_endpoint=target.project_endpoint,
                account_resource_id=target.account_resource_id,
                agent_name=target.agent_name,
                expected_version=target.latest_agent_version,
                model_deployment_aliases=(model,),
            ),
            foundry_target=target,
            baseline_model=model,
            allowed_models=(model,),
            min_candidates=1,
            max_candidates=2,
            primary_metric="quality",
            decision_policy=DecisionPolicy(
                minimum_aggregate_delta=0.01,
                focused_cases_required=True,
                max_regressions=0,
            ),
            hard_guardrails=(
                HardGuardrail(
                    evaluator_name="safety",
                    required_pass_rate=1.0,
                    required=True,
                ),
            ),
            deployment=DeploymentSettings(
                environment="foundry-production",
                enabled=True,
                require_aligned_binding=False,
            ),
            verification=VerificationSettings(),
        )
    verification_settings = _verification_settings(
        profile.verification,
        verification_choice,
        repo_agent_id=repo_agent_id,
    )
    deployment = profile.deployment.model_copy(
        update={"enabled": enabled}
    )
    return profile.model_copy(
        update={
            "deployment": deployment,
            "verification": verification_settings,
        }
    )


def _verification_settings(
    existing: VerificationSettings,
    choice: str | None,
    *,
    repo_agent_id: str,
) -> VerificationSettings:
    if choice in (None, "preserve_existing"):
        return existing
    if choice in {"defer_to_issue", "no_evidence"}:
        return VerificationSettings(
            mode="off",
            evaluation_gate_policy="allow_no_evidence",
        )
    if choice == "repository_checks":
        if not existing.repository_checks:
            raise BootstrapConfigError(
                f"{repo_agent_id} has no existing repository checks to preserve"
            )
        return VerificationSettings(
            mode="optional",
            repository_checks=existing.repository_checks,
            evaluation_gate_policy="allow_repository_checks",
        )
    raise BootstrapConfigError("unsupported verification choice")


def _infer_runtime(
    repository_root: Path,
    package_root: str,
) -> tuple[RuntimeProtocolSettings, str]:
    root = (
        repository_root
        if package_root == "."
        else repository_root.joinpath(*PurePosixPath(package_root).parts)
    )
    entrypoint = next(
        (
            ("python", name)
            for name in ("main.py", "app.py", "agent.py")
            if (root / name).is_file()
        ),
        None,
    )
    if entrypoint is None:
        raise BootstrapConfigError(
            f"quick profile could not infer a Python entrypoint under {package_root}"
        )
    return (
        RuntimeProtocolSettings(
            kind="hosted",
            runtime="python_3_13",
            entrypoint=entrypoint,
            dependency_resolution="remote_build",
            protocol_name="responses",
            protocol_version="2.0.0",
            cpu="1",
            memory="2Gi",
            model_environment_variable="AZURE_AI_MODEL_DEPLOYMENT_NAME",
        ),
        f"{package_root}: hosted Python 3.13 with `{' '.join(entrypoint)}`",
    )


def _infer_model(repository_root: Path, managed_root: str) -> str:
    candidates = (
        repository_root / managed_root / "azure.yaml",
        repository_root / "azure.yaml",
        repository_root / managed_root / ".env",
        repository_root / managed_root / ".env.example",
    )
    keys = {
        "azure_ai_model_deployment_name",
        "model_deployment",
        "model_deployment_name",
        "baseline_model",
    }
    values: set[str] = set()
    for path in candidates:
        if not path.is_file():
            continue
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:
            document = None
        _collect_named_values(document, keys, values)
        if path.name.startswith(".env"):
            for line in path.read_text(encoding="utf-8").splitlines():
                if "=" not in line or line.lstrip().startswith("#"):
                    continue
                key, value = line.split("=", 1)
                if key.strip().casefold() in keys and value.strip():
                    values.add(value.strip().strip("'\""))
    return next(iter(values)) if len(values) == 1 else _DEFAULT_MODEL


def _collect_named_values(
    value: object,
    keys: set[str],
    results: set[str],
) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if str(key).casefold() in keys and isinstance(child, str) and child:
                results.add(child)
            else:
                _collect_named_values(child, keys, results)
    elif isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    ):
        for child in value:
            _collect_named_values(child, keys, results)


def _managed_root(discovery_root: str, source_root: str) -> str:
    if discovery_root != ".":
        return discovery_root
    if source_root == ".":
        raise BootstrapConfigError(
            "repository-root discovery requires a concrete source root"
        )
    return source_root


def _runtime_lock_sha256() -> str:
    value = os.environ.get("FOUNDRY_OPT_RUNTIME_LOCK_SHA256", "").strip()
    if len(value) == 64 and all(character in "0123456789abcdef" for character in value):
        return value
    raise BootstrapConfigError(
        "verified runtime lock SHA-256 is unavailable"
    )


def _default_branch(repository_root: Path, current_branch: str | None) -> str:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(repository_root),
            "symbolic-ref",
            "--quiet",
            "--short",
            "refs/remotes/origin/HEAD",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode == 0:
        value = completed.stdout.strip()
        if value.startswith("origin/"):
            return value.removeprefix("origin/")
    return current_branch or "main"


def _install_local_state_excludes(repository_root: Path) -> None:
    git_directory = subprocess.run(
        [
            "git",
            "-C",
            str(repository_root),
            "rev-parse",
            "--git-dir",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    git_path = Path(git_directory)
    if not git_path.is_absolute():
        git_path = repository_root / git_path
    exclude_path = git_path / "info" / "exclude"
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = (
        exclude_path.read_text(encoding="utf-8")
        if exclude_path.is_file()
        else ""
    )
    lines = existing.splitlines()
    additions = (
        ".foundry-opt/journal/",
        ".foundry-opt/receipts/",
    )
    changed = False
    for value in additions:
        if value not in lines:
            lines.append(value)
            changed = True
    if changed:
        exclude_path.write_text(
            "\n".join(lines).rstrip("\n") + "\n",
            encoding="utf-8",
        )


def _enabled_agent_ids(operation) -> tuple[str, ...]:
    return tuple(
        item.repo_agent_id
        for item in operation.registration_intents
        if item.intent == "register_enabled"
    )


def _deployable_agent_ids(
    operation,
    enabled: Sequence[str],
) -> tuple[str, ...]:
    targets = {
        item.repo_agent_id.casefold(): item.reviewed_target
        for item in operation.foundry_targets
    }
    return tuple(
        repo_agent_id
        for repo_agent_id in enabled
        if (
            repo_agent_id.casefold() in targets
            and targets[repo_agent_id.casefold()].deployment_ready
        )
    )


__all__ = [
    "BootstrapRepositorySetupHandler",
    "RepositorySetupCoordinator",
    "RepositorySetupReview",
    "RepositorySetupStateEnvelope",
    "build_repository_plan_input",
]
