from __future__ import annotations

import re
from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator


_HEADING = re.compile(r"^### (?P<label>[^\r\n]+)$")
_EMPTY_RESPONSES = frozenset({"", "_No response_", "No response"})
_NAMED_CHECK_LINE = re.compile(r"(?i)^check\s*:")
_FIELD_LABELS = {
    "Repository agent ID or explicit Foundry target": "target",
    "Optimization goal": "goal",
    "Observed failures or evidence": "observed_failures",
    "Constraints and guardrails": "constraints",
    "Changed candidates": "candidate_budget",
    "Optional narrower editable scope": "editable_scope",
    "Optional narrower model set": "candidate_models",
    "Optional primary metric": "primary_metric",
    "Optional exact evaluator IDs": "issue_evaluators",
    "Optional exact verification dataset ID or URI": "verification_dataset",
    "Optional verification commands or checks": "verification_checks",
    "Optional no-evidence acknowledgement": "acknowledge_no_evidence",
}
ISSUE_NAMED_CHECK_GUIDANCE = (
    "optimize issues accept only `command: ...` verification entries; named "
    "`check: ...` entries are reserved for trusted repository profiles used by "
    "PR and deployment verification"
)


class IssueDocumentError(ValueError):
    """The optimize-job issue body does not match the approved issue form."""


class ParsedIssue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    target: str = Field(min_length=1, max_length=4000)
    goal: str = Field(min_length=1, max_length=4000)
    observed_failures: str = Field(min_length=1, max_length=8000)
    constraints: str = Field(min_length=1, max_length=4000)
    candidate_budget: int = Field(ge=1, le=16)
    editable_scope: tuple[str, ...] = ()
    candidate_models: tuple[str, ...] = ()
    primary_metric: str | None = Field(default=None, min_length=1, max_length=128)
    issue_evaluators: tuple[str, ...] = ()
    verification_dataset: str | None = None
    verification_checks: tuple[str, ...] = ()
    acknowledge_no_evidence: bool = False

    @field_validator("target", "goal", "observed_failures", "constraints")
    @classmethod
    def validate_text(cls, value: str) -> str:
        normalized = value.strip()
        if any(ord(character) < 32 and character not in "\n\t" for character in normalized):
            raise ValueError("issue text contains control characters")
        return normalized

    @field_validator(
        "editable_scope",
        "candidate_models",
        "issue_evaluators",
        "verification_checks",
    )
    @classmethod
    def validate_unique_lines(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if len(values) != len(set(values)):
            raise ValueError("issue list values must be unique")
        return values


def parse_issue_body(body: str) -> ParsedIssue:
    if not isinstance(body, str):
        raise TypeError("issue body must be a string")
    if len(body.encode("utf-8")) > 64 * 1024:
        raise IssueDocumentError("issue body exceeds the supported size")

    sections = _parse_sections(body)
    unknown = sorted(set(sections) - set(_FIELD_LABELS))
    if unknown:
        raise IssueDocumentError(f"unknown issue section: {unknown[0]}")
    optional_labels = {
        "Optional narrower editable scope",
        "Optional narrower model set",
        "Optional primary metric",
        "Optional exact evaluator IDs",
        "Optional exact verification dataset ID or URI",
        "Optional verification commands or checks",
        "Optional no-evidence acknowledgement",
    }
    required_labels = set(_FIELD_LABELS) - optional_labels - {
        "Repository agent ID or explicit Foundry target"
    }
    missing = sorted(required_labels - set(sections))
    if missing:
        raise IssueDocumentError(f"missing issue section: {missing[0]}")
    for label in optional_labels:
        sections.setdefault(label, "")
    sections.setdefault("Repository agent ID or explicit Foundry target", "")

    values = {
        field: _section_value(sections[label])
        for label, field in _FIELD_LABELS.items()
    }
    try:
        candidate_budget = int(values["candidate_budget"])
    except ValueError as error:
        raise IssueDocumentError("changed candidates must be an integer") from error

    return ParsedIssue(
        target=values["target"].strip() or "default",
        goal=_required(values, "goal"),
        observed_failures=_required(values, "observed_failures"),
        constraints=_required(values, "constraints"),
        candidate_budget=candidate_budget,
        editable_scope=_lines(values["editable_scope"]),
        candidate_models=_lines(values["candidate_models"]),
        primary_metric=values["primary_metric"].strip() or None,
        issue_evaluators=_lines(values["issue_evaluators"]),
        verification_dataset=values["verification_dataset"].strip() or None,
        verification_checks=_issue_verification_checks(values["verification_checks"]),
        acknowledge_no_evidence=_acknowledgement(values["acknowledge_no_evidence"]),
    )


def _parse_sections(body: str) -> Mapping[str, str]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in body.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        match = _HEADING.fullmatch(line)
        if match is not None:
            label = match.group("label").strip()
            if label in sections:
                raise IssueDocumentError(f"duplicate issue section: {label}")
            sections[label] = []
            current = label
            continue
        if current is not None:
            sections[current].append(line)
    return {label: "\n".join(lines).strip() for label, lines in sections.items()}


def _section_value(value: str) -> str:
    stripped = value.strip()
    if stripped.startswith("```") and stripped.endswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 2:
            stripped = "\n".join(lines[1:-1]).strip()
    return "" if stripped in _EMPTY_RESPONSES else stripped


def _required(values: Mapping[str, str], field: str) -> str:
    value = values[field].strip()
    if not value:
        raise IssueDocumentError(f"{field.replace('_', ' ')} is required")
    return value


def _lines(value: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in value.splitlines() if line.strip())


def _issue_verification_checks(value: str) -> tuple[str, ...]:
    checks = _lines(value)
    if any(_NAMED_CHECK_LINE.match(line) for line in checks):
        raise IssueDocumentError(ISSUE_NAMED_CHECK_GUIDANCE)
    return checks


def _acknowledgement(value: str) -> bool:
    normalized = value.strip().casefold()
    if not normalized:
        return False
    if normalized in {"acknowledge", "acknowledged", "true", "yes"}:
        return True
    raise IssueDocumentError(
        "no-evidence acknowledgement must be blank or use the word acknowledge"
    )
