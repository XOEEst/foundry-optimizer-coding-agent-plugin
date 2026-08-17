from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from foundry_opt.bootstrap.canonical import canonical_sha256, safe_persisted_document
from foundry_opt.bootstrap.contracts import BindingAssessment, BootstrapAction, BootstrapPlan, BootstrapReceipt, FingerprintRecord, RedactedStatusInfo
from foundry_opt.bootstrap.discovery import DiscoveryResult, discover_repository_agents
from foundry_opt.bootstrap.errors import BootstrapApplyError
from foundry_opt.bootstrap.operation_state import OperationStateEnvelope, SelectionPlan, next_generation, read_operation_state, status_from_state, write_operation_state
from foundry_opt.bootstrap.providers.foundry import (
    rollback_failure_details as foundry_rollback_failure_details,
)
from foundry_opt.bootstrap.providers.github import (
    rollback_failure_details as github_rollback_failure_details,
)
from foundry_opt.bootstrap.receipts import ApplyPhaseName, ApprovalRecord, EvaluationReplacementRecord, PhaseReceipt, failure_receipt, summarize_receipt

_PHASES: tuple[ApplyPhaseName, ...] = ("repository", "github", "azure", "evaluations")
_SANITIZED_ERROR_CODES = {
    BootstrapApplyError: "apply-invalid",
    ValueError: "provider-invalid",
    RuntimeError: "provider-runtime",
    Exception: "provider-failed",
}


class PhaseDriver(Protocol):
    def live_fingerprints(self, context: Mapping[str, object]) -> Sequence[FingerprintRecord]: ...
    def plan(self, context: Mapping[str, object]) -> Sequence[BootstrapAction]: ...
    def apply(self, phase_plan: BootstrapPlan) -> BootstrapReceipt: ...
    def verify(self, receipt: BootstrapReceipt) -> bool: ...
    def export_provider_state(self, receipt: BootstrapReceipt) -> Mapping[str, object]: ...
    def restore_provider_state(self, mapping: Mapping[str, object]) -> None: ...
    def rollback(self, receipt: BootstrapReceipt) -> None: ...
    def verify_rollback(self, receipt: BootstrapReceipt) -> bool: ...


