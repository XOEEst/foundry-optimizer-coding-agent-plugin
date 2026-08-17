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


class AzureProviderError(BootstrapProviderError):
    pass


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


def _subjects(repository_identity: str) -> tuple[str, str]:
    return (f"repo:{repository_identity}:environment:copilot", f"repo:{repository_identity}:environment:foundry-production")


def _fic_name(subject: str) -> str:
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()[:24]


def _binding(values: Mapping[str, object]) -> Mapping[str, object]:
    safe_persisted_document(values)
    return values


class AzureArmRestProvider:
    def __init__(self, *, token_provider: Callable[[str], str], transport: httpx.BaseTransport | None = None, timeout: float = 10.0, approved_role_definitions: Mapping[str, str] | None = None) -> None:
        self._token_provider = token_provider
        self._approved_role_definitions = dict(approved_role_definitions or {})
        self._http = httpx.Client(transport=transport, timeout=timeout, follow_redirects=False, trust_env=False)
        self._provider_state: dict[str, object] = {}

    def close(self) -> None:
        self._http.close()

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
        created: list[str] = []
        adopted: list[str] = []
        changed: list[str] = []
        compensation: list[str] = []
        state = self._base_state(plan, planned)
        identity = self._resolve_live_or_planned_identity(planned.identity, state, created, adopted, changed, compensation)
        for subject in planned.subjects:
            action = f"azure-fic-{subject.rsplit(':',1)[-1]}"
            fic_url, fic_scope = self._fic_url(identity, subject)
            preimage = self._get_fic(identity, subject)
            self._append_attempt(state, action_id=action, kind="fic", target_resource_id=fic_url, target={"subject": subject})
            if preimage is None:
                compensation.append(action)
            disposition = self._ensure_fic(identity, subject, state, action)
            record = {"action_id": action, "resource_id": fic_url, "scope": fic_scope, "subject": subject, "issuer": _ACTIONS_ISSUER, "audience": _ACTIONS_AUDIENCE, "preimage": preimage, "disposition": disposition}
            state["federated_credentials"].append(_binding(record))
            if disposition == "created":
                created.append(action)
            elif disposition == "changed":
                changed.append(action)
                compensation.append(action)
            else:
                adopted.append(action)
            self._mark_attempt(state, action, disposition if disposition in {"created", "changed", "adopted"} else "ambiguous")
        for role in planned.roles:
            self._assert_role_approval(role)
            assignment_id = _role_assignment_id(role.scope, _text(identity.principal_id, field="identity.principal_id"), role.role_definition_id)
            action = f"azure-rbac-{role.role_key}-{assignment_id}"
            resource_id = f"{role.scope}/{_ROLE_ASSIGNMENTS_SEGMENT}/{assignment_id}"
            preimage = self._get_role(role.scope, assignment_id)
            self._append_attempt(state, action_id=action, kind="role", target_resource_id=resource_id, target={"assignment_id": assignment_id})
            if preimage is None:
                compensation.append(action)
            disposition = self._ensure_role(identity, role, assignment_id, state, action)
            state["role_assignments"].append(_binding({"action_id": action, "resource_id": resource_id, "assignment_id": assignment_id, "role_key": role.role_key, "scope": role.scope, "role_definition_id": role.role_definition_id, "principal_id": _text(identity.principal_id, field="identity.principal_id"), "preimage": preimage, "disposition": disposition}))
            if disposition == "created":
                created.append(action)
            elif disposition == "changed":
                changed.append(action)
                compensation.append(action)
            else:
                adopted.append(action)
            self._mark_attempt(state, action, disposition)
        receipt = BootstrapReceipt.create(operation_id=plan.operation_id, runtime_repository=plan.runtime_repository, runtime_commit=plan.runtime_commit, repository_identity=plan.repository_identity, plan_hash=plan.plan_hash, before_fingerprints=(_fingerprint("azure-plan", {"planned": [role.__dict__ for role in planned.roles]}),), after_fingerprints=(_fingerprint("azure-live", {"principal_id": identity.principal_id, "client_id": identity.client_id}),), created_actions=tuple(created), adopted_actions=tuple(adopted), changed_actions=tuple(changed), compensation_required_actions=tuple(compensation))
        state["status"] = "applied"
        state["compensation_required_actions"] = tuple(compensation)
        self._bind_receipt(state, receipt)
        self._provider_state = state
        return receipt

    def export_provider_state(self, receipt: BootstrapReceipt) -> Mapping[str, object]:
        state = dict(self._validated_state(receipt))
        payload = {**state, "state_hash": canonical_sha256(state)}
        safe_persisted_document(payload)
        return payload

    def restore_provider_state(self, mapping: Mapping[str, object]) -> None:
        state_hash = _text(mapping.get("state_hash"), field="state_hash")
        state = {k: v for k, v in mapping.items() if k != "state_hash"}
        safe_persisted_document(state)
        if canonical_sha256(state) != state_hash:
            raise AzureProviderError("provider state hash mismatch")
        self._provider_state = dict(state)
        self._reconcile_ambiguous_attempts()

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
        return {"version": 3, "operation_id": plan.operation_id, "runtime_repository": plan.runtime_repository, "runtime_commit": plan.runtime_commit, "repository_identity": plan.repository_identity, "plan_hash": plan.plan_hash, "identity": self._identity_state(planned.identity, planned=planned.identity, disposition="planned", preimage=None), "subjects": planned.subjects, "role_assignments": [], "federated_credentials": [], "approved_roles": [{"role_key": role.role_key, "scope": role.scope, "role_definition_id": role.role_definition_id, "approval_fingerprint": role.approval_fingerprint} for role in planned.roles], "attempts": [], "compensation_required_actions": (), "status": "planned"}

    def _identity_state(self, identity: AzureIdentityReference, *, planned: AzureIdentityReference, disposition: str, preimage: Mapping[str, object] | None) -> Mapping[str, object]:
        return _binding({"kind": identity.kind, "resource_id": identity.resource_id, "object_id": identity.object_id, "client_id": identity.client_id or planned.client_id, "principal_id": identity.principal_id or planned.principal_id, "tenant_id": identity.tenant_id or planned.tenant_id, "subscription_id": identity.subscription_id or planned.subscription_id, "name": identity.name, "location": identity.location or planned.location, "adopted": planned.adopted, "disposition": disposition, "preimage": preimage})

    def _append_attempt(self, state: dict[str, object], *, action_id: str, kind: str, target_resource_id: str, target: Mapping[str, object]) -> None:
        attempts = state["attempts"]
        assert isinstance(attempts, list)
        attempts.append({"action_id": action_id, "kind": kind, "target_resource_id": target_resource_id, "target": dict(target), "disposition": "ambiguous"})

    def _mark_attempt(self, state: dict[str, object], action_id: str, disposition: str) -> None:
        for attempt in state["attempts"]:
            if attempt["action_id"] == action_id:
                attempt["disposition"] = disposition

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

    def _resolve_live_or_planned_identity(self, identity: AzureIdentityReference, state: dict[str, object], created: list[str], adopted: list[str], changed: list[str], compensation: list[str]) -> AzureIdentityReference:
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
            live, disposition = self._ensure_uami(identity)
            if disposition == "created":
                created.append("azure-uami-create")
            elif disposition == "changed":
                raise AzureProviderError("uami update without preimage is not allowed")
            state["identity"] = self._identity_state(live, planned=identity, disposition=disposition, preimage=None)
            self._mark_attempt(state, "azure-uami-create", disposition)
            return live
        self._assert_expected_identity(identity, existing, allow_fill_missing=True)
        state["identity"] = self._identity_state(existing, planned=identity, disposition="adopted", preimage=self._uami_live_document(existing))
        adopted.append("azure-uami-create")
        return existing

    def _uami_live_document(self, identity: AzureIdentityReference) -> Mapping[str, object]:
        return _binding({"id": identity.resource_id, "name": identity.name, "location": identity.location, "properties": {"clientId": identity.client_id, "principalId": identity.principal_id, "tenantId": identity.tenant_id}})

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

    def _ensure_uami(self, identity: AzureIdentityReference) -> tuple[AzureIdentityReference, str]:
        response = self._response("PUT", f"https://management.azure.com{identity.resource_id}", scope=_ARM_SCOPE, params={"api-version": _MANAGED_IDENTITY_API_VERSION}, json_body={"location": identity.location})
        if response.status_code not in {200, 201}:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")
        live = self._resolve_identity(identity)
        return live, ("created" if response.status_code == 201 else "changed")

    def _fic_url(self, identity: AzureIdentityReference, subject: str) -> tuple[str, str]:
        name = _fic_name(subject)
        if identity.kind == "user_assigned_managed_identity":
            return (f"https://management.azure.com{identity.resource_id}/federatedIdentityCredentials/{name}", _ARM_SCOPE)
        return (f"{_GRAPH_APPLICATIONS}/{identity.object_id}/federatedIdentityCredentials/{name}", _GRAPH_SCOPE)

    def _get_fic(self, identity: AzureIdentityReference, subject: str) -> Mapping[str, object] | None:
        url, scope = self._fic_url(identity, subject)
        response = self._response("GET", url, scope=scope, params={"api-version": _FIC_API_VERSION} if scope == _ARM_SCOPE else None)
        if response.status_code == 404:
            return None
        return _json_response(response)

    def _ensure_fic(self, identity: AzureIdentityReference, subject: str, state: dict[str, object], action_id: str) -> str:
        existing = self._get_fic(identity, subject)
        if existing is not None:
            props = existing.get("properties", existing)
            assert isinstance(props, Mapping)
            if props.get("issuer") == _ACTIONS_ISSUER and props.get("subject") == subject and props.get("audiences") == [_ACTIONS_AUDIENCE]:
                return "adopted"
        url, scope = self._fic_url(identity, subject)
        response = self._response("PUT" if scope == _ARM_SCOPE else "POST", url if scope == _ARM_SCOPE else f"{_GRAPH_APPLICATIONS}/{identity.object_id}/federatedIdentityCredentials", scope=scope, params={"api-version": _FIC_API_VERSION} if scope == _ARM_SCOPE else None, json_body={"properties": {"issuer": _ACTIONS_ISSUER, "subject": subject, "audiences": [_ACTIONS_AUDIENCE]}} if scope == _ARM_SCOPE else {"name": _fic_name(subject), "issuer": _ACTIONS_ISSUER, "subject": subject, "audiences": [_ACTIONS_AUDIENCE]})
        if response.status_code not in {200, 201}:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")
        return "created" if existing is None and response.status_code == 201 else "changed"

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

    def _ensure_role(self, identity: AzureIdentityReference, role: PlannedRoleAssignment, assignment_id: str, state: dict[str, object], action_id: str) -> str:
        existing = self._get_role(role.scope, assignment_id)
        principal_id = _text(identity.principal_id, field="identity.principal_id")
        if existing is not None:
            self._verify_role_properties(existing, principal_id, role.role_definition_id, role.scope, identity.subscription_id or role.scope.split("/")[2], require_defaults=False)
            props = existing["properties"]
            assert isinstance(props, Mapping)
            if any(props.get(key) not in (None, "") for key in ("condition", "conditionVersion", "delegatedManagedIdentityResourceId")):
                response = self._response("PUT", f"https://management.azure.com{role.scope}/{_ROLE_ASSIGNMENTS_SEGMENT}/{assignment_id}", scope=_ARM_SCOPE, params={"api-version": _AUTHZ_API_VERSION}, json_body={"properties": {"principalId": principal_id, "roleDefinitionId": self._approved_role_definitions[role.role_key], "principalType": "ServicePrincipal"}})
                if response.status_code not in {200, 201}:
                    raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")
                return "changed"
            return "adopted"
        raw_role_definition_id = self._approved_role_definitions[role.role_key]
        response = self._response("PUT", f"https://management.azure.com{role.scope}/{_ROLE_ASSIGNMENTS_SEGMENT}/{assignment_id}", scope=_ARM_SCOPE, params={"api-version": _AUTHZ_API_VERSION}, json_body={"properties": {"principalId": principal_id, "roleDefinitionId": raw_role_definition_id, "principalType": "ServicePrincipal"}})
        if response.status_code == 403:
            raise AzureProviderError("executor is missing Microsoft.Authorization/roleAssignments/write")
        if response.status_code not in {200, 201}:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")
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

    def _verify_fic_binding(self, identity: AzureIdentityReference, fic: Mapping[str, object]) -> None:
        current = self._get_fic(identity, _text(fic.get("subject"), field="subject"))
        if current is None:
            raise AzureProviderError("federated credential missing")
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
        if live is None or canonical_sha256(live) != canonical_sha256(preimage):
            raise AzureProviderError("adopted role assignment drifted after rollback")

    def _delete_role(self, resource_id: str) -> None:
        response = self._response("DELETE", f"https://management.azure.com{resource_id}", scope=_ARM_SCOPE, params={"api-version": _AUTHZ_API_VERSION})
        if response.status_code not in {200, 202, 204, 404}:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")

    def _restore_role(self, preimage: Mapping[str, object]) -> None:
        resource_id = _canonical_resource_id(_text(preimage.get("id"), field="id"))
        props = preimage["properties"]
        assert isinstance(props, Mapping)
        response = self._response("PUT", f"https://management.azure.com{resource_id}", scope=_ARM_SCOPE, params={"api-version": _AUTHZ_API_VERSION}, json_body={"properties": dict(props)})
        if response.status_code not in {200, 201}:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")

    def _delete_fic(self, identity: AzureIdentityReference, subject: str) -> None:
        url, scope = self._fic_url(identity, subject)
        response = self._response("DELETE", url, scope=scope, params={"api-version": _FIC_API_VERSION} if scope == _ARM_SCOPE else None)
        if response.status_code not in {200, 202, 204, 404}:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")

    def _restore_fic(self, identity: AzureIdentityReference, subject: str, preimage: Mapping[str, object]) -> None:
        props = preimage.get("properties", preimage)
        assert isinstance(props, Mapping)
        url, scope = self._fic_url(identity, subject)
        response = self._response("PUT" if scope == _ARM_SCOPE else "POST", url if scope == _ARM_SCOPE else f"{_GRAPH_APPLICATIONS}/{identity.object_id}/federatedIdentityCredentials", scope=scope, params={"api-version": _FIC_API_VERSION} if scope == _ARM_SCOPE else None, json_body={"properties": {"issuer": props.get("issuer"), "subject": props.get("subject"), "audiences": props.get("audiences")}} if scope == _ARM_SCOPE else {"name": _fic_name(subject), "issuer": props.get("issuer"), "subject": props.get("subject"), "audiences": props.get("audiences")})
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
                    self._delete_role(_text(role["resource_id"], field="resource_id"))
                elif role["disposition"] == "changed":
                    self._restore_role(_binding(role["preimage"]))
                return
        for fic in state["federated_credentials"]:
            if fic["action_id"] == action_id:
                if fic["disposition"] == "created":
                    self._delete_fic(identity, _text(fic["subject"], field="subject"))
                elif fic["disposition"] == "changed":
                    self._restore_fic(identity, _text(fic["subject"], field="subject"), _binding(fic["preimage"]))
                return
        if action_id == "azure-uami-create" and state["identity"]["disposition"] == "created":
            self._delete_uami(_text(state["identity"]["resource_id"], field="identity.resource_id"))

    def _reconcile_ambiguous_attempts(self) -> None:
        state = self._provider_state
        if not state:
            return
        identity = self._state_identity(state)
        if state["identity"]["disposition"] == "planned":
            live = self._get_uami_if_exists(identity.resource_id or "") if identity.kind == "user_assigned_managed_identity" else None
            if live is not None:
                state["identity"] = self._identity_state(live, planned=identity, disposition="created", preimage=None)
        for attempt in state["attempts"]:
            if attempt["disposition"] != "ambiguous":
                continue
            if attempt["kind"] == "uami":
                live = self._get_uami_if_exists(identity.resource_id or "")
                if live is not None:
                    state["identity"] = self._identity_state(live, planned=identity, disposition="created", preimage=None)
                    attempt["disposition"] = "created"
                else:
                    state["identity"]["disposition"] = "created"
            elif attempt["kind"] == "fic":
                subject = _text(attempt["target"]["subject"], field="subject")
                if self._get_fic(self._state_identity(state), subject) is not None:
                    attempt["disposition"] = "created"
            elif attempt["kind"] == "role":
                assignment_id = _text(attempt["target"]["assignment_id"], field="assignment_id")
                if self._get_role(_text(next(item["scope"] for item in state["role_assignments"] if item["action_id"] == attempt["action_id"]), field="scope"), assignment_id) is not None:
                    attempt["disposition"] = "created"

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
        roles: list[PlannedRoleAssignment] = []
        for action in plan.actions:
            if action.phase != "azure" or action.kind != "role-assignment":
                continue
            data = _diagnostics_map(action)
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
        return PlannedBindingSet(identity=identity, roles=tuple(roles), subjects=_subjects(plan.repository_identity))

    def _assert_role_approval(self, role: PlannedRoleAssignment) -> None:
        current = self._approved_role_definitions.get(role.role_key)
        if current is None:
            raise AzureProviderError("approved role mapping drifted from planned approval fingerprint")
        canonical = _canonical_role_definition_id(current, role.scope.split("/")[2])
        if _approval_fingerprint(role.scope, canonical) != role.approval_fingerprint or canonical != role.role_definition_id:
            raise AzureProviderError("approved role mapping drifted from planned approval fingerprint")


__all__ = ["AzureArmRestProvider", "AzureIdentityReference", "AzureProviderError"]
