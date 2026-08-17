from __future__ import annotations

import hashlib
import re
from decimal import Decimal
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field

from foundry_opt.poc.decision import CandidateAssessment, Decision, EvaluationSummary, GuardrailRule
from foundry_opt.poc.state import JobState


_CONTROL_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HTML_PATTERN = re.compile(r"[<>]")
_RAW_CONTENT_PATTERNS = (
    re.compile(r"(?i)\b(?:prompt|response|dataset|tool|credential|trace)\s*[:=]"),
    re.compile(r"(?i)\b(?:api[_-]?key|password|secret|token)\s*[:=]"),
)


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceError(ValueError):
    """Rendered issue evidence would leak or corrupt unsafe content."""


class RenderedComment(_FrozenModel):
    marker_id: str = Field(min_length=1, max_length=256)
    title: str = Field(min_length=1, max_length=128)
    body: str = Field(min_length=1)

    @property
    def body_sha256(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()


def baseline_marker_id(job_id: str) -> str:
    return f"foundry-opt-poc:{job_id}:baseline"


def candidate_marker_id(job_id: str, candidate_id: str) -> str:
    return f"foundry-opt-poc:{job_id}:candidate:{candidate_id}"


def final_marker_id(job_id: str) -> str:
    return f"foundry-opt-poc:{job_id}:final"


def render_baseline_update(state: JobState) -> RenderedComment:
    if state.baseline is None:
        raise EvidenceError("baseline evidence is not available")
    evaluation = state.baseline.evaluation
    title = "Baseline update"
    body = _render_document(
        title=title,
        context_lines=(
            f"- Shared commit: `{state.identity.shared_commit}`",
            f"- Base commit: `{state.identity.base_commit}`",
            f"- Source root: `{state.identity.source_root}`",
            "- Hypothesis: baseline control",
            "- Change hash: n/a",
            "- Source tree hash: n/a",
            "- Source ZIP hash: n/a",
            f"- Foundry version: `{_safe_text(evaluation.foundry_version)}`",
            f"- Evaluation: {_safe_text(evaluation.evaluation_link)}",
        ),
        metrics_table=_baseline_metrics_table(evaluation),
        guardrail_lines=_guardrail_lines(
            evaluation=evaluation,
            rules=(),
        ),
        improved_text="Baseline control recorded for candidate comparison.",
        not_improved_text="No candidate has been compared yet.",
        verdict_text="baseline",
        next_step_text="Author the first candidate.",
    )
    return RenderedComment(
        marker_id=baseline_marker_id(state.identity.job_id),
        title=title,
        body=body,
    )


def render_candidate_update(
    state: JobState,
    candidate_id: str,
) -> RenderedComment:
    candidate = state.candidate(candidate_id)
    if candidate is None or candidate.assessment is None:
        raise EvidenceError("candidate assessment is not available")
    finalized = candidate.finalized
    evaluation = candidate.development
    assessment = candidate.assessment
    source_tree_hash = "n/a"
    source_zip_hash = "n/a"
    change_hash = "n/a"
    base_commit = state.identity.base_commit
    if finalized is not None:
        source_tree_hash = finalized.hashes.source_tree_sha256
        source_zip_hash = finalized.hashes.source_zip_sha256
        change_hash = finalized.hashes.patch_sha256
        base_commit = finalized.base_commit
    title = f"Candidate update: {candidate_id}"
    foundry_version = "n/a" if evaluation is None else _safe_text(evaluation.foundry_version)
    evaluation_link = "n/a" if evaluation is None else _safe_text(evaluation.evaluation_link)
    body = _render_document(
        title=title,
        context_lines=(
            f"- Shared commit: `{state.identity.shared_commit}`",
            f"- Base commit: `{base_commit}`",
            f"- Parent candidate: `{candidate.handoff.parent_id or 'baseline'}`",
            f"- Model: `{_safe_text(candidate.handoff.model)}`",
            f"- Hypothesis: {_safe_text(candidate.handoff.hypothesis)}",
            f"- Change hash: `{change_hash}`",
            f"- Source tree hash: `{source_tree_hash}`",
            f"- Source ZIP hash: `{source_zip_hash}`",
            f"- Foundry version: `{foundry_version}`",
            f"- Evaluation: {evaluation_link}",
        ),
        metrics_table=_candidate_metrics_table(
            baseline=state.baseline.evaluation if state.baseline is not None else None,
            evaluation=evaluation,
            assessment=assessment,
        ),
        guardrail_lines=_guardrail_lines(
            evaluation=evaluation,
            rules=() if state.decision is None else state.decision.rules.guardrails,
        ),
        improved_text=_candidate_improved_text(assessment),
        not_improved_text=_candidate_not_improved_text(assessment),
        verdict_text=assessment.outcome,
        next_step_text=_candidate_next_step_text(state, assessment),
    )
    return RenderedComment(
        marker_id=candidate_marker_id(state.identity.job_id, candidate_id),
        title=title,
        body=body,
    )


def render_final_recommendation(
    state: JobState,
    decision: Decision,
) -> RenderedComment:
    candidate = None
    assessment = None
    evaluation = None
    validating = None
    if decision.provisional_winner_id is not None:
        candidate = state.candidate(decision.provisional_winner_id)
        assessment = decision.assessment(decision.provisional_winner_id)
        if candidate is not None:
            evaluation = candidate.development
            validating = candidate.validating
    finalized = None if candidate is None else candidate.finalized
    change_hash = "n/a" if finalized is None else finalized.hashes.patch_sha256
    source_tree_hash = "n/a" if finalized is None else finalized.hashes.source_tree_sha256
    source_zip_hash = "n/a" if finalized is None else finalized.hashes.source_zip_sha256
    hypothesis = "n/a" if candidate is None else _safe_text(candidate.handoff.hypothesis)
    model = "n/a" if candidate is None else _safe_text(candidate.handoff.model)
    foundry_version = "n/a" if evaluation is None else _safe_text(evaluation.foundry_version)
    development_link = "n/a" if evaluation is None else _safe_text(evaluation.evaluation_link)
    validating_link = "n/a" if validating is None else _safe_text(validating.evaluation_link)
    title = "Final recommendation"
    body = _render_document(
        title=title,
        context_lines=(
            f"- Shared commit: `{state.identity.shared_commit}`",
            f"- Base commit: `{state.identity.base_commit}`",
            f"- Provisional winner: `{decision.provisional_winner_id or 'none'}`",
            f"- Final winner: `{decision.winner_id or 'none'}`",
            f"- Model: `{model}`",
            f"- Hypothesis: {hypothesis}",
            f"- Change hash: `{change_hash}`",
            f"- Source tree hash: `{source_tree_hash}`",
            f"- Source ZIP hash: `{source_zip_hash}`",
            f"- Foundry version: `{foundry_version}`",
            f"- Development evaluation: {development_link}",
            f"- Validating evaluation: {validating_link}",
        ),
        metrics_table=_final_metrics_table(
            baseline=state.baseline.evaluation if state.baseline is not None else None,
            assessment=assessment,
            development=evaluation,
            validating=validating,
        ),
        guardrail_lines=_guardrail_lines(
            evaluation=validating or evaluation,
            rules=decision.rules.guardrails,
        ),
        improved_text=_final_improved_text(decision, assessment),
        not_improved_text=_final_not_improved_text(decision),
        verdict_text=decision.outcome,
        next_step_text=_final_next_step_text(decision),
    )
    return RenderedComment(
        marker_id=final_marker_id(state.identity.job_id),
        title=title,
        body=body,
    )


def _render_document(
    *,
    title: str,
    context_lines: Iterable[str],
    metrics_table: tuple[str, ...],
    guardrail_lines: tuple[str, ...],
    improved_text: str,
    not_improved_text: str,
    verdict_text: str,
    next_step_text: str,
) -> str:
    lines = [
        f"## {title}",
        "",
        "### Context",
        *_normalize_lines(context_lines),
        "",
        "### Metrics",
        *metrics_table,
        "",
        "### Guardrails",
        *guardrail_lines,
        "",
        "### Summary",
        f"- Improved: {_safe_text(improved_text)}",
        f"- Not improved: {_safe_text(not_improved_text)}",
        f"- Verdict: `{_safe_text(verdict_text)}`",
        f"- Next step: {_safe_text(next_step_text)}",
    ]
    body = "\n".join(lines).strip()
    _assert_safe_body(body)
    return body


def _normalize_lines(lines: Iterable[str]) -> tuple[str, ...]:
    return tuple(_safe_text(line) for line in lines)


def _baseline_metrics_table(evaluation: EvaluationSummary) -> tuple[str, ...]:
    return (
        "| Metric | Baseline |",
        "| --- | --- |",
        f"| Primary score | {_format_optional_float(evaluation.primary_score)} |",
        "| Aggregate delta vs baseline | n/a |",
        f"| Focused cases improved | {evaluation.focused_cases_improved} |",
        f"| Focused cases regressed | {evaluation.focused_cases_regressed} |",
        f"| Tokens | {_format_optional_int(evaluation.token_count)} |",
        f"| Latency ms | {_format_optional_float(evaluation.latency_ms)} |",
    )


def _candidate_metrics_table(
    *,
    baseline: EvaluationSummary | None,
    evaluation: EvaluationSummary | None,
    assessment: CandidateAssessment,
) -> tuple[str, ...]:
    baseline_score = "n/a" if baseline is None else _format_optional_float(baseline.primary_score)
    candidate_score = "n/a" if evaluation is None else _format_optional_float(evaluation.primary_score)
    return (
        "| Metric | Baseline | Candidate |",
        "| --- | --- | --- |",
        f"| Primary score | {baseline_score} | {candidate_score} |",
        f"| Aggregate delta | n/a | {_format_optional_float(assessment.aggregate_delta)} |",
        f"| Focused cases improved | {0 if baseline is None else baseline.focused_cases_improved} | {_format_optional_int(assessment.focused_cases_improved)} |",
        f"| Focused cases regressed | {0 if baseline is None else baseline.focused_cases_regressed} | {_format_optional_int(assessment.focused_cases_regressed)} |",
        f"| Tokens | {0 if baseline is None or baseline.token_count is None else baseline.token_count} | {_format_optional_int(assessment.token_count)} |",
        f"| Latency ms | {baseline.latency_ms if baseline is not None else 'n/a'} | {_format_optional_float(assessment.latency_ms)} |",
    )


def _final_metrics_table(
    *,
    baseline: EvaluationSummary | None,
    assessment: CandidateAssessment | None,
    development: EvaluationSummary | None,
    validating: EvaluationSummary | None,
) -> tuple[str, ...]:
    baseline_score = "n/a" if baseline is None else _format_optional_float(baseline.primary_score)
    winner_score = "n/a" if development is None else _format_optional_float(development.primary_score)
    validating_score = "n/a" if validating is None else _format_optional_float(validating.primary_score)
    return (
        "| Metric | Baseline | Development winner | Validating |",
        "| --- | --- | --- | --- |",
        f"| Primary score | {baseline_score} | {winner_score} | {validating_score} |",
        f"| Aggregate delta | n/a | {('n/a' if assessment is None else _format_optional_float(assessment.aggregate_delta))} | n/a |",
        f"| Focused cases improved | {0 if baseline is None else baseline.focused_cases_improved} | {('n/a' if development is None else development.focused_cases_improved)} | {('n/a' if validating is None else validating.focused_cases_improved)} |",
        f"| Focused cases regressed | {0 if baseline is None else baseline.focused_cases_regressed} | {('n/a' if development is None else development.focused_cases_regressed)} | {('n/a' if validating is None else validating.focused_cases_regressed)} |",
        f"| Tokens | {0 if baseline is None or baseline.token_count is None else baseline.token_count} | {('n/a' if development is None or development.token_count is None else development.token_count)} | {('n/a' if validating is None or validating.token_count is None else validating.token_count)} |",
        f"| Latency ms | {('n/a' if baseline is None else _format_optional_float(baseline.latency_ms))} | {('n/a' if development is None else _format_optional_float(development.latency_ms))} | {('n/a' if validating is None else _format_optional_float(validating.latency_ms))} |",
    )


def _guardrail_lines(
    *,
    evaluation: EvaluationSummary | None,
    rules: tuple[GuardrailRule, ...] | Iterable[GuardrailRule],
) -> tuple[str, ...]:
    if evaluation is None:
        return ("- n/a",)
    by_name = {item.name: item for item in evaluation.guardrails}
    if not tuple(rules):
        if not evaluation.guardrails:
            return ("- none configured",)
        return tuple(
            f"- {item.name}: {'pass' if item.passed else 'fail'} ({_format_optional_float(item.score)})"
            for item in sorted(evaluation.guardrails, key=lambda item: item.name)
        )
    rendered: list[str] = []
    for rule in sorted(tuple(rules), key=lambda item: item.name):
        result = by_name.get(rule.name)
        if result is None:
            rendered.append(f"- {rule.name}: missing")
            continue
        threshold = (
            ""
            if rule.minimum_score is None
            else f", minimum {_format_optional_float(rule.minimum_score)}"
        )
        rendered.append(
            f"- {rule.name}: {'pass' if result.passed else 'fail'} "
            f"({_format_optional_float(result.score)}{threshold})"
        )
    return tuple(rendered)


def _candidate_improved_text(assessment: CandidateAssessment) -> str:
    if assessment.outcome in {"keep", "winner"}:
        return (
            f"Aggregate delta {_format_optional_float(assessment.aggregate_delta)} "
            f"with {assessment.focused_cases_improved or 0} focused improvements "
            f"and {assessment.focused_cases_regressed or 0} regressions."
        )
    return "No accepted measurable improvement."


def _candidate_not_improved_text(assessment: CandidateAssessment) -> str:
    if assessment.outcome in {"keep", "winner"}:
        return "None."
    return assessment.reason


def _candidate_next_step_text(state: JobState, assessment: CandidateAssessment) -> str:
    if state.completed_candidate_count < state.identity.min_candidates:
        return "Author another candidate."
    if assessment.outcome == "keep":
        return "Run the validating evaluation for the provisional winner."
    return "Review the next candidate or finish without a winner."


def _final_improved_text(
    decision: Decision,
    assessment: CandidateAssessment | None,
) -> str:
    if decision.outcome == "winner" and assessment is not None:
        return (
            f"Winner {decision.winner_id} achieved aggregate delta "
            f"{_format_optional_float(assessment.aggregate_delta)} with "
            f"{assessment.focused_cases_improved or 0} focused improvements."
        )
    if decision.outcome == "platform_failure":
        return "The provisional winner could not be confirmed because validation hit a platform failure."
    return "No candidate cleared the full development and validating bar."


def _final_not_improved_text(decision: Decision) -> str:
    if decision.outcome == "winner":
        losers = [
            assessment.candidate_id
            for assessment in decision.assessments
            if assessment.outcome != "winner"
        ]
        return "None." if not losers else "Other candidates: " + ", ".join(losers) + "."
    return decision.reason


def _final_next_step_text(decision: Decision) -> str:
    if decision.outcome == "winner":
        return "Review and commit the projected winner patch."
    if decision.outcome == "platform_failure":
        return "Investigate the validating platform failure before retrying the job."
    return "Close the early draft PR unchanged."


def _format_optional_float(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{Decimal(str(value)):.4f}"


def _format_optional_int(value: int | None) -> str:
    return "n/a" if value is None else str(value)


def _safe_text(value: str) -> str:
    if not isinstance(value, str):
        raise EvidenceError("rendered evidence must use string values")
    normalized = " ".join(value.split()).strip()
    if not normalized:
        raise EvidenceError("rendered evidence text must not be empty")
    if any(ord(character) > 0x7E for character in normalized):
        raise EvidenceError("rendered evidence must stay ASCII")
    if _CONTROL_PATTERN.search(normalized) is not None:
        raise EvidenceError("rendered evidence contains control characters")
    if _HTML_PATTERN.search(normalized) is not None:
        raise EvidenceError("rendered evidence contains HTML-like characters")
    for pattern in _RAW_CONTENT_PATTERNS:
        if pattern.search(normalized) is not None:
            raise EvidenceError("rendered evidence contains forbidden raw content")
    return normalized


def _assert_safe_body(body: str) -> None:
    if any(ord(character) > 0x7E for character in body):
        raise EvidenceError("rendered markdown must stay ASCII")
    if _CONTROL_PATTERN.search(body.replace("\n", "")) is not None:
        raise EvidenceError("rendered markdown contains control characters")
    for pattern in _RAW_CONTENT_PATTERNS:
        if pattern.search(body) is not None:
            raise EvidenceError("rendered markdown contains forbidden raw content")
