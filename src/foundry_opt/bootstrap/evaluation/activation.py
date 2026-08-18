"""Receipt-bound post-activation repository finalization.

The per-agent sidecar and the registry `enabled` flag are the only repository mutations that
depend on a *successful* cloud evaluation activation, so they are deliberately excluded from
the repository apply phase and performed here instead.

The sidecar is derived, never pre-authored: static policy comes from the approved onboarding
contract, and every immutable identifier, lineage hash, and activation measurement comes from
the provider receipt/state produced by the staged onboarding machine. The mutation is bound
to the parent plan hash, the single phase approval hash, the provider receipt hash, the exact
runtime SHA, and the finalization payload hash. No second human approval is required for an
auto-adopted generated rubric, but every dynamic output must satisfy the pre-approved bounds
and fail-closed gates first.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Literal

import yaml
from pydantic import ValidationError

from foundry_opt.bootstrap.canonical import canonical_sha256
from foundry_opt.bootstrap.contracts import (
    ActivationBinding,
    ActivationOutcomeRecord,
    BootstrapDocument,
    BootstrapLock,
    BootstrapPlan,
    BootstrapSidecar,
    DefaultEvaluatorBundle,
    EvaluationLineage,
    EvaluatorReference,
    ExplicitAgentEntry,
    ImmutableDatasetReference,
    ImmutableDefinitionReference,
    ManagedFileEntry,
    ResolvedEvaluator,
    ResolvedWeightedObjective,
    RootRegistry,
)
from foundry_opt.bootstrap.errors import BootstrapApplyError, BootstrapConfigError
from foundry_opt.bootstrap.evaluation.core import (
    ActivationCleanup,
    ActivationReceipt,
    ActivationRun,
    EvaluatorLifecycleResult,
    ReplacementOperation,
    choose_default_evaluator_bundle,
)
from foundry_opt.bootstrap.evaluation.execution import (
    EvaluationFinalization,
    EvaluationOnboardingRequest,
    ONBOARDING_ACTION_KIND,
    ONBOARDING_STAGES,
    finalization_binding_hash,
)
from foundry_opt.bootstrap.input_contracts import BootstrapPlanInput
from foundry_opt.bootstrap.operation_state import (
    EvaluationAgentReplacement,
    OperationStateEnvelope,
    next_generation,
    operation_directory,
    write_operation_state,
)
from foundry_opt.bootstrap.receipts import PhaseReceipt
from foundry_opt.bootstrap.repository.engine import LOCK_PATH, atomic_write_bytes

REGISTRY_PATH = ".foundry-opt/registry.yaml"
_FINALIZE_JOURNAL = "sidecar-activation.json"


class SidecarActivationEntry(BootstrapDocument):
    repo_agent_id: str
    status: Literal["activated", "stopped"]
    path: str | None = None
    previous_sha256: str | None = None
    applied_sha256: str | None = None
    previous_bundle_objective_hash: str | None = None
    activated_bundle_objective_hash: str | None = None
    retained_bundle_objective_hash: str | None = None
    lineage_hash: str | None = None
    lifecycle_status: str | None = None
    finalization_hash: str | None = None
    finalization_binding_hash: str | None = None
    detail: str | None = None


class SidecarActivationReceipt(BootstrapDocument):
    operation_id: str
    repository_identity: str
    runtime_repository: str
    runtime_commit: str
    plan_hash: str
    approval_hash: str
    activation_receipt_hash: str
    entries: tuple[SidecarActivationEntry, ...]
    registry_path: str = REGISTRY_PATH
    registry_sha256: str | None = None
    lock_sha256: str | None = None
    enabled_agent_ids: tuple[str, ...] = ()
    finalize_hash: str

    @classmethod
    def create(cls, **values: object) -> "SidecarActivationReceipt":
        payload = {key: value for key, value in values.items() if key != "finalize_hash"}
        draft = cls.model_construct(**payload, finalize_hash="0" * 64)
        body = draft.model_dump(mode="json", exclude={"finalize_hash"})
        return cls.model_validate({**body, "finalize_hash": canonical_sha256(body)})


@dataclass(frozen=True, slots=True)
class _AgentFinalization:
    repo_agent_id: str
    path: str
    document_bytes: bytes
    previous_sha256: str | None
    applied_sha256: str
    previous_bundle_objective_hash: str | None
    activated_bundle_objective_hash: str | None
    retained_bundle_objective_hash: str | None
    lineage_hash: str
    lifecycle_status: str
    finalization_hash: str
    binding_hash: str
    replaced: bool


def _sha256_bytes(data: bytes) -> str:
    return sha256(data).hexdigest()


def _read_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise BootstrapApplyError(f"managed file could not be read: {path}") from exc


def _repository_target(root: Path, relative: str) -> Path:
    resolved_root = root.resolve()
    target = (resolved_root / relative).resolve()
    try:
        target.relative_to(resolved_root)
    except ValueError as exc:
        raise BootstrapApplyError("managed path escapes the repository root") from exc
    return target


def _phase_plan(envelope: OperationStateEnvelope, phase: str = "evaluations") -> BootstrapPlan:
    actions = tuple(action for action in envelope.bootstrap_plan.actions if action.phase == phase)
    return BootstrapPlan.create(
        operation_id=envelope.operation_id,
        runtime_repository=envelope.runtime_repository,
        runtime_commit=envelope.runtime_commit,
        repository_identity=envelope.bootstrap_plan.repository_identity,
        actions=actions,
    )


def _applied_evaluations_receipt(envelope: OperationStateEnvelope) -> PhaseReceipt:
    receipt = next((item for item in envelope.phase_receipts if item.phase == "evaluations"), None)
    if receipt is None:
        raise BootstrapApplyError("sidecar activation requires an applied evaluations phase receipt")
    if receipt.state != "applied":
        raise BootstrapApplyError(
            f"sidecar activation requires a successful evaluations phase; current state is {receipt.state}"
        )
    if receipt.parent_plan_hash != envelope.bootstrap_plan.plan_hash:
        raise BootstrapApplyError("evaluations receipt does not match the active plan")
    phase_plan = _phase_plan(envelope)
    if receipt.phase_plan_hash != phase_plan.plan_hash or receipt.receipt.plan_hash != phase_plan.plan_hash:
        raise BootstrapApplyError("evaluations receipt does not match the approved evaluations phase plan")
    if receipt.receipt.operation_id != envelope.operation_id:
        raise BootstrapApplyError("evaluations receipt operation id mismatch")
    if receipt.receipt.repository_identity != envelope.bootstrap_plan.repository_identity:
        raise BootstrapApplyError("evaluations receipt repository identity mismatch")
    if receipt.receipt.runtime_commit != envelope.runtime_commit:
        raise BootstrapApplyError("evaluations receipt runtime commit mismatch")
    if receipt.receipt.error_info is not None:
        raise BootstrapApplyError("evaluations receipt reports a failure")
    if receipt.approval_hash is None:
        raise BootstrapApplyError("evaluations receipt is not bound to an approval record")
    approval = next(
        (
            item
            for item in envelope.approvals
            if item.phase == "evaluations" and item.approval_hash == receipt.approval_hash
        ),
        None,
    )
    if approval is None or approval.parent_plan_hash != envelope.bootstrap_plan.plan_hash:
        raise BootstrapApplyError("evaluations approval record is missing or stale")
    return receipt


def _approved_contracts(envelope: OperationStateEnvelope) -> dict[str, EvaluationOnboardingRequest]:
    """Recover the approved onboarding contracts from the immutable plan itself."""

    contracts: dict[str, EvaluationOnboardingRequest] = {}
    for action in envelope.bootstrap_plan.actions:
        if action.phase != "evaluations":
            continue
        if action.kind != ONBOARDING_ACTION_KIND or len(action.diagnostics) != 3:
            raise BootstrapApplyError("approved evaluations plan contains a non-composite onboarding action")
        repo_agent_id, contract_hash, contract_json = action.diagnostics
        try:
            contract = EvaluationOnboardingRequest.model_validate(json.loads(contract_json))
        except (json.JSONDecodeError, BootstrapConfigError, ValidationError) as exc:
            raise BootstrapApplyError("approved onboarding contract is invalid") from exc
        if contract.contract_hash != contract_hash or contract.repo_agent_id != repo_agent_id:
            raise BootstrapApplyError("approved onboarding contract does not match its plan identity")
        contracts[action.action_id] = contract
    if not contracts:
        raise BootstrapApplyError("sidecar activation requires at least one applied onboarding action")
    return contracts


def _plan_input_matches(plan_input: BootstrapPlanInput, envelope: OperationStateEnvelope) -> None:
    if plan_input.repository.repository_id.casefold() != envelope.repository_id.casefold():
        raise BootstrapApplyError("plan input repository does not match the recorded operation")
    if plan_input.runtime_provenance.runtime_commit != envelope.runtime_commit:
        raise BootstrapApplyError("plan input runtime commit does not match the recorded operation")
    planned = tuple(
        action.model_dump(mode="json")
        for action in envelope.bootstrap_plan.actions
        if action.phase == "evaluations"
    )
    rebuilt = tuple(
        action.model_dump(mode="json")
        for agent in (plan_input.evaluations_phase.agents if plan_input.evaluations_phase else ())
        if agent.onboarding_contract is not None
        for action in agent.onboarding_contract.composite_action()
    )
    if canonical_sha256(planned) != canonical_sha256(rebuilt):
        raise BootstrapApplyError("plan input onboarding contracts do not rebuild the approved plan")


def _finalizations(receipt: PhaseReceipt) -> dict[str, EvaluationFinalization]:
    """Read the receipt-bound onboarding finalizations out of recorded provider state.

    Agents may be onboarded in different Foundry projects, in which case the phase provider
    state aggregates one receipt-bound document per project.
    """

    state = receipt.provider_state if isinstance(receipt.provider_state, Mapping) else {}
    ledgers: dict[str, object] = {}
    if state.get("multi_project"):
        projects = state.get("projects")
        if not isinstance(projects, Mapping):
            raise BootstrapApplyError("evaluations provider state carries no project entries")
        for project in projects.values():
            if not isinstance(project, Mapping):
                raise BootstrapApplyError("evaluations provider state project entry is invalid")
            project_state = project.get("provider_state")
            onboarding = project_state.get("onboarding") if isinstance(project_state, Mapping) else None
            if isinstance(onboarding, Mapping):
                for action_id, ledger in onboarding.items():
                    if str(action_id) in ledgers:
                        raise BootstrapApplyError("onboarding action was recorded by more than one project")
                    ledgers[str(action_id)] = ledger
    else:
        onboarding = state.get("onboarding")
        if isinstance(onboarding, Mapping):
            ledgers = {str(key): value for key, value in onboarding.items()}
    if not ledgers:
        raise BootstrapApplyError("evaluations provider state carries no onboarding finalization")
    finalizations: dict[str, EvaluationFinalization] = {}
    for action_id, ledger in ledgers.items():
        if not isinstance(ledger, Mapping):
            raise BootstrapApplyError("onboarding provider state ledger is invalid")
        stages = ledger.get("stages")
        if not isinstance(stages, Mapping) or any(stage not in stages for stage in ONBOARDING_STAGES):
            raise BootstrapApplyError("onboarding did not complete every staged step")
        payload = ledger.get("finalization")
        if not isinstance(payload, Mapping):
            raise BootstrapApplyError("onboarding provider state carries no finalization payload")
        try:
            finalizations[str(action_id)] = EvaluationFinalization.model_validate(dict(payload))
        except (BootstrapConfigError, ValidationError) as exc:
            raise BootstrapApplyError("onboarding finalization payload is invalid") from exc
    return finalizations


def _build_bundle(finalization: EvaluationFinalization) -> DefaultEvaluatorBundle:
    objective = ResolvedWeightedObjective.create(
        [
            ResolvedEvaluator(
                reference=EvaluatorReference(evaluator_id=item.evaluator_id, provenance=item.provenance),
                normalization=item.normalization,
                weight=item.weight,
            )
            for item in finalization.objective_evaluators
        ]
    )
    if objective.objective_hash != finalization.bundle_objective_hash:
        raise BootstrapApplyError("finalized bundle objective hash does not match the recorded evaluators")
    return DefaultEvaluatorBundle(
        objective=objective,
        datasets=(
            ImmutableDatasetReference(dataset_id=finalization.dataset_for("development").dataset_id),
            ImmutableDatasetReference(dataset_id=finalization.dataset_for("validating").dataset_id),
        ),
        definitions=(
            ImmutableDefinitionReference(definition_id=finalization.definition_for("development").definition_id),
            ImmutableDefinitionReference(definition_id=finalization.definition_for("validating").definition_id),
        ),
    )


def _build_sidecar(
    *,
    contract: EvaluationOnboardingRequest,
    finalization: EvaluationFinalization,
    binding: ActivationBinding,
) -> BootstrapSidecar:
    """Derive the sidecar from approved static policy plus receipt-recorded dynamic ids."""

    policy = contract.sidecar_policy
    assert policy is not None
    bundle = _build_bundle(finalization)
    objective = finalization.objective_evaluators[0]
    lineage = EvaluationLineage(
        split_algorithm_version=finalization.split.algorithm_version,
        split_hash=finalization.split.split_hash,
        split_lineage_hash=finalization.split.split_lineage_hash,
        development_case_count=finalization.split.development_case_count,
        validating_case_count=finalization.split.validating_case_count,
        dataset_strategy=finalization.dataset_strategy,
        generation_context_fingerprint=finalization.generation_context_fingerprint,
        evaluator_provenance=objective.provenance,
        evaluator_generation_operation_id=objective.generation_operation_id,
        bundle_objective_hash=finalization.bundle_objective_hash,
        activation_binding=binding,
    )
    try:
        return BootstrapSidecar(
            repo_agent_id=contract.repo_agent_id,
            source_root=policy.source_root,
            package_root=policy.package_root,
            editable_paths=policy.editable_paths,
            runtime=policy.runtime,
            foundry_project=policy.foundry_project,
            baseline_model=policy.baseline_model,
            allowed_models=policy.allowed_models,
            min_candidates=policy.min_candidates,
            max_candidates=policy.max_candidates,
            primary_metric=policy.primary_metric,
            decision_policy=policy.decision_policy,
            development_dataset=ImmutableDatasetReference(dataset_id=finalization.dataset_for("development").dataset_id),
            validating_dataset=ImmutableDatasetReference(dataset_id=finalization.dataset_for("validating").dataset_id),
            development_definition=ImmutableDefinitionReference(definition_id=finalization.definition_for("development").definition_id),
            validating_definition=ImmutableDefinitionReference(definition_id=finalization.definition_for("validating").definition_id),
            default_evaluator_bundle=bundle,
            evaluation_lineage=lineage,
            max_issue_evaluators=policy.max_issue_evaluators,
            hard_guardrails=policy.hard_guardrails,
            deployment=policy.deployment,
        )
    except (BootstrapConfigError, ValidationError) as exc:
        raise BootstrapApplyError(f"derived sidecar is invalid: {exc}") from exc


def _previous_sidecar(path: Path) -> tuple[bytes | None, BootstrapSidecar | None]:
    data = _read_bytes(path)
    if data is None:
        return None, None
    try:
        return data, BootstrapSidecar.from_document(data.decode("utf-8"))
    except (UnicodeDecodeError, BootstrapConfigError, ValidationError) as exc:
        raise BootstrapApplyError("existing sidecar is not a valid managed document") from exc


def _activation_receipt_document(
    *,
    finalization: EvaluationFinalization,
    envelope: OperationStateEnvelope,
    provider_receipt_hash: str,
) -> ActivationReceipt:
    runs = [
        ActivationRun(
            phase=case.phase,
            evaluator_id=case.evaluator_id,
            executable=case.executable,
            score=case.score,
            normalization_kind=case.normalization_kind,
            source_min=case.source_min,
            source_max=case.source_max,
            passed=bool(case.pass_rate == 1.0) if case.normalization_kind == "pass_fail" else None,
        )
        for case in finalization.activation.cases
    ]
    return ActivationReceipt(
        attempted=True,
        activated=True,
        status="succeeded",
        operation_id=envelope.operation_id,
        runtime_repository=envelope.runtime_repository,
        runtime_commit=envelope.runtime_commit,
        repository_identity=envelope.bootstrap_plan.repository_identity,
        bundle_objective_hash=finalization.bundle_objective_hash,
        split_lineage_hash=finalization.split.split_lineage_hash,
        development_definition_id=finalization.definition_for("development").definition_id,
        validating_definition_id=finalization.definition_for("validating").definition_id,
        runs=runs,
        cleanup=ActivationCleanup(completed=finalization.activation.cleanup_completed),
        detail=provider_receipt_hash,
    )


def _lifecycle(
    *,
    contract: EvaluationOnboardingRequest,
    finalization: EvaluationFinalization,
    envelope: OperationStateEnvelope,
    previous: BootstrapSidecar | None,
    provider_receipt_hash: str,
) -> EvaluatorLifecycleResult:
    definitions = (
        ImmutableDefinitionReference(definition_id=finalization.definition_for("development").definition_id),
        ImmutableDefinitionReference(definition_id=finalization.definition_for("validating").definition_id),
    )
    development_dataset = ImmutableDatasetReference(dataset_id=finalization.dataset_for("development").dataset_id)
    validating_dataset = ImmutableDatasetReference(dataset_id=finalization.dataset_for("validating").dataset_id)
    explicit_replace = contract.replacement is not None
    existing_bundle = previous.default_evaluator_bundle if previous is not None else None
    if explicit_replace and existing_bundle is None:
        raise BootstrapApplyError("explicit replacement requires an existing active sidecar bundle")
    if not explicit_replace and existing_bundle is not None:
        raise BootstrapApplyError(
            "an active sidecar already exists; use bootstrap evaluation replace for an explicit replacement"
        )
    operation = ReplacementOperation(
        operation_id=envelope.operation_id,
        runtime_repository=envelope.runtime_repository,
        runtime_commit=envelope.runtime_commit,
        repository_identity=envelope.bootstrap_plan.repository_identity,
    )
    try:
        return choose_default_evaluator_bundle(
            existing_bundle=existing_bundle,
            generated_bundle=_build_bundle(finalization),
            canonical_split_lineage_hash=finalization.split.split_lineage_hash,
            definitions=definitions,
            development_dataset=development_dataset,
            validating_dataset=validating_dataset,
            persisted_split_lineage_hash=finalization.split.split_lineage_hash,
            explicit_replace=explicit_replace,
            operation=operation,
            activation_receipt=_activation_receipt_document(
                finalization=finalization,
                envelope=envelope,
                provider_receipt_hash=provider_receipt_hash,
            ),
        )
    except BootstrapConfigError as exc:
        raise BootstrapApplyError(f"evaluation activation lifecycle failed: {exc}") from exc


def _prepare_agent(
    *,
    repository_root: Path,
    contract: EvaluationOnboardingRequest,
    finalization: EvaluationFinalization,
    envelope: OperationStateEnvelope,
    binding: ActivationBinding,
    provider_receipt_hash: str,
) -> _AgentFinalization:
    policy = contract.sidecar_policy
    assert policy is not None
    try:
        finalization.verify_against_contract(contract)
    except BootstrapConfigError as exc:
        raise BootstrapApplyError(f"onboarding finalization violates the approved contract: {exc}") from exc
    binding_hash = finalization_binding_hash(binding=binding, finalization=finalization)
    bound = binding.model_copy(update={"finalization_hash": binding_hash})
    target = _repository_target(repository_root, policy.path)
    previous_bytes, previous_document = _previous_sidecar(target)
    document = _build_sidecar(contract=contract, finalization=finalization, binding=bound)
    rendered = yaml.safe_dump(document.model_dump(mode="json"), sort_keys=False, allow_unicode=False).encode("utf-8")
    applied_sha256 = _sha256_bytes(rendered)
    replay = (
        contract.replacement is None
        and previous_bytes is not None
        and _sha256_bytes(previous_bytes) == applied_sha256
    )
    if contract.replacement is not None:
        if previous_bytes is None:
            raise BootstrapApplyError("explicit replacement requires an existing sidecar document")
        if _sha256_bytes(previous_bytes) != contract.replacement.previous_sidecar_sha256:
            raise BootstrapApplyError("existing sidecar does not match the reviewed replacement preimage")
        assert previous_document is not None
        if previous_document.default_evaluator_bundle.objective.objective_hash != contract.replacement.previous_bundle_objective_hash:
            raise BootstrapApplyError("existing sidecar bundle does not match the reviewed replacement lineage")
    lifecycle = _lifecycle(
        contract=contract,
        finalization=finalization,
        envelope=envelope,
        previous=None if replay else previous_document,
        provider_receipt_hash=provider_receipt_hash,
    )
    if lifecycle.activated_bundle is None:
        raise BootstrapApplyError("evaluation activation did not produce an activated bundle")
    return _AgentFinalization(
        repo_agent_id=contract.repo_agent_id,
        path=policy.path,
        document_bytes=rendered,
        previous_sha256=_sha256_bytes(previous_bytes) if previous_bytes is not None else None,
        applied_sha256=applied_sha256,
        previous_bundle_objective_hash=(
            previous_document.default_evaluator_bundle.objective.objective_hash if previous_document else None
        ),
        activated_bundle_objective_hash=lifecycle.activated_bundle.objective.objective_hash,
        retained_bundle_objective_hash=(
            lifecycle.retained_bundle.objective.objective_hash if lifecycle.retained_bundle else None
        ),
        lineage_hash=lifecycle.lineage_hash,
        lifecycle_status=lifecycle.status,
        finalization_hash=finalization.finalization_hash,
        binding_hash=binding_hash,
        replaced=contract.replacement is not None,
    )


def _update_registry(repository_root: Path, enabled_agent_ids: Sequence[str]) -> tuple[Path, bytes]:
    target = _repository_target(repository_root, REGISTRY_PATH)
    data = _read_bytes(target)
    if data is None:
        raise BootstrapApplyError("registry is missing; apply the repository phase before activation")
    try:
        registry = RootRegistry.from_document(data.decode("utf-8"))
    except (UnicodeDecodeError, BootstrapConfigError, ValidationError) as exc:
        raise BootstrapApplyError("registry is not a valid managed document") from exc
    enabled = {item.casefold() for item in enabled_agent_ids}
    known = {item.agent_id.casefold() for item in registry.agents}
    if not enabled <= known:
        raise BootstrapApplyError("activation cannot enable an agent outside the registry")
    updated = registry.model_copy(
        update={
            "agents": tuple(
                ExplicitAgentEntry(
                    agent_id=item.agent_id,
                    root=item.root,
                    config_path=item.config_path,
                    enabled=item.agent_id.casefold() in enabled,
                )
                for item in registry.agents
            )
        }
    )
    return target, yaml.safe_dump(updated.model_dump(mode="json"), sort_keys=False, allow_unicode=False).encode("utf-8")


def _update_lock(
    repository_root: Path,
    *,
    registry_sha256: str,
    finalizations: Sequence[_AgentFinalization],
) -> tuple[Path, bytes]:
    target = _repository_target(repository_root, LOCK_PATH)
    data = _read_bytes(target)
    if data is None:
        raise BootstrapApplyError("managed lock is missing; apply the repository phase before activation")
    try:
        lock = BootstrapLock.model_validate(json.loads(data.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
        raise BootstrapApplyError("managed lock is not a valid managed document") from exc
    entries = {item.path: item for item in lock.managed_files}
    registry_entry = entries.get(REGISTRY_PATH)
    if registry_entry is None:
        raise BootstrapApplyError("managed lock does not own the registry")
    entries[REGISTRY_PATH] = registry_entry.model_copy(update={"applied_sha256": registry_sha256})
    for finalization in finalizations:
        entries[finalization.path] = ManagedFileEntry(
            path=finalization.path,
            ownership_mode="owned",
            owner_scope="agent",
            template_id="sidecar",
            template_base_sha256=finalization.applied_sha256,
            applied_sha256=finalization.applied_sha256,
        )
    updated = lock.model_copy(
        update={
            "managed_files": tuple(sorted(entries.values(), key=lambda item: item.path)),
            "sidecar_paths": tuple(sorted({*lock.sidecar_paths, *(item.path for item in finalizations)})),
            "last_activation": ActivationOutcomeRecord(outcome="succeeded"),
        }
    )
    return target, json.dumps(updated.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def finalize_evaluation_activation(
    *,
    repository_root: Path,
    plan_input: BootstrapPlanInput,
    envelope: OperationStateEnvelope,
    runtime_commit: str,
    state_root: Path | None = None,
) -> SidecarActivationReceipt:
    """Atomically activate receipt-derived sidecars after a successful evaluations phase."""

    if runtime_commit != envelope.runtime_commit:
        raise BootstrapApplyError("sidecar activation requires the exact recorded runtime SHA")
    if plan_input.evaluations_phase is None:
        raise BootstrapApplyError("sidecar activation requires evaluation phase inputs")
    phase_receipt = _applied_evaluations_receipt(envelope)
    contracts = _approved_contracts(envelope)
    _plan_input_matches(plan_input, envelope)
    executed = {
        *phase_receipt.receipt.created_actions,
        *phase_receipt.receipt.adopted_actions,
        *phase_receipt.receipt.changed_actions,
    }
    for action_id in contracts:
        if action_id not in executed:
            raise BootstrapApplyError(f"onboarding action was not executed: {action_id}")
    if phase_receipt.receipt.skipped_actions:
        raise BootstrapApplyError("evaluation activation cannot skip approved actions")
    finalizations_by_action = _finalizations(phase_receipt)
    if set(finalizations_by_action) != set(contracts):
        raise BootstrapApplyError("recorded onboarding finalizations do not match the approved actions")
    assert phase_receipt.approval_hash is not None
    binding = ActivationBinding(
        operation_id=envelope.operation_id,
        plan_hash=envelope.bootstrap_plan.plan_hash,
        approval_hash=phase_receipt.approval_hash,
        receipt_hash=phase_receipt.receipt.receipt_hash,
        runtime_commit=envelope.runtime_commit,
    )
    prepared: list[_AgentFinalization] = []
    entries: list[SidecarActivationEntry] = []
    for agent in plan_input.evaluations_phase.agents:
        contract = agent.onboarding_contract
        if contract is None:
            raise BootstrapApplyError("evaluation activation requires an approved onboarding contract")
        if contract.stopped:
            entries.append(
                SidecarActivationEntry(
                    repo_agent_id=contract.repo_agent_id,
                    status="stopped",
                    detail=contract.stop_reason,
                )
            )
            continue
        action_id = next(
            (key for key, value in contracts.items() if value.contract_hash == contract.contract_hash),
            None,
        )
        if action_id is None:
            raise BootstrapApplyError("plan input onboarding contract is not part of the approved plan")
        prepared.append(
            _prepare_agent(
                repository_root=repository_root,
                contract=contract,
                finalization=finalizations_by_action[action_id],
                envelope=envelope,
                binding=binding,
                provider_receipt_hash=phase_receipt.receipt.receipt_hash,
            )
        )
    if not prepared:
        raise BootstrapApplyError("no agent reached evaluation activation; nothing may be enabled")
    enabled_ids = tuple(sorted(item.repo_agent_id for item in prepared))
    registry_path, registry_bytes = _update_registry(repository_root, enabled_ids)
    registry_sha256 = _sha256_bytes(registry_bytes)
    lock_path, lock_bytes = _update_lock(repository_root, registry_sha256=registry_sha256, finalizations=prepared)
    journal_path = operation_directory(envelope.repository_id, envelope.operation_id, state_root=state_root) / _FINALIZE_JOURNAL
    journal = {
        "operation_id": envelope.operation_id,
        "plan_hash": envelope.bootstrap_plan.plan_hash,
        "approval_hash": binding.approval_hash,
        "activation_receipt_hash": binding.receipt_hash,
        "runtime_commit": envelope.runtime_commit,
        "state": "prepared",
        "targets": [
            {
                "path": item.path,
                "previous_sha256": item.previous_sha256,
                "applied_sha256": item.applied_sha256,
                "finalization_binding_hash": item.binding_hash,
            }
            for item in prepared
        ],
    }
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(journal_path, json.dumps(journal, sort_keys=True).encode("utf-8"))
    for item in prepared:
        atomic_write_bytes(_repository_target(repository_root, item.path), item.document_bytes)
    atomic_write_bytes(registry_path, registry_bytes)
    atomic_write_bytes(lock_path, lock_bytes)
    atomic_write_bytes(journal_path, json.dumps({**journal, "state": "completed"}, sort_keys=True).encode("utf-8"))
    for item in prepared:
        entries.append(
            SidecarActivationEntry(
                repo_agent_id=item.repo_agent_id,
                status="activated",
                path=item.path,
                previous_sha256=item.previous_sha256,
                applied_sha256=item.applied_sha256,
                previous_bundle_objective_hash=item.previous_bundle_objective_hash,
                activated_bundle_objective_hash=item.activated_bundle_objective_hash,
                retained_bundle_objective_hash=item.retained_bundle_objective_hash,
                lineage_hash=item.lineage_hash,
                lifecycle_status=item.lifecycle_status,
                finalization_hash=item.finalization_hash,
                finalization_binding_hash=item.binding_hash,
            )
        )
    receipt = SidecarActivationReceipt.create(
        operation_id=envelope.operation_id,
        repository_identity=envelope.bootstrap_plan.repository_identity,
        runtime_repository=envelope.runtime_repository,
        runtime_commit=envelope.runtime_commit,
        plan_hash=binding.plan_hash,
        approval_hash=binding.approval_hash,
        activation_receipt_hash=binding.receipt_hash,
        entries=tuple(sorted(entries, key=lambda item: item.repo_agent_id)),
        registry_sha256=registry_sha256,
        lock_sha256=_sha256_bytes(lock_bytes),
        enabled_agent_ids=enabled_ids,
    )
    per_agent = tuple(
        EvaluationAgentReplacement(
            repo_agent_id=item.repo_agent_id,
            active_bundle_id=item.activated_bundle_objective_hash or "",
            candidate_bundle_id=item.activated_bundle_objective_hash or "",
            preserved_bundle_id=item.retained_bundle_objective_hash or item.activated_bundle_objective_hash or "",
            lineage_hash=item.lineage_hash,
            status="activated",
            detail=item.lifecycle_status,
        )
        for item in sorted(prepared, key=lambda entry: entry.repo_agent_id)
    )
    # The legacy single record stays as a compatibility projection of the first agent; every
    # agent's own bundle and lineage is recorded in `evaluator_replacements`.
    replacement = per_agent[0].as_legacy_record()
    updated = next_generation(envelope, evaluator_replacement=replacement, evaluator_replacements=per_agent)
    write_operation_state(updated, expected_generation=envelope.generation, state_root=state_root)
    return receipt


def read_finalize_journal(
    repository_id: str,
    operation_id: str,
    *,
    state_root: Path | None = None,
) -> Mapping[str, object] | None:
    path = operation_directory(repository_id, operation_id, state_root=state_root) / _FINALIZE_JOURNAL
    data = _read_bytes(path)
    if data is None:
        return None
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BootstrapApplyError("sidecar activation journal is invalid") from exc
    if not isinstance(payload, Mapping):
        raise BootstrapApplyError("sidecar activation journal must be a mapping")
    return payload


__all__ = [
    "REGISTRY_PATH",
    "SidecarActivationEntry",
    "SidecarActivationReceipt",
    "finalize_evaluation_activation",
    "read_finalize_journal",
]
