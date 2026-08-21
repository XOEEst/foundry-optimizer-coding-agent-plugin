from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from foundry_opt.bootstrap.connection import (
    ConnectionApproval,
    GitHubAzureConnectionManager,
    next_connection_generation,
    read_connection_state,
    write_connection_state,
)
from foundry_opt.bootstrap.contracts import BootstrapAction, BootstrapPlan, BootstrapReceipt, FingerprintRecord, RedactedStatusInfo
from foundry_opt.bootstrap.errors import BootstrapApplyError
from foundry_opt.bootstrap.receipts import PhaseReceipt

REPOSITORY_ID = "org/repo"
RUNTIME_REPOSITORY = "https://github.com/org/repo.git"
RUNTIME_COMMIT = "a" * 40


def _action(action_id: str, phase: str) -> BootstrapAction:
    return BootstrapAction(action_id=action_id, phase=phase, stage="planned", kind=action_id)


class _Driver:
    def __init__(
        self,
        phase: str,
        *,
        actions: Sequence[BootstrapAction],
        live: Sequence[FingerprintRecord],
        created_actions: Sequence[str] = (),
        adopted_actions: Sequence[str] = (),
        changed_actions: Sequence[str] = (),
        compensation_required_actions: Sequence[str] = (),
        error_info: RedactedStatusInfo | None = None,
        resume_info: RedactedStatusInfo | None = None,
        provider_state: dict[str, object] | None = None,
        rollback_verify: bool = True,
        history: list[str] | None = None,
    ) -> None:
        self.phase = phase
        self.actions = tuple(actions)
        self.live = tuple(live)
        self.created_actions = tuple(created_actions)
        self.adopted_actions = tuple(adopted_actions)
        self.changed_actions = tuple(changed_actions)
        self.compensation_required_actions = tuple(compensation_required_actions)
        self.error_info = error_info
        self.resume_info = resume_info
        self.provider_state = provider_state or {"state": f"{phase}-state"}
        self.rollback_verify = rollback_verify
        self.history = history if history is not None else []
        self.applied: list[BootstrapPlan] = []
        self.exported: list[BootstrapReceipt] = []
        self.restored: list[dict[str, object]] = []
        self.rolled_back: list[BootstrapReceipt] = []
        self.rollback_verified: list[BootstrapReceipt] = []

    def live_fingerprints(self, _context):
        return self.live

    def plan(self, _context):
        return self.actions

    def apply(self, phase_plan: BootstrapPlan) -> BootstrapReceipt:
        self.applied.append(phase_plan)
        self.history.append(f"apply:{self.phase}")
        return BootstrapReceipt.create(
            operation_id=phase_plan.operation_id,
            runtime_repository=phase_plan.runtime_repository,
            runtime_commit=phase_plan.runtime_commit,
            repository_identity=phase_plan.repository_identity,
            plan_hash=phase_plan.plan_hash,
            created_actions=self.created_actions,
            adopted_actions=self.adopted_actions,
            changed_actions=self.changed_actions,
            compensation_required_actions=self.compensation_required_actions,
            error_info=self.error_info,
            resume_info=self.resume_info,
        )

    def verify(self, receipt: BootstrapReceipt) -> bool:
        return receipt.error_info is None

    def export_provider_state(self, receipt: BootstrapReceipt):
        self.exported.append(receipt)
        return {**self.provider_state, "phase_plan_hash": receipt.plan_hash}

    def restore_provider_state(self, mapping):
        restored = dict(mapping)
        self.restored.append(restored)
        self.history.append(f"restore:{self.phase}")

    def rollback(self, receipt: BootstrapReceipt) -> None:
        self.rolled_back.append(receipt)
        self.history.append(f"rollback:{self.phase}")

    def verify_rollback(self, receipt: BootstrapReceipt) -> bool:
        self.rollback_verified.append(receipt)
        self.history.append(f"verify_rollback:{self.phase}")
        return self.rollback_verify


def _manager(
    tmp_path: Path,
    *,
    github: _Driver,
    azure: _Driver,
) -> GitHubAzureConnectionManager:
    return GitHubAzureConnectionManager(
        github_driver=github,
        azure_driver=azure,
        state_root=tmp_path / "state",
    )


def _plan(manager: GitHubAzureConnectionManager):
    return manager.build_plan(
        repository_identity=REPOSITORY_ID,
        operation_id="connection-op",
        runtime_repository=RUNTIME_REPOSITORY,
        runtime_commit=RUNTIME_COMMIT,
    )


def _approval(manager: GitHubAzureConnectionManager, plan) -> ConnectionApproval:
    return manager.create_approval(plan, actor="owner", summary="approve connection")


