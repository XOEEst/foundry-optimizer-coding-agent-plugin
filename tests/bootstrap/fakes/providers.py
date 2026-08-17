from __future__ import annotations

from collections.abc import Mapping, Sequence

from foundry_opt.bootstrap.contracts import BootstrapPlan, BootstrapReceipt, IssueEvaluatorRequestEntry, ResolvedWeightedObjective


class FakeGitHubApply:
    def __init__(self) -> None:
        self.plans: list[BootstrapPlan] = []

    def plan_changes(self, plan: BootstrapPlan) -> Sequence[Mapping[str, object]]:
        self.plans.append(plan)
        return ({'operation_id': plan.operation_id},)

    def apply_changes(self, plan: BootstrapPlan) -> BootstrapReceipt:
        self.plans.append(plan)
        return BootstrapReceipt.create(
            operation_id=plan.operation_id,
            runtime_sha256=plan.runtime_sha256,
            repository_identity=plan.repository_identity,
            plan_hash=plan.plan_hash,
            created_actions=[action.action_id for action in plan.actions],
        )

    def verify_changes(self, receipt: BootstrapReceipt) -> bool:
        return True

    def rollback_changes(self, receipt: BootstrapReceipt) -> None:
        return None


class FakeEvaluationOnboarding:
    def resolve_objective(self, request: Sequence[IssueEvaluatorRequestEntry]) -> ResolvedWeightedObjective:
        return ResolvedWeightedObjective.create(request)
