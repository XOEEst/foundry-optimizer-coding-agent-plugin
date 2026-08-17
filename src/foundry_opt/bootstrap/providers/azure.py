from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import unquote

import httpx

from foundry_opt.bootstrap.canonical import canonical_sha256, safe_persisted_document
from foundry_opt.bootstrap.contracts import (
    BindingAssessment,
    BootstrapAction,
    BootstrapPlan,
    BootstrapReceipt,
    FingerprintRecord,
    RedactedStatusInfo,
)
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
    if "//" in decoded:
        raise AzureProviderError("resource ids and scopes must not contain duplicate slashes")
    return "/" + "/".join(segment for segment in decoded.split("/") if segment)


def _canonical_scope(value: str, subscription_id: str) -> str:
    scope = _canonical_resource_id(value)
    lowered = scope.lower()
    if lowered.startswith("/providers/microsoft.management/managementgroups/"):
        raise AzureProviderError("management-group scopes are not allowed")
    if not lowered.startswith(f"/subscriptions/{subscription_id}".lower()):
        raise AzureProviderError("cross-subscription scopes are not allowed")
    if lowered == f"/subscriptions/{subscription_id}".lower():
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
    return hashlib.sha256(_canonical_bytes({"scope": scope.lower(), "role_definition_id": role_definition_id.lower()})).hexdigest()


def _role_assignment_id(scope: str, principal_id: str, role_definition_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scope.lower()}|{principal_id.lower()}|{role_definition_id.lower()}"))


def _diagnostics_map(action: BootstrapAction) -> dict[str, str]:
    return {entry.split("=", 1)[0]: entry.split("=", 1)[1] for entry in action.diagnostics if "=" in entry}


