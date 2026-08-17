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
    ActivationReceipt,
    DeploymentDefaults,
    build_scoring_evidence,
    choose_dataset_strategy,
    choose_default_evaluator_bundle,
    compute_split_lineage_hash,
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


def _generated_bundle() -> DefaultEvaluatorBundle:
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


def test_dataset_strategy_requires_strict_boolean_prerequisites() -> None:
    with pytest.raises(BootstrapConfigError):
        choose_dataset_strategy({"generated_samples": 15, "prerequisites_available": "false"})
    assert choose_dataset_strategy({"generated_samples": 15, "prerequisites_available": False}) == "synthetic_only"
    assert choose_dataset_strategy({"generated_samples": 15, "prerequisites_available": True}) == "trace"


def test_dataset_split_rejects_duplicate_row_ids_across_groups_and_conflicts() -> None:
    with pytest.raises(BootstrapConfigError):
        split_dataset_rows([
            {"row_id": "row-1", "group_id": "group-a", "category": "a"},
            {"row_id": "row-1", "group_id": "group-b", "category": "a"},
        ] + _split_input()[:28])
    with pytest.raises(BootstrapConfigError):
        split_dataset_rows([
            {"row_id": "row-1", "group_id": "group-a", "category": "a"},
            {"row_id": "row-2", "group_id": "group-a", "category": "b"},
        ] + _split_input()[:28])


def test_dataset_split_hash_is_order_independent_and_lineage_is_canonical() -> None:
    first = split_dataset_rows(_split_input())
    second = split_dataset_rows(list(reversed(_split_input())))
    assert first.split_hash == second.split_hash
    assert compute_split_lineage_hash(first) == compute_split_lineage_hash(second) == first.split_hash


def test_generated_rubric_rejects_invalid_thresholds_inputs_and_duplicates() -> None:
    with pytest.raises(BootstrapConfigError):
        validate_generated_rubric({"dimensions": [{"name": "quality", "weight": 1.0, "scalar_range": {"min": 0, "max": 1}, "threshold": 2.0, "required_inputs": ["answer"]}]})
    with pytest.raises(BootstrapConfigError):
        validate_generated_rubric({"dimensions": [{"name": "quality", "weight": 1.0, "threshold": 0.5, "pass_fail": True, "required_inputs": ["", "answer"]}]})
    validate_generated_rubric({"dimensions": [{"name": "quality", "weight": 1.0, "scalar_range": {"min": 0, "max": 1}, "threshold": 0.5, "required_inputs": ["answer"]}]})


def test_activation_requires_strict_boolean_executable_and_headroom_rules() -> None:
    with pytest.raises(BootstrapConfigError):
        validate_activation(
            cases=[{"executable": "false", "score": 0.5, "normalization": {"kind": "scalar", "source_min": 0.0, "source_max": 1.0}}],
            guardrails=[{"name": "Content Safety", "pass_rate": 1.0}],
        )
    validate_activation(
        cases=[
            {"executable": True, "score": 0.5, "normalization": {"kind": "scalar", "source_min": 0.0, "source_max": 1.0}},
            {"executable": True, "score": False, "normalization": {"kind": "pass_fail"}},
        ],
        guardrails=[{"name": "Content Safety", "pass_rate": 1.0}],
        generated_bundle={"provenance": "auto_generated_unreviewed"},
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
                "azureai://built-in/evaluators/groundedness": "false",
                "azureai://accounts/a/projects/p/evaluators/quality/versions/1": 4.0,
            },
        )


