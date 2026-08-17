from __future__ import annotations

import pytest

from foundry_opt.bootstrap.contracts import (
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
    DeploymentDefaults,
    ReplacementOperation,
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


def _split_input() -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for category in ("a", "b", "c"):
        for index in range(6):
            group = f"{category}-group-{index}"
            rows.append({"row_id": f"{group}-1", "group_id": group, "category": category})
            rows.append({"row_id": f"{group}-2", "group_id": group, "category": category})
    return rows


def _generated_bundle(split_hash: str) -> DefaultEvaluatorBundle:
    return DefaultEvaluatorBundle(
        objective=_objective(),
        datasets=(
            ImmutableDatasetReference(dataset_id="azureai://accounts/a/projects/p/data/dev/versions/1"),
            ImmutableDatasetReference(dataset_id="azureai://accounts/a/projects/p/data/val/versions/1"),
        ),
        definitions=(
            ImmutableDefinitionReference(definition_id="eval_development"),
            ImmutableDefinitionReference(definition_id="eval_validating"),
        ),
    )


def test_dataset_strategy_requires_prerequisites_and_threshold() -> None:
    assert choose_dataset_strategy({"generated_samples": 15, "prerequisites_available": False}) == "synthetic_only"
    assert choose_dataset_strategy({"generated_samples": 14, "prerequisites_available": True}) == "synthetic_only"
    assert choose_dataset_strategy({"generated_samples": 15, "prerequisites_available": True}) == "trace"


def test_dataset_split_is_atomic_and_category_aware() -> None:
    result = split_dataset_rows(_split_input())
    assert result.algorithm_version == "evaluation-core-split/v2"
    assert len(result.development_groups) == 12
    assert len(result.validating_groups) == 6
    assert set(result.development_groups).isdisjoint(result.validating_groups)
    for group in result.development_groups:
        assert all(not row_id.startswith(group) or row_id in result.development for row_id in result.development)
    assert result.split_hash == split_dataset_rows(list(reversed(_split_input()))).split_hash


def test_dataset_split_never_splits_group_and_keeps_minimums() -> None:
    result = split_dataset_rows(_split_input())
    assert len(result.development) >= 10
    assert len(result.validating) >= 5
    assert set(result.development).isdisjoint(result.validating)


def test_generated_rubric_rejects_invalid_thresholds_inputs_and_duplicates() -> None:
    with pytest.raises(BootstrapConfigError):
        validate_generated_rubric({"dimensions": [{"name": "quality", "weight": 1.0, "scalar_range": {"min": 0, "max": 1}, "threshold": 2.0, "required_inputs": ["answer"]}]})
    with pytest.raises(BootstrapConfigError):
        validate_generated_rubric({"dimensions": [{"name": "quality", "weight": 1.0, "threshold": 0.5, "pass_fail": True, "required_inputs": ["", "answer"]}]})
    with pytest.raises(BootstrapConfigError):
        validate_generated_rubric({"dimensions": [{"name": "quality", "weight": 1.0, "threshold": 0.5, "pass_fail": True, "required_inputs": ["answer", "Answer"]}]})
    validate_generated_rubric({"dimensions": [{"name": "quality", "weight": 1.0, "scalar_range": {"min": 0, "max": 1}, "threshold": 0.5, "required_inputs": ["answer"]}]})


def test_activation_headroom_uses_normalization_rules() -> None:
    validate_activation(
        cases=[
            {"executable": True, "score": 0.5, "normalization": {"kind": "scalar", "source_min": 0.0, "source_max": 1.0}},
            {"executable": True, "score": False, "normalization": {"kind": "pass_fail"}},
        ],
        guardrails=[{"name": "Content Safety", "pass_rate": 1.0}],
        generated_bundle={"provenance": "auto_generated_unreviewed"},
    )
    with pytest.raises(BootstrapConfigError):
        validate_activation(
            cases=[
                {"executable": True, "score": 1.0, "normalization": {"kind": "scalar", "source_min": 0.0, "source_max": 1.0}},
                {"executable": True, "score": True, "normalization": {"kind": "pass_fail"}},
            ],
            guardrails=[{"name": "Content Safety", "pass_rate": 1.0}],
        )


def test_issue_evaluator_resolution_and_pass_fail_normalization() -> None:
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
    evidence = build_scoring_evidence(
        "baseline",
        resolved,
        {
            "azureai://built-in/evaluators/groundedness": False,
            "azureai://accounts/a/projects/p/evaluators/quality/versions/1": 4.0,
        },
    )
    assert evidence.aggregate_score < 1.0
    with pytest.raises(BootstrapConfigError):
        build_scoring_evidence(
            "baseline",
            resolved,
            {
                "azureai://built-in/evaluators/groundedness": 0.5,
                "azureai://accounts/a/projects/p/evaluators/quality/versions/1": 4.0,
            },
        )


def test_generated_bundle_uses_real_dataset_refs_and_lineage_hash() -> None:
    split = split_dataset_rows(_split_input())
    bundle = _generated_bundle(split.split_hash)
    result = choose_default_evaluator_bundle(
        existing_bundle=None,
        generated_bundle=bundle,
        split_result=split,
        definitions=(ImmutableDefinitionReference(definition_id="eval_development"), ImmutableDefinitionReference(definition_id="eval_validating")),
        development_dataset=ImmutableDatasetReference(dataset_id="azureai://accounts/a/projects/p/data/dev/versions/1"),
        validating_dataset=ImmutableDatasetReference(dataset_id="azureai://accounts/a/projects/p/data/val/versions/1"),
        split_lineage_hash=split.split_hash,
    )
    assert result.active_bundle == bundle


def test_replace_requires_existing_bundle_and_preserves_old_on_failure() -> None:
    split = split_dataset_rows(_split_input())
    existing = _generated_bundle(split.split_hash)
    generated = _generated_bundle(split.split_hash)
    with pytest.raises(BootstrapConfigError):
        choose_default_evaluator_bundle(
            existing_bundle=None,
            generated_bundle=generated,
            split_result=split,
            definitions=(ImmutableDefinitionReference(definition_id="eval_development"), ImmutableDefinitionReference(definition_id="eval_validating")),
            development_dataset=ImmutableDatasetReference(dataset_id="azureai://accounts/a/projects/p/data/dev/versions/1"),
            validating_dataset=ImmutableDatasetReference(dataset_id="azureai://accounts/a/projects/p/data/val/versions/1"),
            split_lineage_hash=split.split_hash,
            explicit_replace=True,
            operation=ReplacementOperation(operation_id="op-1", runtime_repository="https://github.com/org/repo.git", runtime_commit="a" * 40, repository_identity="org/repo"),
        )
    failed = choose_default_evaluator_bundle(
        existing_bundle=existing,
        generated_bundle=generated,
        split_result=split,
        definitions=(ImmutableDefinitionReference(definition_id="eval_development"), ImmutableDefinitionReference(definition_id="eval_validating")),
        development_dataset=ImmutableDatasetReference(dataset_id="azureai://accounts/a/projects/p/data/dev/versions/1"),
        validating_dataset=ImmutableDatasetReference(dataset_id="azureai://accounts/a/projects/p/data/val/versions/1"),
        split_lineage_hash=split.split_hash,
        explicit_replace=True,
        operation=ReplacementOperation(operation_id="op-1", runtime_repository="https://github.com/org/repo.git", runtime_commit="a" * 40, repository_identity="org/repo"),
        activation_succeeded=False,
    )
    assert failed.active_bundle == existing
    assert failed.replaced_bundle == existing
    succeeded = choose_default_evaluator_bundle(
        existing_bundle=existing,
        generated_bundle=generated,
        split_result=split,
        definitions=(ImmutableDefinitionReference(definition_id="eval_development"), ImmutableDefinitionReference(definition_id="eval_validating")),
        development_dataset=ImmutableDatasetReference(dataset_id="azureai://accounts/a/projects/p/data/dev/versions/1"),
        validating_dataset=ImmutableDatasetReference(dataset_id="azureai://accounts/a/projects/p/data/val/versions/1"),
        split_lineage_hash=split.split_hash,
        explicit_replace=True,
        operation=ReplacementOperation(operation_id="op-2", runtime_repository="https://github.com/org/repo.git", runtime_commit="b" * 40, repository_identity="org/repo"),
        activation_succeeded=True,
    )
    assert succeeded.active_bundle == generated
    assert succeeded.previous_bundle == existing


def test_deployment_selector_returns_typed_defaults_and_rejects_raw_fields() -> None:
    defaults = select_default_deployment_contract(
        {
            "environment": "prod",
            "require_aligned_binding": True,
            "enabled": True,
            "hard_guardrail_names": ["safety"],
        }
    )
    assert defaults == DeploymentDefaults(environment="prod", require_aligned_binding=True, enabled=True, hard_guardrail_names=("safety",))
    with pytest.raises(BootstrapConfigError):
        select_default_deployment_contract(
            {
                "environment": "prod",
                "require_aligned_binding": True,
                "enabled": True,
                "hard_guardrail_names": ["safety"],
                "nested": {"dataset_rows": ["secret"]},
            }
        )
