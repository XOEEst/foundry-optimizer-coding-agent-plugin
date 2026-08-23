from __future__ import annotations

import json
import hashlib
import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Literal, Protocol, Self
from urllib.parse import urlparse

import yaml
from pydantic import Field, StringConstraints, model_validator

from foundry_opt.bootstrap.canonical import (
    canonical_json_bytes,
    canonical_sha256,
    safe_persisted_document,
)
from foundry_opt.bootstrap.contracts import (
    BootstrapDocument,
    BootstrapLock,
    BootstrapPlan,
    BootstrapReceipt,
    FingerprintRecord,
    GitHubSettings,
    IdentitySettings,
    RootRegistry,
)
from foundry_opt.bootstrap.drivers import AzurePhaseDriver, GitHubPhaseDriver
from foundry_opt.bootstrap.errors import BootstrapApplyError, BootstrapConfigError
from foundry_opt.bootstrap.input_contracts import BootstrapPlanInput
from foundry_opt.bootstrap.operation_state import default_state_root
from foundry_opt.bootstrap.owner_review import ResourceLink, ResourceLinksReview
from foundry_opt.bootstrap.repository_setup import RepositorySetupCoordinator
from foundry_opt.bootstrap.shared import require_safe_operation_id
from foundry_opt.bootstrap.state_lock import (
    atomic_replace_state,
    state_file_lock,
)

ConnectionSetupLifecycleState = Literal[
    "awaiting_approval",
    "azure_applying",
    "azure_applied",
    "github_applying",
    "cloud_applied",
    "registry_applying",
    "applied",
    "rolled_back",
]

_STATE_FILE_NAME = "state.json"
_LOCK_FILE_NAME = "state.lock"
_MAX_STATE_BYTES = 2 * 1024 * 1024
_FOUNDRY_USER_ROLE_GUID = "53ca6127-db72-4b80-b1b0-d745d6d5456d"


class AzureConnectionInventory(BootstrapDocument):
    tenant_id: str
    subscription_id: str
    location: str
    identity_resource_id: str
    identity_name: str
    identity_exists: bool
    client_id: str | None = None
    principal_id: str | None = None
    identity_source: Literal["derived_default", "repository_registry"] = (
        "derived_default"
    )
    github_oidc_subject_prefix: str
    project_scopes: tuple[str, ...]


class ConnectionSetupPlan(BootstrapDocument):
    operation_id: Annotated[str, StringConstraints(min_length=1, max_length=128)]
    repository_identity: str
    runtime_repository: str
    runtime_commit: str
    optimizer_environment: str
    deployment_environment: str
    client_id_variable: str
    inventory: AzureConnectionInventory
    azure_plan_input: BootstrapPlanInput
    azure_plan: BootstrapPlan
    azure_live_fingerprints: tuple[FingerprintRecord, ...]
    github_plan_input: BootstrapPlanInput | None = None
    github_plan: BootstrapPlan | None = None
    github_live_fingerprints: tuple[FingerprintRecord, ...] = ()
    github_intent_hash: str
    plan_hash: str

    def _hash_payload(self) -> dict[str, object]:
        return {
            "operation_id": self.operation_id,
            "repository_identity": self.repository_identity,
            "runtime_repository": self.runtime_repository,
            "runtime_commit": self.runtime_commit,
            "optimizer_environment": self.optimizer_environment,
            "deployment_environment": self.deployment_environment,
            "client_id_variable": self.client_id_variable,
            "inventory": self.inventory.model_dump(mode="json"),
            "azure_plan_input_hash": self.azure_plan_input.plan_input_hash,
            "azure_plan_hash": self.azure_plan.plan_hash,
            "azure_live_fingerprints": [
                item.model_dump(mode="json")
                for item in self.azure_live_fingerprints
            ],
            "github_plan_input_hash": (
                None
                if self.github_plan_input is None
                else self.github_plan_input.plan_input_hash
            ),
            "github_plan_hash": (
                None if self.github_plan is None else self.github_plan.plan_hash
            ),
            "github_live_fingerprints": [
                item.model_dump(mode="json")
                for item in self.github_live_fingerprints
            ],
            "github_intent_hash": self.github_intent_hash,
        }

    @classmethod
    def create(cls, **values: object) -> "ConnectionSetupPlan":
        validated = cls.model_validate({**values, "plan_hash": "0" * 64})
        return cls.model_validate(
            {
                **validated.model_dump(mode="json", exclude={"plan_hash"}),
                "plan_hash": canonical_sha256(validated._hash_payload()),
            }
        )

    @model_validator(mode="after")
    def _validate_hash(self) -> Self:
        if self.plan_hash != "0" * 64 and self.plan_hash != canonical_sha256(
            self._hash_payload()
        ):
            raise BootstrapApplyError(
                "connection setup plan hash does not match the payload"
            )
        return self


class ConnectionSetupApproval(BootstrapDocument):
    repository_identity: str
    operation_id: str
    runtime_commit: str
    plan_hash: str
    actor: Annotated[str, StringConstraints(min_length=1, max_length=256)]
    summary: Annotated[str, StringConstraints(min_length=1, max_length=4096)]
    approval_hash: str

    def _hash_payload(self) -> dict[str, object]:
        return {
            "repository_identity": self.repository_identity,
            "operation_id": self.operation_id,
            "runtime_commit": self.runtime_commit,
            "plan_hash": self.plan_hash,
            "actor": self.actor,
            "summary": self.summary,
        }

    @classmethod
    def create(
        cls,
        *,
        plan: ConnectionSetupPlan,
        actor: str,
        summary: str,
    ) -> "ConnectionSetupApproval":
        payload = {
            "repository_identity": plan.repository_identity,
            "operation_id": plan.operation_id,
            "runtime_commit": plan.runtime_commit,
            "plan_hash": plan.plan_hash,
            "actor": actor,
            "summary": summary,
        }
        safe_persisted_document(payload)
        return cls.model_validate(
            {**payload, "approval_hash": canonical_sha256(payload)}
        )

    @model_validator(mode="after")
    def _validate_hash(self) -> Self:
        if self.approval_hash != canonical_sha256(self._hash_payload()):
            raise BootstrapApplyError(
                "connection setup approval hash does not match the payload"
            )
        return self


class ConnectionSetupStatePayload(BootstrapDocument):
    generation: int = Field(ge=0)
    lifecycle_state: ConnectionSetupLifecycleState
    plan: ConnectionSetupPlan
    approval: ConnectionSetupApproval | None = None
    azure_receipt: BootstrapReceipt | None = None
    azure_provider_state: Mapping[str, object] = Field(default_factory=dict)
    github_plan_input: BootstrapPlanInput | None = None
    github_plan: BootstrapPlan | None = None
    github_live_fingerprints: tuple[FingerprintRecord, ...] = ()
    github_receipt: BootstrapReceipt | None = None
    github_provider_state: Mapping[str, object] = Field(default_factory=dict)
    repository_preimages: Mapping[str, str] = Field(default_factory=dict)


