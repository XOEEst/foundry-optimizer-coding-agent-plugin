from __future__ import annotations

import pytest

from foundry_opt.bootstrap.contracts import (
    BootstrapAction,
    DefaultEvaluatorBundle,
    EvaluatorNormalization,
    EvaluatorReference,
    ImmutableDatasetReference,
    ImmutableDefinitionReference,
    ResolvedEvaluator,
    ResolvedWeightedObjective,
)
from foundry_opt.bootstrap.errors import BootstrapConfigError
from foundry_opt.bootstrap.evaluation.core import (
    assess_dataset_suitability,
    build_scoring_evidence,
    choose_dataset_strategy,
    choose_default_evaluator_bundle,
    resolve_issue_evaluators,
    select_default_deployment_contract,
    split_dataset_rows,
    validate_activation,
    validate_generated_rubric,
)


def _objective() -> ResolvedWeightedObjective:
    return ResolvedWeightedObjective.create((
        ResolvedEvaluator(
            reference=EvaluatorReference(evaluator_id="azureai://built-in/evaluators/groundedness", provenance="auto_generated_unreviewed"),
            normalization=EvaluatorNormalization(kind="pass_fail"),
            weight=1.0,
        ),
        ResolvedEvaluator(
            reference=EvaluatorReference(evaluator_id="azureai://accounts/a/projects/p/evaluators/quality/versions/1", provenance="reused_existing"),
            normalization=EvaluatorNormalization(kind="scalar", source_min=0.0, source_max=5.0),
            weight=2.0,
        ),
    ))


def test_dataset_strategy_uses_trace_only_at_15_or_more_generated_samples() -> None:
    assert choose_dataset_strategy({"generated_samples": 14}) == "synthetic_only"
    assert choose_dataset_strategy({"generated_samples": 15}) == "trace"


def test_dataset_split_is_stable_and_preserves_groups() -> None:
    rows = [
        {"row_id": f"row-{index}", "group_id": f"group-{index}", "category": "a" if index % 2 == 0 else "b"}
        for index in range(15)
    ]
    result = split_dataset_rows(rows)
    assert result.algorithm_version == "evaluation-core-split/v1"
    assert len(result.development) == 10
    assert len(result.validating) == 5
    assert result.split_hash == split_dataset_rows(list(reversed(rows))).split_hash


def test_dataset_split_deduplicates_and_rejects_overlap() -> None:
    rows = [{"row_id": f"row-{index}", "group_id": f"group-{index // 2}", "category": "a"} for index in range(30)]
    result = split_dataset_rows(rows)
    dev = set(result.development)
    val = set(result.validating)
    assert dev.isdisjoint(val)


def test_activation_requires_headroom_and_content_safety() -> None:
    validate_activation(
        cases=[{"executable": True, "score": 0.1}, {"executable": True, "score": 0.9}],
        guardrails=[{"name": "Content Safety", "pass_rate": 1.0}],
        generated_bundle={"provenance": "auto_generated_unreviewed"},
    )
    with pytest.raises(BootstrapConfigError):
        validate_activation(
            cases=[{"executable": True, "score": 0.5}, {"executable": True, "score": 0.5}],
            guardrails=[{"name": "Content Safety", "pass_rate": 1.0}],
        )


def test_generated_rubric_rejects_malformed_dimensions() -> None:
    with pytest.raises(BootstrapConfigError):
        validate_generated_rubric({"dimensions": [{"name": "quality", "weight": 1.0, "scalar_range": {"min": 1, "max": 0}, "threshold": 0.5, "required_inputs": ["answer"]}]})
    validate_generated_rubric({"dimensions": [{"name": "quality", "weight": 1.0, "scalar_range": {"min": 0, "max": 1}, "threshold": 0.5, "required_inputs": ["answer"]}]})


def test_issue_evaluator_resolution_normalizes_mixed_weights_and_builtin_custom_ids() -> None:
    resolved = resolve_issue_evaluators(
        {"evaluators": [
            {"evaluator_id": "azureai://built-in/evaluators/groundedness"},
            {"evaluator_id": "azureai://accounts/a/projects/p/evaluators/quality/versions/1", "weight": 2.0},
        ]},
        metadata_by_id={
            "azureai://built-in/evaluators/groundedness": {"kind": "pass_fail", "provenance": "auto_generated_unreviewed"},
            "azureai://accounts/a/projects/p/evaluators/quality/versions/1": {"kind": "scalar", "bounds": {"min": 0.0, "max": 5.0}, "provenance": "reused_existing"},
        },
        fallback_objective=_objective(),
    )
    assert [round(item.weight, 4) for item in resolved.evaluators] == [round(1 / 3, 4), round(2 / 3, 4)]
    with pytest.raises(BootstrapConfigError):
        resolve_issue_evaluators(
            {"evaluators": [{"evaluator_id": "azureai://built-in/evaluators/missing"}]},
            metadata_by_id={},
            fallback_objective=_objective(),
        )


def test_scoring_evidence_is_stable() -> None:
    objective = _objective()
    baseline = build_scoring_evidence(
        "baseline",
        objective,
        {
            "azureai://built-in/evaluators/groundedness": 1.0,
            "azureai://accounts/a/projects/p/evaluators/quality/versions/1": 4.0,
        },
    )
    resumed = build_scoring_evidence(
        "baseline",
        objective,
        {
            "azureai://built-in/evaluators/groundedness": 1.0,
            "azureai://accounts/a/projects/p/evaluators/quality/versions/1": 4.0,
        },
    )
    assert baseline.score_hash == resumed.score_hash


def test_default_deployment_selector_returns_repository_defaults() -> None:
    defaults = {"environment": "prod", "guardrails": ["safety"]}
    assert select_default_deployment_contract(defaults) == defaults


def test_default_evaluator_bundle_replace_preserves_old_contract_on_failure() -> None:
    split = split_dataset_rows([{"row_id": f"row-{index}", "group_id": f"group-{index}", "category": "a"} for index in range(15)])
    generated = DefaultEvaluatorBundle(
        objective=_objective(),
        datasets=tuple(
            ImmutableDatasetReference(dataset_id=f"azureai://accounts/generated/projects/evaluation/data/{row_id}/versions/v1")
            for row_id in (*split.development, *split.validating)
        ),
        definitions=(ImmutableDefinitionReference(definition_id="eval_development"), ImmutableDefinitionReference(definition_id="eval_validating")),
    )
    result = choose_default_evaluator_bundle(
        existing_bundle=None,
        generated_bundle=generated,
        split_result=split,
        definitions=(ImmutableDefinitionReference(definition_id="eval_development"), ImmutableDefinitionReference(definition_id="eval_validating")),
        replace_actions=(BootstrapAction(action_id="replace-1", phase="evaluations", stage="planned", kind="replace"),),
        replacement_success=False,
    )
    assert result.status == "rollback_preserved_old_contract"
