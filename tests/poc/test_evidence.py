from __future__ import annotations

from pathlib import Path

import pytest

from foundry_opt.poc.candidate import CandidateHashes, FinalizedCandidate, PreparedCandidate
from foundry_opt.poc.decision import (
    CandidateAssessment,
    Decision,
    DecisionRules,
    EvaluationSummary,
    GuardrailResult,
    GuardrailRule,
)
from foundry_opt.poc.evidence import (
    EvidenceError,
    baseline_marker_id,
    candidate_marker_id,
    final_marker_id,
    render_baseline_update,
    render_candidate_update,
    render_final_recommendation,
)
from foundry_opt.poc.state import BaselineState, CandidateState, JobIdentity, JobState


def _evaluation(run_kind: str, link: str, *, successful: bool = True) -> EvaluationSummary:
    return EvaluationSummary(
        run_kind=run_kind,
        successful=successful,
        primary_score=0.75 if successful else None,
        focused_cases_improved=2,
        focused_cases_regressed=0,
        token_count=100,
        latency_ms=25.0,
        foundry_version="draft-42",
        evaluation_link=link,
        guardrails=(GuardrailResult(name="safety", passed=True, score=1.0),),
    )


def _state(hypothesis: str = "improve greeting quality") -> JobState:
    identity = JobIdentity(
        job_id="job-1",
        repository="owner/repo",
        issue_number=123,
        shared_commit="a" * 40,
        base_commit="a" * 40,
        source_root="src",
        route_fingerprint="b" * 64,
        min_candidates=1,
    )
    prepared = PreparedCandidate(
        candidate_id="candidate-one",
        parent_id=None,
        model="gpt-5-mini",
        hypothesis=hypothesis,
        base_commit="a" * 40,
        origin_commit="a" * 40,
        workspace_path=Path(r"Q:\trusted\worktrees\candidate-one"),
    )
    finalized = FinalizedCandidate(
        candidate_id="candidate-one",
        parent_id=None,
        model="gpt-5-mini",
        hypothesis=hypothesis,
        base_commit="a" * 40,
        origin_commit="a" * 40,
        candidate_commit="c" * 40,
        source_root="src",
        workspace_path=Path(r"Q:\trusted\worktrees\candidate-one"),
        changed_paths=("src/app.py", "tests/test_app.py"),
        incremental_changed_paths=("src/app.py", "tests/test_app.py"),
        hashes=CandidateHashes(
            patch_sha256="d" * 64,
            source_tree_sha256="e" * 64,
            source_zip_sha256="f" * 64,
        ),
        patch_path=Path(r"Q:\trusted\artifacts\candidate-one\candidate.patch"),
        source_zip_path=Path(r"Q:\trusted\artifacts\candidate-one\source.zip"),
    )
    assessment = CandidateAssessment(
        candidate_id="candidate-one",
        outcome="winner",
        reason="Winner: passed development ranking and validating run.",
        primary_score=0.75,
        aggregate_delta=0.15,
        focused_cases_improved=2,
        focused_cases_regressed=0,
        token_count=100,
        latency_ms=25.0,
        changed_path_count=2,
        validating_passed=True,
    )
    decision = Decision(
        rules=DecisionRules(
            aggregate_min_delta=0.05,
            min_focused_cases_improved=1,
            max_focused_regressions=0,
            guardrails=(GuardrailRule(name="safety", minimum_score=1.0),),
        ),
        baseline=_evaluation("development", "https://example.invalid/baseline"),
        assessments=(assessment,),
        provisional_winner_id="candidate-one",
        winner_id="candidate-one",
        outcome="winner",
        reason="Candidate candidate-one won after a successful validating run.",
        validating_candidate_id="candidate-one",
        validating_passed=True,
    )
    return JobState(
        identity=identity,
        baseline=BaselineState(
            evaluation=_evaluation("development", "https://example.invalid/baseline")
        ),
        candidates=(
            CandidateState(
                handoff=prepared,
                finalized=finalized,
                development=_evaluation("development", "https://example.invalid/dev"),
                validating=_evaluation("validating", "https://example.invalid/val"),
                assessment=assessment,
            ),
        ),
        decision=decision,
        provisional_winner_id="candidate-one",
        final_winner_id="candidate-one",
        terminal_outcome="winner",
    )


def test_renderers_include_required_sections_and_stable_markers() -> None:
    state = _state()
    baseline = render_baseline_update(state)
    candidate = render_candidate_update(state, "candidate-one")
    final = render_final_recommendation(state, state.decision)

    assert baseline.marker_id == baseline_marker_id("job-1")
    assert candidate.marker_id == candidate_marker_id("job-1", "candidate-one")
    assert final.marker_id == final_marker_id("job-1")
    assert candidate.body == render_candidate_update(state, "candidate-one").body
    assert "Shared commit" in baseline.body
    assert "Base commit" in candidate.body
    assert "Hypothesis" in candidate.body
    assert "Change hash" in candidate.body
    assert "Source tree hash" in candidate.body
    assert "Source ZIP hash" in candidate.body
    assert "Foundry version" in final.body
    assert "Development evaluation" in final.body
    assert "Validating evaluation" in final.body
    assert "### Metrics" in final.body
    assert "### Guardrails" in final.body
    assert "### Summary" in final.body
    assert "Next step" in final.body


def test_render_rejects_unsafe_text() -> None:
    state = _state("prompt: leaked secret")

    with pytest.raises(EvidenceError, match="forbidden raw content"):
        render_candidate_update(state, "candidate-one")