class ConnectionSetupStateEnvelope(BootstrapDocument):
    payload: ConnectionSetupStatePayload
    generation_hash: str

    @property
    def generation(self) -> int:
        return self.payload.generation

    @property
    def lifecycle_state(self) -> ConnectionSetupLifecycleState:
        return self.payload.lifecycle_state

    @property
    def plan(self) -> ConnectionSetupPlan:
        return self.payload.plan

    @classmethod
    def create(cls, **values: object) -> "ConnectionSetupStateEnvelope":
        payload = ConnectionSetupStatePayload.model_validate(values)
        body = payload.model_dump(mode="json")
        return cls.model_validate(
            {
                "payload": body,
                "generation_hash": canonical_sha256({"payload": body}),
            }
        )

    @model_validator(mode="after")
    def _validate_hash(self) -> Self:
        if self.generation_hash != canonical_sha256(
            {"payload": self.payload.model_dump(mode="json")}
        ):
            raise BootstrapApplyError(
                "connection setup state hash does not match the payload"
            )
        return self


class ConnectionSetupReview(BootstrapDocument):
    plan_hash: str
    repository_identity: str
    optimizer_environment: str
    deployment_environment: str
    client_id_variable: str
    inventory: AzureConnectionInventory
    github_preflight_complete: bool = False

    def render_markdown(self) -> str:
        disposition = "adopt" if self.inventory.identity_exists else "create"
        source = (
            " from the existing repository registry"
            if self.inventory.identity_source == "repository_registry"
            else ""
        )
        lines = [
            "## GitHub-to-Azure connection review",
            f"- Repository: {self.repository_identity}",
            f"- GitHub environments: `{self.optimizer_environment}`, `{self.deployment_environment}`",
            f"- GitHub variable: `{self.client_id_variable}` in both environments",
            f"- Azure identity: {disposition} user-assigned managed identity `{self.inventory.identity_name}`{source}",
            f"- Identity resource: {self.inventory.identity_resource_id}",
            (
                "- Reuse policy: exact matching managed identity, OIDC subjects, "
                "RBAC assignments, GitHub environments, and variables are adopted; "
                "only missing or drifted settings are changed."
            ),
            (
                "- Existing GitHub connection: inspected and bound to this review."
                if self.github_preflight_complete
                else "- Existing GitHub connection: inspected after the new Azure identity resolves."
            ),
            "- OIDC subjects:",
            f"  - `{self.inventory.github_oidc_subject_prefix}:environment:{self.optimizer_environment}`",
            f"  - `{self.inventory.github_oidc_subject_prefix}:environment:{self.deployment_environment}`",
            "- RBAC:",
        ]
        lines.extend(
            f"  - Foundry User on `{scope}`"
            for scope in self.inventory.project_scopes
        )
        lines.extend(
            (
                "- Apply order: Azure identity/OIDC/RBAC first, then GitHub environments and variables.",
                "- Failure behavior: if GitHub setup fails, operation-owned Azure changes are compensated.",
                "- Approval scope: this complete connection only; no agent deployment is included.",
            )
        )
        return "\n".join(lines)


class ConnectionInventoryProtocol(Protocol):
    def inspect(
        self,
        *,
        repository_identity: str,
        targets: Sequence[tuple[str, str]],
        preferred_identity: IdentitySettings | None = None,
    ) -> AzureConnectionInventory: ...


class ConnectionDriverFactoryProtocol(Protocol):
    def azure(self, plan_input: BootstrapPlanInput): ...

    def github(self, plan_input: BootstrapPlanInput): ...


class DefaultConnectionDriverFactory:
    def azure(self, plan_input: BootstrapPlanInput) -> AzurePhaseDriver:
        return AzurePhaseDriver(plan_input=plan_input)

    def github(self, plan_input: BootstrapPlanInput) -> GitHubPhaseDriver:
        return GitHubPhaseDriver(plan_input=plan_input)


class CliConnectionInventory(ConnectionInventoryProtocol):
    def inspect(
        self,
        *,
        repository_identity: str,
        targets: Sequence[tuple[str, str]],
        preferred_identity: IdentitySettings | None = None,
    ) -> AzureConnectionInventory:
        reviewed_targets = tuple(
            sorted(set(targets), key=lambda item: (item[0].casefold(), item[1].casefold()))
        )
        if not reviewed_targets:
            raise BootstrapConfigError(
                "connection setup requires one reviewed account and endpoint per enabled agent"
            )
        accounts = tuple(
            sorted({item[0] for item in reviewed_targets}, key=str.casefold)
        )
        account_parts = [_resource_id_parts(value) for value in accounts]
        subscriptions = {item["subscription"] for item in account_parts}
        if len(subscriptions) != 1:
            raise BootstrapConfigError(
                "one bootstrap connection currently requires all Foundry projects in one Azure subscription"
            )
        account = account_parts[0]
        account_context = _run_json(
            [
                "az",
                "account",
                "show",
                "--query",
                "{tenant_id:tenantId,subscription_id:id}",
                "-o",
                "json",
            ],
            error="Azure CLI login is required for connection planning",
        )
        subscription_id = str(account_context.get("subscription_id") or "").lower()
        tenant_id = str(account_context.get("tenant_id") or "").lower()
        if subscription_id != account["subscription"]:
            raise BootstrapConfigError(
                "active Azure subscription does not match the reviewed Foundry project"
            )
        account_location = _run_text(
            [
                "az",
                "resource",
                "show",
                "--ids",
                accounts[0],
                "--query",
                "location",
                "-o",
                "tsv",
            ],
            error="unable to resolve the Foundry account location",
        )
        slug = re.sub(
            r"[^a-z0-9-]+",
            "-",
            repository_identity.split("/", 1)[-1].casefold(),
        ).strip("-")
        identity_name = f"foundry-opt-{slug}"[:128].rstrip("-")
        identity_resource_id = (
            f"/subscriptions/{subscription_id}/resourceGroups/{account['resource_group']}"
            f"/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{identity_name}"
        )
        identity_source: Literal[
            "derived_default",
            "repository_registry",
        ] = "derived_default"
        if (
            preferred_identity is not None
            and preferred_identity.kind == "entra_application"
        ):
            raise BootstrapConfigError(
                "existing registry Entra application connections require explicit migration; automatic OIDC reuse currently supports managed identities"
            )
        if (
            preferred_identity is not None
            and preferred_identity.kind == "user_assigned_managed_identity"
        ):
            assert preferred_identity.resource_id is not None
            preferred_parts = _resource_id_parts(
                preferred_identity.resource_id
            )
            if preferred_parts["subscription"] != subscription_id:
                raise BootstrapConfigError(
                    "existing registry identity is outside the reviewed Foundry subscription"
                )
            identity_resource_id = preferred_identity.resource_id
            identity_name = identity_resource_id.rstrip("/").rsplit("/", 1)[-1]
            identity_source = "repository_registry"
        identity = _run_json_optional(
            [
                "az",
                "identity",
                "show",
                "--ids",
                identity_resource_id,
                "-o",
                "json",
            ],
            error="unable to inspect the reviewed Azure managed identity",
        )
        location = (
            str(identity.get("location"))
            if identity is not None and identity.get("location")
            else account_location
        )
        if (
            identity is not None
            and preferred_identity is not None
            and preferred_identity.client_id is not None
            and str(identity.get("clientId") or "").casefold()
            != preferred_identity.client_id.casefold()
        ):
            raise BootstrapConfigError(
                "existing registry identity client_id does not match the live managed identity"
            )
        github = _run_json(
            ["gh", "api", f"repos/{repository_identity}"],
            error="GitHub CLI authentication is required for immutable OIDC planning",
        )
        repository_id = str(github.get("id") or "")
        owner = github.get("owner")
        owner_id = str(owner.get("id") or "") if isinstance(owner, Mapping) else ""
        owner_name, repository_name = repository_identity.split("/", 1)
        if not repository_id.isdigit() or not owner_id.isdigit():
            raise BootstrapConfigError(
                "GitHub repository and owner numeric ids are unavailable"
            )
        project_scopes = tuple(
            sorted(
                {
                    _project_scope(account_id, endpoint)
                    for account_id, endpoint in reviewed_targets
                },
                key=str.casefold,
            )
        )
        return AzureConnectionInventory(
            tenant_id=tenant_id,
            subscription_id=subscription_id,
            location=location,
            identity_resource_id=identity_resource_id,
            identity_name=identity_name,
            identity_exists=identity is not None,
            client_id=(
                str(identity.get("clientId")).lower()
                if identity is not None and identity.get("clientId")
                else None
            ),
            principal_id=(
                str(identity.get("principalId")).lower()
                if identity is not None and identity.get("principalId")
                else None
            ),
            identity_source=identity_source,
            github_oidc_subject_prefix=(
                f"repo:{owner_name}@{owner_id}/{repository_name}@{repository_id}"
            ),
            project_scopes=project_scopes,
        )