class BootstrapOrchestrator:
    def __init__(self, *, repository_driver: PhaseDriver, github_driver: PhaseDriver, azure_driver: PhaseDriver, evaluations_driver: PhaseDriver, state_root: Path | None = None) -> None:
        self._drivers: dict[ApplyPhaseName, PhaseDriver] = {
            "repository": repository_driver,
            "github": github_driver,
            "azure": azure_driver,
            "evaluations": evaluations_driver,
        }
        self._state_root = state_root

    def discover(self, repository_root: Path, *, repository_id: str, operation_id: str, runtime_repository: str, runtime_commit: str, binding_evidence_by_root: Mapping[str, Mapping[str, object]] | None = None, approved_shared_sources: Mapping[str, Sequence[str]] | None = None, selected_agents: Sequence[Mapping[str, str] | str] | None = None) -> OperationStateEnvelope:
        result: DiscoveryResult = discover_repository_agents(repository_root, binding_evidence_by_root=binding_evidence_by_root, approved_shared_sources=approved_shared_sources, selected_agents=selected_agents)
        selected = tuple(item["repoAgentId"] if isinstance(item, Mapping) else "" for item in (selected_agents or ()) if isinstance(item, Mapping))
        fingerprints = tuple(FingerprintRecord(label=f"discovery:{agent.root}", sha256=canonical_sha256(agent.model_dump(mode="json"))) for agent in result.agents)
        blockers = tuple(sorted({blocker.detail for agent in result.agents for blocker in agent.blockers}))
        selection = SelectionPlan(repository_root=result.repositoryRoot, selected_agent_ids=selected, binding_assessments=tuple(agent.bindingAssessment for agent in result.agents), discovery_fingerprints=fingerprints, blockers=blockers)
        empty_plan = BootstrapPlan.create(operation_id=operation_id, runtime_repository=runtime_repository, runtime_commit=runtime_commit, repository_identity=repository_id, actions=())
        envelope = OperationStateEnvelope.create(generation=0, repository_id=repository_id, operation_id=operation_id, runtime_repository=runtime_repository, runtime_commit=runtime_commit, selection_plan=selection, bootstrap_plan=empty_plan, discovery_fingerprints=fingerprints)
        write_operation_state(envelope, state_root=self._state_root)
        return envelope

    def build_plan(
        self,
        *,
        repository_id: str,
        operation_id: str,
        runtime_repository: str,
        runtime_commit: str,
        selection_plan: SelectionPlan,
        evaluation_requests: Sequence[Mapping[str, object]] = (),
        evaluator_replacement: EvaluationReplacementRecord | None = None,
        phases: Sequence[ApplyPhaseName] = _PHASES,
    ) -> OperationStateEnvelope:
        if not selection_plan.selected_agent_ids:
            raise BootstrapApplyError("planning requires explicit selected agents")
        requested_phases = tuple(phases)
        if not requested_phases:
            raise BootstrapApplyError("planning requires at least one phase")
        if len(set(requested_phases)) != len(requested_phases):
            raise BootstrapApplyError("planning phases must be unique")
        if any(phase not in _PHASES for phase in requested_phases):
            raise BootstrapApplyError("planning contains an unsupported phase")
        requested_phases = tuple(
            phase for phase in _PHASES if phase in requested_phases
        )
        context = self._build_context(repository_id, operation_id, runtime_repository, runtime_commit, selection_plan, evaluation_requests, evaluator_replacement)
        per_phase_fingerprints: list[FingerprintRecord] = []
        actions: list[BootstrapAction] = []
        for phase in requested_phases:
            phase_context = {**context, "phase": phase}
            phase_live = tuple(self._drivers[phase].live_fingerprints(phase_context))
            per_phase_fingerprints.extend(sorted(phase_live, key=lambda item: (item.label, item.sha256)))
            planned_actions = tuple(self._drivers[phase].plan(phase_context))
            self._validate_phase_actions(phase, planned_actions, selection_plan.selected_agent_ids)
            actions.extend(planned_actions)
        ordered = tuple(sorted(actions, key=lambda action: (_PHASES.index(action.phase), action.target_agent_id or "", action.action_id, action.kind)))
        plan = BootstrapPlan.create(operation_id=operation_id, runtime_repository=runtime_repository, runtime_commit=runtime_commit, repository_identity=repository_id, actions=ordered)
        envelope = OperationStateEnvelope.create(generation=1, repository_id=repository_id, operation_id=operation_id, runtime_repository=runtime_repository, runtime_commit=runtime_commit, selection_plan=selection_plan, bootstrap_plan=plan, discovery_fingerprints=selection_plan.discovery_fingerprints, resource_fingerprints=tuple(per_phase_fingerprints), required_phases=requested_phases, evaluator_replacement=evaluator_replacement)
        write_operation_state(envelope, expected_generation=0, state_root=self._state_root)
        return envelope

    def resume(self, *, repository_id: str, operation_id: str, runtime_commit: str) -> OperationStateEnvelope:
        envelope = read_operation_state(repository_id, operation_id, state_root=self._state_root)
        if envelope.runtime_commit != runtime_commit or envelope.bootstrap_plan.runtime_commit != runtime_commit:
            raise BootstrapApplyError("resume requires the exact runtime commit")
        return envelope

    def apply_phase(self, *, repository_id: str, operation_id: str, phase: ApplyPhaseName, approval: ApprovalRecord, runtime_commit: str) -> PhaseReceipt:
        envelope = self.resume(repository_id=repository_id, operation_id=operation_id, runtime_commit=runtime_commit)
        if phase not in envelope.required_phases:
            raise BootstrapApplyError("phase is not present in the approved plan")
        phase_plan = self._phase_plan(envelope, phase)
        if approval.parent_plan_hash != envelope.bootstrap_plan.plan_hash or approval.phase != phase:
            raise BootstrapApplyError("approval does not match parent plan hash and phase")
        expected_fingerprints = tuple(sorted((item for item in envelope.resource_fingerprints if item.label.startswith(f"{phase}:")), key=lambda item: (item.label, item.sha256)))
        live_fingerprints = tuple(sorted(self._drivers[phase].live_fingerprints(self._build_context_from_envelope(envelope, phase)), key=lambda item: (item.label, item.sha256)))
        if live_fingerprints != expected_fingerprints:
            raise BootstrapApplyError("live fingerprints drifted from planned phase fingerprints")
        applying = PhaseReceipt(phase=phase, state="applying", provider=phase, receipt=self._placeholder_receipt(envelope, phase_plan), parent_plan_hash=envelope.bootstrap_plan.plan_hash, phase_plan_hash=phase_plan.plan_hash, approval_hash=approval.approval_hash, summary="phase applying", provider_state={}, recorded_fingerprints=live_fingerprints)
        updated = next_generation(envelope, approvals=tuple([*envelope.approvals, approval]), phase_receipts=tuple([*{item.phase: item for item in envelope.phase_receipts}.values(), applying]))
        write_operation_state(updated, expected_generation=envelope.generation, state_root=self._state_root)
        envelope = updated
        try:
            receipt = self._drivers[phase].apply(phase_plan)
            if receipt.plan_hash != phase_plan.plan_hash:
                raise BootstrapApplyError(
                    "provider receipt does not match the phase plan"
                )
            provider_state = self._drivers[phase].export_provider_state(receipt)
            verified = self._drivers[phase].verify(receipt)
            if not verified:
                raise BootstrapApplyError("phase verification failed")
            phase_receipt = PhaseReceipt(phase=phase, state="applied", provider=phase, receipt=receipt, parent_plan_hash=envelope.bootstrap_plan.plan_hash, phase_plan_hash=phase_plan.plan_hash, approval_hash=approval.approval_hash, summary=summarize_receipt(receipt), provider_state=provider_state, recorded_fingerprints=live_fingerprints)
            if phase == "evaluations" and envelope.evaluator_replacement is not None:
                replacement = envelope.evaluator_replacement.model_copy(update={"status": "activated"})
                envelope = next_generation(envelope, phase_receipts=self._replace_phase(envelope.phase_receipts, phase_receipt), evaluator_replacement=replacement)
            else:
                envelope = next_generation(envelope, phase_receipts=self._replace_phase(envelope.phase_receipts, phase_receipt))
            write_operation_state(envelope, expected_generation=updated.generation, state_root=self._state_root)
            return phase_receipt
        except Exception as exc:
            code, summary = self._sanitize_error(exc)
            compensation_actions = ()
            original_receipt: BootstrapReceipt | None = None
            provider_state = self._safe_provider_state(locals().get("receipt") if isinstance(locals().get("receipt"), BootstrapReceipt) else None, phase)
            if isinstance(exc, BootstrapApplyError):
                pass
            elif isinstance(exc, BaseException) and hasattr(exc, "args"):
                pass
            rollback_receipt, rollback_state = _rollback_failure_details(exc)
            if rollback_receipt is not None:
                original_receipt = rollback_receipt
                compensation_actions = rollback_receipt.compensation_required_actions
                provider_state = rollback_state
            if 'receipt' in locals() and isinstance(locals().get("receipt"), BootstrapReceipt):
                original_receipt = locals()["receipt"]
                compensation_actions = original_receipt.compensation_required_actions
            failure = failure_receipt(phase=phase, provider=phase, operation_id=envelope.operation_id, runtime_repository=envelope.runtime_repository, runtime_commit=envelope.runtime_commit, repository_identity=envelope.bootstrap_plan.repository_identity, parent_plan_hash=envelope.bootstrap_plan.plan_hash, phase_plan_hash=phase_plan.plan_hash, before_fingerprints=live_fingerprints, code=code, summary=summary, compensation_required_actions=compensation_actions)
            state = "compensation_required" if compensation_actions else "failed"
            failed_receipt = PhaseReceipt(phase=phase, state=state, provider=phase, receipt=original_receipt or failure.receipt, parent_plan_hash=envelope.bootstrap_plan.plan_hash, phase_plan_hash=phase_plan.plan_hash, approval_hash=approval.approval_hash, summary=failure.summary, provider_state=provider_state, recorded_fingerprints=live_fingerprints)
            envelope = next_generation(envelope, phase_receipts=self._replace_phase(envelope.phase_receipts, failed_receipt))
            write_operation_state(envelope, expected_generation=updated.generation, state_root=self._state_root)
            return failed_receipt

    def rollback_phase(self, *, repository_id: str, operation_id: str, phase: ApplyPhaseName, runtime_commit: str) -> PhaseReceipt:
        envelope = self.resume(repository_id=repository_id, operation_id=operation_id, runtime_commit=runtime_commit)
        current = next((item for item in envelope.phase_receipts if item.phase == phase), None)
        if current is None or current.state not in {"applied", "compensation_required"}:
            raise BootstrapApplyError("rollback requires an applied or compensation-required receipt")
        phase_plan = self._phase_plan(envelope, phase)
        if current.parent_plan_hash != envelope.bootstrap_plan.plan_hash or current.phase_plan_hash != phase_plan.plan_hash:
            raise BootstrapApplyError("rollback receipt does not match the active plan")
        if current.receipt.operation_id != envelope.operation_id or current.receipt.repository_identity != envelope.bootstrap_plan.repository_identity:
            raise BootstrapApplyError("rollback receipt identity mismatch")
        self._drivers[phase].restore_provider_state(current.provider_state)
        self._drivers[phase].rollback(current.receipt)
        if not self._drivers[phase].verify_rollback(current.receipt):
            raise BootstrapApplyError("rollback verification failed")
        rolled = current.model_copy(update={"state": "rolled_back", "rollback_summary": f"rolled back {phase}"})
        updated = next_generation(envelope, phase_receipts=self._replace_phase(envelope.phase_receipts, rolled))
        write_operation_state(updated, expected_generation=envelope.generation, state_root=self._state_root)
        return rolled

    def status(self, *, repository_id: str, operation_id: str, runtime_commit: str) -> Mapping[str, object]:
        envelope = self.resume(repository_id=repository_id, operation_id=operation_id, runtime_commit=runtime_commit)
        return status_from_state(envelope).model_dump(mode="json")

    def diff(self, *, current_plan: BootstrapPlan, candidate_plan: BootstrapPlan) -> tuple[str, ...]:
        if current_plan.repository_identity != candidate_plan.repository_identity:
            raise BootstrapApplyError("diff requires the same repository identity")
        current = {action.action_id: action for action in current_plan.actions if action.phase == "repository"}
        candidate = {action.action_id: action for action in candidate_plan.actions if action.phase == "repository"}
        keys = sorted(set(current) | set(candidate))
        return tuple(f"{key}:{canonical_sha256(current[key].model_dump(mode='json')) if key in current else 'missing'}->{canonical_sha256(candidate[key].model_dump(mode='json')) if key in candidate else 'missing'}" for key in keys if current.get(key) != candidate.get(key))

    def _build_context(self, repository_id: str, operation_id: str, runtime_repository: str, runtime_commit: str, selection_plan: SelectionPlan, evaluation_requests: Sequence[Mapping[str, object]], evaluator_replacement: EvaluationReplacementRecord | None) -> Mapping[str, object]:
        payload = {"repository_id": repository_id, "operation_id": operation_id, "runtime_repository": runtime_repository, "runtime_commit": runtime_commit, "selected_agent_ids": list(selection_plan.selected_agent_ids), "binding_assessments": [item.model_dump(mode="json") for item in selection_plan.binding_assessments], "discovery_fingerprints": [item.model_dump(mode="json") for item in selection_plan.discovery_fingerprints], "blockers": list(selection_plan.blockers), "evaluation_contract_hash": canonical_sha256([dict(item) for item in evaluation_requests]), "evaluator_replacement": evaluator_replacement.model_dump(mode="json") if evaluator_replacement else None}
        safe_persisted_document(payload)
        return payload

    def _build_context_from_envelope(self, envelope: OperationStateEnvelope, phase: ApplyPhaseName) -> Mapping[str, object]:
        return self._build_context(envelope.repository_id, envelope.operation_id, envelope.runtime_repository, envelope.runtime_commit, envelope.selection_plan, (), envelope.evaluator_replacement) | {"phase": phase, "parent_plan_hash": envelope.bootstrap_plan.plan_hash}

    def _phase_plan(self, envelope: OperationStateEnvelope, phase: ApplyPhaseName) -> BootstrapPlan:
        actions = tuple(action for action in envelope.bootstrap_plan.actions if action.phase == phase)
        return BootstrapPlan.create(operation_id=envelope.operation_id, runtime_repository=envelope.runtime_repository, runtime_commit=envelope.runtime_commit, repository_identity=envelope.bootstrap_plan.repository_identity, actions=actions)

    def _validate_phase_actions(self, phase: ApplyPhaseName, actions: Sequence[BootstrapAction], selected_agent_ids: Sequence[str]) -> None:
        allowed = {item.casefold() for item in selected_agent_ids}
        for action in actions:
            if action.phase != phase:
                raise BootstrapApplyError("driver returned action for wrong phase")
            if action.target_agent_id is None:
                continue
            if action.target_agent_id.casefold() not in allowed:
                raise BootstrapApplyError("phase action targets unselected agent")

    def _placeholder_receipt(self, envelope: OperationStateEnvelope, phase_plan: BootstrapPlan) -> BootstrapReceipt:
        return BootstrapReceipt.create(operation_id=envelope.operation_id, runtime_repository=envelope.runtime_repository, runtime_commit=envelope.runtime_commit, repository_identity=envelope.bootstrap_plan.repository_identity, plan_hash=phase_plan.plan_hash, error_info=RedactedStatusInfo(code="phase-applying", summary="phase applying"))

    def _replace_phase(self, receipts: Sequence[PhaseReceipt], updated: PhaseReceipt) -> tuple[PhaseReceipt, ...]:
        items = {item.phase: item for item in receipts}
        items[updated.phase] = updated
        return tuple(items[key] for key in sorted(items))

    def _safe_provider_state(self, receipt: BootstrapReceipt | None, phase: ApplyPhaseName) -> Mapping[str, object]:
        if receipt is None:
            return {}
        safe = self._drivers[phase].export_provider_state(receipt)
        safe_persisted_document(safe)
        return safe

    def _sanitize_error(self, exc: Exception) -> tuple[str, str]:
        for error_type, code in _SANITIZED_ERROR_CODES.items():
            if isinstance(exc, error_type):
                return code, type(exc).__name__[:64]
        return "provider-failed", "Exception"


def _rollback_failure_details(
    exc: BaseException,
) -> tuple[BootstrapReceipt | None, Mapping[str, object]]:
    for resolver in (
        foundry_rollback_failure_details,
        github_rollback_failure_details,
    ):
        receipt, state = resolver(exc)
        if receipt is not None:
            return receipt, state
    return None, {}
