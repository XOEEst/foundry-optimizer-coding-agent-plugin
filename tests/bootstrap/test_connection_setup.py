from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from foundry_opt.bootstrap.connection_setup import (
    AzureConnectionInventory,
    BootstrapConnectionSetupHandler,
    CliConnectionInventory,
    ConnectionSetupCoordinator,
)
from foundry_opt.bootstrap.contracts import (
    BootstrapAction,
    BootstrapPlan,
    BootstrapReceipt,
    FingerprintRecord,
    IdentitySettings,
)
from foundry_opt.bootstrap.errors import BootstrapApplyError
from foundry_opt.bootstrap.repository_setup import (
    BootstrapRepositorySetupHandler,
    RepositorySetupCoordinator,
)
from foundry_opt.bootstrap.runner import (
    BootstrapApprovalRecord,
    BootstrapRunnerStateEnvelope,
    next_runner_generation,
)
from tests.bootstrap.test_repository_setup import (
    ACCOUNT_RESOURCE_ID,
    LOCK_SHA,
    _operation,
)

CLIENT_ID = "44444444-4444-4444-4444-444444444444"
PRINCIPAL_ID = "55555555-5555-5555-5555-555555555555"


class _Inventory:
    def inspect(self, **_: object) -> AzureConnectionInventory:
        return AzureConnectionInventory(
            tenant_id="22222222-2222-2222-2222-222222222222",
            subscription_id="33333333-3333-3333-3333-333333333333",
            location="eastus2",
            identity_resource_id=(
                "/subscriptions/33333333-3333-3333-3333-333333333333/"
                "resourceGroups/rg/providers/Microsoft.ManagedIdentity/"
                "userAssignedIdentities/foundry-opt-repo"
            ),
            identity_name="foundry-opt-repo",
            identity_exists=False,
            github_oidc_subject_prefix=(
                "repo:example@123/example-repo@456"
            ),
            project_scopes=(
                f"{ACCOUNT_RESOURCE_ID}/projects/example",
            ),
        )


class _RecordingReuseInventory(_Inventory):
    def __init__(self) -> None:
        self.preferred_identity: IdentitySettings | None = None

    def inspect(
        self,
        *,
        preferred_identity: IdentitySettings | None = None,
        **_: object,
    ) -> AzureConnectionInventory:
        self.preferred_identity = preferred_identity
        inventory = super().inspect()
        assert preferred_identity is not None
        assert preferred_identity.resource_id is not None
        return inventory.model_copy(
            update={
                "identity_resource_id": preferred_identity.resource_id,
                "identity_name": preferred_identity.resource_id.rsplit("/", 1)[-1],
                "identity_exists": True,
                "client_id": CLIENT_ID,
                "principal_id": PRINCIPAL_ID,
                "identity_source": "repository_registry",
            }
        )


class _Driver:
    def __init__(
        self,
        phase: str,
        calls: list[str],
        *,
        fail_apply: bool = False,
    ) -> None:
        self.phase = phase
        self.calls = calls
        self.fail_apply = fail_apply
        self.plan_inputs: list[object] = []
        self.restored: list[dict[str, object]] = []
        self.rolled_back: list[str] = []

    def live_fingerprints(self, context: dict[str, object]):
        self.calls.append(f"{self.phase}:live")
        return (
            FingerprintRecord(
                label=f"{self.phase}:inventory",
                sha256=("a" if self.phase == "azure" else "b") * 64,
            ),
        )

    def plan(self, context: dict[str, object]):
        self.calls.append(f"{self.phase}:plan")
        self.plan_inputs.append(context["plan_input"])
        return (
            BootstrapAction(
                action_id=f"{self.phase}-connection",
                phase=self.phase,
                stage="planned",
                kind=f"{self.phase}-connection",
            ),
        )

    def apply(self, plan: BootstrapPlan) -> BootstrapReceipt:
        self.calls.append(f"{self.phase}:apply")
        if self.fail_apply:
            raise RuntimeError(f"{self.phase} apply failed")
        return BootstrapReceipt.create(
            operation_id=plan.operation_id,
            runtime_repository=plan.runtime_repository,
            runtime_commit=plan.runtime_commit,
            repository_identity=plan.repository_identity,
            plan_hash=plan.plan_hash,
            created_actions=tuple(
                action.action_id for action in plan.actions
            ),
        )

    def verify(self, receipt: BootstrapReceipt) -> bool:
        self.calls.append(f"{self.phase}:verify")
        return bool(receipt.receipt_hash)

    def export_provider_state(
        self,
        receipt: BootstrapReceipt,
    ) -> dict[str, object]:
        self.calls.append(f"{self.phase}:export")
        if self.phase == "azure":
            return {
                "identity": {
                    "client_id": CLIENT_ID,
                    "principal_id": PRINCIPAL_ID,
                },
                "receipt_hash": receipt.receipt_hash,
            }
        return {"receipt_hash": receipt.receipt_hash}

    def restore_provider_state(self, mapping: dict[str, object]) -> None:
        self.restored.append(dict(mapping))

    def rollback(self, receipt: BootstrapReceipt) -> None:
        self.calls.append(f"{self.phase}:rollback")
        self.rolled_back.append(receipt.receipt_hash)

    def verify_rollback(self, receipt: BootstrapReceipt) -> bool:
        return receipt.receipt_hash in self.rolled_back