def _subjects(repository_identity: str) -> tuple[str, str]:
    return (
        f"repo:{repository_identity}:environment:copilot",
        f"repo:{repository_identity}:environment:foundry-production",
    )


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
            response = self._http.request(method, url, params=params, json=json_body, headers={"Accept": "application/json", "Authorization": f"Bearer {token}"})
        except httpx.TimeoutException:
            raise AzureProviderError("Azure request timed out") from None
        except httpx.TransportError:
            raise AzureProviderError("Azure transport failed") from None
        if 300 <= response.status_code < 400:
            raise AzureProviderError("redirect responses are not allowed")
        return response

    def _request(self, method: str, url: str, *, scope: str, params: Mapping[str, object] | None = None) -> Mapping[str, object]:
        return _json_response(self._response(method, url, scope=scope, params=params))

    def inventory_identity(self) -> Mapping[str, object]:
        return {}

    def assess_bindings(self) -> Sequence[BindingAssessment]:
        return ()

    def plan_bindings(self, plan: BootstrapPlan) -> Sequence[BootstrapAction]:
        planned = self._planned_bindings(plan)
        actions: list[BootstrapAction] = []
        for subject in planned.subjects:
            actions.append(BootstrapAction(action_id=f"azure-fic-{subject.rsplit(':',1)[-1]}", phase="azure", stage="planned", kind="federated-credential", diagnostics=(f"subject={subject}",)))
        for role in planned.roles:
            actions.append(BootstrapAction(action_id=f"azure-rbac-{role.role_key}", phase="azure", stage="planned", kind="role-assignment", diagnostics=(f"scope={role.scope}", f"role={role.role_key}", f"role_definition_id={role.role_definition_id}", f"approved_role_sha256={role.approval_fingerprint}")))
        if planned.identity.kind == "user_assigned_managed_identity" and not planned.identity.adopted:
            actions.append(BootstrapAction(action_id="azure-uami-create", phase="azure", stage="planned", kind="managed-identity", diagnostics=(f"resource_id={planned.identity.resource_id}", f"location={planned.identity.location}")))
        return tuple(actions)

    def apply_bindings(self, plan: BootstrapPlan) -> BootstrapReceipt:
        planned = self._planned_bindings(plan)
        created: list[str] = []
        adopted: list[str] = []
        changed: list[str] = []
        compensation: list[str] = []
        state = self._base_state(plan, planned)
        identity = planned.identity
        try:
            if identity.kind == "user_assigned_managed_identity" and identity.adopted:
                identity = self._get_uami(identity.resource_id or "")
                self._assert_expected_identity(planned.identity, identity)
                state["identity"] = self._identity_state(identity, planned=planned.identity, disposition="adopted")
            elif identity.kind == "user_assigned_managed_identity":
                existing = self._get_uami_if_exists(identity.resource_id or "")
                if existing is None:
                    self._append_attempt(state, action_id="azure-uami-create", kind="uami", target_resource_id=_text(identity.resource_id, field="identity.resource_id"))
                    compensation.append("azure-uami-create")
                    identity, created_now = self._create_or_update_uami(identity)
                    self._assert_expected_identity(planned.identity, identity, allow_fill_missing=True)
                    state["identity"] = self._identity_state(identity, planned=planned.identity, disposition="created" if created_now else "changed", preimage=None)
                    if created_now:
                        created.append("azure-uami-create")
                    else:
                        changed.append("azure-uami-create")
                    self._mark_attempt(state, "azure-uami-create", "created" if created_now else "changed")
                    compensation = [item for item in compensation if item != "azure-uami-create"]
                    compensation.insert(0, "azure-uami-create")
                else:
                    self._assert_expected_identity(planned.identity, existing)
                    identity = existing
                    state["identity"] = self._identity_state(identity, planned=planned.identity, disposition="adopted")
            else:
                identity = self._resolve_identity(identity)
                self._assert_expected_identity(planned.identity, identity)
                state["identity"] = self._identity_state(identity, planned=planned.identity, disposition="adopted")
            for subject in planned.subjects:
                action = f"azure-fic-{subject.rsplit(':',1)[-1]}"
                fic_url, fic_scope = self._fic_url(identity, subject)
                self._append_attempt(state, action_id=action, kind="fic", target_resource_id=fic_url)
                compensation.append(action)
                created_now = self._ensure_fic(identity, subject)
                fic_state = _binding({"action_id": action, "resource_id": fic_url, "scope": fic_scope, "subject": subject, "issuer": _ACTIONS_ISSUER, "audience": _ACTIONS_AUDIENCE, "disposition": "created" if created_now else "adopted"})
                if created_now:
                    created.append(action)
                    self._mark_attempt(state, action, "created")
                else:
                    adopted.append(action)
                    compensation.pop()
                    self._mark_attempt(state, action, "adopted")
                state["federated_credentials"].append(fic_state)
            for role in planned.roles:
                self._assert_role_approval(role)
                assignment_id = _role_assignment_id(role.scope, _text(identity.principal_id, field="identity.principal_id"), role.role_definition_id)
                action = f"azure-rbac-{role.role_key}-{assignment_id}"
                role_resource_id = f"{role.scope}/{_ROLE_ASSIGNMENTS_SEGMENT}/{assignment_id}"
                self._append_attempt(state, action_id=action, kind="role", target_resource_id=role_resource_id)
                compensation.append(action)
                existed_before = self._live_role(role.scope, assignment_id)
                created_now = self._ensure_role(identity, role, assignment_id)
                if created_now:
                    created.append(action)
                    self._mark_attempt(state, action, "created")
                    role_state = _binding({"action_id": action, "resource_id": role_resource_id, "assignment_id": assignment_id, "role_key": role.role_key, "scope": role.scope, "role_definition_id": role.role_definition_id, "principal_id": _text(identity.principal_id, field="identity.principal_id"), "disposition": "created", "preimage": None})
                else:
                    adopted.append(action)
                    compensation.pop()
                    self._mark_attempt(state, action, "adopted")
                    role_state = _binding({"action_id": action, "resource_id": role_resource_id, "assignment_id": assignment_id, "role_key": role.role_key, "scope": role.scope, "role_definition_id": role.role_definition_id, "principal_id": _text(identity.principal_id, field="identity.principal_id"), "disposition": "adopted", "preimage": existed_before})
                state["role_assignments"].append(role_state)
            state["compensation_required_actions"] = tuple(compensation)
            receipt = BootstrapReceipt.create(operation_id=plan.operation_id, runtime_repository=plan.runtime_repository, runtime_commit=plan.runtime_commit, repository_identity=plan.repository_identity, plan_hash=plan.plan_hash, before_fingerprints=(_fingerprint("azure-plan", {"planned": [role.__dict__ for role in planned.roles]}),), after_fingerprints=(_fingerprint("azure-live", {"principal_id": identity.principal_id, "client_id": identity.client_id}),), created_actions=tuple(created), adopted_actions=tuple(adopted), changed_actions=tuple(changed), compensation_required_actions=tuple(reversed(compensation)))
            state["status"] = "applied"
            self._bind_receipt(state, receipt)
            self._provider_state = state
            return receipt
        except AzureProviderError as exc:
            state["status"] = "compensation_required" if compensation else "failed"
            state["compensation_required_actions"] = tuple(compensation)
            receipt = BootstrapReceipt.create(operation_id=plan.operation_id, runtime_repository=plan.runtime_repository, runtime_commit=plan.runtime_commit, repository_identity=plan.repository_identity, plan_hash=plan.plan_hash, created_actions=tuple(created), adopted_actions=tuple(adopted), changed_actions=tuple(changed), compensation_required_actions=tuple(compensation), error_info=RedactedStatusInfo(code="azure_apply_failed", summary=type(exc).__name__[:64]), resume_info=RedactedStatusInfo(code="azure_compensation_state", summary="Resume with exported Azure provider state."))
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
        state = {key: value for key, value in mapping.items() if key != "state_hash"}
        safe_persisted_document(state)
        if canonical_sha256(state) != state_hash:
            raise AzureProviderError("provider state hash mismatch")
        identity = state.get("identity")
        if not isinstance(identity, Mapping):
            raise AzureProviderError("provider state identity is missing")
        if not _optional_text(identity.get("client_id")) or not _optional_text(identity.get("principal_id")):
            raise AzureProviderError("provider state identity ids are missing")
        self._provider_state = dict(state)

    def verify_bindings(self, receipt: BootstrapReceipt) -> bool:
        if receipt.error_info is not None:
            raise AzureProviderError("cannot verify failed Azure receipt")
        state = self._validated_state(receipt)
        planned = self._state_planned_bindings(state)
        live = self._resolve_live_identity(planned.identity)
        self._assert_expected_identity(planned.identity, live, allow_fill_missing=False)
        for fic in state["federated_credentials"]:
            self._verify_fic_binding(live, fic)
        for role in state["role_assignments"]:
            self._verify_role_binding(live, role)
        return True

    def rollback_bindings(self, receipt: BootstrapReceipt) -> None:
        state = self._validated_state(receipt)
        identity = self._state_identity(state)
        errors: list[str] = []
        for action_id in state["compensation_required_actions"]:
            try:
                self._rollback_action(state, identity, _text(action_id, field="action_id"))
            except AzureProviderError:
                errors.append("rollback")
        state["status"] = "rolled_back" if not errors else "rollback_failed"
        self._provider_state = state
        if errors:
            raise AzureProviderError("Azure rollback failed")

    def verify_rollback(self, receipt: BootstrapReceipt) -> bool:
        state = self._validated_state(receipt)
        identity = self._state_identity(state)
        live_identity = self._resolve_live_identity(identity, allow_missing=state["identity"]["disposition"] == "created")
        for role in state["role_assignments"]:
            if role["disposition"] == "created":
                if self._resource_exists(f"https://management.azure.com{role['resource_id']}", _ARM_SCOPE, {"api-version": _AUTHZ_API_VERSION}):
                    raise AzureProviderError("created role assignment still exists after rollback")
            else:
                self._verify_adopted_role_exact(role)
        for fic in state["federated_credentials"]:
            url, scope = self._fic_url(identity, fic["subject"])
            if fic["disposition"] == "created":
                if self._resource_exists(url, scope, {"api-version": _FIC_API_VERSION} if scope == _ARM_SCOPE else None):
                    raise AzureProviderError("created federated credential still exists after rollback")
            else:
                if live_identity is None:
                    raise AzureProviderError("adopted identity missing after rollback")
                self._verify_fic_binding(live_identity, fic)
        if state["identity"]["disposition"] == "created":
            if self._resource_exists(f"https://management.azure.com{state['identity']['resource_id']}", _ARM_SCOPE, {"api-version": _MANAGED_IDENTITY_API_VERSION}):
                raise AzureProviderError("created managed identity still exists after rollback")
        else:
            if live_identity is None:
                raise AzureProviderError("adopted identity missing after rollback")
            self._assert_expected_identity(self._state_identity(state), live_identity)
        return True

    def _base_state(self, plan: BootstrapPlan, planned: PlannedBindingSet) -> dict[str, object]:
        return {
            "version": 2,
            "operation_id": plan.operation_id,
            "runtime_repository": plan.runtime_repository,
            "runtime_commit": plan.runtime_commit,
            "repository_identity": plan.repository_identity,
            "plan_hash": plan.plan_hash,
            "identity": self._identity_state(planned.identity, planned=planned.identity, disposition="planned"),
            "subjects": planned.subjects,
            "role_assignments": [],
            "federated_credentials": [],
            "approved_roles": [{"role_key": role.role_key, "scope": role.scope, "role_definition_id": role.role_definition_id, "approval_fingerprint": role.approval_fingerprint} for role in planned.roles],
            "attempts": [],
            "compensation_required_actions": (),
            "status": "planned",
        }

    def _identity_state(self, identity: AzureIdentityReference, *, planned: AzureIdentityReference, disposition: str, preimage: Mapping[str, object] | None = None) -> Mapping[str, object]:
        return _binding({
            "kind": identity.kind,
            "resource_id": identity.resource_id,
            "object_id": identity.object_id,
            "client_id": identity.client_id or planned.client_id,
            "principal_id": identity.principal_id or planned.principal_id,
            "tenant_id": identity.tenant_id or planned.tenant_id,
            "subscription_id": identity.subscription_id or planned.subscription_id,
            "name": identity.name,
            "location": identity.location or planned.location,
            "adopted": planned.adopted,
            "disposition": disposition,
            "preimage": preimage,
        })

    def _append_attempt(self, state: dict[str, object], *, action_id: str, kind: str, target_resource_id: str) -> None:
        attempts = state["attempts"]
        assert isinstance(attempts, list)
        attempts.append(_binding({"action_id": action_id, "kind": kind, "target_resource_id": target_resource_id, "disposition": "ambiguous"}))

    def _mark_attempt(self, state: dict[str, object], action_id: str, disposition: str) -> None:
        attempts = state["attempts"]
        assert isinstance(attempts, list)
        for attempt in attempts:
            if attempt["action_id"] == action_id:
                attempt["disposition"] = disposition
                return

    def _bind_receipt(self, state: dict[str, object], receipt: BootstrapReceipt) -> None:
        state["receipt_hash"] = receipt.receipt_hash
        state["receipt_plan_hash"] = receipt.plan_hash

    def _validated_state(self, receipt: BootstrapReceipt) -> dict[str, object]:
        if not self._provider_state:
            raise AzureProviderError("provider state is unavailable")
        if self._provider_state.get("receipt_hash") != receipt.receipt_hash or self._provider_state.get("receipt_plan_hash") != receipt.plan_hash:
            raise AzureProviderError("provider state receipt binding mismatch")
        return self._provider_state

    def _state_identity(self, state: Mapping[str, object]) -> AzureIdentityReference:
        identity = state["identity"]
        assert isinstance(identity, Mapping)
        return AzureIdentityReference(kind=_text(identity.get("kind"), field="identity.kind"), client_id=_optional_text(identity.get("client_id")), resource_id=_optional_text(identity.get("resource_id")), object_id=_optional_text(identity.get("object_id")), principal_id=_optional_text(identity.get("principal_id")), tenant_id=_optional_text(identity.get("tenant_id")), subscription_id=_optional_text(identity.get("subscription_id")), name=_text(identity.get("name"), field="identity.name"), adopted=bool(identity.get("adopted")), location=_optional_text(identity.get("location")))

    def _state_planned_bindings(self, state: Mapping[str, object]) -> PlannedBindingSet:
        roles = []
        for item in state["approved_roles"]:
            assert isinstance(item, Mapping)
            roles.append(PlannedRoleAssignment(role_key=_text(item.get("role_key"), field="role_key"), scope=_text(item.get("scope"), field="scope"), role_definition_id=_text(item.get("role_definition_id"), field="role_definition_id"), approval_fingerprint=_text(item.get("approval_fingerprint"), field="approval_fingerprint")))
        return PlannedBindingSet(identity=self._state_identity(state), roles=tuple(roles), subjects=tuple(state["subjects"]))  # type: ignore[arg-type]

    def _resource_exists(self, url: str, scope: str, params: Mapping[str, object] | None) -> bool:
        response = self._response("GET", url, scope=scope, params=params)
        if response.status_code == 404:
            return False
        if response.status_code >= 400:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")
        return True

    def _resolve_live_identity(self, identity: AzureIdentityReference, *, allow_missing: bool = False) -> AzureIdentityReference | None:
        if identity.kind == "user_assigned_managed_identity":
            live = self._get_uami_if_exists(identity.resource_id or "")
            if live is None and allow_missing:
                return None
            if live is None:
                raise AzureProviderError("live identity missing")
            return live
        return self._resolve_identity(identity)

    def _get_uami(self, resource_id: str) -> AzureIdentityReference:
        live = self._get_uami_if_exists(resource_id)
        if live is None:
            raise AzureProviderError("live identity missing")
        return live

    def _get_uami_if_exists(self, resource_id: str) -> AzureIdentityReference | None:
        response = self._response("GET", f"https://management.azure.com{_canonical_resource_id(resource_id)}", scope=_ARM_SCOPE, params={"api-version": _MANAGED_IDENTITY_API_VERSION})
        if response.status_code == 404:
            return None
        body = _json_response(response)
        props = body["properties"]
        assert isinstance(props, Mapping)
        rid = _canonical_resource_id(_text(body.get("id"), field="identity.id"))
        return AzureIdentityReference(kind="user_assigned_managed_identity", client_id=_text(props.get("clientId"), field="identity.clientId"), resource_id=rid, object_id=None, principal_id=_text(props.get("principalId"), field="identity.principalId"), tenant_id=_text(props.get("tenantId"), field="identity.tenantId"), subscription_id=rid.split("/")[2], name=_text(body.get("name"), field="identity.name"), adopted=True, location=_optional_text(body.get("location")))

    def _live_role(self, scope: str, assignment_id: str) -> Mapping[str, object] | None:
        response = self._response("GET", f"https://management.azure.com{scope}/{_ROLE_ASSIGNMENTS_SEGMENT}/{assignment_id}", scope=_ARM_SCOPE, params={"api-version": _AUTHZ_API_VERSION})
        if response.status_code == 404:
            return None
        return _json_response(response)

    def _verify_adopted_role_exact(self, role: Mapping[str, object]) -> None:
        preimage = role.get("preimage")
        if not isinstance(preimage, Mapping):
            raise AzureProviderError("adopted role assignment missing preimage")
        live = self._request("GET", f"https://management.azure.com{role['resource_id']}", scope=_ARM_SCOPE, params={"api-version": _AUTHZ_API_VERSION})
        if canonical_sha256(live) != canonical_sha256(preimage):
            raise AzureProviderError("adopted role assignment drifted after rollback")

    def _delete_role(self, resource_id: str) -> None:
        response = self._response("DELETE", f"https://management.azure.com{resource_id}", scope=_ARM_SCOPE, params={"api-version": _AUTHZ_API_VERSION})
        if response.status_code not in {200, 202, 204, 404}:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")

    def _delete_fic(self, identity: AzureIdentityReference, subject: str) -> None:
        url, scope = self._fic_url(identity, subject)
        response = self._response("DELETE", url, scope=scope, params={"api-version": _FIC_API_VERSION} if scope == _ARM_SCOPE else None)
        if response.status_code not in {200, 202, 204, 404}:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")

    def _delete_uami(self, resource_id: str) -> None:
        response = self._response("DELETE", f"https://management.azure.com{resource_id}", scope=_ARM_SCOPE, params={"api-version": _MANAGED_IDENTITY_API_VERSION})
        if response.status_code not in {200, 202, 204, 404}:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")

    def _verify_fic_binding(self, identity: AzureIdentityReference, fic: Mapping[str, object]) -> None:
        url, scope = self._fic_url(identity, _text(fic.get("subject"), field="subject"))
        body = self._request("GET", url, scope=scope, params={"api-version": _FIC_API_VERSION} if scope == _ARM_SCOPE else None)
        props = body.get("properties", body)
        assert isinstance(props, Mapping)
        if props.get("issuer") != fic["issuer"] or props.get("subject") != fic["subject"] or props.get("audiences") != [fic["audience"]]:
            raise AzureProviderError("federated credential verification drifted from exact claims")

    def _verify_role_binding(self, identity: AzureIdentityReference, role: Mapping[str, object]) -> None:
        body = self._request("GET", f"https://management.azure.com{role['resource_id']}", scope=_ARM_SCOPE, params={"api-version": _AUTHZ_API_VERSION})
        props = body["properties"]
        assert isinstance(props, Mapping)
        if _text(props.get("principalId"), field="principalId").lower() != _text(role.get("principal_id"), field="principal_id").lower():
            raise AzureProviderError("role assignment verification principalId mismatch")
        if _canonical_role_definition_id(_text(props.get("roleDefinitionId"), field="roleDefinitionId"), identity.subscription_id or _text(role.get("scope"), field="scope").split("/")[2]) != _text(role.get("role_definition_id"), field="role_definition_id"):
            raise AzureProviderError("role assignment verification roleDefinitionId mismatch")
        scope_id = _canonical_resource_id(_text(body.get("id"), field="id").split(f"/{_ROLE_ASSIGNMENTS_SEGMENT}/", 1)[0])
        if scope_id != _text(role.get("scope"), field="scope"):
            raise AzureProviderError("role assignment verification scope mismatch")

    def _rollback_action(self, state: Mapping[str, object], identity: AzureIdentityReference, action_id: str) -> None:
        if action_id == "azure-uami-create":
            if state["identity"]["disposition"] == "created":
                self._delete_uami(_text(state["identity"]["resource_id"], field="identity.resource_id"))
            return
        for role in reversed(state["role_assignments"]):
            if role["action_id"] == action_id and role["disposition"] == "created":
                self._delete_role(_text(role["resource_id"], field="resource_id"))
                return
        for fic in reversed(state["federated_credentials"]):
            if fic["action_id"] == action_id and fic["disposition"] == "created":
                self._delete_fic(identity, _text(fic["subject"], field="subject"))
                return

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
            roles.append(PlannedRoleAssignment(role_key=role_key, scope=scope, role_definition_id=role_definition_id, approval_fingerprint=_approval_fingerprint(scope, role_definition_id)))
        return PlannedBindingSet(identity=identity, roles=tuple(roles), subjects=_subjects(plan.repository_identity))

    def _assert_role_approval(self, role: PlannedRoleAssignment) -> None:
        current = self._approved_role_definitions.get(role.role_key)
        if current is None:
            raise AzureProviderError("approved role mapping drifted from planned approval fingerprint")
        canonical = _canonical_role_definition_id(current, role.scope.split("/")[2])
        if _approval_fingerprint(role.scope, canonical) != role.approval_fingerprint or canonical != role.role_definition_id:
            raise AzureProviderError("approved role mapping drifted from planned approval fingerprint")

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

    def _assert_expected_identity(self, planned: AzureIdentityReference, live: AzureIdentityReference, *, allow_fill_missing: bool = False) -> None:
        if planned.resource_id and live.resource_id and planned.resource_id != live.resource_id:
            raise AzureProviderError("live identity resource_id did not match planned identity")
        for field_name in ("client_id", "principal_id", "tenant_id"):
            expected = getattr(planned, field_name)
            actual = getattr(live, field_name)
            if expected is not None and actual != expected:
                raise AzureProviderError(f"live identity {field_name} did not match planned identity")
            if expected is None and not allow_fill_missing and actual is None:
                continue

    def _create_or_update_uami(self, identity: AzureIdentityReference) -> tuple[AzureIdentityReference, bool]:
        response = self._response("PUT", f"https://management.azure.com{identity.resource_id}", scope=_ARM_SCOPE, params={"api-version": _MANAGED_IDENTITY_API_VERSION}, json_body={"location": identity.location})
        if response.status_code not in {200, 201}:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")
        return self._get_uami(identity.resource_id or ""), response.status_code == 201

    def _fic_url(self, identity: AzureIdentityReference, subject: str) -> tuple[str, str]:
        name = _fic_name(subject)
        if identity.kind == "user_assigned_managed_identity":
            return (f"https://management.azure.com{identity.resource_id}/federatedIdentityCredentials/{name}", _ARM_SCOPE)
        return (f"{_GRAPH_APPLICATIONS}/{identity.object_id}/federatedIdentityCredentials/{name}", _GRAPH_SCOPE)

    def _ensure_fic(self, identity: AzureIdentityReference, subject: str) -> bool:
        url, scope = self._fic_url(identity, subject)
        get_response = self._response("GET", url, scope=scope, params={"api-version": _FIC_API_VERSION} if scope == _ARM_SCOPE else None)
        if get_response.status_code == 200:
            return False
        if get_response.status_code != 404:
            raise AzureProviderError(f"Azure request failed with HTTP {get_response.status_code}")
        try:
            response = self._response("PUT" if scope == _ARM_SCOPE else "POST", url if scope == _ARM_SCOPE else f"{_GRAPH_APPLICATIONS}/{identity.object_id}/federatedIdentityCredentials", scope=scope, params={"api-version": _FIC_API_VERSION} if scope == _ARM_SCOPE else None, json_body={"properties": {"issuer": _ACTIONS_ISSUER, "subject": subject, "audiences": [_ACTIONS_AUDIENCE]}} if scope == _ARM_SCOPE else {"name": _fic_name(subject), "issuer": _ACTIONS_ISSUER, "subject": subject, "audiences": [_ACTIONS_AUDIENCE]})
        except AzureProviderError as exc:
            if str(exc) == "Azure request timed out":
                if self._resource_exists(url, scope, {"api-version": _FIC_API_VERSION} if scope == _ARM_SCOPE else None):
                    return True
            raise
        if response.status_code not in {200, 201}:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")
        return True

    def _ensure_role(self, identity: AzureIdentityReference, role: PlannedRoleAssignment, assignment_id: str) -> bool:
        principal_id = _text(identity.principal_id, field="identity.principal_id")
        url = f"https://management.azure.com{role.scope}/{_ROLE_ASSIGNMENTS_SEGMENT}/{assignment_id}"
        get_response = self._response("GET", url, scope=_ARM_SCOPE, params={"api-version": _AUTHZ_API_VERSION})
        if get_response.status_code == 200:
            body = _json_response(get_response)
            props = body["properties"]
            assert isinstance(props, Mapping)
            if _text(props.get("principalId"), field="principalId").lower() != principal_id.lower():
                raise AzureProviderError("existing role assignment principalId did not match the frozen planned identity")
            if _canonical_role_definition_id(_text(props.get("roleDefinitionId"), field="roleDefinitionId"), identity.subscription_id or role.scope.split("/")[2]) != role.role_definition_id:
                raise AzureProviderError("existing role assignment roleDefinitionId did not match the frozen planned role")
            return False
        if get_response.status_code != 404:
            raise AzureProviderError(f"Azure request failed with HTTP {get_response.status_code}")
        raw_role_definition_id = self._approved_role_definitions[role.role_key]
        try:
            response = self._response("PUT", url, scope=_ARM_SCOPE, params={"api-version": _AUTHZ_API_VERSION}, json_body={"properties": {"principalId": principal_id, "roleDefinitionId": raw_role_definition_id, "principalType": "ServicePrincipal"}})
        except AzureProviderError as exc:
            if str(exc) == "Azure request timed out" and self._resource_exists(url, _ARM_SCOPE, {"api-version": _AUTHZ_API_VERSION}):
                body = self._request("GET", url, scope=_ARM_SCOPE, params={"api-version": _AUTHZ_API_VERSION})
                props = body["properties"]
                assert isinstance(props, Mapping)
                if _text(props.get("principalId"), field="principalId").lower() == principal_id.lower() and _canonical_role_definition_id(_text(props.get("roleDefinitionId"), field="roleDefinitionId"), identity.subscription_id or role.scope.split("/")[2]) == role.role_definition_id:
                    return True
            raise
        if response.status_code == 403:
            raise AzureProviderError("executor is missing Microsoft.Authorization/roleAssignments/write")
        if response.status_code not in {200, 201}:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")
        return True


__all__ = ["AzureArmRestProvider", "AzureIdentityReference", "AzureProviderError"]
