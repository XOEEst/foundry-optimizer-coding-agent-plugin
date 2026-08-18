from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from foundry_opt.bootstrap.canonical import canonical_sha256
from foundry_opt.bootstrap.contracts import BindingAssessment, BootstrapAction, BootstrapPlan, BootstrapReceipt, FingerprintRecord, RedactedStatusInfo
from foundry_opt.bootstrap.errors import BootstrapApplyError
from foundry_opt.bootstrap.operation_state import OperationStateEnvelope, read_operation_state, write_operation_state
from foundry_opt.bootstrap.orchestrator import BootstrapOrchestrator
from foundry_opt.bootstrap.providers.foundry import FoundryPlatformError, FoundryRollbackError
from foundry_opt.bootstrap.providers.github import GitHubProviderRollbackError
from foundry_opt.bootstrap.receipts import ApprovalRecord, EvaluationReplacementRecord


def _plan_action(action_id: str, phase: str, *, target: str | None = None, kind: str | None = None) -> BootstrapAction:
    return BootstrapAction(action_id=action_id, phase=phase, stage="planned", kind=kind or action_id, target_agent_id=target)


class _Driver:
    def __init__(self, phase: str, *, actions: tuple[BootstrapAction, ...], live: tuple[FingerprintRecord, ...], receipt_actions: str = "changed", verify: bool = True, fail_with_receipt: BootstrapReceipt | None = None, rollback_verify: bool = True, provider_state: dict[str, object] | None = None) -> None:
        self.phase = phase
        self.actions = actions
        self.live = live
        self.verify_result = verify
        self.fail_with_receipt = fail_with_receipt
        self.rollback_verify = rollback_verify
        self.provider_state = provider_state or {"stateKey": f"{phase}-state"}
        self.applied: list[BootstrapPlan] = []
        self.restored: list[dict[str, object]] = []
        self.rolled_back: list[BootstrapReceipt] = []

    def live_fingerprints(self, context):
        return self.live

    def plan(self, context):
        return self.actions

    def apply(self, phase_plan: BootstrapPlan) -> BootstrapReceipt:
        self.applied.append(phase_plan)
        if self.fail_with_receipt is not None:
            return BootstrapReceipt.create(
                operation_id=phase_plan.operation_id,
                runtime_repository=phase_plan.runtime_repository,
                runtime_commit=phase_plan.runtime_commit,
                repository_identity=phase_plan.repository_identity,
                plan_hash=phase_plan.plan_hash,
                before_fingerprints=self.fail_with_receipt.before_fingerprints,
                after_fingerprints=self.fail_with_receipt.after_fingerprints,
                created_actions=self.fail_with_receipt.created_actions,
                adopted_actions=self.fail_with_receipt.adopted_actions,
                changed_actions=self.fail_with_receipt.changed_actions,
                skipped_actions=self.fail_with_receipt.skipped_actions,
                compensation_required_actions=self.fail_with_receipt.compensation_required_actions,
                error_info=self.fail_with_receipt.error_info,
                resume_info=self.fail_with_receipt.resume_info,
            )
        return BootstrapReceipt.create(
            operation_id=phase_plan.operation_id,
            runtime_repository=phase_plan.runtime_repository,
            runtime_commit=phase_plan.runtime_commit,
            repository_identity=phase_plan.repository_identity,
            plan_hash=phase_plan.plan_hash,
            changed_actions=tuple(action.action_id for action in phase_plan.actions),
        )

    def verify(self, receipt: BootstrapReceipt) -> bool:
        return self.verify_result and receipt.error_info is None

    def export_provider_state(self, receipt: BootstrapReceipt):
        return {**self.provider_state, "phase_plan_hash": receipt.plan_hash}

    def restore_provider_state(self, mapping):
        self.restored.append(dict(mapping))

    def rollback(self, receipt: BootstrapReceipt) -> None:
        self.rolled_back.append(receipt)

    def verify_rollback(self, receipt: BootstrapReceipt) -> bool:
        return self.rollback_verify


