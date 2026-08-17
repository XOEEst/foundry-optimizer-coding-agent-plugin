from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

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
_SUB_API_VERSION = "2022-12-01"
_TENANT_DISCOVERY = "https://management.azure.com/tenants"
_SUB_DISCOVERY = "https://management.azure.com/subscriptions"
_GRAPH_APPLICATIONS = "https://graph.microsoft.com/v1.0/applications"
_ROLE_ASSIGNMENTS_SEGMENT = "providers/Microsoft.Authorization/roleAssignments"
_UAMI_SEGMENT = "providers/Microsoft.ManagedIdentity/userAssignedIdentities"
_APP_FIC_SEGMENT = "federatedIdentityCredentials"
_MSI_FIC_SEGMENT = "providers/Microsoft.ManagedIdentity/userAssignedIdentities"


class AzureProviderError(BootstrapProviderError):
    pass


@dataclass(frozen=True)
class AzureToken:
    token: str
    scope: str


@dataclass(frozen=True)
class AzureIdentityReference:
    kind: str
    client_id: str
    resource_id: str | None
    object_id: str | None
    principal_id: str | None
    tenant_id: str | None
    subscription_id: str | None
    name: str
    adopted: bool


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _fingerprint(label: str, value: object) -> FingerprintRecord:
    return FingerprintRecord(label=label, sha256=hashlib.sha256(_canonical_bytes(value)).hexdigest())


def _action_subjects(repository_identity: str) -> tuple[str, str]:
    return (
        f"repo:{repository_identity}:environment:{_COPILOT_ENV}",
        f"repo:{repository_identity}:environment:{_PRODUCTION_ENV}",
    )