def test_connection_apply_succeeds_when_both_phases_are_adopted(tmp_path: Path) -> None:
    github = _Driver(
        "github",
        actions=(_action("github-env", "github"), _action("github-var", "github")),
        live=(FingerprintRecord(label="github:live", sha256="1" * 64),),
        adopted_actions=("github-env", "github-var"),
    )
    azure = _Driver(
        "azure",
        actions=(_action("azure-fic", "azure"), _action("azure-role", "azure")),
        live=(FingerprintRecord(label="azure:live", sha256="2" * 64),),
        adopted_actions=("azure-fic", "azure-role"),
    )
    manager = _manager(tmp_path, github=github, azure=azure)
    plan = _plan(manager)
    approval = _approval(manager, plan)

    receipt = manager.apply(plan, approval)
    status = manager.status(
        repository_identity=REPOSITORY_ID,
        operation_id="connection-op",
        runtime_commit=RUNTIME_COMMIT,
    )

    assert receipt.overall_state == "applied"
    assert receipt.phase_states[0].plan_hash == plan.phase_plan("github").plan.plan_hash
    assert receipt.phase_states[1].plan_hash == plan.phase_plan("azure").plan.plan_hash
    assert receipt.phase_states[0].receipt_hash == github.exported[0].receipt_hash
    assert receipt.phase_states[1].receipt_hash == azure.exported[0].receipt_hash
    assert receipt.phase_states[0].adopted_actions == ("github-env", "github-var")
    assert receipt.phase_states[1].adopted_actions == ("azure-fic", "azure-role")
    assert status.overall_state == "applied"
    assert status.rollback_ready is True


def test_connection_receipt_preserves_mixed_created_and_adopted_child_receipts(tmp_path: Path) -> None:
    github = _Driver(
        "github",
        actions=(
            _action("github-env", "github"),
            _action("github-var", "github"),
            _action("github-branch", "github"),
        ),
        live=(FingerprintRecord(label="github:live", sha256="3" * 64),),
        created_actions=("github-env",),
        adopted_actions=("github-branch",),
        changed_actions=("github-var",),
    )
    azure = _Driver(
        "azure",
        actions=(_action("azure-fic", "azure"), _action("azure-role", "azure")),
        live=(FingerprintRecord(label="azure:live", sha256="4" * 64),),
        created_actions=("azure-fic",),
        adopted_actions=("azure-role",),
    )
    manager = _manager(tmp_path, github=github, azure=azure)
    plan = _plan(manager)
    approval = _approval(manager, plan)

    receipt = manager.apply(plan, approval)

    assert [action.action_id for action in plan.phase_plan("github").plan.actions] == [
        "github-env",
        "github-var",
        "github-branch",
    ]
    assert receipt.overall_state == "applied"
    assert receipt.phase_states[0].created_actions == ("github-env",)
    assert receipt.phase_states[0].adopted_actions == ("github-branch",)
    assert receipt.phase_states[0].changed_actions == ("github-var",)
    assert receipt.phase_states[1].created_actions == ("azure-fic",)
    assert receipt.phase_states[1].adopted_actions == ("azure-role",)


def test_connection_compensates_github_after_azure_failure(tmp_path: Path) -> None:
    github = _Driver(
        "github",
        actions=(_action("github-env", "github"), _action("github-var", "github")),
        live=(FingerprintRecord(label="github:live", sha256="5" * 64),),
        created_actions=("github-env",),
        adopted_actions=("github-env-shared",),
        changed_actions=("github-var",),
        provider_state={"provider": "github"},
    )
    azure = _Driver(
        "azure",
        actions=(_action("azure-fic", "azure"),),
        live=(FingerprintRecord(label="azure:live", sha256="6" * 64),),
        changed_actions=("azure-fic",),
        compensation_required_actions=("azure-fic",),
        error_info=RedactedStatusInfo(code="azure-failed", summary="apply failed"),
        resume_info=RedactedStatusInfo(code="resume", summary="resume"),
        provider_state={"provider": "azure"},
    )
    manager = _manager(tmp_path, github=github, azure=azure)
    plan = _plan(manager)
    approval = _approval(manager, plan)

    receipt = manager.apply(plan, approval)

    assert receipt.overall_state == "failed"
    assert [state.state for state in receipt.phase_states] == [
        "rolled_back",
        "compensation_required",
    ]
    assert github.restored == [{"provider": "github", "phase_plan_hash": plan.phase_plan("github").plan.plan_hash}]
    assert [item.receipt_hash for item in github.rolled_back] == [
        receipt.phase_states[0].receipt_hash
    ]
    assert azure.rolled_back == []
    assert receipt.phase_states[1].compensation_required_actions == ("azure-fic",)


