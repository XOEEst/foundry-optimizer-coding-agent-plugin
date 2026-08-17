from __future__ import annotations

from foundry_opt.bootstrap.contracts import BootstrapAction, EvaluatorNormalization, EvaluatorReference, ResolvedEvaluator, ResolvedWeightedObjective
from tests.bootstrap.fakes import FakeEvaluationOnboarding, FakeGitHubApply


def test_fake_github_apply_returns_receipt() -> None:
    fake = FakeGitHubApply()
    class PlanLike:
        operation_id = 'op-1'
        runtime_repository = 'https://github.com/example/runtime.git'
        runtime_commit = 'a' * 40
        repository_identity = 'org/repo'
        plan_hash = 'b' * 64
        actions = (BootstrapAction(action_id='a1', phase='repository', stage='planned', kind='write'),)
    receipt = fake.apply_changes(PlanLike())
    assert receipt.plan_hash == 'b' * 64
    assert receipt.created_actions == ('a1',)


def test_fake_evaluation_resolves_objective() -> None:
    fake = FakeEvaluationOnboarding()
    resolved = ResolvedWeightedObjective.create((ResolvedEvaluator(reference=EvaluatorReference(evaluator_id='azureai://accounts/a/projects/p/evaluators/metric/versions/1', provenance='reused_existing'), normalization=EvaluatorNormalization(kind='pass_fail'), weight=1.0),))
    echoed = fake.resolve_objective(({'resolved': resolved.model_dump(mode='json')},))
    assert isinstance(echoed, ResolvedWeightedObjective)