class _Drivers:
    def __init__(self, *, github_fails: bool = False) -> None:
        self.calls: list[str] = []
        self.azure_driver = _Driver("azure", self.calls)
        self.github_driver = _Driver(
            "github",
            self.calls,
            fail_apply=github_fails,
        )

    def azure(self, plan_input):
        return self.azure_driver

    def github(self, plan_input):
        return self.github_driver


class _BoundaryDriver(_Driver):
    def __init__(
        self,
        phase: str,
        calls: list[str],
        *,
        fail_at: str | None = None,
    ) -> None:
        super().__init__(phase, calls)
        self.fail_at = fail_at
        self.mutated = False
        self._checkpoint = None

    def set_checkpoint(self, checkpoint) -> None:
        self._checkpoint = checkpoint

    def plan(self, context: dict[str, object]):
        if self.fail_at == "plan":
            raise RuntimeError(f"{self.phase} plan failed")
        return super().plan(context)

    def apply(self, plan: BootstrapPlan) -> BootstrapReceipt:
        self.calls.append(f"{self.phase}:apply")
        receipt = BootstrapReceipt.create(
            operation_id=plan.operation_id,
            runtime_repository=plan.runtime_repository,
            runtime_commit=plan.runtime_commit,
            repository_identity=plan.repository_identity,
            plan_hash=plan.plan_hash,
            created_actions=(f"{self.phase}-connection",),
            compensation_required_actions=(f"{self.phase}-connection",),
        )
        self.mutated = True
        if self._checkpoint is not None:
            provider_state = self._provider_state(receipt)
            self._checkpoint(
                {
                    "version": 1,
                    "checkpoint": True,
                    "complete": self.fail_at != "apply",
                    "receipt": receipt.model_dump(mode="json"),
                    "provider_state": provider_state,
                }
            )
        if self.fail_at == "apply":
            raise RuntimeError(f"{self.phase} apply failed")
        return receipt

    def verify(self, receipt: BootstrapReceipt) -> bool:
        self.calls.append(f"{self.phase}:verify")
        return self.fail_at != "verify"

    def export_provider_state(
        self,
        receipt: BootstrapReceipt,
    ) -> dict[str, object]:
        self.calls.append(f"{self.phase}:export")
        if self.fail_at == "export":
            raise RuntimeError(f"{self.phase} export failed")
        return self._provider_state(receipt)

    def _provider_state(
        self,
        receipt: BootstrapReceipt,
    ) -> dict[str, object]:
        state: dict[str, object] = {
            "receipt_hash": receipt.receipt_hash,
        }
        if self.phase == "azure":
            state["identity"] = {
                "client_id": CLIENT_ID,
                "principal_id": PRINCIPAL_ID,
            }
        return state

    def restore_provider_state(self, mapping: dict[str, object]) -> None:
        self.restored.append(dict(mapping))

    def rollback(self, receipt: BootstrapReceipt) -> None:
        self.calls.append(f"{self.phase}:rollback")
        self.rolled_back.append(receipt.receipt_hash)
        self.mutated = False

    def verify_rollback(self, receipt: BootstrapReceipt) -> bool:
        return not self.mutated