def _require_mapping(value: object, *, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AzureProviderError(f"{field} must be an object")
    return value


def _require_sequence(value: object, *, field: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise AzureProviderError(f"{field} must be an array")
    return value


def _text(value: object, *, field: str, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value):
        raise AzureProviderError(f"{field} must be a non-empty string")
    return value


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _json_response(response: httpx.Response) -> Mapping[str, object]:
    if 300 <= response.status_code < 400:
        raise AzureProviderError("redirect responses are not allowed")
    if response.status_code == 403:
        raise AzureProviderError("Azure denied the request with HTTP 403")
    body = response.content
    if len(body) > _MAX_RESPONSE_BYTES:
        raise AzureProviderError("Azure response exceeded configured limit")
    if response.status_code >= 400:
        try:
            payload = response.json()
        except Exception:
            payload = {"status": response.status_code}
        raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}: {payload}")
    try:
        payload = response.json()
    except Exception as exc:
        raise AzureProviderError("Azure returned malformed JSON") from exc
    if not isinstance(payload, Mapping):
        raise AzureProviderError("Azure returned a non-object JSON document")
    return payload


def _role_assignment_id(scope: str, principal_id: str, role_definition_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scope}|{principal_id}|{role_definition_id}"))


class AzureArmRestProvider:
    def __init__(
        self,
        *,
        token_provider: Callable[[str], str] | Callable[[str], AzureToken],
        transport: httpx.BaseTransport | None = None,
        timeout: float = _REQUEST_TIMEOUT,
        approved_role_definitions: Mapping[str, str] | None = None,
    ) -> None:
        self._token_provider = token_provider
        self._approved_role_definitions = dict(approved_role_definitions or {})
        self._http = httpx.Client(transport=transport, timeout=timeout, follow_redirects=False, trust_env=False)

    def close(self) -> None:
        self._http.close()

    def _token(self, scope: str) -> str:
        value = self._token_provider(scope)
        token = value.token if isinstance(value, AzureToken) else value
        if not isinstance(token, str) or not token:
            raise AzureProviderError("token provider returned an invalid token")
        return token

    def _request(self, method: str, url: str, *, scope: str, params: Mapping[str, object] | None = None, json_body: Mapping[str, object] | None = None) -> Mapping[str, object]:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self._token(scope)}", "User-Agent": "foundry-opt/azure-provider"}
        try:
            response = self._http.request(method, url, params=params, json=json_body, headers=headers)
        except httpx.TimeoutException as exc:
            raise AzureProviderError("Azure request timed out") from exc
        except httpx.TransportError as exc:
            raise AzureProviderError("Azure transport failed") from exc
        return _json_response(response)

    def _request_no_content(self, method: str, url: str, *, scope: str, params: Mapping[str, object] | None = None, json_body: Mapping[str, object] | None = None) -> httpx.Response:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self._token(scope)}", "User-Agent": "foundry-opt/azure-provider"}
        response = self._http.request(method, url, params=params, json=json_body, headers=headers)
        if 300 <= response.status_code < 400:
            raise AzureProviderError("redirect responses are not allowed")
        return response

    def inventory_identity(self) -> Mapping[str, object]:
        tenants = self._request("GET", _TENANT_DISCOVERY, scope=_ARM_SCOPE, params={"api-version": _SUB_API_VERSION})
        subscriptions = self._request("GET", _SUB_DISCOVERY, scope=_ARM_SCOPE, params={"api-version": _SUB_API_VERSION})
        return {"tenants": tenants.get("value", ()), "subscriptions": subscriptions.get("value", ())}

    def assess_bindings(self) -> Sequence[BindingAssessment]:
        return ()

    def plan_bindings(self, plan: BootstrapPlan) -> Sequence[BootstrapAction]:
        identity = self._select_identity(plan)
        actions: list[BootstrapAction] = []
        for subject in _action_subjects(plan.repository_identity):
            actions.append(BootstrapAction(action_id=f"azure-fic-{identity.client_id[:8]}-{subject.rsplit(':',1)[-1]}", phase="azure", stage="planned", kind="federated-credential", diagnostics=(f"subject={subject}", f"issuer={_ACTIONS_ISSUER}", f"audience={_ACTIONS_AUDIENCE}", f"client_id={identity.client_id}")))
        for role_key, scope in self._selected_role_scopes(plan):
            role_definition_id = self._approved_role_definition(role_key)
            actions.append(BootstrapAction(action_id=f"azure-rbac-{role_key}-{_role_assignment_id(scope, identity.principal_id or identity.object_id or identity.client_id, role_definition_id)}", phase="azure", stage="planned", kind="role-assignment", diagnostics=(f"scope={scope}", f"role={role_key}", f"role_definition_id={role_definition_id}", f"client_id={identity.client_id}")))
        if identity.kind == "user_assigned_managed_identity" and not identity.adopted:
            actions.append(BootstrapAction(action_id="azure-uami-create", phase="azure", stage="planned", kind="managed-identity", diagnostics=(f"resource_id={identity.resource_id}",)))
        return tuple(actions)

    def apply_bindings(self, plan: BootstrapPlan) -> BootstrapReceipt:
        identity = self._select_identity(plan)
        before = self._capture_state(plan, identity)
        created_actions: list[str] = []
        adopted_actions: list[str] = []
        compensation: list[str] = []
        try:
            if identity.kind == "user_assigned_managed_identity" and not identity.adopted and identity.resource_id is not None:
                self._ensure_uami(identity)
                created_actions.append("azure-uami-create")
            for subject in _action_subjects(plan.repository_identity):
                action_id = f"azure-fic-{identity.client_id[:8]}-{subject.rsplit(':',1)[-1]}"
                if self._ensure_fic(identity, subject):
                    created_actions.append(action_id)
                    compensation.append(action_id)
                else:
                    adopted_actions.append(action_id)
            for role_key, scope in self._selected_role_scopes(plan):
                action_id = self._ensure_role_assignment(identity, role_key, scope)
                if action_id.endswith(":created"):
                    created_actions.append(action_id.removesuffix(":created"))
                    compensation.append(action_id.removesuffix(":created"))
                else:
                    adopted_actions.append(action_id)
            after = self._capture_state(plan, identity)
            self._verify_exact(plan, identity)
            return BootstrapReceipt.create(
                operation_id=plan.operation_id,
                runtime_repository=plan.runtime_repository,
                runtime_commit=plan.runtime_commit,
                repository_identity=plan.repository_identity,
                plan_hash=plan.plan_hash,
                before_fingerprints=before,
                after_fingerprints=after,
                created_actions=tuple(created_actions),
                adopted_actions=tuple(adopted_actions),
            )
        except AzureProviderError as exc:
            return BootstrapReceipt.create(
                operation_id=plan.operation_id,
                runtime_repository=plan.runtime_repository,
                runtime_commit=plan.runtime_commit,
                repository_identity=plan.repository_identity,
                plan_hash=plan.plan_hash,
                before_fingerprints=before,
                after_fingerprints=(),
                created_actions=tuple(created_actions),
                adopted_actions=tuple(adopted_actions),
                compensation_required_actions=tuple(compensation),
                error_info=RedactedStatusInfo(code="azure_apply_failed", summary=str(exc)),
                resume_info=RedactedStatusInfo(code="azure_compensation_state", summary="Rollback only operation-created federated credentials, role assignments, and managed identity."),
            )

    def verify_bindings(self, receipt: BootstrapReceipt) -> bool:
        return bool(receipt.after_fingerprints) and receipt.error_info is None

    def rollback_bindings(self, receipt: BootstrapReceipt) -> None:
        return None

    def _select_identity(self, plan: BootstrapPlan) -> AzureIdentityReference:
        for action in plan.actions:
            if action.phase != "azure":
                continue
            data = {entry.split("=", 1)[0]: entry.split("=", 1)[1] for entry in action.diagnostics if "=" in entry}
            if action.kind == "managed-identity":
                return AzureIdentityReference(kind="user_assigned_managed_identity", client_id=_text(data.get("client_id"), field="client_id"), resource_id=_text(data.get("resource_id"), field="resource_id"), object_id=_optional_text(data.get("object_id")), principal_id=_optional_text(data.get("principal_id")), tenant_id=_optional_text(data.get("tenant_id")), subscription_id=_optional_text(data.get("subscription_id")), name=_text(data.get("name", "shared"), field="name"), adopted=data.get("adopted", "false") == "true")
            if action.kind == "entra-application":
                return AzureIdentityReference(kind="entra_application", client_id=_text(data.get("client_id"), field="client_id"), resource_id=None, object_id=_optional_text(data.get("object_id")), principal_id=_optional_text(data.get("service_principal_id")), tenant_id=_optional_text(data.get("tenant_id")), subscription_id=_optional_text(data.get("subscription_id")), name=_text(data.get("name", "shared"), field="name"), adopted=True)
        raise AzureProviderError("plan does not contain an Azure identity action")

    def _selected_role_scopes(self, plan: BootstrapPlan) -> Iterable[tuple[str, str]]:
        seen: set[str] = set()
        for action in plan.actions:
            if action.phase != "azure" or action.kind != "role-assignment":
                continue
            data = {entry.split("=", 1)[0]: entry.split("=", 1)[1] for entry in action.diagnostics if "=" in entry}
            role_key = _text(data.get("role"), field="role")
            scope = _text(data.get("scope"), field="scope")
            if scope.count("/") <= 2:
                raise AzureProviderError("subscription-wide role assignment scopes are not allowed")
            if role_key in {"Owner", "Contributor"}:
                raise AzureProviderError("Owner and Contributor assignments are not allowed")
            token = f"{role_key}|{scope}"
            if token not in seen:
                seen.add(token)
                yield role_key, scope

    def _approved_role_definition(self, role_key: str) -> str:
        role_definition_id = self._approved_role_definitions.get(role_key)
        if not role_definition_id:
            raise AzureProviderError(f"role {role_key!r} is not in the approved role-definition map")
        return role_definition_id

    def _capture_state(self, plan: BootstrapPlan, identity: AzureIdentityReference) -> tuple[FingerprintRecord, ...]:
        payload = {"repository": plan.repository_identity, "client_id": identity.client_id, "subjects": _action_subjects(plan.repository_identity), "roles": list(self._selected_role_scopes(plan))}
        return (_fingerprint("azure-bindings", payload),)

    def _fic_url(self, identity: AzureIdentityReference, name: str) -> tuple[str, str]:
        if identity.kind == "user_assigned_managed_identity":
            assert identity.resource_id is not None
            return (f"https://management.azure.com{identity.resource_id}/federatedIdentityCredentials/{name}", _ARM_SCOPE)
        if not identity.object_id:
            raise AzureProviderError("Entra application identity is missing object_id")
        return (f"{_GRAPH_APPLICATIONS}/{identity.object_id}/federatedIdentityCredentials/{name}", _GRAPH_SCOPE)

    def _ensure_fic(self, identity: AzureIdentityReference, subject: str) -> bool:
        name = hashlib.sha256(subject.encode("utf-8")).hexdigest()[:24]
        url, scope = self._fic_url(identity, name)
        payload = {"name": name, "issuer": _ACTIONS_ISSUER, "subject": subject, "audiences": [_ACTIONS_AUDIENCE]}
        if scope == _ARM_SCOPE:
            response = self._request_no_content("PUT", url, scope=scope, params={"api-version": _FIC_API_VERSION}, json_body=payload)
            if response.status_code in {200, 201}:
                body = _json_response(response)
                props = _require_mapping(body.get("properties", body), field="federatedCredential")
                if props.get("issuer") != _ACTIONS_ISSUER or props.get("subject") != subject or props.get("audiences") != [_ACTIONS_AUDIENCE]:
                    raise AzureProviderError("federated credential claims drifted from the required exact issuer/audience/subject")
                return response.status_code == 201
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")
        response = self._request_no_content("POST", f"{_GRAPH_APPLICATIONS}/{identity.object_id}/federatedIdentityCredentials", scope=scope, json_body=payload)
        if response.status_code == 409:
            existing = self._request("GET", url, scope=scope)
            if existing.get("issuer") != _ACTIONS_ISSUER or existing.get("subject") != subject or existing.get("audiences") != [_ACTIONS_AUDIENCE]:
                raise AzureProviderError("existing federated credential does not match the required exact claims")
            return False
        if response.status_code not in {201, 200}:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")
        created = _json_response(response)
        if created.get("issuer") != _ACTIONS_ISSUER or created.get("subject") != subject or created.get("audiences") != [_ACTIONS_AUDIENCE]:
            raise AzureProviderError("created federated credential does not match the required exact claims")
        return True

    def _ensure_role_assignment(self, identity: AzureIdentityReference, role_key: str, scope: str) -> str:
        principal_id = identity.principal_id or identity.object_id
        if not principal_id:
            raise AzureProviderError("identity is missing a principal/object id for RBAC")
        role_definition_id = self._approved_role_definition(role_key)
        assignment_id = _role_assignment_id(scope, principal_id, role_definition_id)
        url = f"https://management.azure.com{scope}/{_ROLE_ASSIGNMENTS_SEGMENT}/{assignment_id}"
        payload = {"properties": {"principalId": principal_id, "roleDefinitionId": role_definition_id, "principalType": "ServicePrincipal"}}
        response = self._request_no_content("PUT", url, scope=_ARM_SCOPE, params={"api-version": _AUTHZ_API_VERSION}, json_body=payload)
        if response.status_code == 403:
            raise AzureProviderError("executor is missing Microsoft.Authorization/roleAssignments/write; compensation may be required for previously created Azure resources")
        if response.status_code not in {200, 201}:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")
        body = _json_response(response)
        props = _require_mapping(body.get("properties"), field="roleAssignment.properties")
        if props.get("roleDefinitionId") != role_definition_id:
            raise AzureProviderError("role assignment roleDefinitionId drifted from the approved map")
        actual_scope = _text(body.get("id"), field="roleAssignment.id")
        if f"{_ROLE_ASSIGNMENTS_SEGMENT}/{assignment_id}".lower() not in actual_scope.lower():
            raise AzureProviderError("role assignment ID drifted from deterministic id")
        action_id = f"azure-rbac-{role_key}-{assignment_id}"
        return f"{action_id}:created" if response.status_code == 201 else action_id

    def _ensure_uami(self, identity: AzureIdentityReference) -> None:
        assert identity.resource_id is not None
        url = f"https://management.azure.com{identity.resource_id}"
        response = self._request_no_content("PUT", url, scope=_ARM_SCOPE, params={"api-version": _MANAGED_IDENTITY_API_VERSION}, json_body={"location": "eastus"})
        if response.status_code not in {200, 201}:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")

    def _verify_exact(self, plan: BootstrapPlan, identity: AzureIdentityReference) -> None:
        for subject in _action_subjects(plan.repository_identity):
            self._ensure_fic(identity, subject)


__all__ = ["AzureArmRestProvider", "AzureIdentityReference", "AzureProviderError", "AzureToken"]
