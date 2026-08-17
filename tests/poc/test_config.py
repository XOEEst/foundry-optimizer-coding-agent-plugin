from __future__ import annotations

from pathlib import Path

import pytest

from foundry_opt.poc.config import (
    AgentMetadata,
    IssueEvaluatorSyntaxError,
    IssueNarrowingError,
    IssueEvaluatorEntry,
    OptimizeIssueRequest,
    POCConfigurationError,
    RepositoryPathError,
    RepositoryPolicy,
    SharedPin,
    apply_issue_request,
    load_agent_metadata,
    load_repository_policy,
    validate_repository_relative_path,
    validate_repository_relative_paths,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]

def _generic_repository_policy() -> dict[str, object]:
    return {
        "schema_version": 1,
        "source_root": "agent",
        "editable_paths": [
            "agent/agent_config/baseline/instructions.md",
            "agent/skills/**",
            "tests/agent/**",
        ],
        "min_candidates": 1,
        "max_candidates": 2,
        "baseline_model": "gpt-5-mini",
        "allowed_models": ["gpt-5-mini", "gpt-5.6-sol"],
        "primary_metric": "policy_coverage",
        "decision_rules": {
            "minimum_aggregate_delta": 0.10,
            "focused_cases_required": True,
            "max_regressions": 0,
        },
        "hard_guardrails": {
            "advisory_safety": {"required_pass_rate": 1.0, "required": True}
        },
        "metadata_path": ".foundry/agent-metadata.yaml",
    }


def _generic_issue_request() -> dict[str, object]:
    return {
        "repo_agent_id": "example-agent",
        "goal": "Raise policy coverage on missed travel-policy cases.",
        "observed_failures": [
            "Hotel reimbursement approvals ignore policy evidence.",
            "Cross-border travel answers omit manager escalation.",
        ],
        "constraints": ["Do not widen the editable surface."],
        "candidate_budget": "baseline+1",
        "candidate_models": "gpt-5-mini only",
        "editable_scope": [
            "agent/agent_config/baseline/instructions.md",
            "agent/skills/**",
        ],
    }


def test_shared_pin_accepts_generic_document() -> None:
    pin = SharedPin.from_document(
        {
            "schema_version": 1,
            "repository_url": "https://github.com/example/foundry-shared.git",
            "commit": "a" * 40,
            "package_path": "src/foundry_opt/poc",
            "skill_path": "skills/foundry-agent-optimizer",
            "uv_lock_sha256": "b" * 64,
        }
    )

    assert pin.repository_url == "https://github.com/example/foundry-shared.git"
    assert pin.commit == "a" * 40


def test_shared_pin_allows_root_package_path() -> None:
    pin = SharedPin.from_document(
        {
            "schema_version": 1,
            "repository_url": "https://github.com/example/foundry-shared.git",
            "commit": "a" * 40,
            "package_path": ".",
            "skill_path": "skills/foundry-agent-optimizer",
            "uv_lock_sha256": "b" * 64,
        }
    )

    assert pin.package_path == "."


def test_shared_pin_keeps_strict_skill_path_rules() -> None:
    with pytest.raises(POCConfigurationError):
        SharedPin.from_document(
            {
                "schema_version": 1,
                "repository_url": "https://github.com/example/foundry-shared.git",
                "commit": "a" * 40,
                "package_path": ".",
                "skill_path": ".",
                "uv_lock_sha256": "b" * 64,
            }
        )


def test_repository_policy_accepts_generic_document() -> None:
    policy = RepositoryPolicy.from_document(_generic_repository_policy())

    assert policy.source_root == "agent"
    assert policy.editable_paths == (
        "agent/agent_config/baseline/instructions.md",
        "agent/skills/**",
        "tests/agent/**",
    )
    assert policy.min_candidates == 1
    assert policy.max_candidates == 2
    assert policy.decision_rules.focused_cases_required is True
    assert policy.metadata_path == ".foundry/agent-metadata.yaml"



def test_generic_documents_reject_unknown_fields() -> None:
    document = _generic_repository_policy()
    document["unexpected"] = True

    with pytest.raises(POCConfigurationError):
        RepositoryPolicy.from_document(document)


def test_duplicate_yaml_keys_fail_closed() -> None:
    document = """
schema_version: 1
source_root: agent
source_root: service
editable_paths:
  - agent/skills/**
min_candidates: 1
max_candidates: 1
baseline_model: gpt-5-mini
allowed_models:
  - gpt-5-mini
primary_metric: policy_coverage
decision_rules:
  minimum_aggregate_delta: 0.10
  focused_cases_required: true
  max_regressions: 0
hard_guardrails:
  advisory_safety:
    required_pass_rate: 1.0
metadata_path: .foundry/agent-metadata.yaml
"""

    with pytest.raises(POCConfigurationError):
        RepositoryPolicy.from_document(document)


