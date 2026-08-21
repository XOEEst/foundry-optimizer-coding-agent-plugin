from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from foundry_opt.bootstrap.contracts import BootstrapSidecar, ReviewedFoundryTarget
from foundry_opt.bootstrap.errors import BootstrapApplyError
from foundry_opt.bootstrap.local_commit import (
    LocalCommitAgentHash,
    LocalCommitProfileHash,
    LocalCommitReceipt,
)
from foundry_opt.bootstrap.local_deploy import (
    BootstrapLocalDeploymentHandler,
    LocalDeploymentAgentPlan,
    LocalDeploymentAgentReceipt,
    LocalDeploymentCoordinator,
)
from foundry_opt.bootstrap.operation_state import (
    DiscoveredAgentRecord,
    SelectionPlan,
)
from foundry_opt.bootstrap.runner import (
    BootstrapApprovalRecord,
    BootstrapFoundryTargetRecord,
    BootstrapRunnerStateEnvelope,
    RepositoryBinding,
    RuntimeBinding,
)

RUNTIME_REPOSITORY = "https://github.com/example/runtime.git"
RUNTIME_COMMIT = "1" * 40
PROJECT_ENDPOINT = "https://example.services.ai.azure.com/api/projects/example"
ACCOUNT_RESOURCE_ID = (
    "/subscriptions/33333333-3333-3333-3333-333333333333/resourceGroups/rg/"
    "providers/Microsoft.CognitiveServices/accounts/example"
)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _target() -> ReviewedFoundryTarget:
    return ReviewedFoundryTarget(
        state="new_target",
        project_endpoint=PROJECT_ENDPOINT,
        project_endpoint_source="owner_answer",
        agent_name="example-agent",
        agent_name_source="owner_answer",
        account_resource_id=ACCOUNT_RESOURCE_ID,
        deployment_ready=True,
        detail="project access succeeded and the name is available",
    )


def _sidecar() -> BootstrapSidecar:
    return BootstrapSidecar.from_document(
        {
            "schema_version": 2,
            "repo_agent_id": "example-agent",
            "source_root": "agent",
            "package_root": "agent",
            "editable_paths": ["agent/**"],
            "runtime": {
                "kind": "hosted",
                "runtime": "python_3_13",
                "entrypoint": ["python", "main.py"],
                "dependency_resolution": "remote_build",
                "protocol_name": "responses",
                "protocol_version": "2.0.0",
            },
            "foundry_project": {
                "project_endpoint": PROJECT_ENDPOINT,
                "account_resource_id": ACCOUNT_RESOURCE_ID,
                "agent_name": "example-agent",
                "model_deployment_aliases": ["baseline-model"],
            },
            "foundry_target": _target().model_dump(mode="json"),
            "baseline_model": "baseline-model",
            "allowed_models": ["baseline-model"],
            "min_candidates": 1,
            "max_candidates": 2,
            "primary_metric": "quality",
            "decision_policy": {
                "minimum_aggregate_delta": 0.01,
                "focused_cases_required": True,
                "max_regressions": 0,
            },
            "hard_guardrails": [
                {
                    "evaluator_name": "safety",
                    "required_pass_rate": 1.0,
                    "required": True,
                }
            ],
            "deployment": {
                "environment": "foundry-production",
                "enabled": True,
                "require_aligned_binding": False,
            },
            "verification": {
                "mode": "off",
                "evaluation_gate_policy": "allow_no_evidence",
            },
        }
    )


@dataclass
class _CommitState:
    lifecycle_state: str
    review: object
    receipt: LocalCommitReceipt


class _CommitCoordinator:
    def __init__(self, state: _CommitState) -> None:
        self.state = state
        self.load_calls: list[tuple[str, str, str]] = []
        self.status_calls: list[object] = []

    def load_state(
        self,
        *,
        repository_identity: str,
        operation_id: str,
        runtime_commit: str,
    ) -> _CommitState:
        self.load_calls.append(
            (repository_identity, operation_id, runtime_commit)
        )
        return self.state

    def status(self, review: object) -> object:
        self.status_calls.append(review)
        return object()


class _DeploymentAdapter:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.fail_once = fail_once
        self.calls: list[LocalDeploymentAgentPlan] = []

    def deploy(
        self,
        repository_root: Path,
        plan: LocalDeploymentAgentPlan,
    ) -> LocalDeploymentAgentReceipt:
        assert repository_root.is_dir()
        self.calls.append(plan)
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("provider unavailable")
        return LocalDeploymentAgentReceipt(
            repo_agent_id=plan.repo_agent_id,
            commit_sha=plan.commit_sha,
            project_endpoint=plan.project_endpoint,
            agent_name=plan.agent_name,
            status="published",
            published_version="1",
            previous_version=plan.previous_version,
            source_tree_sha256="a" * 64,
            source_zip_sha256="b" * 64,
            package_sha256=plan.package_sha256,
            profile_sha256=plan.profile_sha256,
            registry_sha256=plan.registry_sha256,
            target_sha256=plan.target_sha256,
            verification_mode=plan.verification_mode,
            verification_status="unverified",
            verification_warning=plan.verification_warning,
        )


