from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GuardrailResult(_FrozenModel):
    name: str = Field(min_length=1, max_length=128)
    passed: bool
    score: float | None = Field(default=None, ge=0)


class EvaluationSummary(_FrozenModel):
    run_kind: Literal["development", "validating"]
    successful: bool = True
    primary_score: float | None = Field(default=None, ge=0)
    focused_cases_improved: int = Field(default=0, ge=0)
    focused_cases_regressed: int = Field(default=0, ge=0)
    token_count: int | None = Field(default=None, ge=0)
    latency_ms: float | None = Field(default=None, ge=0)
    foundry_version: str = Field(min_length=1, max_length=256)
    evaluation_link: str = Field(min_length=1, max_length=2048)
    guardrails: tuple[GuardrailResult, ...] = ()

    @model_validator(mode="after")
    def validate_guardrail_names(self) -> "EvaluationSummary":
        names = [result.name for result in self.guardrails]
        if len(names) != len(set(names)):
            raise ValueError("guardrail names must be unique")
        return self


class CandidateDecisionInput(_FrozenModel):
    candidate_id: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    changed_path_count: int = Field(ge=0)
    evaluation: EvaluationSummary | None = None
    status: Literal["ok", "invalid", "platform_failure"] = "ok"
    reason: str | None = Field(default=None, min_length=1, max_length=512)

    @model_validator(mode="after")
    def validate_shape(self) -> "CandidateDecisionInput":
        if self.status == "ok" and self.evaluation is None:
            raise ValueError("status='ok' candidates require evaluation data")
        if self.status != "ok" and self.evaluation is not None:
            raise ValueError("non-ok candidates cannot carry evaluation data")
        if self.status != "ok" and self.reason is None:
            raise ValueError("non-ok candidates require a reason")
        return self


class GuardrailRule(_FrozenModel):
    name: str = Field(min_length=1, max_length=128)
    minimum_score: float | None = Field(default=None, ge=0)
    require_pass: bool = True


class DecisionRules(_FrozenModel):
    aggregate_min_delta: float
    min_focused_cases_improved: int = Field(ge=0)
    max_focused_regressions: int = Field(ge=0)
    guardrails: tuple[GuardrailRule, ...] = ()

    @model_validator(mode="after")
    def validate_guardrails(self) -> "DecisionRules":
        names = [rule.name for rule in self.guardrails]
        if len(names) != len(set(names)):
            raise ValueError("guardrail rule names must be unique")
        return self


class CandidateAssessment(_FrozenModel):
    candidate_id: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    outcome: Literal[
        "keep",
        "discard",
        "invalid",
        "platform_failure",
        "winner",
        "recommended",
        "proposed_unverified",
    ]
    reason: str = Field(min_length=1, max_length=512)
    primary_score: float | None = Field(default=None, ge=0)
    aggregate_delta: float | None = None
    focused_cases_improved: int | None = Field(default=None, ge=0)
    focused_cases_regressed: int | None = Field(default=None, ge=0)
    token_count: int | None = Field(default=None, ge=0)
    latency_ms: float | None = Field(default=None, ge=0)
    changed_path_count: int = Field(ge=0)
    guardrail_failures: tuple[str, ...] = ()
    validating_passed: bool | None = None


