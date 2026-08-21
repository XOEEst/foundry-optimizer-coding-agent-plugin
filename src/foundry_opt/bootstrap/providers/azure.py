from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import unquote

import httpx

from foundry_opt.bootstrap.canonical import canonical_sha256, safe_persisted_document
from foundry_opt.bootstrap.contracts import BindingAssessment, BootstrapAction, BootstrapPlan, BootstrapReceipt, FingerprintRecord, RedactedStatusInfo
from foundry_opt.bootstrap.errors import BootstrapProviderError

_ARM_SCOPE = "https://management.azure.com/.default"
_GRAPH_SCOPE = "https://graph.microsoft.com/.default"
_ACTIONS_ISSUER = "https://token.actions.githubusercontent.com"
_ACTIONS_AUDIENCE = "api://AzureADTokenExchange"
_MANAGED_IDENTITY_API_VERSION = "2023-01-31"
_FIC_API_VERSION = "2024-11-30"
_AUTHZ_API_VERSION = "2022-04-01"
_GRAPH_APPLICATIONS = "https://graph.microsoft.com/v1.0/applications"
_GRAPH_SERVICE_PRINCIPALS = "https://graph.microsoft.com/v1.0/servicePrincipals"
_ROLE_ASSIGNMENTS_SEGMENT = "providers/Microsoft.Authorization/roleAssignments"
_OWNER_ROLE_GUID = "8e3af657-a8ff-443c-a75c-2fe8c4bcb635"
_CONTRIBUTOR_ROLE_GUID = "b24988ac-6180-42a0-ab88-20f7382dd24c"
_CHECKPOINT_VERSION = 1
_ROLE_WRITABLE_PROPERTIES = (
    "roleDefinitionId",
    "principalId",
    "principalType",
    "condition",
    "conditionVersion",
    "delegatedManagedIdentityResourceId",
    "description",
)


class AzureProviderError(BootstrapProviderError):
    pass


class AzureProviderApplyError(AzureProviderError):
    def __init__(
        self,
        message: str,
        *,
        compensation_receipt: BootstrapReceipt,
        provider_state: Mapping[str, object],
    ) -> None:
        super().__init__(message)
        self.compensation_receipt = compensation_receipt
        self.provider_state = dict(provider_state)


def rollback_failure_details(
    exc: BaseException,
) -> tuple[BootstrapReceipt | None, Mapping[str, object]]:
    if isinstance(exc, AzureProviderApplyError):
        return exc.compensation_receipt, dict(exc.provider_state)
    return None, {}


@dataclass(frozen=True)
class AzureIdentityReference:
    kind: str
    client_id: str | None
    resource_id: str | None
    object_id: str | None
    principal_id: str | None
    tenant_id: str | None
    subscription_id: str | None
    name: str
    adopted: bool
    location: str | None = None


@dataclass(frozen=True)
class PlannedRoleAssignment:
    role_key: str
    scope: str
    role_definition_id: str
    approval_fingerprint: str


@dataclass(frozen=True)
class PlannedBindingSet:
    identity: AzureIdentityReference
    roles: tuple[PlannedRoleAssignment, ...]
    subjects: tuple[str, str]


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _fingerprint(label: str, value: object) -> FingerprintRecord:
    return FingerprintRecord(label=label, sha256=hashlib.sha256(_canonical_bytes(value)).hexdigest())


def _text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise AzureProviderError(f"{field} must be a non-empty string")
    return value


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _json_response(response: httpx.Response) -> Mapping[str, object]:
    if 300 <= response.status_code < 400:
        raise AzureProviderError("redirect responses are not allowed")
    if response.status_code >= 400:
        raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise AzureProviderError("Azure returned a non-object JSON document")
    return payload


def _canonical_resource_id(value: str) -> str:
    raw = value.strip()
    if raw.lower().startswith("https://management.azure.com/"):
        raw = raw[len("https://management.azure.com") :]
    decoded = unquote(raw)
    lowered = raw.lower()
    if any(token in lowered for token in ("%2f", "%5c", "%2e", "?", "#")) or "\\" in decoded or "/./" in decoded or "/../" in decoded or decoded.endswith("/.") or decoded.endswith("/..") or "//" in decoded:
        raise AzureProviderError("resource ids and scopes contain forbidden delimiters or traversal segments")
    if not decoded.startswith("/"):
        decoded = "/" + decoded
    return "/" + "/".join(segment for segment in decoded.split("/") if segment)


def _canonical_scope(value: str, subscription_id: str) -> str:
    scope = _canonical_resource_id(value)
    lowered = scope.casefold()
    if lowered.startswith("/providers/microsoft.management/managementgroups/"):
        raise AzureProviderError("management-group scopes are not allowed")
    if not lowered.startswith(f"/subscriptions/{subscription_id}".casefold()):
        raise AzureProviderError("cross-subscription scopes are not allowed")
    if lowered == f"/subscriptions/{subscription_id}".casefold():
        raise AzureProviderError("subscription-wide role assignment scopes are not allowed")
    return scope


def _canonical_role_definition_id(value: str, subscription_id: str) -> str:
    raw = value.strip()
    if raw.startswith("/providers/"):
        raw = f"/subscriptions/{subscription_id}{raw}"
    canonical = _canonical_resource_id(raw).lower()
    if "/providers/microsoft.authorization/roledefinitions/" not in canonical:
        raise AzureProviderError("role definition id must point to Microsoft.Authorization/roleDefinitions")
    guid = canonical.rsplit("/", 1)[-1]
    if guid in {_OWNER_ROLE_GUID, _CONTRIBUTOR_ROLE_GUID}:
        raise AzureProviderError("Owner and Contributor role definitions are not allowed")
    return canonical


def _approval_fingerprint(scope: str, role_definition_id: str) -> str:
    return hashlib.sha256(_canonical_bytes({"scope": scope.casefold(), "role_definition_id": role_definition_id.casefold()})).hexdigest()


def _role_assignment_id(scope: str, principal_id: str, role_definition_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scope.casefold()}|{principal_id.casefold()}|{role_definition_id.casefold()}"))


def _diagnostics_map(action: BootstrapAction) -> dict[str, str]:
    return {entry.split("=", 1)[0]: entry.split("=", 1)[1] for entry in action.diagnostics if "=" in entry}


def _fic_name(subject: str) -> str:
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()[:24]


def _binding(values: Mapping[str, object]) -> Mapping[str, object]:
    safe_persisted_document(values)
    return values