def _create_repo(root: Path) -> None:
    (root / ".foundry").mkdir(parents=True, exist_ok=True)
    (root / ".foundry" / "agent-metadata.yaml").write_text(
        "agent_name: root\nsource_root: app\npackage_root: app\nproject_endpoint: https://example\n",
        encoding="utf-8",
    )
    (root / "app").mkdir(exist_ok=True)
    (root / "app" / "main.py").write_text("import fastapi\napp = fastapi.FastAPI()\n", encoding="utf-8")


def _drivers():
    return {
        "repository": _Driver("repository", actions=(_plan_action("repo", "repository", target="root"),), live=(FingerprintRecord(label="repository:live", sha256="1" * 64),)),
        "github": _Driver("github", actions=(_plan_action("gh", "github"),), live=(FingerprintRecord(label="github:live", sha256="2" * 64),)),
        "azure": _Driver("azure", actions=(_plan_action("az", "azure", target="root"),), live=(FingerprintRecord(label="azure:live", sha256="3" * 64),)),
        "evaluations": _Driver("evaluations", actions=(_plan_action("eval", "evaluations", target="root"),), live=(FingerprintRecord(label="evaluations:live", sha256="4" * 64),)),
    }


def _build_orchestrator(tmp_path: Path, drivers: dict[str, _Driver] | None = None) -> tuple[BootstrapOrchestrator, dict[str, _Driver]]:
    active = drivers or _drivers()
    orch = BootstrapOrchestrator(repository_driver=active["repository"], github_driver=active["github"], azure_driver=active["azure"], evaluations_driver=active["evaluations"], state_root=tmp_path / "state")
    return orch, active


def _discover_and_plan(tmp_path: Path, orch: BootstrapOrchestrator, *, op: str = "op", selected_agents=None, evaluation_requests=(), replacement=None):
    repo_root = tmp_path / "repo"
    _create_repo(repo_root)
    discovered = orch.discover(repo_root, repository_id="org/repo", operation_id=op, runtime_repository="https://github.com/org/repo.git", runtime_commit="a" * 40, selected_agents=selected_agents)
    selection = discovered.selection_plan.model_copy(update={"binding_assessments": (BindingAssessment(agent_id="root", classification="bound-aligned", detail="ok"),)})
    planned = orch.build_plan(repository_id="org/repo", operation_id=op, runtime_repository="https://github.com/org/repo.git", runtime_commit="a" * 40, selection_plan=selection, evaluation_requests=evaluation_requests, evaluator_replacement=replacement)
    return repo_root, discovered, planned


def test_discovery_keeps_selection_empty_without_explicit_agents(tmp_path: Path) -> None:
    orch, _ = _build_orchestrator(tmp_path)
    repo_root = tmp_path / "repo"
    _create_repo(repo_root)
    discovered = orch.discover(repo_root, repository_id="org/repo", operation_id="op1", runtime_repository="https://github.com/org/repo.git", runtime_commit="a" * 40)
    assert discovered.selection_plan.selected_agent_ids == ()
    with pytest.raises(BootstrapApplyError):
        orch.build_plan(repository_id="org/repo", operation_id="op1", runtime_repository="https://github.com/org/repo.git", runtime_commit="a" * 40, selection_plan=discovered.selection_plan)


def test_explicit_selection_and_unselected_action_rejection(tmp_path: Path) -> None:
    drivers = _drivers()
    drivers["repository"] = _Driver("repository", actions=(_plan_action("repo", "repository", target="other"),), live=(FingerprintRecord(label="repository:live", sha256="1" * 64),))
    orch, _ = _build_orchestrator(tmp_path, drivers)
    repo_root = tmp_path / "repo"
    _create_repo(repo_root)
    discovered = orch.discover(repo_root, repository_id="org/repo", operation_id="op2", runtime_repository="https://github.com/org/repo.git", runtime_commit="a" * 40, selected_agents=({"root": ".", "repoAgentId": "root"},))
    assert discovered.selection_plan.selected_agent_ids == ("root",)
    with pytest.raises(BootstrapApplyError):
        orch.build_plan(repository_id="org/repo", operation_id="op2", runtime_repository="https://github.com/org/repo.git", runtime_commit="a" * 40, selection_plan=discovered.selection_plan)


