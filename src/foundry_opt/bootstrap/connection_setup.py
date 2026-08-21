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

ConnectionSetupLifecycleState = Literal[
    "awaiting_approval",
    "azure_applied",
    "cloud_applied",
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

    def render_markdown(self) -> str:
        disposition = "adopt" if self.inventory.identity_exists else "create"
        lines = [
            "## GitHub-to-Azure connection review",
            f"- Repository: {self.repository_identity}",
            f"- GitHub environments: `{self.optimizer_environment}`, `{self.deployment_environment}`",
            f"- GitHub variable: `{self.client_id_variable}` in both environments",
            f"- Azure identity: {disposition} user-assigned managed identity `{self.inventory.identity_name}`",
            f"- Identity resource: {self.inventory.identity_resource_id}",
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
        location = _run_text(
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
        )
        azure_input = _connection_plan_input(
            base_input,
            inventory=inventory,
            repository_identity=operation.repository_binding.repository_id,
            client_id=inventory.client_id,
            require_github=False,
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
        github_intent = {
            "optimizer_environment": "copilot",
            "deployment_environment": "foundry-production",
            "client_id_variable": "AZURE_FOUNDRY_OPT_CLIENT_ID",
            "oidc_subject_prefix": inventory.github_oidc_subject_prefix,
        }
        plan = ConnectionSetupPlan.create(
            operation_id=operation.operation_id,
            repository_identity=operation.repository_binding.repository_id,
            runtime_repository=operation.runtime_binding.runtime_repository,
            runtime_commit=operation.runtime_binding.runtime_commit,
            optimizer_environment="copilot",
            deployment_environment="foundry-production",
            client_id_variable="AZURE_FOUNDRY_OPT_CLIENT_ID",
            inventory=inventory,
            azure_plan_input=azure_input,
            azure_plan=azure_plan,
            azure_live_fingerprints=live,
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
        current = envelope
        if current.lifecycle_state == "awaiting_approval":
            azure_driver = self._drivers.azure(current.plan.azure_plan_input)
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
            try:
                azure_receipt = azure_driver.apply(current.plan.azure_plan)
            except Exception as exc:
                _compensate_driver_failure(azure_driver, exc)
                raise
            if not azure_driver.verify(azure_receipt):
                raise BootstrapApplyError("Azure connection verification failed")
            azure_state = azure_driver.export_provider_state(azure_receipt)
            identity = azure_state.get("identity")
            if not isinstance(identity, Mapping) or not identity.get("client_id"):
                raise BootstrapApplyError(
                    "Azure connection did not return the managed identity client id"
                )
            client_id = str(identity["client_id"])
            github_input = _connection_plan_input(
                current.plan.azure_plan_input,
                inventory=current.plan.inventory,
                repository_identity=current.plan.repository_identity,
                client_id=client_id,
                require_github=True,
            )
            github_driver = self._drivers.github(github_input)
            github_context = _driver_context(
                operation,
                github_input,
                phase="github",
            )
            github_live = tuple(github_driver.live_fingerprints(github_context))
            github_plan = BootstrapPlan.create(
                operation_id=current.plan.operation_id,
                runtime_repository=current.plan.runtime_repository,
                runtime_commit=current.plan.runtime_commit,
                repository_identity=current.plan.repository_identity,
                actions=tuple(github_driver.plan(github_context)),
            )
            azure_applied = self._next(
                current,
                lifecycle_state="azure_applied",
                approval=approval,
                azure_receipt=azure_receipt,
                azure_provider_state=azure_state,
                github_plan_input=github_input,
                github_plan=github_plan,
                github_live_fingerprints=github_live,
            )
            self._write(azure_applied, expected=current)
            current = azure_applied
        if current.lifecycle_state == "azure_applied":
            if (
                current.payload.github_plan_input is None
                or current.payload.github_plan is None
                or current.payload.azure_receipt is None
            ):
                raise BootstrapApplyError(
                    "connection setup is missing its Azure-applied continuation"
                )
            github_driver = self._drivers.github(
                current.payload.github_plan_input
            )
            github_context = _driver_context(
                operation,
                current.payload.github_plan_input,
                phase="github",
            )
            live = tuple(github_driver.live_fingerprints(github_context))
            if live != current.payload.github_live_fingerprints:
                raise BootstrapApplyError(
                    "GitHub connection inventory drifted from the reviewed continuation"
                )
            try:
                github_receipt = github_driver.apply(
                    current.payload.github_plan
                )
                if not github_driver.verify(github_receipt):
                    raise BootstrapApplyError(
                        "GitHub connection verification failed"
                    )
                github_state = github_driver.export_provider_state(
                    github_receipt
                )
            except Exception as exc:
                _compensate_driver_failure(github_driver, exc)
                azure_driver = self._drivers.azure(
                    current.plan.azure_plan_input
                )
                azure_driver.restore_provider_state(
                    current.payload.azure_provider_state
                )
                azure_driver.rollback(current.payload.azure_receipt)
                if not azure_driver.verify_rollback(
                    current.payload.azure_receipt
                ):
                    raise BootstrapApplyError(
                        "Azure compensation verification failed"
                    )
                reset = self._next(
                    current,
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
                self._write(reset, expected=current)
                raise
            preimages = _registry_connection_preimages(
                Path(operation.repository_binding.repository_root)
            )
            cloud_applied = self._next(
                current,
                lifecycle_state="cloud_applied",
                github_receipt=github_receipt,
                github_provider_state=github_state,
                repository_preimages=preimages,
            )
            self._write(cloud_applied, expected=current)
            current = cloud_applied
        if current.lifecycle_state != "cloud_applied":
            raise BootstrapApplyError(
                "connection setup is missing its cloud-applied continuation"
            )
        identity = current.payload.azure_provider_state.get("identity")
        if not isinstance(identity, Mapping) or not identity.get("client_id"):
            raise BootstrapApplyError(
                "connection setup is missing the resolved Azure identity"
            )
        _apply_registry_connection(
            Path(operation.repository_binding.repository_root),
            identity_resource_id=current.plan.inventory.identity_resource_id,
            client_id=str(identity["client_id"]),
            preimages=current.payload.repository_preimages,
        )
        applied = self._next(current, lifecycle_state="applied")
        self._write(applied, expected=current)
        return applied

    def rollback(self, operation) -> ConnectionSetupStateEnvelope:
        envelope = self.build(operation)
        if envelope.lifecycle_state not in {
            "azure_applied",
            "cloud_applied",
            "applied",
        }:
            raise BootstrapApplyError(
                "connection rollback requires applied Azure or GitHub work"
            )
        if (
            envelope.lifecycle_state in {"cloud_applied", "applied"}
            and envelope.payload.github_receipt is not None
            and envelope.payload.github_plan_input is not None
        ):
            github_driver = self._drivers.github(
                envelope.payload.github_plan_input
            )
            github_driver.restore_provider_state(
                envelope.payload.github_provider_state
            )
            github_driver.rollback(envelope.payload.github_receipt)
            if not github_driver.verify_rollback(
                envelope.payload.github_receipt
            ):
                raise BootstrapApplyError(
                    "GitHub connection rollback verification failed"
                )
        if envelope.payload.azure_receipt is not None:
            azure_driver = self._drivers.azure(envelope.plan.azure_plan_input)
            azure_driver.restore_provider_state(
                envelope.payload.azure_provider_state
            )
            azure_driver.rollback(envelope.payload.azure_receipt)
            if not azure_driver.verify_rollback(
                envelope.payload.azure_receipt
            ):
                raise BootstrapApplyError(
                    "Azure connection rollback verification failed"
                )
        if envelope.lifecycle_state == "applied":
            _restore_registry_connection(
                Path(operation.repository_binding.repository_root),
                envelope.payload.repository_preimages,
            )
        rolled = self._next(envelope, lifecycle_state="rolled_back")
        self._write(rolled, expected=envelope)
        return rolled

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
        try:
            lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise BootstrapApplyError(
                "connection setup state is locked by another writer"
            ) from exc
        try:
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
            temp = path.with_name(
                f"{path.stem}.{envelope.generation_hash}.tmp"
            )
            with open(temp, "xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            os.close(lock_fd)
            os.unlink(lock)

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
        child_refs = tuple(
            item for item in operation.child_refs if item.step != "connection"
        )
        return BootstrapStageOutcome(
            stage="commit_approval",
            note="Connected the reviewed GitHub environments to the Azure identity. Review the local commit next.",
            child_refs=(
                *child_refs,
                BootstrapChildReference(
                    step="connection",
                    kind="github-azure-connection",
                    identifier=state.plan.plan_hash,
                    summary=(
                        f"{state.plan.inventory.identity_name}; "
                        f"{state.plan.optimizer_environment}, "
                        f"{state.plan.deployment_environment}"
                    ),
                ),
            ),
        )

    def rollback(self, *, operation, step, child_ref) -> object:
        from foundry_opt.bootstrap.runner import BootstrapStageOutcome

        if step != "connection" or child_ref.step != "connection":
            raise BootstrapApplyError(
                "connection setup handler can only roll back connection work"
            )
        self._coordinator.rollback(operation)
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
) -> BootstrapPlanInput:
    payload = base.model_dump(mode="json")
    payload["offline_plan"] = False
    payload["required_phases"] = (
        ["github"] if require_github else ["azure"]
    )
    payload["github_phase"] = {
        "optimizer_environment": "copilot",
        "deployment_environment": "foundry-production",
        "shared_client_id": (
            client_id
            if client_id is not None
            else "azure_identity_resolution_required"
        ),
        "client_id_variable_name": "AZURE_FOUNDRY_OPT_CLIENT_ID",
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


def _compensate_driver_failure(driver, error: Exception) -> None:
    receipt = getattr(error, "compensation_receipt", None)
    provider_state = getattr(error, "provider_state", None)
    if not isinstance(receipt, BootstrapReceipt) or not isinstance(
        provider_state,
        Mapping,
    ):
        return
    driver.restore_provider_state(provider_state)
    driver.rollback(receipt)
    if not driver.verify_rollback(receipt):
        raise BootstrapApplyError(
            "connection child compensation verification failed"
        ) from error


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
    preimages: Mapping[str, str],
) -> None:
    desired = _desired_registry_connection(
        preimages,
        identity_resource_id=identity_resource_id,
        client_id=client_id,
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


def _desired_registry_connection(
    preimages: Mapping[str, str],
    *,
    identity_resource_id: str,
    client_id: str,
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