class Decision(_FrozenModel):
    rules: DecisionRules
    baseline: EvaluationSummary | None = None
    assessments: tuple[CandidateAssessment, ...]
    provisional_winner_id: str | None = Field(default=None, pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    winner_id: str | None = Field(default=None, pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    selected_candidate_id: str | None = Field(default=None, pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    outcome: Literal[
        "winner",
        "recommended",
        "proposed_unverified",
        "no_winner",
        "platform_failure",
    ]
    reason: str = Field(min_length=1, max_length=512)
    validating_candidate_id: str | None = Field(default=None, pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    validating_passed: bool | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_selected_candidate(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        if payload.get("selected_candidate_id") is None and isinstance(
            payload.get("winner_id"), str
        ):
            payload["selected_candidate_id"] = payload["winner_id"]
        return payload

    @model_validator(mode="after")
    def validate_links(self) -> "Decision":
        candidate_ids = {assessment.candidate_id for assessment in self.assessments}
        if len(candidate_ids) != len(self.assessments):
            raise ValueError("assessments must reference unique candidates")
        for name, value in {
            "provisional_winner_id": self.provisional_winner_id,
            "winner_id": self.winner_id,
            "selected_candidate_id": self.selected_candidate_id,
            "validating_candidate_id": self.validating_candidate_id,
        }.items():
            if value is not None and value not in candidate_ids:
                raise ValueError(f"{name} must reference an assessed candidate")
        if self.outcome == "winner" and self.winner_id is None:
            raise ValueError("winner decisions require winner_id")
        if self.outcome != "winner" and self.winner_id is not None:
            raise ValueError("non-winner decisions cannot carry winner_id")
        if self.outcome in {"winner", "recommended", "proposed_unverified"}:
            if self.selected_candidate_id is None:
                raise ValueError("positive decisions require selected_candidate_id")
        elif self.selected_candidate_id is not None:
            raise ValueError("non-positive decisions cannot carry selected_candidate_id")
        if self.winner_id is not None and self.selected_candidate_id != self.winner_id:
            raise ValueError("winner decisions must select the winner candidate")
        return self

    def assessment(self, candidate_id: str) -> CandidateAssessment | None:
        for assessment in self.assessments:
            if assessment.candidate_id == candidate_id:
                return assessment
        return None


def decide(
    rules: DecisionRules,
    baseline: EvaluationSummary,
    candidates: tuple[CandidateDecisionInput, ...] | list[CandidateDecisionInput],
    *,
    validating: EvaluationSummary | None = None,
    validating_candidate_id: str | None = None,
) -> Decision:
    if baseline.run_kind != "development":
        raise ValueError("baseline must be the fresh development control run")
    if not baseline.successful or baseline.primary_score is None:
        raise ValueError("baseline must have a successful development score")
    candidate_inputs = tuple(candidates)
    seen_ids = [candidate.candidate_id for candidate in candidate_inputs]
    if len(seen_ids) != len(set(seen_ids)):
        raise ValueError("candidate IDs must be unique")
    provisional_best: CandidateDecisionInput | None = None
    provisional_assessments: dict[str, CandidateAssessment] = {}
    for candidate in sorted(candidate_inputs, key=lambda item: item.candidate_id):
        assessment = _assess_candidate(
            rules=rules,
            baseline=baseline,
            candidate=candidate,
        )
        if assessment.outcome == "keep":
            if provisional_best is None:
                provisional_best = candidate
            else:
                current = provisional_assessments[provisional_best.candidate_id]
                if _rank_key(assessment) < _rank_key(current):
                    provisional_assessments[provisional_best.candidate_id] = current.model_copy(
                        update={
                            "outcome": "discard",
                            "reason": (
                                "Discarded: ranked below current best "
                                f"{candidate.candidate_id}."
                            ),
                        }
                    )
                    provisional_best = candidate
                else:
                    assessment = assessment.model_copy(
                        update={
                            "outcome": "discard",
                            "reason": (
                                "Discarded: ranked below current best "
                                f"{provisional_best.candidate_id}."
                            ),
                        }
                    )
        provisional_assessments[candidate.candidate_id] = assessment
    provisional_winner_id = (
        None if provisional_best is None else provisional_best.candidate_id
    )
    ordered = tuple(
        provisional_assessments[candidate_id]
        for candidate_id in sorted(provisional_assessments)
    )
    if provisional_winner_id is None:
        return Decision(
            rules=rules,
            baseline=baseline,
            assessments=ordered,
            provisional_winner_id=None,
            winner_id=None,
            selected_candidate_id=None,
            outcome="no_winner",
            reason="No candidate met the development decision rules.",
        )
    if validating is None:
        return Decision(
            rules=rules,
            baseline=baseline,
            assessments=ordered,
            provisional_winner_id=provisional_winner_id,
            winner_id=None,
            selected_candidate_id=None,
            outcome="no_winner",
            reason=(
                f"Candidate {provisional_winner_id} is the provisional winner "
                "pending a validating run."
            ),
        )
    if validating_candidate_id != provisional_winner_id:
        raise ValueError("validating run must target the provisional winner")
    validating_passed, validating_reason = _validating_passes(
        rules=rules,
        evaluating=validating,
    )
    final_assessments: list[CandidateAssessment] = []
    winner_id: str | None = None
    selected_candidate_id: str | None = None
    outcome: Literal["winner", "no_winner"] = "no_winner"
    reason = validating_reason
    for assessment in ordered:
        if assessment.candidate_id != provisional_winner_id:
            final_assessments.append(assessment)
            continue
        if validating_passed:
            winner_id = provisional_winner_id
            selected_candidate_id = provisional_winner_id
            outcome = "winner"
            reason = (
                f"Candidate {provisional_winner_id} won after a successful "
                "validating run."
            )
            final_assessments.append(
                assessment.model_copy(
                    update={
                        "outcome": "winner",
                        "reason": "Winner: passed development ranking and validating run.",
                        "validating_passed": True,
                    }
                )
            )
        else:
            final_assessments.append(
                assessment.model_copy(
                    update={
                        "outcome": "discard",
                        "reason": validating_reason,
                        "validating_passed": False,
                    }
                )
            )
    return Decision(
        rules=rules,
        baseline=baseline,
        assessments=tuple(final_assessments),
        provisional_winner_id=provisional_winner_id,
        winner_id=winner_id,
        selected_candidate_id=selected_candidate_id,
        outcome=outcome,
        reason=reason,
        validating_candidate_id=provisional_winner_id,
        validating_passed=validating_passed,
    )


def _assess_candidate(
    *,
    rules: DecisionRules,
    baseline: EvaluationSummary,
    candidate: CandidateDecisionInput,
) -> CandidateAssessment:
    if candidate.status == "invalid":
        return CandidateAssessment(
            candidate_id=candidate.candidate_id,
            outcome="invalid",
            reason=candidate.reason or "Invalid candidate.",
            changed_path_count=candidate.changed_path_count,
        )
    if candidate.status == "platform_failure":
        return CandidateAssessment(
            candidate_id=candidate.candidate_id,
            outcome="platform_failure",
            reason=candidate.reason or "Platform failure.",
            changed_path_count=candidate.changed_path_count,
        )
    assert candidate.evaluation is not None
    evaluation = candidate.evaluation
    if evaluation.run_kind != "development":
        raise ValueError("candidate development evidence must use run_kind='development'")
    if not evaluation.successful or evaluation.primary_score is None:
        return CandidateAssessment(
            candidate_id=candidate.candidate_id,
            outcome="platform_failure",
            reason="Platform failure: development evaluation did not complete successfully.",
            changed_path_count=candidate.changed_path_count,
        )
    aggregate_delta = float(
        Decimal(str(evaluation.primary_score)) - Decimal(str(baseline.primary_score))
    )
    guardrail_failures = _guardrail_failures(rules, evaluation)
    if guardrail_failures:
        return CandidateAssessment(
            candidate_id=candidate.candidate_id,
            outcome="discard",
            reason=(
                "Discarded: hard guardrails failed: "
                + ", ".join(guardrail_failures)
                + "."
            ),
            primary_score=evaluation.primary_score,
            aggregate_delta=aggregate_delta,
            focused_cases_improved=evaluation.focused_cases_improved,
            focused_cases_regressed=evaluation.focused_cases_regressed,
            token_count=evaluation.token_count,
            latency_ms=evaluation.latency_ms,
            changed_path_count=candidate.changed_path_count,
            guardrail_failures=tuple(guardrail_failures),
        )
    if aggregate_delta < rules.aggregate_min_delta:
        return CandidateAssessment(
            candidate_id=candidate.candidate_id,
            outcome="discard",
            reason=(
                "Discarded: aggregate delta "
                f"{_format_delta(aggregate_delta)} is below the minimum "
                f"{_format_delta(rules.aggregate_min_delta)}."
            ),
            primary_score=evaluation.primary_score,
            aggregate_delta=aggregate_delta,
            focused_cases_improved=evaluation.focused_cases_improved,
            focused_cases_regressed=evaluation.focused_cases_regressed,
            token_count=evaluation.token_count,
            latency_ms=evaluation.latency_ms,
            changed_path_count=candidate.changed_path_count,
        )
    if evaluation.focused_cases_improved < rules.min_focused_cases_improved:
        return CandidateAssessment(
            candidate_id=candidate.candidate_id,
            outcome="discard",
            reason=(
                "Discarded: focused improvements "
                f"{evaluation.focused_cases_improved} are below the minimum "
                f"{rules.min_focused_cases_improved}."
            ),
            primary_score=evaluation.primary_score,
            aggregate_delta=aggregate_delta,
            focused_cases_improved=evaluation.focused_cases_improved,
            focused_cases_regressed=evaluation.focused_cases_regressed,
            token_count=evaluation.token_count,
            latency_ms=evaluation.latency_ms,
            changed_path_count=candidate.changed_path_count,
        )
    if evaluation.focused_cases_regressed > rules.max_focused_regressions:
        return CandidateAssessment(
            candidate_id=candidate.candidate_id,
            outcome="discard",
            reason=(
                "Discarded: focused regressions "
                f"{evaluation.focused_cases_regressed} exceed the maximum "
                f"{rules.max_focused_regressions}."
            ),
            primary_score=evaluation.primary_score,
            aggregate_delta=aggregate_delta,
            focused_cases_improved=evaluation.focused_cases_improved,
            focused_cases_regressed=evaluation.focused_cases_regressed,
            token_count=evaluation.token_count,
            latency_ms=evaluation.latency_ms,
            changed_path_count=candidate.changed_path_count,
        )
    return CandidateAssessment(
        candidate_id=candidate.candidate_id,
        outcome="keep",
        reason="Kept: current best candidate pending validating run.",
        primary_score=evaluation.primary_score,
        aggregate_delta=aggregate_delta,
        focused_cases_improved=evaluation.focused_cases_improved,
        focused_cases_regressed=evaluation.focused_cases_regressed,
        token_count=evaluation.token_count,
        latency_ms=evaluation.latency_ms,
        changed_path_count=candidate.changed_path_count,
    )


def _guardrail_failures(
    rules: DecisionRules,
    evaluation: EvaluationSummary,
) -> list[str]:
    by_name = {result.name: result for result in evaluation.guardrails}
    failures: list[str] = []
    for rule in rules.guardrails:
        result = by_name.get(rule.name)
        if result is None:
            failures.append(rule.name)
            continue
        if rule.require_pass and not result.passed:
            failures.append(rule.name)
            continue
        if rule.minimum_score is not None:
            if result.score is None or result.score < rule.minimum_score:
                failures.append(rule.name)
    return failures


def _rank_key(assessment: CandidateAssessment) -> tuple[object, ...]:
    assert assessment.primary_score is not None
    return (
        -Decimal(str(assessment.primary_score)),
        assessment.focused_cases_regressed is None,
        assessment.focused_cases_regressed or 0,
        assessment.focused_cases_improved is None,
        -(assessment.focused_cases_improved or 0),
        assessment.token_count is None,
        assessment.token_count or 0,
        assessment.latency_ms is None,
        Decimal(str(assessment.latency_ms or 0)),
        assessment.changed_path_count,
        assessment.candidate_id,
    )


def _validating_passes(
    *,
    rules: DecisionRules,
    evaluating: EvaluationSummary,
) -> tuple[bool, str]:
    if evaluating.run_kind != "validating":
        raise ValueError("validating evidence must use run_kind='validating'")
    if not evaluating.successful:
        return False, "Discarded: validating run did not succeed."
    failures = _guardrail_failures(rules, evaluating)
    if failures:
        return (
            False,
            "Discarded: validating run failed hard guardrails: "
            + ", ".join(failures)
            + ".",
        )
    return True, "Validating run passed."


def _format_delta(value: float) -> str:
    decimal = Decimal(str(value))
    if decimal >= 0:
        return f"+{decimal:.4f}"
    return f"{decimal:.4f}"