class AzureArmRestProvider:
    def __init__(
        self,
        *,
        token_provider: Callable[[str], str],
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
        approved_role_definitions: Mapping[str, str] | None = None,
        checkpoint: Callable[[Mapping[str, object]], None] | None = None,
    ) -> None:
        self._token_provider = token_provider
        self._approved_role_definitions = dict(approved_role_definitions or {})
        self._http = httpx.Client(transport=transport, timeout=timeout, follow_redirects=False, trust_env=False)
        self._provider_state: dict[str, object] = {}
        self._checkpoint = checkpoint
        self._last_checkpoint: tuple[
            BootstrapReceipt,
            Mapping[str, object],
            bool,
        ] | None = None
        self._restored_checkpoint: tuple[BootstrapReceipt, bool] | None = None

    def close(self) -> None:
        self._http.close()

    def set_checkpoint(
        self,
        checkpoint: Callable[[Mapping[str, object]], None] | None,
    ) -> None:
        self._checkpoint = checkpoint

    def _response(self, method: str, url: str, *, scope: str, params: Mapping[str, object] | None = None, json_body: Mapping[str, object] | None = None) -> httpx.Response:
        token = self._token_provider(scope)
        if not isinstance(token, str) or not token:
            raise AzureProviderError("token provider returned an empty token")
        try:
            return self._http.request(method, url, params=params, json=json_body, headers={"Accept": "application/json", "Authorization": f"Bearer {token}"})
        except httpx.TimeoutException:
            raise AzureProviderError("Azure request timed out") from None
        except httpx.TransportError:
            raise AzureProviderError("Azure transport failed") from None

    def _request(self, method: str, url: str, *, scope: str, params: Mapping[str, object] | None = None, json_body: Mapping[str, object] | None = None) -> Mapping[str, object]:
        return _json_response(self._response(method, url, scope=scope, params=params, json_body=json_body))

    def inventory_identity(self) -> Mapping[str, object]:
        return {}

    def assess_bindings(self) -> Sequence[BindingAssessment]:
        return ()

    def live_binding_fingerprints(
        self,
        plan: BootstrapPlan,
    ) -> Sequence[FingerprintRecord]:
        planned = self._planned_bindings(plan)
        live_identity = self._resolve_identity_if_present(planned.identity)
        identity_document = self._live_identity_document(
            planned.identity,
            live_identity,
        )
        fingerprints = [
            _fingerprint("azure:identity", identity_document),
        ]
        identity_for_children = live_identity or planned.identity
        for subject in planned.subjects:
            credential = (
                self._get_fic(identity_for_children, subject)
                if live_identity is not None
                else None
            )
            fingerprints.append(
                _fingerprint(
                    f"azure:fic:{hashlib.sha256(subject.encode('utf-8')).hexdigest()[:16]}",
                    self._live_fic_document(subject, credential),
                )
            )
        principal_id = (
            live_identity.principal_id
            if live_identity is not None
            else planned.identity.principal_id
        )
        for role in planned.roles:
            assignment = None
            if principal_id is not None:
                assignment = self._get_role(
                    role.scope,
                    _role_assignment_id(
                        role.scope,
                        principal_id,
                        role.role_definition_id,
                    ),
                )
            role_key = canonical_sha256(
                {
                    "role_key": role.role_key,
                    "scope": role.scope.casefold(),
                    "role_definition_id": role.role_definition_id.casefold(),
                }
            )[:16]
            fingerprints.append(
                _fingerprint(
                    f"azure:role:{role_key}",
                    self._live_role_document(role, assignment),
                )
            )
        return tuple(sorted(fingerprints, key=lambda item: (item.label, item.sha256)))

    def plan_bindings(self, plan: BootstrapPlan) -> Sequence[BootstrapAction]:
        planned = self._planned_bindings(plan)
        identity = planned.identity
        identity_diagnostics = [
            f"subscription_id={identity.subscription_id}",
            f"name={identity.name}",
            f"adopted={'true' if identity.adopted else 'false'}",
        ]
        for key, value in (
            ("resource_id", identity.resource_id),
            ("client_id", identity.client_id),
            ("object_id", identity.object_id),
            ("principal_id", identity.principal_id),
            ("tenant_id", identity.tenant_id),
            ("location", identity.location),
        ):
            if value:
                identity_diagnostics.append(f"{key}={value}")
        actions: list[BootstrapAction] = [
            BootstrapAction(
                action_id="azure-identity",
                phase="azure",
                stage="planned",
                kind=(
                    "managed-identity"
                    if identity.kind == "user_assigned_managed_identity"
                    else "entra-application"
                ),
                diagnostics=tuple(identity_diagnostics),
            )
        ]
        for subject in planned.subjects:
            actions.append(BootstrapAction(action_id=f"azure-fic-{subject.rsplit(':',1)[-1]}", phase="azure", stage="planned", kind="federated-credential", diagnostics=(f"subject={subject}",)))
        for role in planned.roles:
            actions.append(BootstrapAction(action_id=f"azure-rbac-{role.role_key}", phase="azure", stage="planned", kind="role-assignment", diagnostics=(f"scope={role.scope}", f"role={role.role_key}", f"role_definition_id={role.role_definition_id}", f"approved_role_sha256={role.approval_fingerprint}")))
        return tuple(actions)

    def apply_bindings(self, plan: BootstrapPlan) -> BootstrapReceipt:
        planned = self._planned_bindings(plan)
        resumed = self._resume_checkpoint(plan)
        if resumed is not None:
            return resumed
        state = self._base_state(plan, planned)
        created = state["created_actions"]
        adopted = state["adopted_actions"]
        changed = state["changed_actions"]
        compensation = state["compensation_required_actions"]
        assert isinstance(created, list)
        assert isinstance(adopted, list)
        assert isinstance(changed, list)
        assert isinstance(compensation, list)
        self._publish_checkpoint(plan, state, complete=False)
        try:
            identity = self._resolve_live_or_planned_identity(
                planned.identity,
                state,
                created,
                adopted,
                changed,
                compensation,
                checkpoint=lambda: self._publish_checkpoint(
                    plan,
                    state,
                    complete=False,
                ),
            )
            for subject in planned.subjects:
                action = f"azure-fic-{subject.rsplit(':',1)[-1]}"
                fic_url, fic_scope = self._fic_url(identity, subject)
                preimage = self._get_fic(identity, subject)
                properties = (
                    preimage.get("properties", preimage)
                    if preimage is not None
                    else None
                )
                exact = bool(
                    isinstance(properties, Mapping)
                    and properties.get("issuer") == _ACTIONS_ISSUER
                    and properties.get("subject") == subject
                    and properties.get("audiences") == [_ACTIONS_AUDIENCE]
                )
                record = {
                    "action_id": action,
                    "resource_id": fic_url,
                    "scope": fic_scope,
                    "subject": subject,
                    "issuer": _ACTIONS_ISSUER,
                    "audience": _ACTIONS_AUDIENCE,
                    "preimage": preimage,
                    "disposition": "adopted" if exact else "ambiguous",
                }
                credentials = state["federated_credentials"]
                assert isinstance(credentials, list)
                credentials.append(_binding(record))
                if exact:
                    assert preimage is not None
                    record["resource_id"] = self._fic_resource_url(
                        identity,
                        subject,
                        preimage,
                    )
                    adopted.append(action)
                    continue
                self._append_attempt(
                    state,
                    action_id=action,
                    kind="fic",
                    target_resource_id=fic_url,
                    target={"subject": subject},
                )
                compensation.append(action)
                self._publish_checkpoint(plan, state, complete=False)
                disposition, live_fic = self._ensure_fic(
                    identity,
                    subject,
                    preimage,
                    after_mutation=lambda: self._publish_acknowledged_checkpoint(
                        plan, state, action
                    ),
                )
                if preimage is None and disposition == "changed":
                    self._mark_attempt_unsafe_without_preimage(state, action)
                    self._publish_checkpoint(plan, state, complete=False)
                    raise AzureProviderError(
                        "federated credential update without a preimage is not safely recoverable"
                    )
                record["resource_id"] = self._fic_resource_url(
                    identity,
                    subject,
                    live_fic,
                )
                record["disposition"] = disposition
                if disposition == "created":
                    created.append(action)
                elif disposition == "changed":
                    changed.append(action)
                else:
                    adopted.append(action)
                    compensation.remove(action)
                self._mark_attempt(state, action, disposition)
                self._publish_checkpoint(plan, state, complete=False)
            for role in planned.roles:
                self._assert_role_approval(role)
                principal_id = _text(
                    identity.principal_id,
                    field="identity.principal_id",
                )
                assignment_id = _role_assignment_id(
                    role.scope,
                    principal_id,
                    role.role_definition_id,
                )
                action = f"azure-rbac-{role.role_key}-{assignment_id}"
                resource_id = (
                    f"{role.scope}/{_ROLE_ASSIGNMENTS_SEGMENT}/{assignment_id}"
                )
                preimage = self._get_role(role.scope, assignment_id)
                exact = False
                if preimage is not None:
                    self._verify_role_properties(
                        preimage,
                        principal_id,
                        role.role_definition_id,
                        role.scope,
                        identity.subscription_id or role.scope.split("/")[2],
                        require_defaults=False,
                    )
                    properties = preimage["properties"]
                    assert isinstance(properties, Mapping)
                    exact = not any(
                        properties.get(key) not in (None, "")
                        for key in (
                            "condition",
                            "conditionVersion",
                            "delegatedManagedIdentityResourceId",
                        )
                    )
                record = {
                    "action_id": action,
                    "resource_id": resource_id,
                    "assignment_id": assignment_id,
                    "role_key": role.role_key,
                    "scope": role.scope,
                    "role_definition_id": role.role_definition_id,
                    "principal_id": principal_id,
                    "preimage": preimage,
                    "disposition": "adopted" if exact else "ambiguous",
                }
                assignments = state["role_assignments"]
                assert isinstance(assignments, list)
                assignments.append(_binding(record))
                if exact:
                    adopted.append(action)
                    continue
                self._append_attempt(
                    state,
                    action_id=action,
                    kind="role",
                    target_resource_id=resource_id,
                    target={
                        "assignment_id": assignment_id,
                        "scope": role.scope,
                    },
                )
                compensation.append(action)
                self._publish_checkpoint(plan, state, complete=False)
                disposition = self._ensure_role(
                    identity,
                    role,
                    assignment_id,
                    existing=preimage,
                    after_mutation=lambda: self._publish_acknowledged_checkpoint(
                        plan, state, action
                    ),
                )
                if preimage is None and disposition == "changed":
                    self._mark_attempt_unsafe_without_preimage(state, action)
                    self._publish_checkpoint(plan, state, complete=False)
                    raise AzureProviderError(
                        "role assignment update without a preimage is not safely recoverable"
                    )
                record["disposition"] = disposition
                if disposition == "created":
                    created.append(action)
                elif disposition == "changed":
                    changed.append(action)
                else:
                    adopted.append(action)
                    compensation.remove(action)
                self._mark_attempt(state, action, disposition)
                self._publish_checkpoint(plan, state, complete=False)
            receipt = BootstrapReceipt.create(
                operation_id=plan.operation_id,
                runtime_repository=plan.runtime_repository,
                runtime_commit=plan.runtime_commit,
                repository_identity=plan.repository_identity,
                plan_hash=plan.plan_hash,
                before_fingerprints=(
                    _fingerprint(
                        "azure-plan",
                        {"planned": [role.__dict__ for role in planned.roles]},
                    ),
                ),
                after_fingerprints=(
                    _fingerprint(
                        "azure-live",
                        {
                            "principal_id": identity.principal_id,
                            "client_id": identity.client_id,
                        },
                    ),
                ),
                created_actions=tuple(created),
                adopted_actions=tuple(adopted),
                changed_actions=tuple(changed),
                compensation_required_actions=tuple(compensation),
            )
            state["status"] = "applied"
            self._publish_checkpoint(
                plan,
                state,
                complete=True,
                receipt=receipt,
            )
            return receipt
        except Exception as exc:
            checkpoint = self._last_checkpoint
            if checkpoint is None:
                raise
            receipt, provider_state, _ = checkpoint
            if not (
                receipt.created_actions
                or receipt.changed_actions
                or receipt.compensation_required_actions
            ):
                raise
            raise AzureProviderApplyError(
                "Azure binding apply failed",
                compensation_receipt=receipt,
                provider_state=provider_state,
            ) from None

    def export_provider_state(self, receipt: BootstrapReceipt) -> Mapping[str, object]:
        state = dict(self._validated_state(receipt))
        payload = json.loads(
            json.dumps(
                {**state, "state_hash": canonical_sha256(state)},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            )
        )
        if not isinstance(payload, Mapping):
            raise AzureProviderError("provider state is not an object")
        safe_persisted_document(payload)
        return payload

    def restore_provider_state(self, mapping: Mapping[str, object]) -> None:
        if mapping.get("checkpoint") is True:
            if mapping.get("version") != _CHECKPOINT_VERSION:
                raise AzureProviderError("provider checkpoint version mismatch")
            receipt_raw = mapping.get("receipt")
            provider_state = mapping.get("provider_state")
            if not isinstance(receipt_raw, Mapping) or not isinstance(
                provider_state,
                Mapping,
            ):
                raise AzureProviderError("provider checkpoint is incomplete")
            receipt = BootstrapReceipt.model_validate(receipt_raw)
            self._restore_exported_state(provider_state)
            self._validated_state(receipt)
            complete = mapping.get("complete")
            if not isinstance(complete, bool):
                raise AzureProviderError(
                    "provider checkpoint completion state is invalid"
                )
            self._restored_checkpoint = (receipt, complete)
            self._last_checkpoint = (receipt, dict(mapping), complete)
            return
        self._restore_exported_state(mapping)

    def _restore_exported_state(self, mapping: Mapping[str, object]) -> None:
        state_hash = _text(mapping.get("state_hash"), field="state_hash")
        state = {k: v for k, v in mapping.items() if k != "state_hash"}
        safe_persisted_document(state)
        if canonical_sha256(state) != state_hash:
            raise AzureProviderError("provider state hash mismatch")
        self._provider_state = dict(state)
        self._reconcile_ambiguous_attempts()

    def _publish_checkpoint(
        self,
        plan: BootstrapPlan,
        state: dict[str, object],
        *,
        complete: bool,
        receipt: BootstrapReceipt | None = None,
    ) -> BootstrapReceipt:
        checkpoint_receipt = receipt or self._checkpoint_receipt(plan, state)
        self._bind_receipt(state, checkpoint_receipt)
        self._provider_state = state
        provider_state = self.export_provider_state(checkpoint_receipt)
        payload = {
            "version": _CHECKPOINT_VERSION,
            "checkpoint": True,
            "complete": complete,
            "receipt": checkpoint_receipt.model_dump(mode="json"),
            "provider_state": provider_state,
        }
        safe_persisted_document(payload)
        self._last_checkpoint = (
            checkpoint_receipt,
            payload,
            complete,
        )
        if self._checkpoint is not None:
            self._checkpoint(payload)
        return checkpoint_receipt

    def _checkpoint_receipt(
        self,
        plan: BootstrapPlan,
        state: Mapping[str, object],
    ) -> BootstrapReceipt:
        identity = state.get("identity")
        identity_mapping = identity if isinstance(identity, Mapping) else {}
        return BootstrapReceipt.create(
            operation_id=plan.operation_id,
            runtime_repository=plan.runtime_repository,
            runtime_commit=plan.runtime_commit,
            repository_identity=plan.repository_identity,
            plan_hash=plan.plan_hash,
            before_fingerprints=(
                _fingerprint(
                    "azure-plan",
                    {"approved_roles": state.get("approved_roles", [])},
                ),
            ),
            after_fingerprints=(
                _fingerprint(
                    "azure-live",
                    {
                        "principal_id": identity_mapping.get("principal_id"),
                        "client_id": identity_mapping.get("client_id"),
                        "status": state.get("status"),
                    },
                ),
            ),
            created_actions=tuple(state.get("created_actions", ())),
            adopted_actions=tuple(state.get("adopted_actions", ())),
            changed_actions=tuple(state.get("changed_actions", ())),
            compensation_required_actions=tuple(
                state.get("compensation_required_actions", ())
            ),
            error_info=RedactedStatusInfo(
                code="apply-in-flight",
                summary="Azure binding apply is in flight",
            ),
        )

    def _resume_checkpoint(
        self,
        plan: BootstrapPlan,
    ) -> BootstrapReceipt | None:
        restored = self._restored_checkpoint
        if restored is None:
            return None
        receipt, complete = restored
        if (
            receipt.operation_id != plan.operation_id
            or receipt.repository_identity != plan.repository_identity
            or receipt.runtime_commit != plan.runtime_commit
            or receipt.plan_hash != plan.plan_hash
        ):
            raise AzureProviderError(
                "provider checkpoint does not match the active Azure plan"
            )
        self._restored_checkpoint = None
        if complete:
            self.verify_bindings(receipt)
            return receipt
        if (
            receipt.created_actions
            or receipt.changed_actions
            or receipt.compensation_required_actions
        ):
            self.rollback_bindings(receipt)
            if not self.verify_rollback(receipt):
                raise AzureProviderError(
                    "interrupted Azure apply compensation verification failed"
                )
        self._provider_state = {}
        self._last_checkpoint = None
        return None

    def verify_bindings(self, receipt: BootstrapReceipt) -> bool:
        if receipt.error_info is not None:
            raise AzureProviderError("cannot verify failed Azure receipt")
        state = self._validated_state(receipt)
        planned = self._state_planned_bindings(state)
        live = self._resolve_identity(planned.identity)
        self._assert_expected_identity(planned.identity, live)
        for fic in state["federated_credentials"]:
            self._verify_fic_binding(live, fic)
        for role in state["role_assignments"]:
            self._verify_role_binding(live, role)
        return True

    def rollback_bindings(self, receipt: BootstrapReceipt) -> None:
        state = self._validated_state(receipt)
        self._reconcile_ambiguous_attempts()
        identity = self._state_identity(state)
        for action_id in reversed(state["compensation_required_actions"]):
            self._rollback_action(state, identity, _text(action_id, field="action_id"))
        state["status"] = "rolled_back"
        self._provider_state = state

    def verify_rollback(self, receipt: BootstrapReceipt) -> bool:
        state = self._validated_state(receipt)
        self._reconcile_ambiguous_attempts()
        identity = self._state_identity(state)
        live = self._resolve_identity_if_present(identity)
        for role in state["role_assignments"]:
            if role["disposition"] == "created":
                if self._get_role_by_resource_id(_text(role["resource_id"], field="resource_id")) is not None:
                    raise AzureProviderError("created role assignment still exists after rollback")
            else:
                self._verify_role_restored_exact(role)
        for fic in state["federated_credentials"]:
            current = self._get_fic(identity, _text(fic["subject"], field="subject"))
            if fic["disposition"] == "created":
                if current is not None:
                    raise AzureProviderError("created federated credential still exists after rollback")
            else:
                if current is None or canonical_sha256(current) != canonical_sha256(_binding(fic["preimage"])):
                    raise AzureProviderError("adopted federated credential drifted after rollback")
        if state["identity"]["disposition"] == "created":
            if live is not None:
                raise AzureProviderError("created managed identity still exists after rollback")
        else:
            if live is None:
                raise AzureProviderError("adopted identity missing after rollback")
            self._assert_expected_identity(identity, live)
            if state["identity"]["preimage"] is not None and canonical_sha256(self._uami_live_document(live)) != canonical_sha256(state["identity"]["preimage"]):
                raise AzureProviderError("managed identity drifted after rollback")
        return True

    def _base_state(self, plan: BootstrapPlan, planned: PlannedBindingSet) -> dict[str, object]:
        return {
            "version": 4,
            "operation_id": plan.operation_id,
            "runtime_repository": plan.runtime_repository,
            "runtime_commit": plan.runtime_commit,
            "repository_identity": plan.repository_identity,
            "plan_hash": plan.plan_hash,
            "identity": self._identity_state(
                planned.identity,
                planned=planned.identity,
                disposition="planned",
                preimage=None,
            ),
            "subjects": planned.subjects,
            "role_assignments": [],
            "federated_credentials": [],
            "approved_roles": [
                {
                    "role_key": role.role_key,
                    "scope": role.scope,
                    "role_definition_id": role.role_definition_id,
                    "approval_fingerprint": role.approval_fingerprint,
                }
                for role in planned.roles
            ],
            "attempts": [],
            "created_actions": [],
            "adopted_actions": [],
            "changed_actions": [],
            "compensation_required_actions": [],
            "status": "applying",
        }

    def _identity_state(self, identity: AzureIdentityReference, *, planned: AzureIdentityReference, disposition: str, preimage: Mapping[str, object] | None) -> Mapping[str, object]:
        return _binding({"kind": identity.kind, "resource_id": identity.resource_id, "object_id": identity.object_id, "client_id": identity.client_id or planned.client_id, "principal_id": identity.principal_id or planned.principal_id, "tenant_id": identity.tenant_id or planned.tenant_id, "subscription_id": identity.subscription_id or planned.subscription_id, "name": identity.name, "location": identity.location or planned.location, "adopted": planned.adopted, "disposition": disposition, "preimage": preimage})

    def _append_attempt(self, state: dict[str, object], *, action_id: str, kind: str, target_resource_id: str, target: Mapping[str, object]) -> None:
        attempts = state["attempts"]
        assert isinstance(attempts, list)
        attempts.append(
            {
                "action_id": action_id,
                "kind": kind,
                "target_resource_id": target_resource_id,
                "target": dict(target),
                "stage": "intent",
                "disposition": "ambiguous",
            }
        )

    def _mark_attempt(self, state: dict[str, object], action_id: str, disposition: str) -> None:
        for attempt in state["attempts"]:
            if attempt["action_id"] == action_id:
                attempt["disposition"] = disposition
                attempt["stage"] = "resolved"

    def _publish_acknowledged_checkpoint(
        self,
        plan: BootstrapPlan,
        state: dict[str, object],
        action_id: str,
    ) -> None:
        for attempt in state["attempts"]:
            if attempt["action_id"] == action_id:
                attempt["stage"] = "acknowledged"
                self._publish_checkpoint(plan, state, complete=False)
                return
        raise AzureProviderError("Azure mutation attempt is missing")

    def _mark_attempt_unsafe_without_preimage(
        self,
        state: dict[str, object],
        action_id: str,
    ) -> None:
        for attempt in state["attempts"]:
            if attempt["action_id"] == action_id:
                target = attempt["target"]
                assert isinstance(target, dict)
                target["unsafe_without_preimage"] = True
                return
        raise AzureProviderError("Azure mutation attempt is missing")

    def _bind_receipt(self, state: dict[str, object], receipt: BootstrapReceipt) -> None:
        state["receipt_hash"] = receipt.receipt_hash
        state["receipt_plan_hash"] = receipt.plan_hash

    def _validated_state(self, receipt: BootstrapReceipt) -> dict[str, object]:
        if not self._provider_state or self._provider_state.get("receipt_hash") != receipt.receipt_hash:
            raise AzureProviderError("provider state receipt binding mismatch")
        return self._provider_state

    def _state_identity(self, state: Mapping[str, object]) -> AzureIdentityReference:
        identity = state["identity"]
        assert isinstance(identity, Mapping)
        return AzureIdentityReference(kind=_text(identity.get("kind"), field="identity.kind"), client_id=_optional_text(identity.get("client_id")), resource_id=_optional_text(identity.get("resource_id")), object_id=_optional_text(identity.get("object_id")), principal_id=_optional_text(identity.get("principal_id")), tenant_id=_optional_text(identity.get("tenant_id")), subscription_id=_optional_text(identity.get("subscription_id")), name=_text(identity.get("name"), field="identity.name"), adopted=bool(identity.get("adopted")), location=_optional_text(identity.get("location")))

    def _state_planned_bindings(self, state: Mapping[str, object]) -> PlannedBindingSet:
        roles = [PlannedRoleAssignment(role_key=_text(item["role_key"], field="role_key"), scope=_text(item["scope"], field="scope"), role_definition_id=_text(item["role_definition_id"], field="role_definition_id"), approval_fingerprint=_text(item["approval_fingerprint"], field="approval_fingerprint")) for item in state["approved_roles"]]
        return PlannedBindingSet(identity=self._state_identity(state), roles=tuple(roles), subjects=tuple(state["subjects"]))  # type: ignore[arg-type]

    def _resolve_live_or_planned_identity(
        self,
        identity: AzureIdentityReference,
        state: dict[str, object],
        created: list[str],
        adopted: list[str],
        changed: list[str],
        compensation: list[str],
        *,
        checkpoint: Callable[[], None],
    ) -> AzureIdentityReference:
        if identity.kind != "user_assigned_managed_identity":
            live = self._resolve_identity(identity)
            state["identity"] = self._identity_state(live, planned=identity, disposition="adopted", preimage=None)
            return live
        existing = self._get_uami_if_exists(identity.resource_id or "")
        if identity.adopted:
            if existing is None:
                raise AzureProviderError("live identity missing")
            self._assert_expected_identity(identity, existing)
            state["identity"] = self._identity_state(existing, planned=identity, disposition="adopted", preimage=self._uami_live_document(existing))
            return existing
        if existing is None:
            self._append_attempt(state, action_id="azure-uami-create", kind="uami", target_resource_id=_text(identity.resource_id, field="identity.resource_id"), target={})
            compensation.append("azure-uami-create")
            state["identity"] = self._identity_state(
                identity,
                planned=identity,
                disposition="ambiguous",
                preimage=None,
            )
            checkpoint()
            live, disposition = self._ensure_uami(
                identity,
                after_mutation=lambda: self._mark_identity_acknowledged(
                    state,
                    checkpoint,
                ),
            )
            if disposition == "created":
                created.append("azure-uami-create")
            elif disposition == "changed":
                self._mark_attempt_unsafe_without_preimage(
                    state,
                    "azure-uami-create",
                )
                state["identity"] = self._identity_state(
                    live,
                    planned=identity,
                    disposition="ambiguous",
                    preimage=None,
                )
                checkpoint()
                raise AzureProviderError("uami update without preimage is not allowed")
            state["identity"] = self._identity_state(live, planned=identity, disposition=disposition, preimage=None)
            self._mark_attempt(state, "azure-uami-create", disposition)
            checkpoint()
            return live
        self._assert_expected_identity(identity, existing, allow_fill_missing=True)
        state["identity"] = self._identity_state(existing, planned=identity, disposition="adopted", preimage=self._uami_live_document(existing))
        adopted.append("azure-uami-create")
        return existing

    def _mark_identity_acknowledged(
        self,
        state: dict[str, object],
        checkpoint: Callable[[], None],
    ) -> None:
        for attempt in state["attempts"]:
            if attempt["action_id"] == "azure-uami-create":
                attempt["stage"] = "acknowledged"
                checkpoint()
                return
        raise AzureProviderError("Azure identity mutation attempt is missing")

    def _uami_live_document(self, identity: AzureIdentityReference) -> Mapping[str, object]:
        return _binding({"id": identity.resource_id, "name": identity.name, "location": identity.location, "properties": {"clientId": identity.client_id, "principalId": identity.principal_id, "tenantId": identity.tenant_id}})

    def _live_identity_document(
        self,
        planned: AzureIdentityReference,
        live: AzureIdentityReference | None,
    ) -> Mapping[str, object]:
        document = {
            "kind": planned.kind,
            "exists": live is not None,
            "resource_id": (
                _canonical_resource_id(live.resource_id).casefold()
                if live is not None and live.resource_id is not None
                else None
            ),
            "object_id": live.object_id.casefold() if live is not None and live.object_id else None,
            "client_id": live.client_id.casefold() if live is not None and live.client_id else None,
            "principal_id": live.principal_id.casefold() if live is not None and live.principal_id else None,
            "tenant_id": live.tenant_id.casefold() if live is not None and live.tenant_id else None,
            "subscription_id": (
                live.subscription_id.casefold()
                if live is not None and live.subscription_id
                else None
            ),
            "name": live.name if live is not None else None,
            "location": live.location.casefold() if live is not None and live.location else None,
        }
        return _binding(document)

    def _live_fic_document(
        self,
        subject: str,
        credential: Mapping[str, object] | None,
    ) -> Mapping[str, object]:
        if credential is None:
            return _binding(
                {
                    "exists": False,
                    "subject_sha256": canonical_sha256(subject),
                }
            )
        properties = credential.get("properties", credential)
        if not isinstance(properties, Mapping):
            raise AzureProviderError(
                "federated credential properties must be an object"
            )
        return _binding(
            {
                "exists": True,
                "id": _optional_text(credential.get("id")),
                "name": _optional_text(credential.get("name")),
                "issuer": _optional_text(properties.get("issuer")),
                "subject": _optional_text(properties.get("subject")),
                "audiences": properties.get("audiences"),
            }
        )

    def _live_role_document(
        self,
        planned: PlannedRoleAssignment,
        assignment: Mapping[str, object] | None,
    ) -> Mapping[str, object]:
        if assignment is None:
            return _binding(
                {
                    "exists": False,
                    "scope": planned.scope.casefold(),
                    "role_definition_id": planned.role_definition_id.casefold(),
                    "approval_fingerprint": planned.approval_fingerprint,
                }
            )
        properties = assignment.get("properties")
        if not isinstance(properties, Mapping):
            raise AzureProviderError("role assignment properties must be an object")
        resource_id = _text(assignment.get("id"), field="roleAssignment.id")
        return _binding(
            {
                "exists": True,
                "id": _canonical_resource_id(resource_id).casefold(),
                "principal_id": _text(
                    properties.get("principalId"),
                    field="roleAssignment.principalId",
                ).casefold(),
                "principal_type": _optional_text(properties.get("principalType")),
                "role_definition_id": _canonical_role_definition_id(
                    _text(
                        properties.get("roleDefinitionId"),
                        field="roleAssignment.roleDefinitionId",
                    ),
                    planned.scope.split("/")[2],
                ),
                "condition": properties.get("condition"),
                "condition_version": properties.get("conditionVersion"),
                "delegated_managed_identity_resource_id": properties.get(
                    "delegatedManagedIdentityResourceId"
                ),
            }
        )

    def _resolve_identity(self, identity: AzureIdentityReference) -> AzureIdentityReference:
        if identity.kind == "user_assigned_managed_identity":
            live = self._get_uami_if_exists(identity.resource_id or "")
            if live is None:
                raise AzureProviderError("live identity missing")
            return live
        app = self._request("GET", f"{_GRAPH_APPLICATIONS}/{identity.object_id}", scope=_GRAPH_SCOPE)
        app_id = _text(app.get("appId"), field="application.appId")
        sp = self._request("GET", _GRAPH_SERVICE_PRINCIPALS, scope=_GRAPH_SCOPE, params={"$filter": f"appId eq '{app_id}'"})
        values = sp.get("value")
        if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], Mapping):
            raise AzureProviderError("corresponding service principal could not be resolved uniquely")
        return AzureIdentityReference(kind="entra_application", client_id=app_id, resource_id=None, object_id=_text(app.get("id"), field="application.id"), principal_id=_text(values[0].get("id"), field="servicePrincipal.id"), tenant_id=_text(values[0].get("appOwnerOrganizationId"), field="servicePrincipal.appOwnerOrganizationId"), subscription_id=identity.subscription_id, name=_text(app.get("displayName"), field="application.displayName"), adopted=True)

    def _resolve_identity_if_present(self, identity: AzureIdentityReference) -> AzureIdentityReference | None:
        if identity.kind == "user_assigned_managed_identity":
            return self._get_uami_if_exists(identity.resource_id or "")
        return self._resolve_identity(identity)

    def _assert_expected_identity(self, planned: AzureIdentityReference, live: AzureIdentityReference, *, allow_fill_missing: bool = False) -> None:
        if planned.resource_id and live.resource_id and _canonical_resource_id(planned.resource_id).casefold() != _canonical_resource_id(live.resource_id).casefold():
            raise AzureProviderError("live identity resource_id did not match planned identity")
        for field_name in ("client_id", "principal_id", "tenant_id"):
            expected = getattr(planned, field_name)
            actual = getattr(live, field_name)
            if expected is not None and actual != expected:
                raise AzureProviderError(f"live identity {field_name} did not match planned identity")
            if expected is None and actual is None and not allow_fill_missing:
                continue

    def _get_uami_if_exists(self, resource_id: str) -> AzureIdentityReference | None:
        response = self._response("GET", f"https://management.azure.com{_canonical_resource_id(resource_id)}", scope=_ARM_SCOPE, params={"api-version": _MANAGED_IDENTITY_API_VERSION})
        if response.status_code == 404:
            return None
        body = _json_response(response)
        props = body["properties"]
        assert isinstance(props, Mapping)
        rid = _canonical_resource_id(_text(body.get("id"), field="identity.id"))
        return AzureIdentityReference(kind="user_assigned_managed_identity", client_id=_text(props.get("clientId"), field="identity.clientId"), resource_id=rid, object_id=None, principal_id=_text(props.get("principalId"), field="identity.principalId"), tenant_id=_text(props.get("tenantId"), field="identity.tenantId"), subscription_id=rid.split("/")[2], name=_text(body.get("name"), field="identity.name"), adopted=True, location=_optional_text(body.get("location")))

    def _ensure_uami(
        self,
        identity: AzureIdentityReference,
        *,
        after_mutation: Callable[[], None] | None = None,
    ) -> tuple[AzureIdentityReference, str]:
        response = self._response("PUT", f"https://management.azure.com{identity.resource_id}", scope=_ARM_SCOPE, params={"api-version": _MANAGED_IDENTITY_API_VERSION}, json_body={"location": identity.location})
        if response.status_code not in {200, 201}:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")
        if after_mutation is not None:
            after_mutation()
        live = self._resolve_identity(identity)
        return live, ("created" if response.status_code == 201 else "changed")

    def _fic_url(self, identity: AzureIdentityReference, subject: str) -> tuple[str, str]:
        if identity.kind == "user_assigned_managed_identity":
            return (f"https://management.azure.com{identity.resource_id}/federatedIdentityCredentials/{_fic_name(subject)}", _ARM_SCOPE)
        object_id = _text(identity.object_id, field="identity.object_id")
        return (f"{_GRAPH_APPLICATIONS}/{object_id}/federatedIdentityCredentials", _GRAPH_SCOPE)

    def _get_fic(self, identity: AzureIdentityReference, subject: str) -> Mapping[str, object] | None:
        url, scope = self._fic_url(identity, subject)
        if scope == _ARM_SCOPE:
            response = self._response("GET", url, scope=scope, params={"api-version": _FIC_API_VERSION})
            if response.status_code == 404:
                return None
            return _json_response(response)
        payload = self._request("GET", url, scope=scope)
        if payload.get("@odata.nextLink") is not None:
            raise AzureProviderError(
                "paginated federated credential inventory is not supported"
            )
        values = payload.get("value")
        if not isinstance(values, list):
            raise AzureProviderError(
                "federated credential inventory must contain a value list"
            )
        credentials: list[Mapping[str, object]] = []
        for item in values:
            if not isinstance(item, Mapping):
                raise AzureProviderError(
                    "federated credential inventory contains a non-object item"
                )
            credentials.append(item)
        subject_matches = [
            item for item in credentials if item.get("subject") == subject
        ]
        exact_matches = [
            item
            for item in subject_matches
            if item.get("issuer") == _ACTIONS_ISSUER
            and item.get("audiences") == [_ACTIONS_AUDIENCE]
        ]
        if len(exact_matches) > 1 or len(subject_matches) > 1:
            raise AzureProviderError(
                "federated credential subject resolved ambiguously"
            )
        expected_name = _fic_name(subject)
        name_conflicts = [
            item
            for item in credentials
            if item.get("name") == expected_name and item not in exact_matches
        ]
        if name_conflicts:
            raise AzureProviderError(
                "deterministic federated credential name is already in use"
            )
        if exact_matches:
            return exact_matches[0]
        if subject_matches:
            raise AzureProviderError(
                "existing federated credential subject has unexpected issuer or audience"
            )
        return None

    def _fic_resource_url(
        self,
        identity: AzureIdentityReference,
        subject: str,
        credential: Mapping[str, object],
    ) -> str:
        url, scope = self._fic_url(identity, subject)
        if scope == _ARM_SCOPE:
            return url
        credential_id = _text(
            credential.get("id"),
            field="federatedCredential.id",
        )
        return f"{url}/{credential_id}"

    def _ensure_fic(
        self,
        identity: AzureIdentityReference,
        subject: str,
        existing: Mapping[str, object] | None,
        *,
        after_mutation: Callable[[], None] | None = None,
    ) -> tuple[str, Mapping[str, object]]:
        if existing is not None:
            props = existing.get("properties", existing)
            assert isinstance(props, Mapping)
            if (
                props.get("issuer") == _ACTIONS_ISSUER
                and props.get("subject") == subject
                and props.get("audiences") == [_ACTIONS_AUDIENCE]
            ):
                return "adopted", existing
            if identity.kind != "user_assigned_managed_identity":
                raise AzureProviderError(
                    "existing federated credential does not match the approved contract"
                )
        url, scope = self._fic_url(identity, subject)
        response = self._response("PUT" if scope == _ARM_SCOPE else "POST", url if scope == _ARM_SCOPE else f"{_GRAPH_APPLICATIONS}/{identity.object_id}/federatedIdentityCredentials", scope=scope, params={"api-version": _FIC_API_VERSION} if scope == _ARM_SCOPE else None, json_body={"properties": {"issuer": _ACTIONS_ISSUER, "subject": subject, "audiences": [_ACTIONS_AUDIENCE]}} if scope == _ARM_SCOPE else {"name": _fic_name(subject), "issuer": _ACTIONS_ISSUER, "subject": subject, "audiences": [_ACTIONS_AUDIENCE]})
        if response.status_code not in {200, 201}:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")
        if after_mutation is not None:
            after_mutation()
        return (
            "created" if response.status_code == 201 else "changed",
            _json_response(response),
        )

    def _get_role(self, scope: str, assignment_id: str) -> Mapping[str, object] | None:
        response = self._response("GET", f"https://management.azure.com{scope}/{_ROLE_ASSIGNMENTS_SEGMENT}/{assignment_id}", scope=_ARM_SCOPE, params={"api-version": _AUTHZ_API_VERSION})
        if response.status_code == 404:
            return None
        return _json_response(response)

    def _get_role_by_resource_id(self, resource_id: str) -> Mapping[str, object] | None:
        response = self._response("GET", f"https://management.azure.com{resource_id}", scope=_ARM_SCOPE, params={"api-version": _AUTHZ_API_VERSION})
        if response.status_code == 404:
            return None
        return _json_response(response)

    def _ensure_role(
        self,
        identity: AzureIdentityReference,
        role: PlannedRoleAssignment,
        assignment_id: str,
        *,
        existing: Mapping[str, object] | None,
        after_mutation: Callable[[], None] | None = None,
    ) -> str:
        principal_id = _text(identity.principal_id, field="identity.principal_id")
        if existing is not None:
            self._verify_role_properties(existing, principal_id, role.role_definition_id, role.scope, identity.subscription_id or role.scope.split("/")[2], require_defaults=False)
            props = existing["properties"]
            assert isinstance(props, Mapping)
            if any(props.get(key) not in (None, "") for key in ("condition", "conditionVersion", "delegatedManagedIdentityResourceId")):
                response = self._response("PUT", f"https://management.azure.com{role.scope}/{_ROLE_ASSIGNMENTS_SEGMENT}/{assignment_id}", scope=_ARM_SCOPE, params={"api-version": _AUTHZ_API_VERSION}, json_body={"properties": {"principalId": principal_id, "roleDefinitionId": self._approved_role_definitions[role.role_key], "principalType": "ServicePrincipal"}})
                if response.status_code not in {200, 201}:
                    raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")
                if after_mutation is not None:
                    after_mutation()
                return "changed"
            return "adopted"
        raw_role_definition_id = self._approved_role_definitions[role.role_key]
        response = self._response("PUT", f"https://management.azure.com{role.scope}/{_ROLE_ASSIGNMENTS_SEGMENT}/{assignment_id}", scope=_ARM_SCOPE, params={"api-version": _AUTHZ_API_VERSION}, json_body={"properties": {"principalId": principal_id, "roleDefinitionId": raw_role_definition_id, "principalType": "ServicePrincipal"}})
        if response.status_code == 403:
            raise AzureProviderError("executor is missing Microsoft.Authorization/roleAssignments/write")
        if response.status_code not in {200, 201}:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")
        if after_mutation is not None:
            after_mutation()
        return "created" if response.status_code == 201 else "changed"

    def _verify_role_properties(self, body: Mapping[str, object], principal_id: str, role_definition_id: str, scope: str, subscription_id: str, *, require_defaults: bool) -> None:
        props = body["properties"]
        assert isinstance(props, Mapping)
        if _text(props.get("principalId"), field="principalId").casefold() != principal_id.casefold():
            raise AzureProviderError("role assignment verification principalId mismatch")
        if _canonical_role_definition_id(_text(props.get("roleDefinitionId"), field="roleDefinitionId"), subscription_id) != role_definition_id:
            raise AzureProviderError("role assignment verification roleDefinitionId mismatch")
        scope_id = _canonical_resource_id(_text(body.get("id"), field="id").split(f"/{_ROLE_ASSIGNMENTS_SEGMENT}/", 1)[0]).casefold()
        if scope_id != _canonical_resource_id(scope).casefold():
            raise AzureProviderError("role assignment verification scope mismatch")
        if require_defaults and any(props.get(key) not in (None, "") for key in ("condition", "conditionVersion", "delegatedManagedIdentityResourceId")):
            raise AzureProviderError("role assignment verification unexpected conditional properties")
        if require_defaults and props.get("principalType") not in (
            None,
            "",
            "ServicePrincipal",
        ):
            raise AzureProviderError(
                "role assignment verification principalType mismatch"
            )

    def _verify_fic_binding(self, identity: AzureIdentityReference, fic: Mapping[str, object]) -> None:
        subject = _text(fic.get("subject"), field="subject")
        current = self._get_fic(identity, subject)
        if current is None:
            raise AzureProviderError("federated credential missing")
        expected_resource = _text(
            fic.get("resource_id"),
            field="resource_id",
        )
        current_resource = self._fic_resource_url(
            identity,
            subject,
            current,
        )
        if current_resource.casefold() != expected_resource.casefold():
            raise AzureProviderError(
                "federated credential identity drifted"
            )
        props = current.get("properties", current)
        assert isinstance(props, Mapping)
        if props.get("issuer") != fic["issuer"] or props.get("subject") != fic["subject"] or props.get("audiences") != [fic["audience"]]:
            raise AzureProviderError("federated credential verification drifted from exact claims")

    def _verify_role_binding(self, identity: AzureIdentityReference, role: Mapping[str, object]) -> None:
        live = self._get_role_by_resource_id(_text(role["resource_id"], field="resource_id"))
        if live is None:
            raise AzureProviderError("role assignment missing")
        self._verify_role_properties(live, _text(role["principal_id"], field="principal_id"), _text(role["role_definition_id"], field="role_definition_id"), _text(role["scope"], field="scope"), identity.subscription_id or _text(role["scope"], field="scope").split("/")[2], require_defaults=True)

    def _verify_role_restored_exact(self, role: Mapping[str, object]) -> None:
        preimage = role.get("preimage")
        if not isinstance(preimage, Mapping):
            raise AzureProviderError("adopted role assignment missing preimage")
        live = self._get_role_by_resource_id(_text(role["resource_id"], field="resource_id"))
        if (
            live is None
            or canonical_sha256(self._role_contract_document(live))
            != canonical_sha256(self._role_contract_document(preimage))
        ):
            raise AzureProviderError("adopted role assignment drifted after rollback")

    def _delete_role(self, resource_id: str) -> None:
        response = self._response("DELETE", f"https://management.azure.com{resource_id}", scope=_ARM_SCOPE, params={"api-version": _AUTHZ_API_VERSION})
        if response.status_code not in {200, 202, 204, 404}:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")

    def _restore_role(self, preimage: Mapping[str, object]) -> None:
        resource_id = _canonical_resource_id(_text(preimage.get("id"), field="id"))
        props = preimage["properties"]
        assert isinstance(props, Mapping)
        writable = {
            key: props[key]
            for key in _ROLE_WRITABLE_PROPERTIES
            if key in props
        }
        response = self._response("PUT", f"https://management.azure.com{resource_id}", scope=_ARM_SCOPE, params={"api-version": _AUTHZ_API_VERSION}, json_body={"properties": writable})
        if response.status_code not in {200, 201}:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")

    def _role_contract_document(
        self,
        role: Mapping[str, object],
    ) -> Mapping[str, object]:
        resource_id = _canonical_resource_id(
            _text(role.get("id"), field="roleAssignment.id")
        ).casefold()
        props = role.get("properties")
        if not isinstance(props, Mapping):
            raise AzureProviderError(
                "role assignment properties must be an object"
            )
        contract = {
            key: props.get(key)
            for key in _ROLE_WRITABLE_PROPERTIES
        }
        principal_id = contract.get("principalId")
        if isinstance(principal_id, str):
            contract["principalId"] = principal_id.casefold()
        role_definition_id = contract.get("roleDefinitionId")
        if isinstance(role_definition_id, str):
            contract["roleDefinitionId"] = _canonical_role_definition_id(
                role_definition_id,
                resource_id.split("/")[2],
            )
        delegated_id = contract.get(
            "delegatedManagedIdentityResourceId"
        )
        if isinstance(delegated_id, str) and delegated_id:
            contract[
                "delegatedManagedIdentityResourceId"
            ] = _canonical_resource_id(delegated_id).casefold()
        return _binding(
            {
                "id": resource_id,
                "properties": contract,
            }
        )

    def _delete_fic(self, identity: AzureIdentityReference, subject: str) -> None:
        url, scope = self._fic_url(identity, subject)
        if scope == _GRAPH_SCOPE:
            existing = self._get_fic(identity, subject)
            if existing is None:
                return
            url = self._fic_resource_url(identity, subject, existing)
        response = self._response("DELETE", url, scope=scope, params={"api-version": _FIC_API_VERSION} if scope == _ARM_SCOPE else None)
        if response.status_code not in {200, 202, 204, 404}:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")

    def _restore_fic(self, identity: AzureIdentityReference, subject: str, preimage: Mapping[str, object]) -> None:
        props = preimage.get("properties", preimage)
        assert isinstance(props, Mapping)
        url, scope = self._fic_url(identity, subject)
        response = self._response("PUT" if scope == _ARM_SCOPE else "POST", url, scope=scope, params={"api-version": _FIC_API_VERSION} if scope == _ARM_SCOPE else None, json_body={"properties": {"issuer": props.get("issuer"), "subject": props.get("subject"), "audiences": props.get("audiences")}} if scope == _ARM_SCOPE else {"name": _text(preimage.get("name"), field="federatedCredential.name"), "issuer": props.get("issuer"), "subject": props.get("subject"), "audiences": props.get("audiences")})
        if response.status_code not in {200, 201}:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")

    def _delete_uami(self, resource_id: str) -> None:
        response = self._response("DELETE", f"https://management.azure.com{resource_id}", scope=_ARM_SCOPE, params={"api-version": _MANAGED_IDENTITY_API_VERSION})
        if response.status_code not in {200, 202, 204, 404}:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")

    def _rollback_action(self, state: Mapping[str, object], identity: AzureIdentityReference, action_id: str) -> None:
        for role in state["role_assignments"]:
            if role["action_id"] == action_id:
                if role["disposition"] == "created":
                    current = self._get_role_by_resource_id(
                        _text(role["resource_id"], field="resource_id")
                    )
                    if current is None:
                        return
                    self._verify_role_binding(identity, role)
                    self._delete_role(_text(role["resource_id"], field="resource_id"))
                elif role["disposition"] == "changed":
                    preimage = role.get("preimage")
                    if not isinstance(preimage, Mapping):
                        raise AzureProviderError(
                            "changed role assignment is missing its preimage"
                        )
                    current = self._get_role_by_resource_id(
                        _text(role["resource_id"], field="resource_id")
                    )
                    if (
                        current is not None
                        and canonical_sha256(
                            self._role_contract_document(current)
                        )
                        == canonical_sha256(
                            self._role_contract_document(preimage)
                        )
                    ):
                        return
                    self._verify_role_binding(identity, role)
                    self._restore_role(_binding(preimage))
                return
        for fic in state["federated_credentials"]:
            if fic["action_id"] == action_id:
                if fic["disposition"] == "created":
                    current = self._get_fic(
                        identity,
                        _text(fic["subject"], field="subject"),
                    )
                    if current is None:
                        return
                    self._verify_fic_binding(identity, fic)
                    self._delete_fic(identity, _text(fic["subject"], field="subject"))
                elif fic["disposition"] == "changed":
                    preimage = fic.get("preimage")
                    if not isinstance(preimage, Mapping):
                        raise AzureProviderError(
                            "changed federated credential is missing its preimage"
                        )
                    current = self._get_fic(
                        identity,
                        _text(fic["subject"], field="subject"),
                    )
                    if (
                        current is not None
                        and canonical_sha256(current)
                        == canonical_sha256(preimage)
                    ):
                        return
                    self._verify_fic_binding(identity, fic)
                    self._restore_fic(
                        identity,
                        _text(fic["subject"], field="subject"),
                        _binding(preimage),
                    )
                return
        if action_id == "azure-uami-create" and state["identity"]["disposition"] == "created":
            live = self._get_uami_if_exists(
                _text(
                    state["identity"]["resource_id"],
                    field="identity.resource_id",
                )
            )
            if live is None:
                return
            if canonical_sha256(self._uami_live_document(live)) != canonical_sha256(
                self._uami_live_document(identity)
            ):
                raise AzureProviderError(
                    "managed identity rollback refused external drift"
                )
            self._delete_uami(
                _text(
                    state["identity"]["resource_id"],
                    field="identity.resource_id",
                )
            )

    def _reconcile_ambiguous_attempts(self) -> None:
        state = self._provider_state
        if not state:
            return
        created = state.setdefault("created_actions", [])
        changed = state.setdefault("changed_actions", [])
        assert isinstance(created, list)
        assert isinstance(changed, list)
        identity = self._state_identity(state)
        for attempt in state["attempts"]:
            if attempt["disposition"] != "ambiguous":
                continue
            target = attempt.get("target")
            if (
                isinstance(target, Mapping)
                and target.get("unsafe_without_preimage") is True
            ):
                raise AzureProviderError(
                    "ambiguous Azure mutation cannot be adopted or compensated without a preimage"
                )
            if attempt["kind"] == "uami":
                live = self._get_uami_if_exists(identity.resource_id or "")
                if live is not None:
                    self._assert_expected_identity(
                        identity,
                        live,
                        allow_fill_missing=True,
                    )
                    state["identity"] = self._identity_state(live, planned=identity, disposition="created", preimage=None)
                else:
                    state["identity"]["disposition"] = "created"
                attempt["disposition"] = "created"
                if attempt["action_id"] not in created:
                    created.append(attempt["action_id"])
            elif attempt["kind"] == "fic":
                subject = _text(attempt["target"]["subject"], field="subject")
                record = next(
                    item
                    for item in state["federated_credentials"]
                    if item["action_id"] == attempt["action_id"]
                )
                current = self._get_fic(self._state_identity(state), subject)
                preimage = record.get("preimage")
                disposition = "created" if preimage is None else "changed"
                if current is not None:
                    properties = current.get("properties", current)
                    if not isinstance(properties, Mapping):
                        raise AzureProviderError(
                            "federated credential properties must be an object"
                        )
                    desired = (
                        properties.get("issuer") == _ACTIONS_ISSUER
                        and properties.get("subject") == subject
                        and properties.get("audiences") == [_ACTIONS_AUDIENCE]
                    )
                    restored = (
                        isinstance(preimage, Mapping)
                        and canonical_sha256(current)
                        == canonical_sha256(preimage)
                    )
                    if not desired and not restored:
                        raise AzureProviderError(
                            "ambiguous federated credential drifted externally"
                        )
                    record["resource_id"] = self._fic_resource_url(
                        self._state_identity(state),
                        subject,
                        current,
                    )
                record["disposition"] = disposition
                attempt["disposition"] = disposition
                actions = created if disposition == "created" else changed
                if attempt["action_id"] not in actions:
                    actions.append(attempt["action_id"])
            elif attempt["kind"] == "role":
                assignment_id = _text(attempt["target"]["assignment_id"], field="assignment_id")
                record = next(
                    item
                    for item in state["role_assignments"]
                    if item["action_id"] == attempt["action_id"]
                )
                scope = _text(record["scope"], field="scope")
                current = self._get_role(scope, assignment_id)
                preimage = record.get("preimage")
                disposition = "created" if preimage is None else "changed"
                if current is not None:
                    restored = (
                        isinstance(preimage, Mapping)
                        and canonical_sha256(current)
                        == canonical_sha256(preimage)
                    )
                    if not restored:
                        self._verify_role_properties(
                            current,
                            _text(record["principal_id"], field="principal_id"),
                            _text(
                                record["role_definition_id"],
                                field="role_definition_id",
                            ),
                            scope,
                            self._state_identity(state).subscription_id
                            or scope.split("/")[2],
                            require_defaults=True,
                        )
                record["disposition"] = disposition
                attempt["disposition"] = disposition
                actions = created if disposition == "created" else changed
                if attempt["action_id"] not in actions:
                    actions.append(attempt["action_id"])

    def _select_identity(self, plan: BootstrapPlan) -> AzureIdentityReference:
        for action in plan.actions:
            if action.phase != "azure":
                continue
            data = _diagnostics_map(action)
            if action.kind == "managed-identity":
                return AzureIdentityReference(kind="user_assigned_managed_identity", client_id=_optional_text(data.get("client_id")), resource_id=_canonical_resource_id(_text(data.get("resource_id"), field="resource_id")), object_id=None, principal_id=_optional_text(data.get("principal_id")), tenant_id=_optional_text(data.get("tenant_id")), subscription_id=_optional_text(data.get("subscription_id")), name=_text(data.get("name", "shared"), field="name"), adopted=data.get("adopted", "false") == "true", location=_optional_text(data.get("location")) or "eastus")
            if action.kind == "entra-application":
                return AzureIdentityReference(kind="entra_application", client_id=_optional_text(data.get("client_id")), resource_id=None, object_id=_text(data.get("object_id"), field="object_id"), principal_id=None, tenant_id=_optional_text(data.get("tenant_id")), subscription_id=_optional_text(data.get("subscription_id")), name=_text(data.get("name", "shared"), field="name"), adopted=True)
        raise AzureProviderError("plan does not contain an Azure identity action")

    def _planned_bindings(self, plan: BootstrapPlan) -> PlannedBindingSet:
        identity = self._select_identity(plan)
        if not identity.subscription_id:
            raise AzureProviderError("identity must include subscription_id")
        subjects: list[str] = []
        roles: list[PlannedRoleAssignment] = []
        for action in plan.actions:
            if action.phase != "azure":
                continue
            data = _diagnostics_map(action)
            if action.kind == "federated-credential":
                subject = _text(data.get("subject"), field="subject")
                if subject in subjects:
                    raise AzureProviderError(
                        "federated credential subjects must be unique"
                    )
                subjects.append(subject)
                continue
            if action.kind != "role-assignment":
                continue
            role_key = _text(data.get("role"), field="role")
            if role_key not in self._approved_role_definitions:
                raise AzureProviderError(f"approved_role_definitions.{role_key} must be configured")
            scope = _canonical_scope(_text(data.get("scope"), field="scope"), identity.subscription_id)
            role_definition_id = _canonical_role_definition_id(_text(self._approved_role_definitions.get(role_key), field=f"approved_role_definitions.{role_key}"), identity.subscription_id)
            approval_fingerprint = _approval_fingerprint(scope, role_definition_id)
            planned_fingerprint = _optional_text(data.get("approved_role_sha256"))
            if planned_fingerprint is not None and planned_fingerprint != approval_fingerprint:
                raise AzureProviderError("approved role mapping drifted from planned approval fingerprint")
            roles.append(PlannedRoleAssignment(role_key=role_key, scope=scope, role_definition_id=role_definition_id, approval_fingerprint=approval_fingerprint))
        if len(subjects) != 2:
            raise AzureProviderError(
                "Azure plan must contain exactly two federated credential subjects"
            )
        return PlannedBindingSet(
            identity=identity,
            roles=tuple(roles),
            subjects=(subjects[0], subjects[1]),
        )

    def _assert_role_approval(self, role: PlannedRoleAssignment) -> None:
        current = self._approved_role_definitions.get(role.role_key)
        if current is None:
            raise AzureProviderError("approved role mapping drifted from planned approval fingerprint")
        canonical = _canonical_role_definition_id(current, role.scope.split("/")[2])
        if _approval_fingerprint(role.scope, canonical) != role.approval_fingerprint or canonical != role.role_definition_id:
            raise AzureProviderError("approved role mapping drifted from planned approval fingerprint")


__all__ = [
    "AzureArmRestProvider",
    "AzureIdentityReference",
    "AzureProviderApplyError",
    "AzureProviderError",
    "rollback_failure_details",
]