def default_connection_setup_state_root() -> Path:
    return default_state_root() / "connection-setup"


class ConnectionSetupCoordinator:
    def __init__(
        self,
        *,
        inventory: ConnectionInventoryProtocol | None = None,
        drivers: ConnectionDriverFactoryProtocol | None = None,
        repository_coordinator: RepositorySetupCoordinator | None = None,
        state_root: Path | None = None,
    ) -> None:
        self._inventory = inventory or CliConnectionInventory()
        self._drivers = drivers or DefaultConnectionDriverFactory()
        self._repository_coordinator = (
            repository_coordinator or RepositorySetupCoordinator()
        )
        self._state_root = (
            Path(state_root)
            if state_root is not None
            else default_connection_setup_state_root()
        )

    def build(self, operation) -> ConnectionSetupStateEnvelope:
        existing = self._try_load(
            operation.repository_binding.repository_id,
            operation.operation_id,
        )
        if existing is not None:
            self._validate_operation(existing.plan, operation)
            return existing
        setup = operation.handler_context.get("repository_setup")
        if not isinstance(setup, Mapping):
            raise BootstrapApplyError(
                "connection setup requires the applied repository plan context"
            )
        repository_state = self._repository_coordinator.status(operation)
        if repository_state.lifecycle_state != "applied":
            raise BootstrapApplyError(
                "connection setup requires an applied repository plan"
            )
        base_input = repository_state.plan_input
        repository_root = Path(operation.repository_binding.repository_root)
        registry = RootRegistry.from_document(
            (
                repository_root
                / ".foundry-opt"
                / "registry.yaml"
            ).read_text(encoding="utf-8")
        )
        github_settings = registry.github
        enabled = tuple(str(item) for item in setup.get("enabled_agent_ids", ()))
        if not enabled:
            raise BootstrapApplyError(
                "connection setup is unnecessary when no agents are enabled"
            )
        targets = {
            item.repo_agent_id.casefold(): item.reviewed_target
            for item in operation.foundry_targets
        }
        selected_targets = [targets[item.casefold()] for item in enabled]
        if any(
            target.account_resource_id is None
            or target.project_endpoint is None
            for target in selected_targets
        ):
            raise BootstrapConfigError(
                "enabled agents require reviewed Foundry account and project targets"
            )
        inventory = self._inventory.inspect(
            repository_identity=operation.repository_binding.repository_id,
            targets=tuple(
                (target.account_resource_id, target.project_endpoint)
                for target in selected_targets
                if target.account_resource_id is not None
                and target.project_endpoint is not None
            ),
            preferred_identity=registry.identity,
        )
        azure_input = _connection_plan_input(
            base_input,
            inventory=inventory,
            repository_identity=operation.repository_binding.repository_id,
            client_id=inventory.client_id,
            require_github=False,
            github=github_settings,
        )
        azure_driver = self._drivers.azure(azure_input)
        context = _driver_context(operation, azure_input, phase="azure")
        live = tuple(azure_driver.live_fingerprints(context))
        actions = tuple(azure_driver.plan(context))
        azure_plan = BootstrapPlan.create(
            operation_id=operation.operation_id,
            runtime_repository=operation.runtime_binding.runtime_repository,
            runtime_commit=operation.runtime_binding.runtime_commit,
            repository_identity=operation.repository_binding.repository_id,
            actions=actions,
        )
        github_input: BootstrapPlanInput | None = None
        github_plan: BootstrapPlan | None = None
        github_live: tuple[FingerprintRecord, ...] = ()
        if inventory.client_id is not None:
            github_input = _connection_plan_input(
                base_input,
                inventory=inventory,
                repository_identity=operation.repository_binding.repository_id,
                client_id=inventory.client_id,
                require_github=True,
                github=github_settings,
            )
            github_driver = self._drivers.github(github_input)
            github_context = _driver_context(
                operation,
                github_input,
                phase="github",
            )
            github_live = tuple(
                github_driver.live_fingerprints(github_context)
            )
            github_plan = BootstrapPlan.create(
                operation_id=operation.operation_id,
                runtime_repository=operation.runtime_binding.runtime_repository,
                runtime_commit=operation.runtime_binding.runtime_commit,
                repository_identity=operation.repository_binding.repository_id,
                actions=tuple(github_driver.plan(github_context)),
            )
        github_intent = {
            "optimizer_environment": github_settings.optimizer_environment,
            "deployment_environment": github_settings.deployment_environment,
            "client_id_variable": github_settings.client_id_variable,
            "oidc_subject_prefix": inventory.github_oidc_subject_prefix,
        }
        plan = ConnectionSetupPlan.create(
            operation_id=operation.operation_id,
            repository_identity=operation.repository_binding.repository_id,
            runtime_repository=operation.runtime_binding.runtime_repository,
            runtime_commit=operation.runtime_binding.runtime_commit,
            optimizer_environment=github_settings.optimizer_environment,
            deployment_environment=github_settings.deployment_environment,
            client_id_variable=github_settings.client_id_variable,
            inventory=inventory,
            azure_plan_input=azure_input,
            azure_plan=azure_plan,
            azure_live_fingerprints=live,
            github_plan_input=github_input,
            github_plan=github_plan,
            github_live_fingerprints=github_live,
            github_intent_hash=canonical_sha256(github_intent),
        )
        envelope = ConnectionSetupStateEnvelope.create(
            generation=0,
            lifecycle_state="awaiting_approval",
            plan=plan,
        )
        self._write(envelope)
        return envelope

    def review(self, operation) -> ConnectionSetupReview:
        plan = self.build(operation).plan
        return ConnectionSetupReview(
            plan_hash=plan.plan_hash,
            repository_identity=plan.repository_identity,
            optimizer_environment=plan.optimizer_environment,
            deployment_environment=plan.deployment_environment,
            client_id_variable=plan.client_id_variable,
            inventory=plan.inventory,
            github_preflight_complete=plan.github_plan is not None,
        )

    def approve(
        self,
        operation,
        *,
        actor: str,
        summary: str,
    ) -> ConnectionSetupStateEnvelope:
        envelope = self.build(operation)
        approval = ConnectionSetupApproval.create(
            plan=envelope.plan,
            actor=actor,
            summary=summary,
        )
        if envelope.lifecycle_state == "applied":
            if envelope.payload.approval != approval:
                raise BootstrapApplyError(
                    "connection approval does not match the recorded approval"
                )
            return envelope
        if (
            envelope.payload.approval is not None
            and envelope.payload.approval != approval
        ):
            raise BootstrapApplyError(
                "connection approval does not match the recorded approval"
            )
        if envelope.lifecycle_state in {"azure_applying", "github_applying"}:
            envelope = self._recover_connection_failure(operation)
        current = envelope
        if current.lifecycle_state == "awaiting_approval":
            azure_driver = self._drivers.azure(current.plan.azure_plan_input)
            applying = self._next(
                current,
                lifecycle_state="azure_applying",
                approval=approval,
            )
            self._write(applying, expected=current)
            current = applying
            self._install_child_checkpoint(
                operation,
                phase="azure",
                driver=azure_driver,
            )
            azure_receipt: BootstrapReceipt | None = None
            azure_state: Mapping[str, object] = {}
            try:
                context = _driver_context(
                    operation,
                    current.plan.azure_plan_input,
                    phase="azure",
                )
                live = tuple(azure_driver.live_fingerprints(context))
                if live != current.plan.azure_live_fingerprints:
                    raise BootstrapApplyError(
                        "Azure connection inventory drifted from the reviewed plan"
                    )
                azure_receipt = azure_driver.apply(current.plan.azure_plan)
                if azure_receipt.plan_hash != current.plan.azure_plan.plan_hash:
                    raise BootstrapApplyError(
                        "Azure connection receipt does not match the reviewed plan"
                    )
                azure_state = azure_driver.export_provider_state(
                    azure_receipt
                )
                current = self._persist_child_result(
                    operation,
                    phase="azure",
                    receipt=azure_receipt,
                    provider_state=azure_state,
                )
                if not azure_driver.verify(azure_receipt):
                    raise BootstrapApplyError(
                        "Azure connection verification failed"
                    )
            except Exception:
                self._clear_child_checkpoint(azure_driver)
                try:
                    self._recover_connection_failure(
                        operation,
                        ephemeral=(
                            "azure",
                            azure_driver,
                            azure_receipt,
                            azure_state,
                        ),
                    )
                except Exception as recovery_exc:
                    raise BootstrapApplyError(
                        "Azure connection compensation failed"
                    ) from recovery_exc
                raise
            finally:
                self._clear_child_checkpoint(azure_driver)
            identity = azure_state.get("identity")
            if not isinstance(identity, Mapping) or not identity.get("client_id"):
                try:
                    self._recover_connection_failure(
                        operation,
                        ephemeral=(
                            "azure",
                            azure_driver,
                            azure_receipt,
                            azure_state,
                        ),
                    )
                except Exception as recovery_exc:
                    raise BootstrapApplyError(
                        "Azure connection compensation failed"
                    ) from recovery_exc
                raise BootstrapApplyError(
                    "Azure connection did not return the managed identity client id"
                )
            azure_applied = self._next(
                current,
                lifecycle_state="azure_applied",
                azure_receipt=azure_receipt,
                azure_provider_state=azure_state,
            )
            try:
                self._write(azure_applied, expected=current)
            except Exception:
                try:
                    self._recover_connection_failure(
                        operation,
                        ephemeral=(
                            "azure",
                            azure_driver,
                            azure_receipt,
                            azure_state,
                        ),
                    )
                except Exception as recovery_exc:
                    raise BootstrapApplyError(
                        "Azure connection compensation failed"
                    ) from recovery_exc
                raise
            current = azure_applied
        if current.lifecycle_state == "azure_applied":
            if current.payload.azure_receipt is None:
                raise BootstrapApplyError(
                    "connection setup is missing its Azure-applied continuation"
                )
            try:
                identity = current.payload.azure_provider_state.get("identity")
                if not isinstance(identity, Mapping) or not identity.get(
                    "client_id"
                ):
                    raise BootstrapApplyError(
                        "connection setup is missing the resolved Azure identity"
                    )
                if (
                    current.plan.github_plan_input is not None
                    and current.plan.github_plan is not None
                ):
                    github_input = current.plan.github_plan_input
                    github_plan = current.plan.github_plan
                    github_live = current.plan.github_live_fingerprints
                    planned_client_id = (
                        github_input.github_phase.shared_client_id
                        if github_input.github_phase is not None
                        else None
                    )
                    if planned_client_id != str(identity["client_id"]):
                        raise BootstrapApplyError(
                            "resolved Azure identity no longer matches the reviewed GitHub connection"
                        )
                else:
                    github_input = _connection_plan_input(
                        current.plan.azure_plan_input,
                        inventory=current.plan.inventory,
                        repository_identity=current.plan.repository_identity,
                        client_id=str(identity["client_id"]),
                        require_github=True,
                        github=_plan_github_settings(current.plan),
                    )
                    github_driver = self._drivers.github(github_input)
                    github_context = _driver_context(
                        operation,
                        github_input,
                        phase="github",
                    )
                    github_live = tuple(
                        github_driver.live_fingerprints(github_context)
                    )
                    github_plan = BootstrapPlan.create(
                        operation_id=current.plan.operation_id,
                        runtime_repository=current.plan.runtime_repository,
                        runtime_commit=current.plan.runtime_commit,
                        repository_identity=current.plan.repository_identity,
                        actions=tuple(github_driver.plan(github_context)),
                    )
                preimages = _registry_connection_preimages(
                    Path(operation.repository_binding.repository_root)
                )
                github_applying = self._next(
                    current,
                    lifecycle_state="github_applying",
                    github_plan_input=github_input,
                    github_plan=github_plan,
                    github_live_fingerprints=github_live,
                    repository_preimages=preimages,
                )
                self._write(github_applying, expected=current)
                current = github_applying
            except Exception:
                try:
                    self._recover_connection_failure(operation)
                except Exception as recovery_exc:
                    raise BootstrapApplyError(
                        "connection compensation failed after GitHub planning"
                    ) from recovery_exc
                raise
        if current.lifecycle_state == "github_applying":
            if (
                current.payload.github_plan_input is None
                or current.payload.github_plan is None
                or current.payload.azure_receipt is None
            ):
                raise BootstrapApplyError(
                    "connection setup is missing its GitHub-applying continuation"
                )
            github_driver = self._drivers.github(
                current.payload.github_plan_input
            )
            self._install_child_checkpoint(
                operation,
                phase="github",
                driver=github_driver,
            )
            github_receipt: BootstrapReceipt | None = None
            github_state: Mapping[str, object] = {}
            try:
                github_context = _driver_context(
                    operation,
                    current.payload.github_plan_input,
                    phase="github",
                )
                live = tuple(
                    github_driver.live_fingerprints(github_context)
                )
                if live != current.payload.github_live_fingerprints:
                    raise BootstrapApplyError(
                        "GitHub connection inventory drifted from the reviewed continuation"
                    )
                github_receipt = github_driver.apply(
                    current.payload.github_plan
                )
                if github_receipt.plan_hash != current.payload.github_plan.plan_hash:
                    raise BootstrapApplyError(
                        "GitHub connection receipt does not match the reviewed continuation"
                    )
                github_state = github_driver.export_provider_state(
                    github_receipt
                )
                current = self._persist_child_result(
                    operation,
                    phase="github",
                    receipt=github_receipt,
                    provider_state=github_state,
                )
                if not github_driver.verify(github_receipt):
                    raise BootstrapApplyError(
                        "GitHub connection verification failed"
                    )
            except Exception:
                self._clear_child_checkpoint(github_driver)
                try:
                    self._recover_connection_failure(
                        operation,
                        ephemeral=(
                            "github",
                            github_driver,
                            github_receipt,
                            github_state,
                        ),
                    )
                except Exception as recovery_exc:
                    raise BootstrapApplyError(
                        "GitHub and Azure connection compensation failed"
                    ) from recovery_exc
                raise
            finally:
                self._clear_child_checkpoint(github_driver)
            cloud_applied = self._next(
                current,
                lifecycle_state="cloud_applied",
                github_receipt=github_receipt,
                github_provider_state=github_state,
            )
            try:
                self._write(cloud_applied, expected=current)
            except Exception:
                try:
                    self._recover_connection_failure(
                        operation,
                        ephemeral=(
                            "github",
                            github_driver,
                            github_receipt,
                            github_state,
                        ),
                    )
                except Exception as recovery_exc:
                    raise BootstrapApplyError(
                        "GitHub and Azure connection compensation failed"
                    ) from recovery_exc
                raise
            current = cloud_applied
        if current.lifecycle_state == "cloud_applied":
            registry_applying = self._next(
                current,
                lifecycle_state="registry_applying",
            )
            try:
                self._write(registry_applying, expected=current)
            except Exception:
                try:
                    self._recover_connection_failure(operation)
                except Exception as recovery_exc:
                    raise BootstrapApplyError(
                        "connection compensation failed before registry apply"
                    ) from recovery_exc
                raise
            current = registry_applying
        if current.lifecycle_state != "registry_applying":
            raise BootstrapApplyError(
                "connection setup is missing its cloud-applied continuation"
            )
        identity = current.payload.azure_provider_state.get("identity")
        if not isinstance(identity, Mapping) or not identity.get("client_id"):
            raise BootstrapApplyError(
                "connection setup is missing the resolved Azure identity"
            )
        try:
            _apply_registry_connection(
                Path(operation.repository_binding.repository_root),
                identity_resource_id=current.plan.inventory.identity_resource_id,
                client_id=str(identity["client_id"]),
                github=_plan_github_settings(current.plan),
                preimages=current.payload.repository_preimages,
            )
            latest = self._load(
                current.plan.repository_identity,
                current.plan.operation_id,
            )
            applied = self._next(latest, lifecycle_state="applied")
            self._write(applied, expected=latest)
        except Exception:
            try:
                self._recover_connection_failure(
                    operation,
                    restore_registry=True,
                )
            except Exception as recovery_exc:
                raise BootstrapApplyError(
                    "connection compensation failed after registry apply"
                ) from recovery_exc
            raise
        return applied

    def _install_child_checkpoint(
        self,
        operation,
        *,
        phase: Literal["azure", "github"],
        driver,
    ) -> None:
        setter = getattr(driver, "set_checkpoint", None)
        if not callable(setter):
            return
        lifecycle = f"{phase}_applying"

        def _persist(snapshot: Mapping[str, object]) -> None:
            if snapshot.get("checkpoint") is not True:
                raise BootstrapApplyError(
                    "connection child checkpoint marker is invalid"
                )
            receipt_raw = snapshot.get("receipt")
            if not isinstance(receipt_raw, Mapping):
                raise BootstrapApplyError(
                    "connection child checkpoint receipt is missing"
                )
            receipt = BootstrapReceipt.model_validate(receipt_raw)
            latest = self._load(
                operation.repository_binding.repository_id,
                operation.operation_id,
            )
            if latest.lifecycle_state != lifecycle:
                raise BootstrapApplyError(
                    "connection child checkpoint has no applying parent state"
                )
            phase_plan = (
                latest.plan.azure_plan
                if phase == "azure"
                else latest.payload.github_plan
            )
            if (
                phase_plan is None
                or receipt.operation_id != latest.plan.operation_id
                or receipt.repository_identity
                != latest.plan.repository_identity
                or receipt.plan_hash != phase_plan.plan_hash
            ):
                raise BootstrapApplyError(
                    "connection child checkpoint does not match its plan"
                )
            safe_persisted_document(snapshot)
            updates = (
                {
                    "azure_receipt": receipt,
                    "azure_provider_state": dict(snapshot),
                }
                if phase == "azure"
                else {
                    "github_receipt": receipt,
                    "github_provider_state": dict(snapshot),
                }
            )
            updated = self._next(latest, **updates)
            self._write(updated, expected=latest)

        setter(_persist)

    @staticmethod
    def _clear_child_checkpoint(driver) -> None:
        setter = getattr(driver, "set_checkpoint", None)
        if callable(setter):
            setter(None)

    def _persist_child_result(
        self,
        operation,
        *,
        phase: Literal["azure", "github"],
        receipt: BootstrapReceipt,
        provider_state: Mapping[str, object],
    ) -> ConnectionSetupStateEnvelope:
        safe_persisted_document(provider_state)
        latest = self._load(
            operation.repository_binding.repository_id,
            operation.operation_id,
        )
        if latest.lifecycle_state != f"{phase}_applying":
            raise BootstrapApplyError(
                "connection child result is not in its applying state"
            )
        updates = (
            {
                "azure_receipt": receipt,
                "azure_provider_state": dict(provider_state),
            }
            if phase == "azure"
            else {
                "github_receipt": receipt,
                "github_provider_state": dict(provider_state),
            }
        )
        updated = self._next(latest, **updates)
        self._write(updated, expected=latest)
        return updated

    def _recover_connection_failure(
        self,
        operation,
        *,
        ephemeral: tuple[
            Literal["azure", "github"],
            object,
            BootstrapReceipt | None,
            Mapping[str, object],
        ]
        | None = None,
        restore_registry: bool = False,
    ) -> ConnectionSetupStateEnvelope:
        latest = self._load(
            operation.repository_binding.repository_id,
            operation.operation_id,
        )
        compensated: set[str] = set()
        if restore_registry and latest.payload.repository_preimages:
            _restore_registry_connection_safely(
                Path(operation.repository_binding.repository_root),
                latest.payload.repository_preimages,
                identity_resource_id=latest.plan.inventory.identity_resource_id,
                client_id=self._connection_client_id(latest),
                github=_plan_github_settings(latest.plan),
            )
        if ephemeral is not None:
            phase, driver, receipt, provider_state = ephemeral
            if receipt is not None and _receipt_has_mutations(receipt):
                self._compensate_child(
                    driver,
                    receipt,
                    provider_state,
                    label=phase,
                )
                compensated.add(phase)
        latest = self._load(
            operation.repository_binding.repository_id,
            operation.operation_id,
        )
        if (
            "github" not in compensated
            and latest.payload.github_receipt is not None
            and latest.payload.github_plan_input is not None
            and _receipt_has_mutations(latest.payload.github_receipt)
        ):
            self._compensate_child(
                self._drivers.github(latest.payload.github_plan_input),
                latest.payload.github_receipt,
                latest.payload.github_provider_state,
                label="GitHub",
            )
        if (
            "azure" not in compensated
            and latest.payload.azure_receipt is not None
            and _receipt_has_mutations(latest.payload.azure_receipt)
        ):
            self._compensate_child(
                self._drivers.azure(latest.plan.azure_plan_input),
                latest.payload.azure_receipt,
                latest.payload.azure_provider_state,
                label="Azure",
            )
        latest = self._load(
            operation.repository_binding.repository_id,
            operation.operation_id,
        )
        reset = self._next(
            latest,
            lifecycle_state="awaiting_approval",
            approval=None,
            azure_receipt=None,
            azure_provider_state={},
            github_plan_input=None,
            github_plan=None,
            github_live_fingerprints=(),
            github_receipt=None,
            github_provider_state={},
            repository_preimages={},
        )
        self._write(reset, expected=latest)
        return reset

    @staticmethod
    def _compensate_child(
        driver,
        receipt: BootstrapReceipt,
        provider_state: Mapping[str, object],
        *,
        label: str,
    ) -> None:
        if provider_state:
            driver.restore_provider_state(provider_state)
        try:
            already_rolled_back = bool(driver.verify_rollback(receipt))
        except Exception:
            already_rolled_back = False
        if already_rolled_back:
            return
        driver.rollback(receipt)
        if not driver.verify_rollback(receipt):
            raise BootstrapApplyError(
                f"{label} connection compensation verification failed"
            )

    @staticmethod
    def _connection_client_id(
        envelope: ConnectionSetupStateEnvelope,
    ) -> str:
        identity = envelope.payload.azure_provider_state.get("identity")
        if not isinstance(identity, Mapping) or not identity.get("client_id"):
            raise BootstrapApplyError(
                "connection setup is missing the resolved Azure identity"
            )
        return str(identity["client_id"])

    def rollback(self, operation) -> ConnectionSetupStateEnvelope:
        envelope = self.build(operation)
        if envelope.lifecycle_state == "rolled_back":
            return envelope
        if envelope.lifecycle_state not in {
            "azure_applied",
            "github_applying",
            "cloud_applied",
            "registry_applying",
            "applied",
        }:
            raise BootstrapApplyError(
                "connection rollback requires applied Azure or GitHub work"
            )
        if envelope.lifecycle_state in {"registry_applying", "applied"}:
            _restore_registry_connection_safely(
                Path(operation.repository_binding.repository_root),
                envelope.payload.repository_preimages,
                identity_resource_id=envelope.plan.inventory.identity_resource_id,
                client_id=self._connection_client_id(envelope),
                github=_plan_github_settings(envelope.plan),
            )
        if (
            envelope.lifecycle_state
            in {"github_applying", "cloud_applied", "registry_applying", "applied"}
            and envelope.payload.github_receipt is not None
            and envelope.payload.github_plan_input is not None
            and _receipt_has_mutations(envelope.payload.github_receipt)
        ):
            github_driver = self._drivers.github(
                envelope.payload.github_plan_input
            )
            self._compensate_child(
                github_driver,
                envelope.payload.github_receipt,
                envelope.payload.github_provider_state,
                label="GitHub",
            )
        if (
            envelope.payload.azure_receipt is not None
            and _receipt_has_mutations(envelope.payload.azure_receipt)
        ):
            azure_driver = self._drivers.azure(envelope.plan.azure_plan_input)
            self._compensate_child(
                azure_driver,
                envelope.payload.azure_receipt,
                envelope.payload.azure_provider_state,
                label="Azure",
            )
        rolled = self._next(envelope, lifecycle_state="rolled_back")
        self._write(rolled, expected=envelope)
        return rolled

    def status(self, operation) -> ConnectionSetupStateEnvelope:
        envelope = self._load(
            operation.repository_binding.repository_id,
            operation.operation_id,
        )
        self._validate_operation(envelope.plan, operation)
        return envelope

    def resource_links(self, operation) -> ResourceLinksReview:
        plan = self.build(operation).plan
        return ResourceLinksReview(
            github=(
                ResourceLink(
                    label="GitHub environments",
                    target=plan.repository_identity,
                    url=f"https://github.com/{plan.repository_identity}/settings/environments",
                ),
            ),
            azure=(
                ResourceLink(
                    label="Azure managed identity",
                    target=plan.inventory.identity_resource_id,
                    url=(
                        "https://portal.azure.com/#@/resource"
                        + plan.inventory.identity_resource_id
                    ),
                ),
            ),
        )

    def _validate_operation(self, plan: ConnectionSetupPlan, operation) -> None:
        if (
            plan.operation_id != operation.operation_id
            or plan.repository_identity
            != operation.repository_binding.repository_id
            or plan.runtime_repository
            != operation.runtime_binding.runtime_repository
            or plan.runtime_commit != operation.runtime_binding.runtime_commit
        ):
            raise BootstrapApplyError(
                "connection setup state does not match the active bootstrap operation"
            )

    def _operation_directory(
        self,
        repository_identity: str,
        operation_id: str,
    ) -> Path:
        root = self._state_root.resolve()
        operation_segment = require_safe_operation_id(
            operation_id,
            message="connection setup operation id is invalid",
            error_factory=BootstrapApplyError,
        )
        target = (
            root
            / canonical_sha256({"repository_identity": repository_identity})
            / operation_segment
        ).resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise BootstrapApplyError(
                "connection setup state escapes the state root"
            ) from exc
        return target

    def _path(self, repository_identity: str, operation_id: str) -> Path:
        return (
            self._operation_directory(repository_identity, operation_id)
            / _STATE_FILE_NAME
        )

    def _try_load(
        self,
        repository_identity: str,
        operation_id: str,
    ) -> ConnectionSetupStateEnvelope | None:
        try:
            return self._load(repository_identity, operation_id)
        except FileNotFoundError:
            return None

    def _load(
        self,
        repository_identity: str,
        operation_id: str,
    ) -> ConnectionSetupStateEnvelope:
        data = self._path(repository_identity, operation_id).read_bytes()
        if len(data) > _MAX_STATE_BYTES:
            raise BootstrapApplyError(
                "connection setup state exceeds the size limit"
            )
        try:
            return ConnectionSetupStateEnvelope.model_validate_json(data)
        except Exception as exc:
            raise BootstrapApplyError(
                "connection setup state is invalid or tampered"
            ) from exc

    def _write(
        self,
        envelope: ConnectionSetupStateEnvelope,
        *,
        expected: ConnectionSetupStateEnvelope | None = None,
    ) -> None:
        path = self._path(
            envelope.plan.repository_identity,
            envelope.plan.operation_id,
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = path.parent / _LOCK_FILE_NAME
        with state_file_lock(
            lock,
            locked_message=(
                "connection setup state is locked by another writer"
            ),
        ):
            if expected is None:
                if path.exists():
                    raise BootstrapApplyError(
                        "connection setup state already exists"
                    )
            else:
                current = self._load(
                    envelope.plan.repository_identity,
                    envelope.plan.operation_id,
                )
                if (
                    current.generation != expected.generation
                    or current.generation_hash != expected.generation_hash
                ):
                    raise BootstrapApplyError(
                        "connection setup state generation conflict"
                    )
            data = canonical_json_bytes(envelope.model_dump(mode="json")) + b"\n"
            atomic_replace_state(
                path,
                data,
                generation_hash=envelope.generation_hash,
            )

    @staticmethod
    def _next(
        envelope: ConnectionSetupStateEnvelope,
        **updates: object,
    ) -> ConnectionSetupStateEnvelope:
        payload = envelope.payload.model_dump(mode="python")
        payload.update(updates)
        payload["generation"] = envelope.generation + 1
        return ConnectionSetupStateEnvelope.create(**payload)


class BootstrapConnectionSetupHandler:
    def __init__(
        self,
        *,
        coordinator: ConnectionSetupCoordinator | None = None,
    ) -> None:
        self._coordinator = coordinator or ConnectionSetupCoordinator()

    def review(self, *, operation) -> ConnectionSetupReview:
        return self._coordinator.review(operation)

    def approve(self, *, operation, approval) -> object:
        from foundry_opt.bootstrap.runner import (
            BootstrapChildReference,
            BootstrapStageOutcome,
        )

        state = self._coordinator.approve(
            operation,
            actor=approval.actor,
            summary=approval.summary,
        )
        assert state.payload.azure_receipt is not None
        assert state.payload.github_receipt is not None
        adopted = (
            len(state.payload.azure_receipt.adopted_actions)
            + len(state.payload.github_receipt.adopted_actions)
        )
        created = (
            len(state.payload.azure_receipt.created_actions)
            + len(state.payload.github_receipt.created_actions)
        )
        changed = (
            len(state.payload.azure_receipt.changed_actions)
            + len(state.payload.github_receipt.changed_actions)
        )
        connection_summary = (
            f"reused {adopted}, created {created}, updated {changed}"
        )
        child_refs = tuple(
            item for item in operation.child_refs if item.step != "connection"
        )
        return BootstrapStageOutcome(
            stage="commit_approval",
            note=(
                "Connected the reviewed GitHub environments to the Azure identity "
                f"({connection_summary}). Review the local commit next."
            ),
            child_refs=(
                *child_refs,
                BootstrapChildReference(
                    step="connection",
                    kind="github-azure-connection",
                    identifier=state.plan.plan_hash,
                    summary=connection_summary,
                ),
            ),
        )

    def rollback(self, *, operation, step, child_ref) -> object:
        if step != "connection" or child_ref.step != "connection":
            raise BootstrapApplyError(
                "connection setup handler can only roll back connection work"
            )
        state = self._coordinator.status(operation)
        if child_ref.identifier != state.plan.plan_hash:
            raise BootstrapApplyError(
                "connection rollback child reference does not match state"
            )
        self._coordinator.rollback(operation)
        return self._rollback_outcome(operation)

    def reconcile_rollback(
        self,
        *,
        operation,
        step,
        child_ref,
    ) -> object | None:
        if step != "connection" or child_ref.step != "connection":
            raise BootstrapApplyError(
                "connection setup handler can only reconcile connection work"
            )
        state = self._coordinator.status(operation)
        if state.lifecycle_state != "rolled_back":
            return None
        if child_ref.identifier != state.plan.plan_hash:
            raise BootstrapApplyError(
                "connection rollback child reference does not match state"
            )
        return self._rollback_outcome(operation)

    @staticmethod
    def _rollback_outcome(operation) -> object:
        from foundry_opt.bootstrap.runner import BootstrapStageOutcome

        remaining = tuple(
            item for item in operation.child_refs if item.step != "connection"
        )
        return BootstrapStageOutcome(
            stage="rolled_back",
            note="Rolled back operation-owned GitHub and Azure connection changes.",
            child_refs=remaining,
        )

    def build_resource_links(self, *, operation) -> ResourceLinksReview:
        return self._coordinator.resource_links(operation)


def _connection_plan_input(
    base: BootstrapPlanInput,
    *,
    inventory: AzureConnectionInventory,
    repository_identity: str,
    client_id: str | None,
    require_github: bool,
    github: GitHubSettings,
) -> BootstrapPlanInput:
    payload = base.model_dump(mode="json")
    payload["offline_plan"] = False
    payload["required_phases"] = (
        ["github"] if require_github else ["azure"]
    )
    payload["github_phase"] = {
        "optimizer_environment": github.optimizer_environment,
        "deployment_environment": github.deployment_environment,
        "shared_client_id": (
            client_id
            if client_id is not None
            else "azure_identity_resolution_required"
        ),
        "client_id_variable_name": github.client_id_variable,
        "oidc_subject_prefix": inventory.github_oidc_subject_prefix,
        "default_branch_policy_intent": "preserve_repository_default",
    }
    payload["azure_phase"] = {
        "tenant_id": inventory.tenant_id,
        "subscription_id": inventory.subscription_id,
        "identity": {
            "identity_kind": "user_assigned_managed_identity",
            "existing_resource_id": inventory.identity_resource_id,
            "existing_client_id": inventory.client_id,
            "existing_object_id": inventory.principal_id,
            "create_if_missing": not inventory.identity_exists,
        },
        "resource_group": _resource_id_parts(
            inventory.identity_resource_id
        )["resource_group"],
        "location": inventory.location,
        "github_repository_id": repository_identity,
        "approved_role_assignments": [
            {
                "alias": f"foundry-user-{index + 1}",
                "role_definition_id": (
                    f"/subscriptions/{inventory.subscription_id}"
                    f"/providers/Microsoft.Authorization/roleDefinitions/{_FOUNDRY_USER_ROLE_GUID}"
                ),
                "scope": scope,
            }
            for index, scope in enumerate(inventory.project_scopes)
        ],
    }
    return BootstrapPlanInput.model_validate(payload)


def _driver_context(
    operation,
    plan_input: BootstrapPlanInput,
    *,
    phase: str,
) -> dict[str, object]:
    return {
        "repository_id": operation.repository_binding.repository_id,
        "operation_id": operation.operation_id,
        "runtime_repository": operation.runtime_binding.runtime_repository,
        "runtime_commit": operation.runtime_binding.runtime_commit,
        "selection_plan": operation.selection_plan,
        "phase": phase,
        "plan_input": plan_input,
    }


def _receipt_has_mutations(receipt: BootstrapReceipt) -> bool:
    return bool(
        receipt.created_actions
        or receipt.changed_actions
        or receipt.compensation_required_actions
    )


def _registry_connection_preimages(
    repository_root: Path,
) -> dict[str, str]:
    paths = (
        ".foundry-opt/registry.yaml",
        ".foundry-opt/bootstrap.lock.json",
    )
    preimages: dict[str, str] = {}
    for repo_path in paths:
        path = repository_root / repo_path
        try:
            preimages[repo_path] = path.read_bytes().hex()
        except OSError as exc:
            raise BootstrapApplyError(
                f"connection setup requires {repo_path}"
            ) from exc
    return preimages


def _apply_registry_connection(
    repository_root: Path,
    *,
    identity_resource_id: str,
    client_id: str,
    github: GitHubSettings,
    preimages: Mapping[str, str],
) -> None:
    desired = _desired_registry_connection(
        preimages,
        identity_resource_id=identity_resource_id,
        client_id=client_id,
        github=github,
    )
    for repo_path, desired_bytes in desired.items():
        path = repository_root / repo_path
        try:
            before = bytes.fromhex(preimages[repo_path])
            current = path.read_bytes()
        except (KeyError, ValueError, OSError) as exc:
            raise BootstrapApplyError(
                "connection registry preimage is unavailable"
            ) from exc
        if current not in {before, desired_bytes}:
            raise BootstrapApplyError(
                f"connection setup refused repository drift at {repo_path}"
            )
    for repo_path in (
        ".foundry-opt/registry.yaml",
        ".foundry-opt/bootstrap.lock.json",
    ):
        _atomic_write(repository_root / repo_path, desired[repo_path])


def _restore_registry_connection(
    repository_root: Path,
    preimages: Mapping[str, str],
) -> None:
    if not preimages:
        raise BootstrapApplyError(
            "connection rollback is missing repository preimages"
        )
    for repo_path in (
        ".foundry-opt/bootstrap.lock.json",
        ".foundry-opt/registry.yaml",
    ):
        try:
            content = bytes.fromhex(preimages[repo_path])
        except (KeyError, ValueError) as exc:
            raise BootstrapApplyError(
                "connection rollback preimage is invalid"
            ) from exc
        _atomic_write(repository_root / repo_path, content)


def _restore_registry_connection_safely(
    repository_root: Path,
    preimages: Mapping[str, str],
    *,
    identity_resource_id: str,
    client_id: str,
    github: GitHubSettings,
) -> None:
    desired = _desired_registry_connection(
        preimages,
        identity_resource_id=identity_resource_id,
        client_id=client_id,
        github=github,
    )
    for repo_path, desired_bytes in desired.items():
        path = repository_root / repo_path
        try:
            before = bytes.fromhex(preimages[repo_path])
            current = path.read_bytes()
        except (KeyError, ValueError, OSError) as exc:
            raise BootstrapApplyError(
                "connection rollback preimage is unavailable"
            ) from exc
        if current not in {before, desired_bytes}:
            raise BootstrapApplyError(
                f"connection rollback refused repository drift at {repo_path}"
            )
    _restore_registry_connection(repository_root, preimages)


def _desired_registry_connection(
    preimages: Mapping[str, str],
    *,
    identity_resource_id: str,
    client_id: str,
    github: GitHubSettings,
) -> dict[str, bytes]:
    try:
        registry_before = bytes.fromhex(
            preimages[".foundry-opt/registry.yaml"]
        )
        lock_before = bytes.fromhex(
            preimages[".foundry-opt/bootstrap.lock.json"]
        )
    except (KeyError, ValueError) as exc:
        raise BootstrapApplyError(
            "connection registry preimages are invalid"
        ) from exc
    registry = RootRegistry.from_document(
        registry_before.decode("utf-8")
    )
    registry_payload = registry.model_dump(mode="json")
    registry_payload["github"] = github.model_dump(mode="json")
    registry_payload["identity"] = IdentitySettings(
        kind="user_assigned_managed_identity",
        resource_id=identity_resource_id,
        client_id=client_id,
    ).model_dump(mode="json")
    registry_bytes = yaml.safe_dump(
        registry_payload,
        sort_keys=False,
        allow_unicode=False,
    ).encode("utf-8")
    lock = BootstrapLock.model_validate_json(lock_before)
    lock_payload = lock.model_dump(mode="json")
    managed = []
    found_registry = False
    for item in lock_payload["managed_files"]:
        updated = dict(item)
        if updated["path"] == ".foundry-opt/registry.yaml":
            updated["applied_sha256"] = hashlib.sha256(
                registry_bytes
            ).hexdigest()
            found_registry = True
        managed.append(updated)
    if not found_registry:
        raise BootstrapApplyError(
            "managed lock does not track the repository registry"
        )
    lock_payload["managed_files"] = managed
    lock_bytes = (
        json.dumps(
            lock_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("utf-8")
    return {
        ".foundry-opt/registry.yaml": registry_bytes,
        ".foundry-opt/bootstrap.lock.json": lock_bytes,
    }


def _plan_github_settings(
    plan: ConnectionSetupPlan,
) -> GitHubSettings:
    return GitHubSettings(
        optimizer_environment=plan.optimizer_environment,
        deployment_environment=plan.deployment_environment,
        client_id_variable=plan.client_id_variable,
        oidc_subject_prefix=plan.inventory.github_oidc_subject_prefix,
    )


def _atomic_write(path: Path, content: bytes) -> None:
    temp = path.with_name(f"{path.name}.connection.tmp")
    with open(temp, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _resource_id_parts(value: str) -> dict[str, str]:
    parts = [part for part in value.split("/") if part]
    lowered = [part.casefold() for part in parts]
    try:
        subscription = parts[lowered.index("subscriptions") + 1].lower()
        resource_group = parts[lowered.index("resourcegroups") + 1]
    except (ValueError, IndexError) as exc:
        raise BootstrapConfigError(
            "Azure resource id omits subscription or resource group"
        ) from exc
    return {
        "subscription": subscription,
        "resource_group": resource_group,
    }


def _project_scope(account_resource_id: str, endpoint: str) -> str:
    parsed = urlparse(endpoint)
    marker = "/api/projects/"
    if marker not in parsed.path:
        raise BootstrapConfigError(
            "Foundry project endpoint does not contain /api/projects/<name>"
        )
    project_name = parsed.path.split(marker, 1)[1].strip("/")
    if not project_name or "/" in project_name:
        raise BootstrapConfigError("Foundry project endpoint has an invalid name")
    return f"{account_resource_id}/projects/{project_name}"


def _run_json(command: Sequence[str], *, error: str) -> Mapping[str, object]:
    completed = subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise BootstrapConfigError(error)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise BootstrapConfigError(error) from exc
    if not isinstance(payload, Mapping):
        raise BootstrapConfigError(error)
    return payload


def _run_json_optional(
    command: Sequence[str],
    *,
    error: str,
) -> Mapping[str, object] | None:
    completed = subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).casefold()
        if any(
            marker in detail
            for marker in (
                "resourcenotfound",
                "resource was not found",
                "could not be found",
                "was not found",
            )
        ):
            return None
        raise BootstrapConfigError(error)
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, Mapping) else None


def _run_text(command: Sequence[str], *, error: str) -> str:
    completed = subprocess.run(
        list(command),
        check=False,
        capture_output=True,
        text=True,
    )
    value = completed.stdout.strip()
    if completed.returncode != 0 or not value:
        raise BootstrapConfigError(error)
    return value


__all__ = [
    "AzureConnectionInventory",
    "BootstrapConnectionSetupHandler",
    "ConnectionSetupCoordinator",
    "ConnectionSetupReview",
    "ConnectionSetupStateEnvelope",
]