def _fixture(
    tmp_path: Path,
) -> tuple[Path, BootstrapRunnerStateEnvelope, _CommitCoordinator]:
    repository = tmp_path / "repo"
    (repository / ".foundry-opt").mkdir(parents=True)
    (repository / "agent" / ".foundry").mkdir(parents=True)
    (repository / "agent" / "main.py").write_text(
        "print('ready')\n",
        encoding="utf-8",
    )
    sidecar_path = repository / "agent" / ".foundry" / "foundry-opt.yaml"
    sidecar_path.write_text(
        yaml.safe_dump(_sidecar().model_dump(mode="json"), sort_keys=False),
        encoding="utf-8",
    )
    registry_path = repository / ".foundry-opt" / "registry.yaml"
    registry_path.write_text(
        yaml.safe_dump(
            {
                "distribution": {
                    "repository": RUNTIME_REPOSITORY,
                    "channel": "pinned",
                    "pin": RUNTIME_COMMIT,
                },
                "github": {
                    "optimizer_environment": "copilot",
                    "deployment_environment": "foundry-production",
                    "client_id_variable": "AZURE_FOUNDRY_OPT_CLIENT_ID",
                },
                "identity": {"kind": "unresolved_migration"},
                "agents": [
                    {
                        "agent_id": "example-agent",
                        "root": "agent",
                        "config_path": "agent/.foundry/foundry-opt.yaml",
                        "enabled": True,
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.name", "Local Deploy Test")
    _git(repository, "config", "user.email", "local-deploy@example.invalid")
    _git(repository, "add", ".")
    _git(repository, "commit", "--quiet", "-m", "bootstrap")
    commit = _git(repository, "rev-parse", "HEAD")
    tree = _git(repository, "rev-parse", "HEAD^{tree}")
    registry_sha = hashlib.sha256(registry_path.read_bytes()).hexdigest()
    profile_sha = hashlib.sha256(sidecar_path.read_bytes()).hexdigest()
    receipt = LocalCommitReceipt.create(
        operation_id="bootstrap-local-deploy",
        repository_identity="example-org/example-repo",
        runtime_repository=RUNTIME_REPOSITORY,
        runtime_commit=RUNTIME_COMMIT,
        repository_plan_hash="2" * 64,
        review_hash="3" * 64,
        approval_hash="4" * 64,
        base_commit=commit,
        branch_name="foundry-bootstrap/local",
        commit_sha=commit,
        tree_sha=tree,
        commit_message="Bootstrap Foundry",
        registry_path=".foundry-opt/registry.yaml",
        registry_sha256=registry_sha,
        committed_paths=(
            ".foundry-opt/registry.yaml",
            "agent/.foundry/foundry-opt.yaml",
            "agent/main.py",
        ),
        profile_hashes=(
            LocalCommitProfileHash(
                repo_agent_id="example-agent",
                profile_path="agent/.foundry/foundry-opt.yaml",
                sha256=profile_sha,
            ),
        ),
        agent_hashes=(
            LocalCommitAgentHash(
                repo_agent_id="example-agent",
                source_root="agent",
                source_sha256="5" * 64,
                package_root="agent",
                package_sha256="6" * 64,
            ),
        ),
    )
    commit_coordinator = _CommitCoordinator(
        _CommitState(
            lifecycle_state="committed",
            review=object(),
            receipt=receipt,
        )
    )
    operation = BootstrapRunnerStateEnvelope.create(
        generation=4,
        operation_id="bootstrap-local-deploy",
        lifecycle_stage="deployment_approval",
        started_at="2026-08-21T00:00:00Z",
        updated_at="2026-08-21T00:05:00Z",
        repository_binding=RepositoryBinding(
            repository_root=str(repository.resolve()),
            repository_id="example-org/example-repo",
            repository_url="https://github.com/example-org/example-repo.git",
            head_commit=commit,
            branch_name="foundry-bootstrap/local",
        ),
        runtime_binding=RuntimeBinding(
            runtime_repository=RUNTIME_REPOSITORY,
            runtime_commit=RUNTIME_COMMIT,
        ),
        selection_plan=SelectionPlan(
            repository_root=str(repository.resolve()),
            selected_agent_ids=("example-agent",),
            binding_assessments=(),
            discovery_fingerprints=(),
            blockers=(),
            discovered_agents=(
                DiscoveredAgentRecord(
                    repo_agent_id="example-agent",
                    root="agent",
                    config_path="agent/.foundry/foundry-opt.yaml",
                    source_root="agent",
                    package_root="agent",
                    source_fingerprint="5" * 64,
                    package_fingerprint="6" * 64,
                    classification="ready-unbound",
                    confidence=1.0,
                ),
            ),
        ),
        foundry_targets=(
            BootstrapFoundryTargetRecord(
                repo_agent_id="example-agent",
                root="agent",
                reviewed_target=_target(),
            ),
        ),
    )
    return repository, operation, commit_coordinator


def test_local_deployment_plan_binds_exact_commit_profile_and_target(
    tmp_path: Path,
) -> None:
    _, operation, commit_coordinator = _fixture(tmp_path)
    adapter = _DeploymentAdapter()
    coordinator = LocalDeploymentCoordinator(
        adapter=adapter,
        commit_coordinator=commit_coordinator,
        state_root=tmp_path / "state",
    )

    plan = coordinator.build_plan(operation)

    assert plan.commit_sha == operation.repository_binding.head_commit
    assert plan.commit_receipt_hash == commit_coordinator.state.receipt.receipt_hash
    assert plan.agents[0].target_state == "new_target"
    assert plan.agents[0].verification_mode == "none"
    assert "without foundry evaluation" in (
        plan.agents[0].verification_warning or ""
    ).casefold()
    assert "current local Azure identity" in plan.render_markdown()
    assert adapter.calls == []


def test_local_deployment_approval_publishes_and_resumes_idempotently(
    tmp_path: Path,
) -> None:
    _, operation, commit_coordinator = _fixture(tmp_path)
    adapter = _DeploymentAdapter()
    coordinator = LocalDeploymentCoordinator(
        adapter=adapter,
        commit_coordinator=commit_coordinator,
        state_root=tmp_path / "state",
    )
    plan = coordinator.build_plan(operation)
    approval = coordinator.create_approval(
        plan,
        actor="repo-owner",
        summary="Deploy the reviewed exact commit.",
    )

    first = coordinator.apply(operation, plan, approval)
    second = coordinator.apply(operation, plan, approval)

    assert first == second
    assert first.agents[0].published_version == "1"
    assert first.agents[0].commit_sha == plan.commit_sha
    assert len(adapter.calls) == 1
    status = coordinator.status(
        repository_identity=plan.repository_identity,
        operation_id=plan.operation_id,
        runtime_commit=plan.runtime_commit,
    )
    assert status.lifecycle_state == "applied"


def test_local_deployment_failure_persists_and_same_approval_can_resume(
    tmp_path: Path,
) -> None:
    _, operation, commit_coordinator = _fixture(tmp_path)
    adapter = _DeploymentAdapter(fail_once=True)
    coordinator = LocalDeploymentCoordinator(
        adapter=adapter,
        commit_coordinator=commit_coordinator,
        state_root=tmp_path / "state",
    )
    plan = coordinator.build_plan(operation)
    approval = coordinator.create_approval(
        plan,
        actor="repo-owner",
        summary="Deploy the reviewed exact commit.",
    )

    with pytest.raises(BootstrapApplyError, match="can be resumed"):
        coordinator.apply(operation, plan, approval)

    failed = coordinator.status(
        repository_identity=plan.repository_identity,
        operation_id=plan.operation_id,
        runtime_commit=plan.runtime_commit,
    )
    assert failed.lifecycle_state == "failed"
    receipt = coordinator.apply(operation, plan, approval)

    assert receipt.agents[0].published_version == "1"
    assert len(adapter.calls) == 2


def test_bootstrap_deployment_handler_returns_final_handoff(
    tmp_path: Path,
) -> None:
    _, operation, commit_coordinator = _fixture(tmp_path)
    coordinator = LocalDeploymentCoordinator(
        adapter=_DeploymentAdapter(),
        commit_coordinator=commit_coordinator,
        state_root=tmp_path / "state",
    )
    handler = BootstrapLocalDeploymentHandler(coordinator=coordinator)
    approval = BootstrapApprovalRecord.create(
        step="deployment",
        actor="repo-owner",
        summary="Deploy the reviewed exact commit.",
        approved_at="2026-08-21T00:06:00Z",
        state_generation=operation.generation,
        state_generation_hash=operation.generation_hash,
    )

    outcome = handler.approve(operation=operation, approval=approval)

    assert outcome.stage == "final_handoff"
    assert outcome.child_refs is not None
    assert outcome.child_refs[-1].step == "deployment"
    assert "example-agent=1" in (outcome.note or "")
