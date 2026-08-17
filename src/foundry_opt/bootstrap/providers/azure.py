from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import httpx

from foundry_opt.bootstrap.contracts import BindingAssessment, BootstrapAction, BootstrapPlan, BootstrapReceipt, FingerprintRecord, RedactedStatusInfo
from foundry_opt.bootstrap.errors import BootstrapProviderError

_ARM_SCOPE = "https://management.azure.com/.default"
_GRAPH_SCOPE = "https://graph.microsoft.com/.default"
_ACTIONS_ISSUER = "https://token.actions.githubusercontent.com"
_ACTIONS_AUDIENCE = "api://AzureADTokenExchange"
_COPILOT_ENV = "copilot"
_PRODUCTION_ENV = "foundry-production"
_REQUEST_TIMEOUT = 10.0
_MAX_RESPONSE_BYTES = 256 * 1024
_FIC_API_VERSION = "2024-11-30"
_MANAGED_IDENTITY_API_VERSION = "2023-01-31"
_AUTHZ_API_VERSION = "2022-04-01"
_GRAPH_SP_API = "https://graph.microsoft.com/v1.0/servicePrincipals"
_GRAPH_APPLICATIONS = "https://graph.microsoft.com/v1.0/applications"
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
    assignment_id: str
    fingerprint: str


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


def _action_subjects(repository_identity: str) -> tuple[str, str]:
    return (
        f"repo:{repository_identity}:environment:{_COPILOT_ENV}",
        f"repo:{repository_identity}:environment:{_PRODUCTION_ENV}",
    )


def _json_response(response: httpx.Response) -> Mapping[str, object]:
    if 300 <= response.status_code < 400:
        raise AzureProviderError("redirect responses are not allowed")
    if len(response.content) > _MAX_RESPONSE_BYTES:
        raise AzureProviderError("Azure response exceeded configured limit")
    if response.status_code >= 400:
        try:
            payload = response.json()
        except Exception:
            payload = {"status": response.status_code}
        raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}: {payload}")
    payload = response.json()
    if not isinstance(payload, Mapping):
        raise AzureProviderError("Azure returned a non-object JSON document")
    return payload


def _canonical_resource_id(value: str) -> str:
    raw = value.strip()
    if "/./" in raw or "/../" in raw or raw.endswith("/.") or raw.endswith("/.."):
        raise AzureProviderError("resource ids and scopes must not contain dot segments")
    if not raw.startswith("/"):
        raw = "/" + raw
    return "/" + "/".join(segment for segment in raw.split("/") if segment)


def _canonical_scope(value: str, subscription_id: str) -> str:
    scope = _canonical_resource_id(value)
    if scope.lower().startswith("/providers/microsoft.management/managementgroups/"):
        raise AzureProviderError("management-group scopes are not allowed")
    if not scope.lower().startswith(f"/subscriptions/{subscription_id}".lower()):
        raise AzureProviderError("cross-subscription scopes are not allowed")
    if scope.lower() == f"/subscriptions/{subscription_id}".lower():
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


def _role_assignment_id(scope: str, principal_id: str, role_definition_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scope.lower()}|{principal_id.lower()}|{role_definition_id.lower()}"))


def _diagnostics_map(action: BootstrapAction) -> dict[str, str]:
    return {entry.split("=", 1)[0]: entry.split("=", 1)[1] for entry in action.diagnostics if "=" in entry}


def _fic_name(subject: str) -> str:
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()[:24]


