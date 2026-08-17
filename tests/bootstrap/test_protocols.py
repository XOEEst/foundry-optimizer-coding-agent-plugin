from __future__ import annotations

from foundry_opt.bootstrap.contracts import BootstrapAction, EvaluatorReference, IssueEvaluatorRequestEntry, ResolvedWeightedObjective
from tests.bootstrap.fakes import FakeEvaluationOnboarding, FakeGitHubApply


def test_fake_github_apply_returns_receipt() -> None:
    fake = FakeGitHubApply()
    class PlanLike:
        plan_id = 'plan-1'
        plan_hash = 'a' * 64
        actions = (BootstrapAction(action_id='a1', phase='apply', kind='write'),)

    plan = PlanLike()
    receipt = fake.apply_changes(plan)
    assert receipt.plan_hash == 'a' * 64
    assert receipt.applied_actions == ('a1',)


def test_fake_evaluation_resolves_objective() -> None:
    fake = FakeEvaluationOnboarding()
    resolved = fake.resolve_objective((IssueEvaluatorRequestEntry(evaluator=EvaluatorReference(evaluator_id='metric@1', provenance='reused_existing'), weight=2.0),))
    assert isinstance(resolved, ResolvedWeightedObjective)
    assert resolved.normalized_weights == (1.0,)