def test_deterministic_actions_order_and_hashes(tmp_path: Path) -> None:
    orch, _ = _build_orchestrator(tmp_path)
    _, _, planned = _discover_and_plan(tmp_path, orch, op="op3", selected_agents=({"root": ".", "repoAgentId": "root"},))
    repeat = orch.build_plan(repository_id="org/repo", operation_id="op3b", runtime_repository="https://github.com/org/repo.git", runtime_commit="a" * 40, selection_plan=planned.selection_plan)
    assert [action.phase for action in planned.bootstrap_plan.actions] == ["repository", "github", "azure", "evaluations"]
    assert [item.label for item in planned.resource_fingerprints] == ["repository:live", "github:live", "azure:live", "evaluations:live"]
    assert repeat.bootstrap_plan.actions == planned.bootstrap_plan.actions


def test_offline_plan_invokes_only_repository_driver(tmp_path: Path) -> None:
    drivers = _drivers()

    def unexpected_cloud_call(_context):
        raise AssertionError("offline plan invoked a cloud driver")

    for phase in ("github", "azure", "evaluations"):
        drivers[phase].live_fingerprints = unexpected_cloud_call
        drivers[phase].plan = unexpected_cloud_call
    orch, _ = _build_orchestrator(tmp_path, drivers)
    repo_root = tmp_path / "repo"
    _create_repo(repo_root)
    discovered = orch.discover(
        repo_root,
        repository_id="org/repo",
        operation_id="offline",
        runtime_repository="https://github.com/org/repo.git",
        runtime_commit="a" * 40,
        selected_agents=({"root": ".", "repoAgentId": "root"},),
    )
    selection = discovered.selection_plan.model_copy(
        update={
            "binding_assessments": (
                BindingAssessment(
                    agent_id="root",
                    classification="bound-aligned",
                    detail="ok",
                ),
            )
        }
    )

    planned = orch.build_plan(
        repository_id="org/repo",
        operation_id="offline",
        runtime_repository="https://github.com/org/repo.git",
        runtime_commit="a" * 40,
        selection_plan=selection,
        phases=("repository",),
    )

    assert planned.required_phases == ("repository",)
    assert [action.phase for action in planned.bootstrap_plan.actions] == [
        "repository"
    ]
    assert orch.status(
        repository_id="org/repo",
        operation_id="offline",
        runtime_commit="a" * 40,
    )["deployment_eligible"] is False


def test_apply_binds_parent_phase_hashes_and_internal_live_drift(tmp_path: Path) -> None:
    orch, drivers = _build_orchestrator(tmp_path)
    _, _, planned = _discover_and_plan(tmp_path, orch, op="op4", selected_agents=({"root": ".", "repoAgentId": "root"},))
    approval = ApprovalRecord.create(parent_plan_hash=planned.bootstrap_plan.plan_hash, phase="repository", actor="tester", summary="ok")
    receipt = orch.apply_phase(repository_id="org/repo", operation_id="op4", phase="repository", approval=approval, runtime_commit="a" * 40)
    assert receipt.parent_plan_hash == planned.bootstrap_plan.plan_hash
    assert receipt.phase_plan_hash == drivers["repository"].applied[0].plan_hash
    drivers["github"].live = (FingerprintRecord(label="github:live", sha256="9" * 64),)
    approval2 = ApprovalRecord.create(parent_plan_hash=planned.bootstrap_plan.plan_hash, phase="github", actor="tester", summary="ok")
    with pytest.raises(BootstrapApplyError):
        orch.apply_phase(repository_id="org/repo", operation_id="op4", phase="github", approval=approval2, runtime_commit="a" * 40)


def test_approval_mismatch_and_exact_sha_resume(tmp_path: Path) -> None:
    orch, _ = _build_orchestrator(tmp_path)
    _, _, planned = _discover_and_plan(tmp_path, orch, op="op5", selected_agents=({"root": ".", "repoAgentId": "root"},))
    wrong = ApprovalRecord.create(parent_plan_hash="d" * 64, phase="repository", actor="tester", summary="bad")
    with pytest.raises(BootstrapApplyError):
        orch.apply_phase(repository_id="org/repo", operation_id="op5", phase="repository", approval=wrong, runtime_commit="a" * 40)
    with pytest.raises(BootstrapApplyError):
        orch.resume(repository_id="org/repo", operation_id="op5", runtime_commit="b" * 40)