def test_generated_bundle_uses_distinct_refs_and_canonical_lineage() -> None:
    split = split_dataset_rows(_split_input())
    bundle = _generated_bundle()
    result = choose_default_evaluator_bundle(
        existing_bundle=None,
        generated_bundle=bundle,
        split_result=split,
        definitions=(ImmutableDefinitionReference(definition_id="eval_development"), ImmutableDefinitionReference(definition_id="eval_validating")),
        development_dataset=ImmutableDatasetReference(dataset_id="azureai://accounts/a/projects/p/data/dev/versions/1"),
        validating_dataset=ImmutableDatasetReference(dataset_id="azureai://accounts/a/projects/p/data/val/versions/1"),
        persisted_split_lineage_hash=compute_split_lineage_hash(split),
    )
    assert result.active_bundle == bundle
    with pytest.raises(BootstrapConfigError):
        choose_default_evaluator_bundle(
            existing_bundle=None,
            generated_bundle=bundle,
            split_result=split,
            definitions=(ImmutableDefinitionReference(definition_id="eval_development"), ImmutableDefinitionReference(definition_id="eval_validating")),
            development_dataset=ImmutableDatasetReference(dataset_id="azureai://accounts/a/projects/p/data/dev/versions/1"),
            validating_dataset=ImmutableDatasetReference(dataset_id="azureai://accounts/a/projects/p/data/dev/versions/1"),
            persisted_split_lineage_hash=compute_split_lineage_hash(split),
        )


def test_replace_requires_validated_metadata_and_reports_retained_old_on_failure() -> None:
    split = split_dataset_rows(_split_input())
    existing = _generated_bundle()
    generated = _generated_bundle()
    with pytest.raises(BootstrapConfigError):
        choose_default_evaluator_bundle(
            existing_bundle=existing,
            generated_bundle=generated,
            split_result=split,
            definitions=(ImmutableDefinitionReference(definition_id="eval_development"), ImmutableDefinitionReference(definition_id="eval_validating")),
            development_dataset=ImmutableDatasetReference(dataset_id="azureai://accounts/a/projects/p/data/dev/versions/1"),
            validating_dataset=ImmutableDatasetReference(dataset_id="azureai://accounts/a/projects/p/data/val/versions/1"),
            persisted_split_lineage_hash=compute_split_lineage_hash(split),
            explicit_replace=True,
            operation={"operation_id": "", "runtime_repository": "not-a-url", "runtime_commit": "bad", "repository_identity": "repo"},
            activation_receipt={"attempted": True, "activated": False, "status": "failed"},
        )
    failed = choose_default_evaluator_bundle(
        existing_bundle=existing,
        generated_bundle=generated,
        split_result=split,
        definitions=(ImmutableDefinitionReference(definition_id="eval_development"), ImmutableDefinitionReference(definition_id="eval_validating")),
        development_dataset=ImmutableDatasetReference(dataset_id="azureai://accounts/a/projects/p/data/dev/versions/1"),
        validating_dataset=ImmutableDatasetReference(dataset_id="azureai://accounts/a/projects/p/data/val/versions/1"),
        persisted_split_lineage_hash=compute_split_lineage_hash(split),
        explicit_replace=True,
        operation={"operation_id": "op-1", "runtime_repository": "https://github.com/org/repo.git", "runtime_commit": "a" * 40, "repository_identity": "org/repo"},
        activation_receipt={"attempted": True, "activated": False, "status": "failed"},
    )
    assert failed.active_bundle == existing
    assert failed.attempted_bundle == generated
    assert failed.activated_bundle is None
    assert failed.retained_bundle == existing
    succeeded = choose_default_evaluator_bundle(
        existing_bundle=existing,
        generated_bundle=generated,
        split_result=split,
        definitions=(ImmutableDefinitionReference(definition_id="eval_development"), ImmutableDefinitionReference(definition_id="eval_validating")),
        development_dataset=ImmutableDatasetReference(dataset_id="azureai://accounts/a/projects/p/data/dev/versions/1"),
        validating_dataset=ImmutableDatasetReference(dataset_id="azureai://accounts/a/projects/p/data/val/versions/1"),
        persisted_split_lineage_hash=compute_split_lineage_hash(split),
        explicit_replace=True,
        operation={"operation_id": "op-2", "runtime_repository": "https://github.com/org/repo.git", "runtime_commit": "b" * 40, "repository_identity": "org/repo"},
        activation_receipt=ActivationReceipt(attempted=True, activated=True, status="succeeded"),
    )
    assert succeeded.active_bundle == generated
    assert succeeded.activated_bundle == generated
    assert succeeded.retained_bundle == existing


def test_deployment_selector_rejects_camel_case_prohibited_keys() -> None:
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
                "nested": {"datasetRows": ["secret"]},
            }
        )
