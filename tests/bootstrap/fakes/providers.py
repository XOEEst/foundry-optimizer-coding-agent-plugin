from __future__ import annotations

from collections.abc import Mapping, Sequence

from foundry_opt.bootstrap.contracts import BootstrapPlan, BootstrapReceipt, ResolvedWeightedObjective


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
            runtime_repository=plan.runtime_repository,
            runtime_commit=plan.runtime_commit,
            repository_identity=plan.repository_identity,
            plan_hash=plan.plan_hash,
            created_actions=tuple(action.action_id for action in plan.actions),
        )

    def verify_changes(self, receipt: BootstrapReceipt) -> bool:
        return True

    def rollback_changes(self, receipt: BootstrapReceipt) -> None:
        return None


class FakeEvaluationOnboarding:
    def resolve_objective(self, request: Sequence[Mapping[str, object]]) -> ResolvedWeightedObjective:
        return ResolvedWeightedObjective.model_validate(request[0]['resolved'])

    def verify_objective(self, objective: ResolvedWeightedObjective) -> bool:
        return bool(objective.evaluators)

    def rollback_objective(self, objective: ResolvedWeightedObjective) -> None:
        return None