class _BoundaryDrivers:
    def __init__(
        self,
        *,
        azure_failure: str | None = None,
        github_failure: str | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.azure_driver = _BoundaryDriver(
            "azure",
            self.calls,
            fail_at=azure_failure,
        )
        self.github_driver = _BoundaryDriver(
            "github",
            self.calls,
            fail_at=github_failure,
        )

    def azure(self, plan_input):
        return self.azure_driver

    def github(self, plan_input):
        return self.github_driver


def _connection_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> BootstrapRunnerStateEnvelope:
    monkeypatch.setenv("FOUNDRY_OPT_RUNTIME_LOCK_SHA256", LOCK_SHA)
    operation = _operation(tmp_path)
    repository_handler = BootstrapRepositorySetupHandler(
        coordinator=RepositorySetupCoordinator(
            state_root=tmp_path / "repository-state"
        )
    )
    approval = BootstrapApprovalRecord.create(
        step="repository",
        actor="repo-owner",
        summary="Apply repository setup.",
        approved_at="2026-08-21T00:06:00Z",
        state_generation=operation.generation,
        state_generation_hash=operation.generation_hash,
    )
    outcome = repository_handler.approve(
        operation=operation,
        approval=approval,
    )
    return next_runner_generation(
        operation,
        now=__import__("datetime").datetime.now(
            __import__("datetime").UTC
        ),
        lifecycle_stage=outcome.stage,
        child_refs=outcome.child_refs,
        handler_context=outcome.handler_context,
        note=outcome.note,
    )


def test_connection_setup_applies_azure_then_github(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = _connection_operation(tmp_path, monkeypatch)
    drivers = _Drivers()
    coordinator = ConnectionSetupCoordinator(
        inventory=_Inventory(),
        drivers=drivers,
        repository_coordinator=RepositorySetupCoordinator(
            state_root=tmp_path / "repository-state"
        ),
        state_root=tmp_path / "connection-state",
    )
    review = coordinator.review(operation)

    state = coordinator.approve(
        operation,
        actor="repo-owner",
        summary="Approve the reviewed GitHub and Azure connection.",
    )

    assert "create user-assigned managed identity" in review.render_markdown()
    assert state.lifecycle_state == "applied"
    assert drivers.calls.index("azure:apply") < drivers.calls.index(
        "github:apply"
    )
    github_input = drivers.github_driver.plan_inputs[0]
    assert github_input.github_phase.shared_client_id == CLIENT_ID
    assert (
        github_input.github_phase.oidc_subject_prefix
        == "repo:example@123/example-repo@456"
    )


def test_connection_review_reuses_repository_registry_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = _connection_operation(tmp_path, monkeypatch)
    repository = Path(operation.repository_binding.repository_root)
    registry_path = repository / ".foundry-opt" / "registry.yaml"
    registry = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    existing_identity = (
        "/subscriptions/33333333-3333-3333-3333-333333333333/"
        "resourceGroups/identity-rg/providers/Microsoft.ManagedIdentity/"
        "userAssignedIdentities/existing-foundry-opt"
    )
    registry["identity"] = {
        "schema_version": 1,
        "kind": "user_assigned_managed_identity",
        "resource_id": existing_identity,
        "client_id": CLIENT_ID,
    }
    registry_path.write_text(
        yaml.safe_dump(registry, sort_keys=False),
        encoding="utf-8",
    )
    inventory = _RecordingReuseInventory()
    coordinator = ConnectionSetupCoordinator(
        inventory=inventory,
        drivers=_Drivers(),
        repository_coordinator=RepositorySetupCoordinator(
            state_root=tmp_path / "repository-state"
        ),
        state_root=tmp_path / "connection-state",
    )

    review = coordinator.review(operation)

    assert inventory.preferred_identity is not None
    assert inventory.preferred_identity.resource_id == existing_identity
    assert review.github_preflight_complete is True
    assert "existing repository registry" in review.render_markdown()
    assert "exact matching managed identity, OIDC subjects" in review.render_markdown()
    assert "inspected and bound to this review" in review.render_markdown()


def test_cli_connection_inventory_uses_preferred_registry_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preferred_resource_id = (
        "/subscriptions/33333333-3333-3333-3333-333333333333/"
        "resourceGroups/identity-rg/providers/Microsoft.ManagedIdentity/"
        "userAssignedIdentities/existing-foundry-opt"
    )
    calls: list[list[str]] = []

    def _json(command, *, error):
        calls.append(list(command))
        if command[:3] == ["az", "account", "show"]:
            return {
                "tenant_id": "22222222-2222-2222-2222-222222222222",
                "subscription_id": "33333333-3333-3333-3333-333333333333",
            }
        return {
            "id": 456,
            "owner": {"id": 123},
        }

    def _optional(command, *, error):
        calls.append(list(command))
        return {
            "clientId": CLIENT_ID,
            "principalId": PRINCIPAL_ID,
        }

    monkeypatch.setattr(
        "foundry_opt.bootstrap.connection_setup._run_json",
        _json,
    )
    monkeypatch.setattr(
        "foundry_opt.bootstrap.connection_setup._run_json_optional",
        _optional,
    )
    monkeypatch.setattr(
        "foundry_opt.bootstrap.connection_setup._run_text",
        lambda command, *, error: "eastus2",
    )

    inventory = CliConnectionInventory().inspect(
        repository_identity="example/repo",
        targets=(
            (
                ACCOUNT_RESOURCE_ID,
                "https://example.services.ai.azure.com/api/projects/example",
            ),
        ),
        preferred_identity=IdentitySettings(
            kind="user_assigned_managed_identity",
            resource_id=preferred_resource_id,
            client_id=CLIENT_ID,
        ),
    )

    assert inventory.identity_resource_id == preferred_resource_id
    assert inventory.identity_source == "repository_registry"
    assert any(
        command[:4] == ["az", "identity", "show", "--ids"]
        and command[4] == preferred_resource_id
        for command in calls
    )


def test_connection_handler_advances_to_commit_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = _connection_operation(tmp_path, monkeypatch)
    handler = BootstrapConnectionSetupHandler(
        coordinator=ConnectionSetupCoordinator(
            inventory=_Inventory(),
            drivers=_Drivers(),
            repository_coordinator=RepositorySetupCoordinator(
                state_root=tmp_path / "repository-state"
            ),
            state_root=tmp_path / "connection-state",
        )
    )
    approval = BootstrapApprovalRecord.create(
        step="connection",
        actor="repo-owner",
        summary="Approve connection.",
        approved_at="2026-08-21T00:07:00Z",
        state_generation=operation.generation,
        state_generation_hash=operation.generation_hash,
    )

    outcome = handler.approve(operation=operation, approval=approval)

    assert outcome.stage == "commit_approval"
    assert outcome.child_refs is not None
    assert outcome.child_refs[-1].step == "connection"


def test_azure_external_drift_invalidates_connection_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = _connection_operation(tmp_path, monkeypatch)
    drivers = _Drivers()
    coordinator = ConnectionSetupCoordinator(
        inventory=_Inventory(),
        drivers=drivers,
        repository_coordinator=RepositorySetupCoordinator(
            state_root=tmp_path / "repository-state"
        ),
        state_root=tmp_path / "connection-state",
    )
    coordinator.review(operation)
    drivers.azure_driver.live_fingerprints = lambda context: (
        FingerprintRecord(
            label="azure:inventory",
            sha256="c" * 64,
        ),
    )

    with pytest.raises(
        BootstrapApplyError,
        match="inventory drifted",
    ):
        coordinator.approve(
            operation,
            actor="repo-owner",
            summary="Approve connection.",
        )

    assert "azure:apply" not in drivers.calls
    assert coordinator.build(operation).lifecycle_state == "awaiting_approval"


def test_github_failure_compensates_azure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = _connection_operation(tmp_path, monkeypatch)
    drivers = _Drivers(github_fails=True)
    coordinator = ConnectionSetupCoordinator(
        inventory=_Inventory(),
        drivers=drivers,
        repository_coordinator=RepositorySetupCoordinator(
            state_root=tmp_path / "repository-state"
        ),
        state_root=tmp_path / "connection-state",
    )

    with pytest.raises(RuntimeError, match="github apply failed"):
        coordinator.approve(
            operation,
            actor="repo-owner",
            summary="Approve connection.",
        )

    assert drivers.azure_driver.restored
    assert drivers.azure_driver.rolled_back
    assert "azure:rollback" in drivers.calls
    drivers.github_driver.fail_apply = False
    state = coordinator.approve(
        operation,
        actor="repo-owner",
        summary="Approve connection.",
    )
    assert state.lifecycle_state == "applied"
    assert drivers.calls.count("azure:apply") == 2


def test_connection_rollback_restores_registry_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    operation = _connection_operation(tmp_path, monkeypatch)
    drivers = _Drivers()
    coordinator = ConnectionSetupCoordinator(
        inventory=_Inventory(),
        drivers=drivers,
        repository_coordinator=RepositorySetupCoordinator(
            state_root=tmp_path / "repository-state"
        ),
        state_root=tmp_path / "connection-state",
    )
    coordinator.approve(
        operation,
        actor="repo-owner",
        summary="Approve connection.",
    )
    repository = Path(operation.repository_binding.repository_root)
    applied = yaml.safe_load(
        (repository / ".foundry-opt" / "registry.yaml").read_text(
            encoding="utf-8"
        )
    )

    state = coordinator.rollback(operation)
    calls_after_first_rollback = tuple(drivers.calls)
    repeated = coordinator.rollback(operation)
    restored = yaml.safe_load(
        (repository / ".foundry-opt" / "registry.yaml").read_text(
            encoding="utf-8"
        )
    )

    assert applied["identity"]["kind"] == "user_assigned_managed_identity"
    assert state.lifecycle_state == "rolled_back"
    assert repeated == state
    assert tuple(drivers.calls) == calls_after_first_rollback
    assert restored["identity"]["kind"] == "unresolved_migration"


@pytest.mark.parametrize(
    ("phase", "boundary"),
    [
        ("azure", "apply"),
        ("azure", "verify"),
        ("azure", "export"),
        ("github", "plan"),
        ("github", "apply"),
        ("github", "verify"),
        ("github", "export"),
    ],
)
def test_connection_failure_boundaries_compensate_all_mutations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    boundary: str,
) -> None:
    operation = _connection_operation(tmp_path, monkeypatch)
    drivers = _BoundaryDrivers(
        azure_failure=boundary if phase == "azure" else None,
        github_failure=boundary if phase == "github" else None,
    )
    coordinator = ConnectionSetupCoordinator(
        inventory=_Inventory(),
        drivers=drivers,
        repository_coordinator=RepositorySetupCoordinator(
            state_root=tmp_path / "repository-state"
        ),
        state_root=tmp_path / "connection-state",
    )

    with pytest.raises((RuntimeError, BootstrapApplyError)):
        coordinator.approve(
            operation,
            actor="repo-owner",
            summary="Approve connection.",
        )

    state = coordinator.build(operation)
    assert state.lifecycle_state == "awaiting_approval"
    assert drivers.azure_driver.mutated is False
    assert drivers.github_driver.mutated is False
    if phase == "github":
        assert drivers.azure_driver.rolled_back


@pytest.mark.parametrize(
    "failed_state",
    [
        "azure_applying_result",
        "azure_applied",
        "github_applying",
        "github_applying_result",
        "cloud_applied",
        "applied",
    ],
)
def test_parent_state_write_failures_leave_no_unmanaged_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_state: str,
) -> None:
    operation = _connection_operation(tmp_path, monkeypatch)
    drivers = _BoundaryDrivers()
    coordinator = ConnectionSetupCoordinator(
        inventory=_Inventory(),
        drivers=drivers,
        repository_coordinator=RepositorySetupCoordinator(
            state_root=tmp_path / "repository-state"
        ),
        state_root=tmp_path / "connection-state",
    )
    coordinator.review(operation)
    original_write = coordinator._write
    failed = False

    def fail_once(envelope, *, expected=None):
        nonlocal failed
        provider_state = (
            envelope.payload.azure_provider_state
            if failed_state.startswith("azure")
            else envelope.payload.github_provider_state
        )
        is_result_write = (
            failed_state.endswith("_result")
            and envelope.lifecycle_state
            == failed_state.removesuffix("_result")
            and bool(provider_state)
            and provider_state.get("checkpoint") is not True
        )
        is_named_write = (
            not failed_state.endswith("_result")
            and envelope.lifecycle_state == failed_state
        )
        if not failed and (is_result_write or is_named_write):
            failed = True
            raise RuntimeError(f"state write failed at {failed_state}")
        return original_write(envelope, expected=expected)

    monkeypatch.setattr(coordinator, "_write", fail_once)

    with pytest.raises((RuntimeError, BootstrapApplyError)):
        coordinator.approve(
            operation,
            actor="repo-owner",
            summary="Approve connection.",
        )

    state = coordinator.build(operation)
    assert failed is True
    assert state.lifecycle_state == "awaiting_approval"
    assert drivers.azure_driver.mutated is False
    assert drivers.github_driver.mutated is False
    repository = Path(operation.repository_binding.repository_root)
    registry = yaml.safe_load(
        (repository / ".foundry-opt" / "registry.yaml").read_text(
            encoding="utf-8"
        )
    )
    assert registry["identity"]["kind"] == "unresolved_migration"