class AzureArmRestProvider:
    def __init__(self, *, token_provider: Callable[[str], str], transport: httpx.BaseTransport | None = None, timeout: float = _REQUEST_TIMEOUT, approved_role_definitions: Mapping[str, str] | None = None) -> None:
        self._token_provider = token_provider
        self._approved_role_definitions = dict(approved_role_definitions or {})
        self._http = httpx.Client(transport=transport, timeout=timeout, follow_redirects=False, trust_env=False)

    def close(self) -> None:
        self._http.close()

    def _request(self, method: str, url: str, *, scope: str, params: Mapping[str, object] | None = None, json_body: Mapping[str, object] | None = None) -> Mapping[str, object]:
        try:
            response = self._http.request(method, url, params=params, json=json_body, headers={"Accept": "application/json", "Authorization": f"Bearer {self._token_provider(scope)}"})
        except httpx.TimeoutException as exc:
            raise AzureProviderError("Azure request timed out") from exc
        except httpx.TransportError as exc:
            raise AzureProviderError("Azure transport failed") from exc
        return _json_response(response)

    def _response(self, method: str, url: str, *, scope: str, params: Mapping[str, object] | None = None, json_body: Mapping[str, object] | None = None) -> httpx.Response:
        try:
            response = self._http.request(method, url, params=params, json=json_body, headers={"Accept": "application/json", "Authorization": f"Bearer {self._token_provider(scope)}"})
        except httpx.TimeoutException as exc:
            raise AzureProviderError("Azure request timed out") from exc
        except httpx.TransportError as exc:
            raise AzureProviderError("Azure transport failed") from exc
        if 300 <= response.status_code < 400:
            raise AzureProviderError("redirect responses are not allowed")
        return response

    def inventory_identity(self) -> Mapping[str, object]:
        return {}

    def assess_bindings(self) -> Sequence[BindingAssessment]:
        return ()

    def plan_bindings(self, plan: BootstrapPlan) -> Sequence[BootstrapAction]:
        planned = self._planned_bindings(plan)
        actions: list[BootstrapAction] = []
        for subject in planned.subjects:
            actions.append(BootstrapAction(action_id=f"azure-fic-{subject.rsplit(':',1)[-1]}", phase="azure", stage="planned", kind="federated-credential", diagnostics=(f"subject={subject}", f"issuer={_ACTIONS_ISSUER}", f"audience={_ACTIONS_AUDIENCE}")))
        for role in planned.roles:
            actions.append(BootstrapAction(action_id=f"azure-rbac-{role.role_key}", phase="azure", stage="planned", kind="role-assignment", diagnostics=(f"scope={role.scope}", f"role={role.role_key}", f"role_definition_id={role.role_definition_id}", f"role_assignment_id={role.assignment_id}", f"approved_role_sha256={role.fingerprint}")))
        if planned.identity.kind == "user_assigned_managed_identity" and not planned.identity.adopted:
            actions.append(BootstrapAction(action_id="azure-uami-create", phase="azure", stage="planned", kind="managed-identity", diagnostics=(f"resource_id={planned.identity.resource_id}", f"location={planned.identity.location}")))
        return tuple(actions)

    def apply_bindings(self, plan: BootstrapPlan) -> BootstrapReceipt:
        planned = self._planned_bindings(plan)
        before = (_fingerprint("azure-plan", {"subjects": planned.subjects, "roles": [role.__dict__ for role in planned.roles], "identity": planned.identity.__dict__}),)
        created: list[str] = []
        adopted: list[str] = []
        compensation: list[str] = []
        try:
            identity = self._resolve_identity(planned.identity)
            if identity.kind == "user_assigned_managed_identity" and not identity.adopted:
                identity = self._create_uami(identity)
                created.append("azure-uami-create")
                compensation.append("azure-uami-create")
            for subject in planned.subjects:
                disposition = self._apply_fic(identity, subject)
                action_id = f"azure-fic-{subject.rsplit(':',1)[-1]}"
                (created if disposition == "created" else adopted).append(action_id)
                if disposition == "created":
                    compensation.append(action_id)
            for role in planned.roles:
                disposition = self._apply_role_assignment(identity, role)
                action_id = f"azure-rbac-{role.role_key}-{role.assignment_id}"
                (created if disposition == "created" else adopted).append(action_id)
                if disposition == "created":
                    compensation.append(action_id)
            self._verify_read_only(identity, planned)
            after = (_fingerprint("azure-live", {"client_id": identity.client_id, "principal_id": identity.principal_id, "tenant_id": identity.tenant_id}),)
            return BootstrapReceipt.create(operation_id=plan.operation_id, runtime_repository=plan.runtime_repository, runtime_commit=plan.runtime_commit, repository_identity=plan.repository_identity, plan_hash=plan.plan_hash, before_fingerprints=before, after_fingerprints=after, created_actions=tuple(created), adopted_actions=tuple(adopted))
        except AzureProviderError as exc:
            return BootstrapReceipt.create(operation_id=plan.operation_id, runtime_repository=plan.runtime_repository, runtime_commit=plan.runtime_commit, repository_identity=plan.repository_identity, plan_hash=plan.plan_hash, before_fingerprints=before, created_actions=tuple(created), adopted_actions=tuple(adopted), compensation_required_actions=tuple(reversed(compensation)), error_info=RedactedStatusInfo(code="azure_apply_failed", summary=str(exc)), resume_info=RedactedStatusInfo(code="azure_compensation_state", summary="Rollback intended/created UAMI, FIC, and RBAC in reverse order."))

    def verify_bindings(self, receipt: BootstrapReceipt) -> bool:
        return bool(receipt.after_fingerprints) and receipt.error_info is None

    def rollback_bindings(self, receipt: BootstrapReceipt) -> None:
        return None

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
            scope = _canonical_scope(_text(data.get("scope"), field="scope"), identity.subscription_id)
            role_definition_id = _canonical_role_definition_id(_text(self._approved_role_definitions.get(role_key), field=f"approved_role_definitions.{role_key}"), identity.subscription_id)
            fingerprint = hashlib.sha256(_canonical_bytes({"scope": scope.lower(), "role_definition_id": role_definition_id})).hexdigest()
            roles.append(PlannedRoleAssignment(role_key=role_key, scope=scope, role_definition_id=role_definition_id, assignment_id="", fingerprint=fingerprint))
        return PlannedBindingSet(identity=identity, roles=tuple(roles), subjects=_action_subjects(plan.repository_identity))

    def _resolve_identity(self, identity: AzureIdentityReference) -> AzureIdentityReference:
        if identity.kind == "user_assigned_managed_identity":
            return self._get_uami(identity.resource_id or "") if identity.adopted else identity
        application = self._request("GET", f"{_GRAPH_APPLICATIONS}/{identity.object_id}", scope=_GRAPH_SCOPE)
        app_id = _text(application.get("appId"), field="application.appId")
        service_principals = self._request("GET", _GRAPH_SP_API, scope=_GRAPH_SCOPE, params={"$filter": f"appId eq '{app_id}'"})
        values = service_principals.get("value")
        if not isinstance(values, list) or len(values) != 1:
            raise AzureProviderError("corresponding service principal could not be resolved uniquely")
        sp = values[0]
        if not isinstance(sp, Mapping):
            raise AzureProviderError("service principal payload was malformed")
        return AzureIdentityReference(kind="entra_application", client_id=app_id, resource_id=None, object_id=_text(application.get("id"), field="application.id"), principal_id=_text(sp.get("id"), field="servicePrincipal.id"), tenant_id=_text(sp.get("appOwnerOrganizationId"), field="servicePrincipal.appOwnerOrganizationId"), subscription_id=identity.subscription_id, name=_text(application.get("displayName"), field="application.displayName"), adopted=True)

    def _get_uami(self, resource_id: str) -> AzureIdentityReference:
        payload = self._request("GET", f"https://management.azure.com{_canonical_resource_id(resource_id)}", scope=_ARM_SCOPE, params={"api-version": _MANAGED_IDENTITY_API_VERSION})
        props = payload["properties"]
        assert isinstance(props, Mapping)
        rid = _canonical_resource_id(_text(payload.get("id"), field="identity.id"))
        return AzureIdentityReference(kind="user_assigned_managed_identity", client_id=_text(props.get("clientId"), field="identity.clientId"), resource_id=rid, object_id=None, principal_id=_text(props.get("principalId"), field="identity.principalId"), tenant_id=_text(props.get("tenantId"), field="identity.tenantId"), subscription_id=rid.split("/")[2], name=_text(payload.get("name"), field="identity.name"), adopted=True, location=_optional_text(payload.get("location")))

    def _create_uami(self, identity: AzureIdentityReference) -> AzureIdentityReference:
        response = self._response("PUT", f"https://management.azure.com{identity.resource_id}", scope=_ARM_SCOPE, params={"api-version": _MANAGED_IDENTITY_API_VERSION}, json_body={"location": identity.location})
        if response.status_code not in {200, 201}:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")
        return self._get_uami(identity.resource_id or "")

    def _fic_url(self, identity: AzureIdentityReference, subject: str) -> tuple[str, str]:
        name = _fic_name(subject)
        if identity.kind == "user_assigned_managed_identity":
            return (f"https://management.azure.com{identity.resource_id}/federatedIdentityCredentials/{name}", _ARM_SCOPE)
        return (f"{_GRAPH_APPLICATIONS}/{identity.object_id}/federatedIdentityCredentials/{name}", _GRAPH_SCOPE)

    def _apply_fic(self, identity: AzureIdentityReference, subject: str) -> str:
        url, scope = self._fic_url(identity, subject)
        get_response = self._response("GET", url, scope=scope, params={"api-version": _FIC_API_VERSION} if scope == _ARM_SCOPE else None)
        if get_response.status_code == 200:
            return "adopted"
        if get_response.status_code != 404:
            raise AzureProviderError(f"Azure request failed with HTTP {get_response.status_code}")
        payload = {"properties": {"issuer": _ACTIONS_ISSUER, "subject": subject, "audiences": [_ACTIONS_AUDIENCE]}} if scope == _ARM_SCOPE else {"name": _fic_name(subject), "issuer": _ACTIONS_ISSUER, "subject": subject, "audiences": [_ACTIONS_AUDIENCE]}
        response = self._response("PUT" if scope == _ARM_SCOPE else "POST", url if scope == _ARM_SCOPE else f"{_GRAPH_APPLICATIONS}/{identity.object_id}/federatedIdentityCredentials", scope=scope, params={"api-version": _FIC_API_VERSION} if scope == _ARM_SCOPE else None, json_body=payload)
        if response.status_code not in {200, 201}:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")
        return "created"

    def _apply_role_assignment(self, identity: AzureIdentityReference, role: PlannedRoleAssignment) -> str:
        principal_id = _text(identity.principal_id, field="identity.principal_id")
        assignment_id = role.assignment_id if role.assignment_id else _role_assignment_id(role.scope, principal_id, role.role_definition_id)
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
            return "adopted"
        if get_response.status_code != 404:
            raise AzureProviderError(f"Azure request failed with HTTP {get_response.status_code}")
        response = self._response("PUT", url, scope=_ARM_SCOPE, params={"api-version": _AUTHZ_API_VERSION}, json_body={"properties": {"principalId": principal_id, "roleDefinitionId": role.role_definition_id, "principalType": "ServicePrincipal"}})
        if response.status_code == 403:
            raise AzureProviderError("executor is missing Microsoft.Authorization/roleAssignments/write; compensation may be required for intended Azure resources")
        if response.status_code not in {200, 201}:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")
        return "created"

    def _verify_read_only(self, identity: AzureIdentityReference, planned: PlannedBindingSet) -> None:
        live = self._get_uami(identity.resource_id or "") if identity.kind == "user_assigned_managed_identity" else self._resolve_identity(identity)
        if live.principal_id != identity.principal_id or live.client_id != identity.client_id:
            raise AzureProviderError("live identity drifted during verification")
        for subject in planned.subjects:
            url, scope = self._fic_url(live, subject)
            body = self._request("GET", url, scope=scope, params={"api-version": _FIC_API_VERSION} if live.kind == "user_assigned_managed_identity" else None)
            props = body.get("properties", body)
            assert isinstance(props, Mapping)
            if props.get("issuer") != _ACTIONS_ISSUER or props.get("subject") != subject or props.get("audiences") != [_ACTIONS_AUDIENCE]:
                raise AzureProviderError("federated credential verification drifted from exact claims")
        for role in planned.roles:
            principal_id = _text(live.principal_id, field="identity.principal_id")
            assignment_id = _role_assignment_id(role.scope, principal_id, role.role_definition_id)
            body = self._request("GET", f"https://management.azure.com{role.scope}/{_ROLE_ASSIGNMENTS_SEGMENT}/{assignment_id}", scope=_ARM_SCOPE, params={"api-version": _AUTHZ_API_VERSION})
            props = body["properties"]
            assert isinstance(props, Mapping)
            if _text(props.get("principalId"), field="principalId").lower() != principal_id.lower():
                raise AzureProviderError("role assignment verification principalId mismatch")
            if _canonical_role_definition_id(_text(props.get("roleDefinitionId"), field="roleDefinitionId"), live.subscription_id or role.scope.split("/")[2]) != role.role_definition_id:
                raise AzureProviderError("role assignment verification roleDefinitionId mismatch")


__all__ = ["AzureArmRestProvider", "AzureIdentityReference", "AzureProviderError"]