@pytest.mark.parametrize(
    "value",
    [
        "",
        ".",
        "..",
        "/agent/skills/**",
        r"agent\skills\**",
        "C:/agent/skills/**",
        "../agent/skills/**",
        "agent/../skills/**",
        "agent/\x00/skills",
    ],
)
def test_repository_relative_path_validation_rejects_unsafe_values(
    value: str,
) -> None:
    with pytest.raises(RepositoryPathError):
        validate_repository_relative_path(
            value,
            field="editable_path",
            allow_glob=True,
        )


def test_repository_relative_paths_reject_case_fold_duplicates() -> None:
    with pytest.raises(RepositoryPathError):
        validate_repository_relative_paths(
            ["agent/skills/**", "Agent/Skills/**"],
            field="editable_paths",
            allow_glob=True,
        )


def test_issue_request_only_narrows_repository_policy() -> None:
    policy = RepositoryPolicy.from_document(_generic_repository_policy())
    issue = OptimizeIssueRequest.from_document(_generic_issue_request())

    narrowed = apply_issue_request(policy, issue)

    assert narrowed.min_candidates == 1
    assert narrowed.max_candidates == 1
    assert narrowed.allowed_models == ("gpt-5-mini",)
    assert narrowed.editable_paths == (
        "agent/agent_config/baseline/instructions.md",
        "agent/skills/**",
    )


def test_issue_request_accepts_repo_agent_and_weighted_evaluators() -> None:
    issue = OptimizeIssueRequest.from_document(
        {
            **_generic_issue_request(),
            "issue_evaluators": [
                "azureai://accounts/a/projects/p/evaluators/quality/versions/1",
                "azureai://built-in/evaluators/safety weight=2",
            ],
        }
    )

    assert issue.repo_agent_id == "example-agent"
    assert issue.explicit_target is None
    assert issue.issue_evaluators == (
        IssueEvaluatorEntry(
            evaluator_id="azureai://accounts/a/projects/p/evaluators/quality/versions/1",
            weight=None,
        ),
        IssueEvaluatorEntry(
            evaluator_id="azureai://built-in/evaluators/safety",
            weight=2.0,
        ),
    )


def test_issue_request_rejects_duplicate_evaluator_ids_even_with_different_weights() -> None:
    with pytest.raises(POCConfigurationError, match="duplicate evaluator IDs"):
        OptimizeIssueRequest.from_document(
            {
                **_generic_issue_request(),
                "issue_evaluators": [
                    "azureai://built-in/evaluators/safety",
                    "azureai://built-in/evaluators/safety weight=2",
                ],
            }
        )


def test_issue_request_accepts_explicit_target_when_not_repo_agent_id() -> None:
    issue = OptimizeIssueRequest.from_document(
        {
            **{k: v for k, v in _generic_issue_request().items() if k != "repo_agent_id"},
            "target": "azureai://accounts/example/projects/example/agents/demo",
        }
    )

    assert issue.repo_agent_id is None
    assert issue.explicit_target == "azureai://accounts/example/projects/example/agents/demo"


@pytest.mark.parametrize(
    "line",
    [
        "azureai://built-in/evaluators/safety weight=0",
        "azureai://built-in/evaluators/safety weight=nan",
        "azureai://built-in/evaluators/safety weight=nope",
        "azureai://built-in/evaluators/safety extra=1",
    ],
)
def test_issue_request_rejects_invalid_evaluator_lines(line: str) -> None:
    with pytest.raises(POCConfigurationError, match="issue evaluator entry|weight must be a positive finite number"):
        OptimizeIssueRequest.from_document(
            {
                **_generic_issue_request(),
                "issue_evaluators": [line],
            }
        )


def test_issue_request_rejects_model_widening() -> None:
    policy = RepositoryPolicy.from_document(_generic_repository_policy())
    issue = OptimizeIssueRequest.from_document(
        {
            **_generic_issue_request(),
            "model_subset": ["gpt-5-mini", "gpt-4.1"],
        }
    )

    with pytest.raises(IssueNarrowingError):
        apply_issue_request(policy, issue)


def test_issue_request_rejects_editable_scope_widening() -> None:
    policy = RepositoryPolicy.from_document(_generic_repository_policy())
    issue = OptimizeIssueRequest.from_document(
        {
            **_generic_issue_request(),
            "editable_scope_subset": [
                "agent/agent_config/baseline/instructions.md",
                "agent/tools.py",
            ],
        }
    )

    with pytest.raises(IssueNarrowingError):
        apply_issue_request(policy, issue)


def test_issue_request_rejects_budget_widening() -> None:
    policy = RepositoryPolicy.from_document(_generic_repository_policy())
    issue = OptimizeIssueRequest.from_document(
        {
            **_generic_issue_request(),
            "candidate_budget": "baseline+3",
        }
    )

    with pytest.raises(IssueNarrowingError):
        apply_issue_request(policy, issue)