def test_strict_hash_tamper_create_only_cas_and_lock(tmp_path: Path) -> None:
    orch, _ = _build_orchestrator(tmp_path)
    _, discovered, _ = _discover_and_plan(tmp_path, orch, op="op6", selected_agents=({"root": ".", "repoAgentId": "root"},))
    with pytest.raises(BootstrapApplyError):
        orch.discover(tmp_path / "repo", repository_id="org/repo", operation_id="op6", runtime_repository="https://github.com/org/repo.git", runtime_commit="a" * 40, selected_agents=({"root": ".", "repoAgentId": "root"},))
    path = tmp_path / "state" / canonical_sha256({"repository_id": "org/repo"}) / "op6" / "state.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["generation_hash"] = "0" * 64
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(BootstrapApplyError):
        read_operation_state("org/repo", "op6", state_root=tmp_path / "state")
    env = OperationStateEnvelope.create(generation=discovered.generation, repository_id="org/repo", operation_id="race", runtime_repository="https://github.com/org/repo.git", runtime_commit="a" * 40, selection_plan=discovered.selection_plan, bootstrap_plan=discovered.bootstrap_plan, discovery_fingerprints=discovered.discovery_fingerprints)
    results = []
    def writer():
        try:
            write_operation_state(env, state_root=tmp_path / "state")
            results.append("ok")
        except BootstrapApplyError:
            results.append("blocked")
    threads = [threading.Thread(target=writer) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == ["blocked", "ok"]


def test_applying_crash_and_compensation_preserved(tmp_path: Path) -> None:
    fail_receipt = BootstrapReceipt.create(operation_id="op7", runtime_repository="https://github.com/org/repo.git", runtime_commit="a" * 40, repository_identity="org/repo", plan_hash="f" * 64, compensation_required_actions=("az",), error_info=RedactedStatusInfo(code="azure_apply_failed", summary="failed"), resume_info=RedactedStatusInfo(code="resume", summary="resume"))
    drivers = _drivers()
    drivers["azure"] = _Driver("azure", actions=(_plan_action("az", "azure", target="root"),), live=(FingerprintRecord(label="azure:live", sha256="3" * 64),), fail_with_receipt=fail_receipt)
    orch, _ = _build_orchestrator(tmp_path, drivers)
    _, _, planned = _discover_and_plan(tmp_path, orch, op="op7", selected_agents=({"root": ".", "repoAgentId": "root"},))
    approval = ApprovalRecord.create(parent_plan_hash=planned.bootstrap_plan.plan_hash, phase="azure", actor="tester", summary="azure")
    receipt = orch.apply_phase(repository_id="org/repo", operation_id="op7", phase="azure", approval=approval, runtime_commit="a" * 40)
    assert receipt.state == "compensation_required"
    assert receipt.receipt.compensation_required_actions == ("az",)
    state = read_operation_state("org/repo", "op7", state_root=tmp_path / "state")
    assert any(item.state == "compensation_required" for item in state.phase_receipts)


def test_state_redaction_restart_rollback_and_eligibility(tmp_path: Path) -> None:
    orch, drivers = _build_orchestrator(tmp_path)
    _, _, planned = _discover_and_plan(tmp_path, orch, op="op8", selected_agents=({"root": ".", "repoAgentId": "root"},), replacement=EvaluationReplacementRecord(active_bundle_id="old", candidate_bundle_id="new", preserved_bundle_id="old", lineage_hash="a" * 64, status="planned"))
    for phase in ("repository", "github", "azure", "evaluations"):
        approval = ApprovalRecord.create(parent_plan_hash=planned.bootstrap_plan.plan_hash, phase=phase, actor="tester", summary=phase)
        orch.apply_phase(repository_id="org/repo", operation_id="op8", phase=phase, approval=approval, runtime_commit="a" * 40)
    status = orch.status(repository_id="org/repo", operation_id="op8", runtime_commit="a" * 40)
    rendered = json.dumps(status, sort_keys=True)
    assert "prompt" not in rendered and "response" not in rendered and "token" not in rendered
    assert status["deployment_eligible"] is True
    rolled = orch.rollback_phase(repository_id="org/repo", operation_id="op8", phase="repository", runtime_commit="a" * 40)


def test_orchestrator_persists_rollback_error_receipt_and_state(tmp_path: Path) -> None:
    drivers = _drivers()

    class _RollbackFailDriver(_Driver):
        def apply(self, phase_plan: BootstrapPlan) -> BootstrapReceipt:
            fail_receipt = BootstrapReceipt.create(operation_id="op9", runtime_repository="https://github.com/org/repo.git", runtime_commit="a" * 40, repository_identity="org/repo", plan_hash=phase_plan.plan_hash, compensation_required_actions=("eval",), error_info=RedactedStatusInfo(code="apply_failed", summary="failed"))
            raise FoundryRollbackError("rollback failed", kind="platform", compensation_receipt=fail_receipt, provider_state={"stateKey": "evaluations-state", "phase_plan_hash": phase_plan.plan_hash})

    drivers["evaluations"] = _RollbackFailDriver("evaluations", actions=(_plan_action("eval", "evaluations", target="root"),), live=(FingerprintRecord(label="evaluations:live", sha256="4" * 64),))
    orch, _ = _build_orchestrator(tmp_path, drivers)
    _, _, planned = _discover_and_plan(tmp_path, orch, op="op9", selected_agents=({"root": ".", "repoAgentId": "root"},))
    approval = ApprovalRecord.create(parent_plan_hash=planned.bootstrap_plan.plan_hash, phase="evaluations", actor="tester", summary="eval")
    receipt = orch.apply_phase(repository_id="org/repo", operation_id="op9", phase="evaluations", approval=approval, runtime_commit="a" * 40)
    assert receipt.state == "compensation_required"
    assert receipt.receipt.compensation_required_actions == ("eval",)
    assert receipt.provider_state["stateKey"] == "evaluations-state"


def test_orchestrator_persists_github_rollback_error_state(tmp_path: Path) -> None:
    drivers = _drivers()

    class _GitHubRollbackFailDriver(_Driver):
        def apply(self, phase_plan: BootstrapPlan) -> BootstrapReceipt:
            fail_receipt = BootstrapReceipt.create(
                operation_id="op10",
                runtime_repository="https://github.com/org/repo.git",
                runtime_commit="a" * 40,
                repository_identity="org/repo",
                plan_hash=phase_plan.plan_hash,
                compensation_required_actions=("github-environment",),
                error_info=RedactedStatusInfo(
                    code="apply_failed",
                    summary="failed",
                ),
            )
            raise GitHubProviderRollbackError(
                "rollback failed",
                compensation_receipt=fail_receipt,
                provider_state={
                    "stateKey": "github-state",
                    "phase_plan_hash": phase_plan.plan_hash,
                },
            )

    drivers["github"] = _GitHubRollbackFailDriver(
        "github",
        actions=(_plan_action("github-environment", "github"),),
        live=(
            FingerprintRecord(
                label="github:live",
                sha256="5" * 64,
            ),
        ),
    )
    orch, _ = _build_orchestrator(tmp_path, drivers)
    _, _, planned = _discover_and_plan(
        tmp_path,
        orch,
        op="op10",
        selected_agents=({"root": ".", "repoAgentId": "root"},),
    )
    approval = ApprovalRecord.create(
        parent_plan_hash=planned.bootstrap_plan.plan_hash,
        phase="github",
        actor="tester",
        summary="github",
    )

    receipt = orch.apply_phase(
        repository_id="org/repo",
        operation_id="op10",
        phase="github",
        approval=approval,
        runtime_commit="a" * 40,
    )

    assert receipt.state == "compensation_required"
    assert receipt.receipt.compensation_required_actions == (
        "github-environment",
    )
    assert receipt.provider_state["stateKey"] == "github-state"


def test_foundry_error_summary_keeps_only_safe_status_metadata(tmp_path: Path) -> None:
    drivers = _drivers()
    orch, _ = _build_orchestrator(tmp_path, drivers)

    code, summary = orch._sanitize_error(
        FoundryPlatformError(
            "sensitive platform response",
            kind="platform",
            status_code=400,
            code="UserError",
        )
    )

    assert code == "provider-invalid"
    assert summary == "FoundryPlatformError kind=platform status=400 code=UserError"
    assert "sensitive" not in summary
