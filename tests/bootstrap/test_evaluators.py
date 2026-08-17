from __future__ import annotations

import pytest
from pydantic import ValidationError

from foundry_opt.bootstrap.contracts import (
    DefaultEvaluatorBundle,
    EvaluatorReference,
    ImmutableDatasetReference,
    ImmutableDefinitionReference,
    IssueEvaluatorRequest,
    IssueEvaluatorRequestEntry,
    ResolvedWeightedObjective,
    ScoreNormalizationContract,
)
from foundry_opt.bootstrap.errors import BootstrapConfigError


def test_issue_evaluators_normalize_equal_weights_when_omitted() -> None:
    resolved = ResolvedWeightedObjective.create((
        IssueEvaluatorRequestEntry(evaluator=EvaluatorReference(evaluator_id='azureai://accounts/a/projects/p/evaluators/quality/versions/1', provenance='reused_existing')),
        IssueEvaluatorRequestEntry(evaluator=EvaluatorReference(evaluator_id='azureai://accounts/a/projects/p/evaluators/safety/versions/1', provenance='auto_generated_unreviewed')),
    ))
    assert resolved.normalized_weights == (0.5, 0.5)


def test_issue_evaluators_mixed_weights_default_omitted_to_one() -> None:
    resolved = ResolvedWeightedObjective.create((
        IssueEvaluatorRequestEntry(evaluator=EvaluatorReference(evaluator_id='azureai://accounts/a/projects/p/evaluators/quality/versions/1', provenance='reused_existing'), weight=2.0),
        IssueEvaluatorRequestEntry(evaluator=EvaluatorReference(evaluator_id='azureai://accounts/a/projects/p/evaluators/safety/versions/1', provenance='issue_supplied_existing')),
    ))
    assert resolved.normalized_weights == pytest.approx((2.0 / 3.0, 1.0 / 3.0))


def test_issue_evaluator_request_rejects_invalid_weight_and_too_many() -> None:
    with pytest.raises(BootstrapConfigError):
        IssueEvaluatorRequest.from_document({'evaluators': [{'evaluator': {'evaluator_id': 'azureai://accounts/a/projects/p/evaluators/metric/versions/1', 'provenance': 'reused_existing'}, 'weight': 0.0}]})
    with pytest.raises(BootstrapConfigError):
        IssueEvaluatorRequest.from_document({'evaluators': [{'evaluator': {'evaluator_id': f'azureai://accounts/a/projects/p/evaluators/metric{i}/versions/1', 'provenance': 'reused_existing'}} for i in range(9)]})


def test_score_normalization_contract_is_fixed_zero_to_one() -> None:
    assert ScoreNormalizationContract().normalized_range == (0.0, 1.0)
    with pytest.raises(ValidationError):
        ScoreNormalizationContract(minimum=-1.0)


def test_default_evaluator_bundle_keeps_immutable_refs() -> None:
    bundle = DefaultEvaluatorBundle(
        objective=ResolvedWeightedObjective.create((IssueEvaluatorRequestEntry(evaluator=EvaluatorReference(evaluator_id='azureai://accounts/a/projects/p/evaluators/quality/versions/1', provenance='reused_existing')),)),
        datasets=(ImmutableDatasetReference(dataset_id='azureai://accounts/a/projects/p/data/dataset/versions/2026.08.17'),),
        definitions=(ImmutableDefinitionReference(definition_id='azureai://accounts/a/projects/p/evaluationDefinitions/definition/versions/1'),),
    )
    assert bundle.datasets[0].dataset_id.endswith('/versions/2026.08.17')
