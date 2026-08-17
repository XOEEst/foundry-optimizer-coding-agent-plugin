from __future__ import annotations

import pytest
from pydantic import ValidationError

from foundry_opt.bootstrap.contracts import (
    DefaultEvaluatorBundle,
    EvaluatorNormalization,
    EvaluatorReference,
    ImmutableDatasetReference,
    ImmutableDefinitionReference,
    IssueEvaluatorRequest,
    IssueEvaluatorRequestEntry,
    ResolvedEvaluator,
    ResolvedWeightedObjective,
)
from foundry_opt.bootstrap.errors import BootstrapConfigError


def test_issue_evaluators_normalize_equal_weights_when_omitted() -> None:
    resolved = ResolvedWeightedObjective.create((
        ResolvedEvaluator(reference=EvaluatorReference(evaluator_id='azureai://accounts/a/projects/p/evaluators/quality/versions/1', provenance='reused_existing'), normalization=EvaluatorNormalization(kind='pass_fail'), weight=1.0),
        ResolvedEvaluator(reference=EvaluatorReference(evaluator_id='azureai://built-in/evaluators/groundedness', provenance='auto_generated_unreviewed'), normalization=EvaluatorNormalization(kind='scalar', source_min=0.0, source_max=5.0), weight=1.0),
    ))
    assert tuple(item.weight for item in resolved.evaluators) == (0.5, 0.5)


def test_issue_request_accepts_builtin_and_versioned_ids() -> None:
    request = IssueEvaluatorRequest.from_document({'evaluators': [
        {'evaluator_id': 'azureai://built-in/evaluators/groundedness'},
        {'evaluator_id': 'azureai://accounts/a/projects/p/evaluators/quality/versions/1', 'weight': 2.0},
    ]})
    assert len(request.evaluators) == 2


def test_issue_evaluator_request_rejects_invalid_weight_and_too_many() -> None:
    with pytest.raises(BootstrapConfigError):
        IssueEvaluatorRequest.from_document({'evaluators': [{'evaluator_id': 'azureai://built-in/evaluators/groundedness', 'weight': 0.0}]})
    with pytest.raises(BootstrapConfigError):
        IssueEvaluatorRequest.from_document({'evaluators': [{'evaluator_id': f'azureai://built-in/evaluators/metric{i}'} for i in range(9)]})


def test_scalar_normalization_requires_bounds() -> None:
    with pytest.raises(ValidationError):
        EvaluatorNormalization(kind='scalar')


def test_default_evaluator_bundle_keeps_immutable_refs() -> None:
    bundle = DefaultEvaluatorBundle(
        objective=ResolvedWeightedObjective.create((ResolvedEvaluator(reference=EvaluatorReference(evaluator_id='azureai://accounts/a/projects/p/evaluators/quality/versions/1', provenance='reused_existing'), normalization=EvaluatorNormalization(kind='pass_fail'), weight=1.0),)),
        datasets=(ImmutableDatasetReference(dataset_id='azureai://accounts/a/projects/p/data/dataset/versions/2026.08.17'),),
        definitions=(ImmutableDefinitionReference(definition_id='eval_development'),),
    )
    assert bundle.definitions[0].definition_id == 'eval_development'
