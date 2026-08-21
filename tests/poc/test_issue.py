from __future__ import annotations

import pytest

from foundry_opt.poc.issue import IssueDocumentError, parse_issue_body


BODY = """### Repository agent ID or explicit Foundry target

example-agent

### Optimization goal

Improve complete policy coverage.

### Observed failures or evidence

Multi-rule requests sometimes omit one rule.

### Constraints and guardrails

Preserve advisory-only behavior.

### Changed candidates

2

### Optional narrower editable scope

```text
agent/agent_config/baseline/instructions.md
agent/skills/**
```

### Optional narrower model set

```text
gpt-5-mini
```

### Optional exact evaluator IDs

```text
azureai://accounts/example/projects/example/evaluators/quality/versions/1
azureai://built-in/evaluators/safety weight=2
```
"""


def test_parse_issue_body_is_backward_compatible() -> None:
    parsed = parse_issue_body(BODY)

    assert parsed.target == "example-agent"
    assert parsed.goal == "Improve complete policy coverage."
    assert parsed.candidate_budget == 2
    assert parsed.editable_scope == (
        "agent/agent_config/baseline/instructions.md",
        "agent/skills/**",
    )
    assert parsed.candidate_models == ("gpt-5-mini",)
    assert parsed.issue_evaluators == (
        "azureai://accounts/example/projects/example/evaluators/quality/versions/1",
        "azureai://built-in/evaluators/safety weight=2",
    )
    assert parsed.verification_dataset is None
    assert parsed.verification_checks == ()
    assert parsed.acknowledge_no_evidence is False


def test_parse_issue_body_with_optional_verification_sections() -> None:
    parsed = parse_issue_body(
        BODY
        + """
### Optional exact verification dataset ID or URI

azureai://accounts/example/projects/example/data/validation/versions/1

### Optional verification commands or checks

```text
command: python -m pytest tests/agent -q
```

### Optional no-evidence acknowledgement

acknowledge
"""
    )

    assert parsed.verification_dataset == "azureai://accounts/example/projects/example/data/validation/versions/1"
    assert parsed.verification_checks == (
        "command: python -m pytest tests/agent -q",
    )
    assert parsed.acknowledge_no_evidence is True


def test_parse_issue_body_rejects_named_check_entries() -> None:
    with pytest.raises(IssueDocumentError, match="command: .*check:"):
        parse_issue_body(
            BODY
            + """
### Optional verification commands or checks

```text
check: CI / unit-tests
```
"""
        )


def test_optional_no_response_is_empty() -> None:
    parsed = parse_issue_body(
        BODY.replace(
            "```text\nagent/agent_config/baseline/instructions.md\nagent/skills/**\n```",
            "_No response_",
        ).replace("```text\ngpt-5-mini\n```", "_No response_")
    )

    assert parsed.editable_scope == ()
    assert parsed.candidate_models == ()
    assert parsed.issue_evaluators == (
        "azureai://accounts/example/projects/example/evaluators/quality/versions/1",
        "azureai://built-in/evaluators/safety weight=2",
    )


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("### Changed candidates", "### Unknown", "unknown issue section"),
        ("2", "two", "must be an integer"),
        (
            "### Optimization goal\n\nImprove complete policy coverage.\n\n",
            "",
            "missing issue section",
        ),
    ],
)
def test_invalid_issue_documents(old: str, new: str, message: str) -> None:
    with pytest.raises(IssueDocumentError, match=message):
        parse_issue_body(BODY.replace(old, new, 1))


def test_duplicate_section_is_rejected() -> None:
    with pytest.raises(IssueDocumentError, match="duplicate issue section"):
        parse_issue_body(BODY + "\n### Optimization goal\n\nAgain\n")
