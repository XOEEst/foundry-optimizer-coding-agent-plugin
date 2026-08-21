from __future__ import annotations

from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from foundry_opt.poc.checks import (
    RepositoryCheckResult,
    RepositoryCheckRunnerProtocol,
)
from foundry_opt.poc.candidate import (
    AppliedPatch,
    CandidatePolicyError,
    FinalizedCandidate,
    PreparedCandidate,
)
from foundry_opt.poc.decision import (
    CandidateAssessment,
    CandidateDecisionInput,
    Decision,
    DecisionRules,
    EvaluationSummary,
    decide,
)
from foundry_opt.poc.evidence import (
    RenderedComment,
    render_baseline_update,
    render_candidate_update,
    render_final_recommendation,
)
from foundry_opt.poc.state import (
    BaselineState,
    CandidateState,
    CleanupReceipt,
    ClosureReceipt,
    IssueCommentReceipt,
    JobIdentity,
    JobState,
    JobStateStore,
    ProjectionReceipt,
)
from foundry_opt.poc.verification import (
    VerificationResolution,
    verification_mode_blocker,
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ControllerError(RuntimeError):
    """Base error for the optimize-job POC controller."""


class RunResult(_FrozenModel):
    status: str = Field(pattern=r"^(?:ok|retry|platform_failure)$")
    evaluation: EvaluationSummary | None = None
    draft_id: str | None = Field(default=None, min_length=1, max_length=256)
    reason: str | None = Field(default=None, min_length=1, max_length=512)
    retry_phase: Literal["baseline", "candidate", "validating"] | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "RunResult":
        if self.status == "ok" and self.evaluation is None:
            raise ValueError("successful runs require evaluation evidence")
        if self.status == "retry" and self.reason is None:
            raise ValueError("retryable runs require a reason")
        if self.status == "retry" and self.retry_phase is None:
            raise ValueError("retryable runs require an explicit retry phase")
        if self.status == "platform_failure" and self.reason is None:
            raise ValueError("platform failures require a reason")
        if self.status in {"retry", "platform_failure"} and self.evaluation is not None:
            raise ValueError("non-ok runs cannot carry evaluation evidence")
        if self.status != "retry" and self.retry_phase is not None:
            raise ValueError("retry phases require status='retry'")
        return self


class CleanupResult(_FrozenModel):
    success: bool
    receipt_id: str | None = Field(default=None, min_length=1, max_length=256)
    reason: str | None = Field(default=None, min_length=1, max_length=512)
    retry_phase: Literal["candidate", "validating"] | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "CleanupResult":
        if self.success and self.receipt_id is None:
            raise ValueError("successful cleanup requires a durable receipt")
        if not self.success and self.reason is None:
            raise ValueError("failed cleanup requires a reason")
        if self.retry_phase is not None and not self.success:
            raise ValueError("retryable cleanup receipts require a successful cleanup")
        return self


@runtime_checkable
class CandidateWorkspaceProtocol(Protocol):
    def prepare(
        self,
        candidate_id: str,
        *,
        model: str,
        hypothesis: str,
        parent_id: str | None = None,
    ) -> PreparedCandidate: ...

    def finalize(self, candidate_id: str) -> FinalizedCandidate: ...

    def apply_winner(
        self,
        candidate: str | FinalizedCandidate,
        destination_checkout: Path,
    ) -> AppliedPatch: ...


@runtime_checkable
class FoundryOperations(Protocol):
    def evaluate_baseline(self, identity: JobIdentity) -> RunResult: ...

    def evaluate_candidate(self, candidate: FinalizedCandidate) -> RunResult: ...

    def evaluate_validating(self, candidate: FinalizedCandidate) -> RunResult: ...

    def cleanup_draft(self, draft_id: str) -> CleanupResult: ...


@runtime_checkable
class IssueCommentOperations(Protocol):
    def upsert_comment(self, comment: RenderedComment) -> str: ...


@runtime_checkable
class ClosureOperations(Protocol):
    def signal_no_winner(self, identity: JobIdentity) -> str: ...


class OptimizeJobController:
    """Synchronous controller for the issue-driven Foundry optimize-job POC."""

    def __init__(
        self,
        *,
        store: JobStateStore,
        workspace: CandidateWorkspaceProtocol,
        foundry: FoundryOperations,
        comments: IssueCommentOperations,
        closure: ClosureOperations,
        rules: DecisionRules,
        check_runner: RepositoryCheckRunnerProtocol | None = None,
    ) -> None:
        self._store = store
        self._workspace = workspace
        self._foundry = foundry
        self._comments = comments
        self._closure = closure
        self._rules = rules
        self._check_runner = check_runner

    def start(
        self,
        identity: JobIdentity,
        verification: VerificationResolution | None = None,
    ) -> JobState:
        state = self._store.initialize(identity)
        if verification is not None:
            blocker = verification_mode_blocker(verification)
            if blocker is not None:
                raise ControllerError(blocker)
            if state.verification is None:
                state = self._store.update(
                    lambda current: current.with_verification(verification)
                )
            elif state.verification != verification:
                raise ControllerError("verification resolution is immutable")
        if state.verification_mode == "foundry_evaluation":
            if state.baseline is None or state.baseline.evaluation is None:
                result = self._foundry.evaluate_baseline(identity)
                if result.status == "retry":
                    return self._store.load()
                if result.status != "ok":
                    raise ControllerError(result.reason or "baseline evaluation failed")
                state = self._store.update(
                    lambda current: current.with_baseline(
                        BaselineState(
                            evaluation=result.evaluation,
                            comment_receipt=(
                                None
                                if current.baseline is None
                                else current.baseline.comment_receipt
                            ),
                        )
                    )
                )
        elif state.baseline is None:
            state = self._store.update(
                lambda current: current.with_baseline(BaselineState())
            )
        if state.baseline is None:
            raise ControllerError("baseline state is unavailable")
        if state.baseline.comment_receipt is None:
            comment = render_baseline_update(state)
            receipt = self._record_comment(comment)
            state = self._store.update(
                lambda current: current.with_baseline(
                    current.baseline.model_copy(update={"comment_receipt": receipt})
                )
            )
        return state

    def resume(self) -> JobState:
        return self._store.load()

    def handoff_candidate(
        self,
        candidate_id: str,
        *,
        model: str,
        hypothesis: str,
        parent_id: str | None = None,
    ) -> PreparedCandidate:
        state = self._store.load()
        if state.baseline is None:
            raise ControllerError("baseline must be recorded before candidate handoff")
        if state.terminal_outcome is not None:
            raise ControllerError("cannot hand off a candidate after terminal outcome")
        existing = state.candidate(candidate_id)
        if existing is not None:
            if (
                existing.handoff.model == model
                and existing.handoff.hypothesis == " ".join(hypothesis.split()).strip()
                and existing.handoff.parent_id == parent_id
            ):
                return existing.handoff
            raise ControllerError("candidate handoff already exists with different inputs")
        prepared = self._workspace.prepare(
            candidate_id,
            model=model,
            hypothesis=hypothesis,
            parent_id=parent_id,
        )
        self._store.update(
            lambda current: current.with_candidate(CandidateState(handoff=prepared))
        )
        return prepared

    def complete_candidate(self, candidate_id: str) -> JobState:
        state = self._store.load()
        candidate = state.candidate(candidate_id)
        if candidate is None:
            raise ControllerError("candidate handoff does not exist")
        if candidate.assessment is not None and candidate.comment_receipt is not None:
            updated = self._retry_candidate_cleanup(state, candidate_id)
            return updated
        attempted_platform_failure_cleanup = False
        if candidate.finalized is None and (
            candidate.assessment is None or candidate.assessment.outcome != "invalid"
        ):
            try:
                finalized = self._workspace.finalize(candidate_id)
            except CandidatePolicyError as error:
                assessment = CandidateAssessment(
                    candidate_id=candidate_id,
                    outcome="invalid",
                    reason=str(error),
                    changed_path_count=0,
                )
                state = self._store.update(
                    lambda current: current.with_candidate(
                        current.candidate(candidate_id).model_copy(
                            update={"assessment": assessment}
                        )
                    )
                )
            else:
                state = self._store.update(
                    lambda current: current.with_candidate(
                        current.candidate(candidate_id).model_copy(
                            update={"finalized": finalized}
                        )
                    )
                )
        state = self._store.load()
        candidate = state.candidate(candidate_id)
        if candidate is None:
            raise ControllerError("candidate disappeared from state")
        if candidate.assessment is None and candidate.finalized is not None:
            if state.verification_mode == "foundry_evaluation":
                result = self._foundry.evaluate_candidate(candidate.finalized)
                if result.status == "retry":
                    if result.retry_phase != "candidate":
                        raise ControllerError(
                            "candidate retries must report retry_phase='candidate'"
                        )
                    return self._prepare_candidate_retry(
                        self._store.load(),
                        candidate_id,
                        retry_phase="candidate",
                    )
                if result.status == "platform_failure":
                    assessment = CandidateAssessment(
                        candidate_id=candidate_id,
                        outcome="platform_failure",
                        reason=result.reason or "Platform failure.",
                        changed_path_count=len(candidate.finalized.changed_paths),
                    )
                    state = self._store.update(
                        lambda current: current.with_candidate(
                            current.candidate(candidate_id).model_copy(
                                update={
                                    "assessment": assessment,
                                    "draft_id": result.draft_id,
                                }
                            )
                        )
                    )
                    attempted_platform_failure_cleanup = True
                else:
                    state = self._store.update(
                        lambda current: current.with_candidate(
                            current.candidate(candidate_id).model_copy(
                                update={
                                    "development": result.evaluation,
                                    "draft_id": result.draft_id,
                                }
                            )
                        )
                    )
                    state = self._apply_development_decision(self._store.load())
            elif state.verification_mode == "repository_checks":
                state = self._record_repository_checks(self._store.load(), candidate_id)
            else:
                state = self._record_unverified_candidate(self._store.load(), candidate_id)
        elif candidate.assessment is not None:
            state = self._apply_development_decision(state)
        state = self._store.load()
        candidate = state.candidate(candidate_id)
        if candidate is None or candidate.assessment is None:
            raise ControllerError("candidate assessment could not be recorded")
        if attempted_platform_failure_cleanup:
            state = self._retry_candidate_cleanup(state, candidate_id)
            candidate = state.candidate(candidate_id)
            if candidate is None:
                raise ControllerError("candidate disappeared from state")
            if candidate.assessment is None:
                return state
        if candidate.comment_receipt is None:
            comment = render_candidate_update(state, candidate_id)
            receipt = self._record_comment(comment)
            state = self._store.update(
                lambda current: current.with_candidate(
                    current.candidate(candidate_id).model_copy(
                        update={"comment_receipt": receipt}
                    )
                )
            )
        if attempted_platform_failure_cleanup:
            return self._store.load()
        return self._retry_candidate_cleanup(self._store.load(), candidate_id)

    def finish(self, destination_checkout: Path) -> JobState:
        state = self._store.load()
        state = self._retry_pending_cleanups(state)
        if state.terminal_outcome is not None:
            resumed = self._resume_terminal_state(state, destination_checkout)
            return self._retry_pending_cleanups(resumed)
        if state.verification_mode != "foundry_evaluation":
            if state.completed_candidate_count < state.identity.min_candidates:
                raise ControllerError("minimum candidate count has not been reached")
            state = self._apply_development_decision(state)
            decision = state.decision
            if decision is None:
                raise ControllerError("decision state is unavailable")
            if decision.selected_candidate_id is None:
                state = self._ensure_no_winner(state, decision)
                state = self._ensure_final_comment(state)
                return self._retry_pending_cleanups(self._store.load())
            state = self._project_selected_candidate(
                self._store.load(),
                candidate_id=decision.selected_candidate_id,
                destination_checkout=destination_checkout,
                terminal_outcome=decision.outcome,
            )
            state = self._ensure_final_comment(self._store.load())
            return self._retry_pending_cleanups(self._store.load())
        if state.baseline is None or state.baseline.evaluation is None:
            raise ControllerError("baseline must be recorded before finish")
        if state.completed_candidate_count < state.identity.min_candidates:
            raise ControllerError("minimum candidate count has not been reached")
        state = self._apply_development_decision(state)
        decision = state.decision
        if decision is None:
            raise ControllerError("decision state is unavailable")
        if decision.provisional_winner_id is None:
            state = self._ensure_no_winner(state, decision)
            state = self._ensure_final_comment(state)
            return self._retry_pending_cleanups(self._store.load())
        winner_candidate = state.candidate(decision.provisional_winner_id)
        if winner_candidate is None or winner_candidate.finalized is None:
            raise ControllerError("provisional winner is missing finalized artifacts")
        if winner_candidate.validating is None:
            validating = self._foundry.evaluate_validating(winner_candidate.finalized)
            if validating.status == "retry":
                if validating.retry_phase != "validating":
                    raise ControllerError(
                        "validating retries must report retry_phase='validating'"
                    )
                state = self._prepare_candidate_retry(
                    self._store.load(),
                    decision.provisional_winner_id,
                    retry_phase="validating",
                )
                return self._retry_pending_cleanups(state)
            if validating.status != "ok":
                state = self._record_validating_platform_failure(
                    self._store.load(),
                    decision=decision,
                    draft_id=validating.draft_id,
                    reason=validating.reason or "validating run failed",
                )
                state = self._retry_candidate_cleanup(
                    state,
                    decision.provisional_winner_id,
                )
                if state.terminal_outcome is None:
                    return self._retry_pending_cleanups(state)
                state = self._ensure_final_comment(self._store.load())
                return self._retry_pending_cleanups(self._store.load())
            validating_updates = {"validating": validating.evaluation}
            if validating.draft_id is not None:
                validating_updates["draft_id"] = validating.draft_id
            state = self._store.update(
                lambda current: current.with_candidate(
                    current.candidate(decision.provisional_winner_id).model_copy(
                        update=validating_updates
                    )
                )
            )
        state = self._apply_final_decision(self._store.load())
        decision = state.decision
        if decision is None:
            raise ControllerError("final decision is unavailable")
        if decision.outcome == "winner":
            state = self._project_selected_candidate(
                self._store.load(),
                candidate_id=decision.winner_id,
                destination_checkout=destination_checkout,
                terminal_outcome="winner",
            )
        else:
            state = self._ensure_no_winner(state, decision)
        state = self._ensure_final_comment(self._store.load())
        return self._retry_pending_cleanups(self._store.load())

    def _resume_terminal_state(
        self,
        state: JobState,
        destination_checkout: Path,
    ) -> JobState:
        if state.terminal_outcome in {
            "winner",
            "recommended",
            "proposed_unverified",
        }:
            if state.decision is None or state.selected_candidate_id is None:
                raise ControllerError(
                    "positive terminal state requires a selected candidate"
                )
            if state.terminal_outcome == "winner" and state.final_winner_id is None:
                raise ControllerError("winner terminal state requires a final decision")
            if state.projection_receipt is None:
                state = self._project_selected_candidate(
                    state,
                    candidate_id=state.selected_candidate_id,
                    destination_checkout=destination_checkout,
                    terminal_outcome=state.terminal_outcome,
                )
            if state.final_comment_receipt is None:
                state = self._ensure_final_comment(self._store.load())
            return state
        if state.terminal_outcome == "no_winner":
            if state.decision is None:
                raise ControllerError(
                    "no-winner terminal state requires a final decision"
                )
            if state.no_winner_receipt is None:
                state = self._ensure_no_winner(state, state.decision)
            if state.final_comment_receipt is None:
                state = self._ensure_final_comment(self._store.load())
            return state
        if state.terminal_outcome == "platform_failure":
            if state.decision is None:
                raise ControllerError(
                    "platform-failure terminal state requires a final decision"
                )
            if state.final_comment_receipt is None:
                state = self._ensure_final_comment(self._store.load())
            return state
        return state

    def _apply_development_decision(self, state: JobState) -> JobState:
        if state.verification_mode == "repository_checks":
            decision = self._repository_checks_decision(state)
            return self._store.update(lambda current: _merge_decision(current, decision))
        if state.verification_mode == "none":
            decision = self._unverified_decision(state)
            return self._store.update(lambda current: _merge_decision(current, decision))
        if state.baseline is None or state.baseline.evaluation is None:
            raise ControllerError("baseline is required for candidate comparison")
        decision = decide(
            self._rules,
            state.baseline.evaluation,
            self._decision_inputs(state),
        )
        return self._store.update(lambda current: _merge_decision(current, decision))

    def _apply_final_decision(self, state: JobState) -> JobState:
        if state.baseline is None or state.decision is None:
            raise ControllerError("baseline and provisional decision are required")
        provisional_id = state.decision.provisional_winner_id
        if provisional_id is None:
            return state
        candidate = state.candidate(provisional_id)
        if candidate is None or candidate.validating is None:
            raise ControllerError("validating evidence is not available")
        decision = decide(
            self._rules,
            state.baseline.evaluation,
            self._decision_inputs(state),
            validating=candidate.validating,
            validating_candidate_id=provisional_id,
        )
        updates = {"terminal_outcome": "winner" if decision.outcome == "winner" else "no_winner"}
        return self._store.update(
            lambda current: _merge_decision(current, decision).model_copy(update=updates)
        )

    def _ensure_no_winner(self, state: JobState, decision: Decision) -> JobState:
        if state.no_winner_receipt is None:
            receipt_id = self._closure.signal_no_winner(state.identity)
            state = self._store.update(
                lambda current: current.model_copy(
                    update={
                        "no_winner_receipt": ClosureReceipt(
                            receipt_id=receipt_id,
                            outcome="no_winner",
                        ),
                        "terminal_outcome": "no_winner",
                    }
                )
            )
        else:
            state = self._store.update(
                lambda current: current.model_copy(update={"terminal_outcome": "no_winner"})
            )
        return state

    def _record_validating_platform_failure(
        self,
        state: JobState,
        *,
        decision: Decision,
        draft_id: str | None = None,
        reason: str,
    ) -> JobState:
        candidate_id = decision.provisional_winner_id
        if candidate_id is None:
            raise ControllerError(
                "validating platform failures require a provisional winner"
            )
        failure_decision = decision.model_copy(
            update={
                "outcome": "platform_failure",
                "winner_id": None,
                "selected_candidate_id": None,
                "reason": (
                    f"Validating platform failure for {candidate_id}: "
                    f"{reason}"
                ),
                "validating_candidate_id": candidate_id,
                "validating_passed": None,
            }
        )
        return self._store.update(
            lambda current: self._merge_validating_platform_failure_state(
                current,
                candidate_id=candidate_id,
                decision=failure_decision,
                draft_id=draft_id,
            )
        )

    def _ensure_final_comment(self, state: JobState) -> JobState:
        if state.decision is None:
            raise ControllerError("final decision is not available")
        if state.final_comment_receipt is not None:
            return state
        comment = render_final_recommendation(state, state.decision)
        receipt = self._record_comment(comment)
        return self._store.update(
            lambda current: current.model_copy(update={"final_comment_receipt": receipt})
        )

    def _retry_pending_cleanups(self, state: JobState) -> JobState:
        current = state
        for candidate in current.candidates:
            if candidate.draft_id is None or candidate.cleanup_receipt is not None:
                continue
            current = self._retry_candidate_cleanup(current, candidate.handoff.candidate_id)
        return current

    def _retry_candidate_cleanup(self, state: JobState, candidate_id: str) -> JobState:
        candidate = state.candidate(candidate_id)
        if candidate is None:
            raise ControllerError("candidate handoff does not exist")
        if candidate.draft_id is None or candidate.cleanup_receipt is not None:
            return state
        result = self._foundry.cleanup_draft(candidate.draft_id)
        if not result.success:
            return state
        if result.retry_phase is not None and state.terminal_outcome in {None, "platform_failure"}:
            return self._prepare_candidate_retry(
                state,
                candidate_id,
                retry_phase=result.retry_phase,
            )
        receipt = CleanupReceipt(
            draft_id=candidate.draft_id,
            receipt_id=result.receipt_id,
        )
        return self._store.update(
            lambda current: current.with_candidate(
                current.candidate(candidate_id).model_copy(update={"cleanup_receipt": receipt})
            )
        )

    def _prepare_candidate_retry(
        self,
        state: JobState,
        candidate_id: str,
        *,
        retry_phase: Literal["candidate", "validating"],
    ) -> JobState:
        if state.terminal_outcome in {"winner", "no_winner"}:
            return state

        def mutate(current: JobState) -> JobState:
            candidate = current.candidate(candidate_id)
            if candidate is None:
                raise ControllerError("candidate handoff does not exist")
            updates: dict[str, object | None] = {
                "draft_id": None,
                "cleanup_receipt": None,
            }
            if retry_phase == "candidate":
                updates.update(
                    {
                        "development": None,
                        "validating": None,
                        "assessment": None,
                        "comment_receipt": None,
                    }
                )
            else:
                updates.update({"validating": None})
            updated = current.with_candidate(candidate.model_copy(update=updates))
            return updated.model_copy(
                update={
                    "decision": None,
                    "provisional_winner_id": None,
                    "final_winner_id": None,
                    "selected_candidate_id": None,
                    "final_comment_receipt": None,
                    "projection_receipt": None,
                    "no_winner_receipt": None,
                    "terminal_outcome": None,
                }
            )

        updated = self._store.update(mutate)
        if updated.baseline is None:
            return updated
        return self._apply_development_decision(updated)

    def _merge_validating_platform_failure_state(
        self,
        state: JobState,
        *,
        candidate_id: str,
        decision: Decision,
        draft_id: str | None,
    ) -> JobState:
        updated = _merge_decision(state, decision)
        if draft_id is not None:
            candidate = updated.candidate(candidate_id)
            if candidate is None:
                raise ControllerError("validating platform failure candidate disappeared")
            updated = updated.with_candidate(
                candidate.model_copy(update={"draft_id": draft_id})
            )
        return updated.model_copy(update={"terminal_outcome": "platform_failure"})

    def _project_selected_candidate(
        self,
        state: JobState,
        *,
        candidate_id: str,
        destination_checkout: Path,
        terminal_outcome: str,
    ) -> JobState:
        if state.projection_receipt is None:
            projected = self._workspace.apply_winner(
                candidate_id,
                destination_checkout,
            )
            return self._store.update(
                lambda current: current.model_copy(
                    update={
                        "projection_receipt": ProjectionReceipt(
                            candidate_id=projected.candidate_id,
                            receipt_id=projected.patch_sha256,
                            patch_sha256=projected.patch_sha256,
                        ),
                        "terminal_outcome": terminal_outcome,
                    }
                )
            )
        return self._store.update(
            lambda current: current.model_copy(update={"terminal_outcome": terminal_outcome})
        )

    def _record_repository_checks(
        self,
        state: JobState,
        candidate_id: str,
    ) -> JobState:
        if self._check_runner is None:
            raise ControllerError("repository checks runner is unavailable")
        resolution = state.verification
        if resolution is None or resolution.repository_checks is None:
            raise ControllerError("repository checks resolution is unavailable")
        candidate = state.candidate(candidate_id)
        if candidate is None or candidate.finalized is None:
            raise ControllerError("candidate is not ready for repository checks")
        results = self._check_runner.run_checks(
            candidate.finalized,
            checks=resolution.repository_checks.checks,
        )
        failures = tuple(
            result.spec.render() for result in results if not result.passed
        )
        assessment = CandidateAssessment(
            candidate_id=candidate_id,
            outcome="keep" if not failures else "discard",
            reason=(
                "Kept: all configured repository checks passed."
                if not failures
                else "Discarded: repository checks failed: " + ", ".join(failures) + "."
            ),
            changed_path_count=len(candidate.finalized.changed_paths),
        )
        updated = self._store.update(
            lambda current: current.with_candidate(
                current.candidate(candidate_id).model_copy(
                    update={
                        "repository_checks": results,
                        "assessment": assessment,
                    }
                )
            )
        )
        return self._apply_development_decision(updated)

    def _record_unverified_candidate(
        self,
        state: JobState,
        candidate_id: str,
    ) -> JobState:
        candidate = state.candidate(candidate_id)
        if candidate is None or candidate.finalized is None:
            raise ControllerError("candidate is not ready for proposal selection")
        assessment = CandidateAssessment(
            candidate_id=candidate_id,
            outcome="keep",
            reason=(
                "Kept: no approved verification evidence is available; this candidate"
                " can only be considered as an unverified proposal."
            ),
            changed_path_count=len(candidate.finalized.changed_paths),
        )
        updated = self._store.update(
            lambda current: current.with_candidate(
                current.candidate(candidate_id).model_copy(
                    update={"assessment": assessment}
                )
            )
        )
        return self._apply_development_decision(updated)

    def _repository_checks_decision(self, state: JobState) -> Decision:
        ordered_candidates = sorted(
            state.candidates,
            key=lambda item: item.handoff.candidate_id,
        )
        passing_ids = [
            candidate.handoff.candidate_id
            for candidate in ordered_candidates
            if candidate.assessment is not None
            and candidate.assessment.outcome not in {"invalid", "platform_failure"}
            and candidate.repository_checks
            and all(result.passed for result in candidate.repository_checks)
        ]
        chosen_id = None if not passing_ids else passing_ids[0]
        assessments: list[CandidateAssessment] = []
        for candidate in ordered_candidates:
            assessment = candidate.assessment
            if assessment is None:
                continue
            if assessment.outcome in {"invalid", "platform_failure"}:
                assessments.append(assessment)
                continue
            if not candidate.repository_checks or not all(
                result.passed for result in candidate.repository_checks
            ):
                assessments.append(
                    assessment.model_copy(
                        update={
                            "outcome": "discard",
                            "reason": assessment.reason,
                        }
                    )
                )
                continue
            if chosen_id == candidate.handoff.candidate_id:
                assessments.append(
                    assessment.model_copy(
                        update={
                            "outcome": "keep",
                            "reason": (
                                "Kept: all configured repository checks passed."
                                if len(passing_ids) == 1
                                else "Kept: all configured repository checks passed; selected deterministically by candidate ID order."
                            ),
                        }
                    )
                )
                continue
            assessments.append(
                assessment.model_copy(
                    update={
                        "outcome": "discard",
                        "reason": (
                            f"Discarded: repository checks also passed, but {chosen_id} was selected deterministically by candidate ID order."
                        ),
                    }
                )
            )
        if chosen_id is None:
            return Decision(
                rules=self._rules,
                baseline=None,
                assessments=tuple(assessments),
                provisional_winner_id=None,
                winner_id=None,
                selected_candidate_id=None,
                outcome="no_winner",
                reason="No candidate passed all configured repository checks.",
            )
        return Decision(
            rules=self._rules,
            baseline=None,
            assessments=tuple(assessments),
            provisional_winner_id=None,
            winner_id=None,
            selected_candidate_id=chosen_id,
            outcome="recommended",
            reason=(
                f"Candidate {chosen_id} passed all configured repository checks and is recommended for projection."
                if len(passing_ids) == 1
                else f"Multiple candidates passed all configured repository checks; candidate {chosen_id} was selected deterministically by candidate ID order."
            ),
        )

    def _unverified_decision(self, state: JobState) -> Decision:
        ordered_candidates = sorted(
            state.candidates,
            key=lambda item: item.handoff.candidate_id,
        )
        viable_ids = [
            candidate.handoff.candidate_id
            for candidate in ordered_candidates
            if candidate.assessment is not None
            and candidate.assessment.outcome not in {"invalid", "platform_failure"}
            and candidate.finalized is not None
        ]
        chosen_id = None if not viable_ids else viable_ids[0]
        assessments: list[CandidateAssessment] = []
        for candidate in ordered_candidates:
            assessment = candidate.assessment
            if assessment is None:
                continue
            if assessment.outcome in {"invalid", "platform_failure"}:
                assessments.append(assessment)
                continue
            if chosen_id == candidate.handoff.candidate_id:
                assessments.append(
                    assessment.model_copy(
                        update={
                            "outcome": "keep",
                            "reason": (
                                "Kept: selected as the current unverified proposal."
                                if len(viable_ids) == 1
                                else "Kept: selected deterministically by candidate ID order because no approved verification evidence is available."
                            ),
                        }
                    )
                )
                continue
            assessments.append(
                assessment.model_copy(
                    update={
                        "outcome": "discard",
                        "reason": (
                            f"Discarded: no approved verification evidence is available, so {chosen_id} was selected deterministically by candidate ID order."
                        ),
                    }
                )
            )
        if chosen_id is None:
            return Decision(
                rules=self._rules,
                baseline=None,
                assessments=tuple(assessments),
                provisional_winner_id=None,
                winner_id=None,
                selected_candidate_id=None,
                outcome="no_winner",
                reason="No candidate produced a viable proposal.",
            )
        return Decision(
            rules=self._rules,
            baseline=None,
            assessments=tuple(assessments),
            provisional_winner_id=None,
            winner_id=None,
            selected_candidate_id=chosen_id,
            outcome="proposed_unverified",
            reason=(
                f"Candidate {chosen_id} is the selected unverified proposal because no approved verification evidence is available."
                if len(viable_ids) == 1
                else f"Multiple candidates were completed without approved verification evidence; candidate {chosen_id} was selected deterministically by candidate ID order."
            ),
        )

    def _decision_inputs(self, state: JobState) -> tuple[CandidateDecisionInput, ...]:
        inputs: list[CandidateDecisionInput] = []
        for candidate in state.candidates:
            changed_path_count = (
                0 if candidate.finalized is None else len(candidate.finalized.changed_paths)
            )
            if candidate.assessment is not None and candidate.assessment.outcome == "invalid":
                inputs.append(
                    CandidateDecisionInput(
                        candidate_id=candidate.handoff.candidate_id,
                        changed_path_count=changed_path_count,
                        status="invalid",
                        reason=candidate.assessment.reason,
                    )
                )
                continue
            if candidate.assessment is not None and candidate.assessment.outcome == "platform_failure":
                inputs.append(
                    CandidateDecisionInput(
                        candidate_id=candidate.handoff.candidate_id,
                        changed_path_count=changed_path_count,
                        status="platform_failure",
                        reason=candidate.assessment.reason,
                    )
                )
                continue
            if candidate.development is None:
                continue
            inputs.append(
                CandidateDecisionInput(
                    candidate_id=candidate.handoff.candidate_id,
                    changed_path_count=changed_path_count,
                    status="ok",
                    evaluation=candidate.development,
                )
            )
        return tuple(inputs)

    def _record_comment(self, comment: RenderedComment) -> IssueCommentReceipt:
        receipt_id = self._comments.upsert_comment(comment)
        return IssueCommentReceipt(
            marker_id=comment.marker_id,
            receipt_id=receipt_id,
            body_sha256=comment.body_sha256,
        )


def _merge_decision(state: JobState, decision: Decision) -> JobState:
    updated = state.with_decision(decision)
    for assessment in decision.assessments:
        candidate = updated.candidate(assessment.candidate_id)
        if candidate is None:
            continue
        updated = updated.with_candidate(
            candidate.model_copy(update={"assessment": assessment})
        )
    return updated