def test_connection_rollback_verifies_children_in_reverse_order(tmp_path: Path) -> None:
    history: list[str] = []
    github = _Driver(
        "github",
        actions=(_action("github-env", "github"),),
        live=(FingerprintRecord(label="github:live", sha256="7" * 64),),
        created_actions=("github-env",),
        history=history,
    )
    azure = _Driver(
        "azure",
        actions=(_action("azure-fic", "azure"),),
        live=(FingerprintRecord(label="azure:live", sha256="8" * 64),),
        created_actions=("azure-fic",),
        history=history,
    )
    manager = _manager(tmp_path, github=github, azure=azure)
    plan = _plan(manager)
    approval = _approval(manager, plan)

    manager.apply(plan, approval)
    receipt = manager.rollback(plan, approval)
    status = manager.status(
        repository_identity=REPOSITORY_ID,
        operation_id="connection-op",
        runtime_commit=RUNTIME_COMMIT,
    )

    assert receipt.overall_state == "rolled_back"
    assert [state.state for state in receipt.phase_states] == ["rolled_back", "rolled_back"]
    assert history[-6:] == [
        "restore:azure",
        "rollback:azure",
        "verify_rollback:azure",
        "restore:github",
        "rollback:github",
        "verify_rollback:github",
    ]
    assert status.overall_state == "rolled_back"
    assert status.rollback_ready is False


def test_connection_refuses_stale_approval_and_runtime(tmp_path: Path) -> None:
    github = _Driver(
        "github",
        actions=(_action("github-env", "github"),),
        live=(FingerprintRecord(label="github:live", sha256="9" * 64),),
    )
    azure = _Driver(
        "azure",
        actions=(_action("azure-fic", "azure"),),
        live=(FingerprintRecord(label="azure:live", sha256="a" * 64),),
    )
    manager = _manager(tmp_path, github=github, azure=azure)
    plan = _plan(manager)

    stale = ConnectionApproval.create(
        repository_identity=REPOSITORY_ID,
        operation_id="connection-op",
        parent_plan_hash="f" * 64,
        runtime_commit="b" * 40,
        actor="owner",
        summary="stale",
    )

    with pytest.raises(BootstrapApplyError):
        manager.apply(plan, stale)
    with pytest.raises(BootstrapApplyError):
        manager.status(
            repository_identity=REPOSITORY_ID,
            operation_id="connection-op",
            runtime_commit="b" * 40,
        )


def test_connection_resume_reuses_recorded_github_phase_and_finishes_azure(tmp_path: Path) -> None:
    github = _Driver(
        "github",
        actions=(_action("github-env", "github"),),
        live=(FingerprintRecord(label="github:live", sha256="b" * 64),),
        created_actions=("github-env",),
        provider_state={"provider": "github"},
    )
    azure = _Driver(
        "azure",
        actions=(_action("azure-fic", "azure"),),
        live=(FingerprintRecord(label="azure:live", sha256="c" * 64),),
        created_actions=("azure-fic",),
        provider_state={"provider": "azure"},
    )
    manager = _manager(tmp_path, github=github, azure=azure)
    plan = _plan(manager)
    approval = _approval(manager, plan)
    manager.bind_approval(plan, approval)

    envelope = read_connection_state(
        REPOSITORY_ID,
        "connection-op",
        state_root=tmp_path / "state",
    )
    github_receipt = BootstrapReceipt.create(
        operation_id=plan.operation_id,
        runtime_repository=plan.runtime_repository,
        runtime_commit=plan.runtime_commit,
        repository_identity=plan.repository_identity,
        plan_hash=plan.phase_plan("github").plan.plan_hash,
        created_actions=("github-env",),
    )
    github_phase = PhaseReceipt(
        phase="github",
        state="applied",
        provider="github",
        receipt=github_receipt,
        parent_plan_hash=plan.plan_hash,
        phase_plan_hash=plan.phase_plan("github").plan.plan_hash,
        approval_hash=approval.approval_hash,
        summary="github resumed",
        provider_state={
            "provider": "github",
            "phase_plan_hash": plan.phase_plan("github").plan.plan_hash,
        },
        recorded_fingerprints=plan.phase_plan("github").live_fingerprints,
    )
    resumed = next_connection_generation(
        envelope,
        approval=approval,
        phase_receipts=(github_phase,),
    )
    write_connection_state(
        resumed,
        expected_generation=envelope.generation,
        state_root=tmp_path / "state",
    )

    status = manager.status(
        repository_identity=REPOSITORY_ID,
        operation_id="connection-op",
        runtime_commit=RUNTIME_COMMIT,
    )
    receipt = manager.apply(plan, approval)

    assert status.overall_state == "partial"
    assert status.resumable is True
    assert github.applied == []
    assert len(azure.applied) == 1
    assert receipt.overall_state == "applied"
