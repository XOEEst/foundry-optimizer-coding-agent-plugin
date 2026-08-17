from __future__ import annotations

from foundry_opt.poc.decision import (
    CandidateDecisionInput,
    DecisionRules,
    EvaluationSummary,
    GuardrailResult,
    GuardrailRule,
    decide,
)


def _summary(
    *,
    run_kind: str,
    score: float | None,
    improved: int = 0,
    regressed: int = 0,
    tokens: int | None = 100,
    latency: float | None = 10.0,
    successful: bool = True,
    guardrails: tuple[GuardrailResult, ...] = (
        GuardrailResult(name="safety", passed=True, score=1.0),
    ),
) -> EvaluationSummary:
    return EvaluationSummary(
        run_kind=run_kind,
        successful=successful,
        primary_score=score,
        focused_cases_improved=improved,
        focused_cases_regressed=regressed,
        token_count=tokens,
        latency_ms=latency,
        foundry_version="draft-1",
        evaluation_link=f"https://example.invalid/{run_kind}",
        guardrails=guardrails,
    )


def _candidate(
    candidate_id: str,
    *,
    score: float | None = 0.6,
    improved: int = 1,
    regressed: int = 0,
    tokens: int | None = 100,
    latency: float | None = 10.0,
    changed_paths: int = 1,
    status: str = "ok",
    reason: str | None = None,
    successful: bool = True,
    guardrails: tuple[GuardrailResult, ...] = (
        GuardrailResult(name="safety", passed=True, score=1.0),
    ),
) -> CandidateDecisionInput:
    return CandidateDecisionInput(
        candidate_id=candidate_id,
        changed_path_count=changed_paths,
        status=status,
        reason=reason,
        evaluation=(
            None
            if status != "ok"
            else _summary(
                run_kind="development",
                score=score,
                improved=improved,
                regressed=regressed,
                tokens=tokens,
                latency=latency,
                successful=successful,
                guardrails=guardrails,
            )
        ),
    )


def test_decide_selects_winner_with_deterministic_tiebreak() -> None:
    rules = DecisionRules(
        aggregate_min_delta=0.05,
        min_focused_cases_improved=1,
        max_focused_regressions=0,
        guardrails=(GuardrailRule(name="safety", minimum_score=1.0),),
    )
    baseline = _summary(run_kind="development", score=0.50)
    decision = decide(
        rules,
        baseline,
        (
            _candidate("candidate-a", score=0.70, tokens=120, latency=20.0),
            _candidate("candidate-b", score=0.70, tokens=100, latency=20.0),
        ),
        validating=_summary(run_kind="validating", score=0.68),
        validating_candidate_id="candidate-b",
    )

    assert decision.outcome == "winner"
    assert decision.provisional_winner_id == "candidate-b"
    assert decision.winner_id == "candidate-b"
    assert decision.assessment("candidate-a").outcome == "discard"
    assert decision.assessment("candidate-b").outcome == "winner"


def test_decide_returns_no_winner_when_rules_are_not_met() -> None:
    rules = DecisionRules(
        aggregate_min_delta=0.10,
        min_focused_cases_improved=2,
        max_focused_regressions=0,
        guardrails=(GuardrailRule(name="safety", minimum_score=1.0),),
    )
    baseline = _summary(run_kind="development", score=0.50)
    decision = decide(
        rules,
        baseline,
        (
            _candidate("candidate-a", score=0.55, improved=1),
            _candidate(
                "candidate-b",
                score=0.70,
                improved=3,
                guardrails=(GuardrailResult(name="safety", passed=False, score=0.0),),
            ),
        ),
    )

    assert decision.outcome == "no_winner"
    assert decision.provisional_winner_id is None
    assert [assessment.outcome for assessment in decision.assessments] == [
        "discard",
        "discard",
    ]


def test_invalid_and_platform_failure_candidates_are_reported_but_not_ranked() -> None:
    rules = DecisionRules(
        aggregate_min_delta=0.05,
        min_focused_cases_improved=1,
        max_focused_regressions=0,
        guardrails=(GuardrailRule(name="safety", minimum_score=1.0),),
    )
    baseline = _summary(run_kind="development", score=0.50)
    decision = decide(
        rules,
        baseline,
        (
            _candidate("candidate-a", status="invalid", reason="test-only candidate"),
            _candidate(
                "candidate-b",
                status="platform_failure",
                reason="deployment failed",
            ),
            _candidate("candidate-c", score=0.65, improved=2),
        ),
        validating=_summary(run_kind="validating", score=0.63),
        validating_candidate_id="candidate-c",
    )

    assert decision.winner_id == "candidate-c"
    assert decision.assessment("candidate-a").outcome == "invalid"
    assert decision.assessment("candidate-b").outcome == "platform_failure"
    assert decision.assessment("candidate-c").outcome == "winner"


def test_validating_failure_prevents_a_final_winner() -> None:
    rules = DecisionRules(
        aggregate_min_delta=0.05,
        min_focused_cases_improved=1,
        max_focused_regressions=0,
        guardrails=(GuardrailRule(name="safety", minimum_score=1.0),),
    )
    baseline = _summary(run_kind="development", score=0.50)
    decision = decide(
        rules,
        baseline,
        (_candidate("candidate-a", score=0.70, improved=2),),
        validating=_summary(
            run_kind="validating",
            score=None,
            successful=False,
            guardrails=(GuardrailResult(name="safety", passed=True, score=1.0),),
        ),
        validating_candidate_id="candidate-a",
    )

    assert decision.outcome == "no_winner"
    assert decision.winner_id is None
    assert decision.assessment("candidate-a").outcome == "discard"
    assert decision.assessment("candidate-a").validating_passed is False

