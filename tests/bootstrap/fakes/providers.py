from __future__ import annotations

from collections.abc import Mapping, Sequence

from foundry_opt.bootstrap.contracts import BootstrapPlan, BootstrapReceipt, IssueEvaluatorRequestEntry, ResolvedWeightedObjective


class FakeGitHubApply:
    def __init__(self) -> None:
        self.plans: list[BootstrapPlan] = []

    def plan_changes(self, plan: BootstrapPlan) -> Sequence[Mapping[str, object]]:
        self.plans.append(plan)
        return ({'plan_id': plan.plan_id},)

    def apply_changes(self, plan: BootstrapPlan) -> BootstrapReceipt:
        self.plans.append(plan)
        return BootstrapReceipt.create(plan_hash=plan.plan_hash, applied_actions=[action.action_id for action in plan.actions])

    def verify_changes(self, receipt: BootstrapReceipt) -> bool:
        return True

    def rollback_changes(self, receipt: BootstrapReceipt) -> None:
        return None


class FakeEvaluationOnboarding:
    def resolve_objective(self, request: Sequence[IssueEvaluatorRequestEntry]) -> ResolvedWeightedObjective:
        return ResolvedWeightedObjective.create(request)
