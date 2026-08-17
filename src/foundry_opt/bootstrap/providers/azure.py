from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

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
_OWNER_ROLE_GUID = "8e3af657-a8ff-443c-a75c-2fe8c4bcb635"
_CONTRIBUTOR_ROLE_GUID = "b24988ac-6180-42a0-ab88-20f7382dd24c"


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


def _action_subjects(repository_identity: str) -> tuple[str, str]:
    return (
        f"repo:{repository_identity}:environment:{_COPILOT_ENV}",
        f"repo:{repository_identity}:environment:{_PRODUCTION_ENV}",
    )


def _require_mapping(value: object, *, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise AzureProviderError(f"{field} must be an object")
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


def _canonical_resource_id(value: str) -> str:
    text = value.strip()
    if not text.startswith("/"):
        text = "/" + text
    return "/" + "/".join(segment for segment in text.split("/") if segment)


def _canonical_role_definition_id(value: str, subscription_id: str) -> str:
    raw = value.strip()
    if raw.startswith("/providers/"):
        raw = f"/subscriptions/{subscription_id}{raw}"
    canonical = _canonical_resource_id(raw)
    if "/providers/microsoft.authorization/roledefinitions/" not in canonical.lower():
        raise AzureProviderError("role definition id must point to Microsoft.Authorization/roleDefinitions")
    return canonical.lower()


def _role_guid(role_definition_id: str) -> str:
    return role_definition_id.rsplit("/", 1)[-1]


def _role_assignment_id(scope: str, principal_id: str, role_definition_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{scope}|{principal_id}|{role_definition_id}"))


def _diagnostics_map(action: BootstrapAction) -> dict[str, str]:
    return {entry.split("=", 1)[0]: entry.split("=", 1)[1] for entry in action.diagnostics if "=" in entry}


def _fic_name(subject: str) -> str:
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()[:24]


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
        self._last_verified_identity: AzureIdentityReference | None = None

    def close(self) -> None:
        self._http.close()

    def _token(self, scope: str) -> str:
        value = self._token_provider(scope)
        token = value.token if isinstance(value, AzureToken) else value
        if not isinstance(token, str) or not token:
            raise AzureProviderError("token provider returned an invalid token")
        return token

    def _request(
        self,
        method: str,
        url: str,
        *,
        scope: str,
        params: Mapping[str, object] | None = None,
        json_body: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self._token(scope)}", "User-Agent": "foundry-opt/azure-provider"}
        try:
            response = self._http.request(method, url, params=params, json=json_body, headers=headers)
        except httpx.TimeoutException as exc:
            raise AzureProviderError("Azure request timed out") from exc
        except httpx.TransportError as exc:
            raise AzureProviderError("Azure transport failed") from exc
        return _json_response(response)

    def _response(
        self,
        method: str,
        url: str,
        *,
        scope: str,
        params: Mapping[str, object] | None = None,
        json_body: Mapping[str, object] | None = None,
    ) -> httpx.Response:
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self._token(scope)}", "User-Agent": "foundry-opt/azure-provider"}
        try:
            response = self._http.request(method, url, params=params, json=json_body, headers=headers)
        except httpx.TimeoutException as exc:
            raise AzureProviderError("Azure request timed out") from exc
        except httpx.TransportError as exc:
            raise AzureProviderError("Azure transport failed") from exc
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
        planned = self._planned_bindings(plan)
        actions: list[BootstrapAction] = []
        for subject in planned.subjects:
            actions.append(
                BootstrapAction(
                    action_id=f"azure-fic-{planned.identity.client_id[:8]}-{subject.rsplit(':',1)[-1]}",
                    phase="azure",
                    stage="planned",
                    kind="federated-credential",
                    diagnostics=(
                        f"subject={subject}",
                        f"issuer={_ACTIONS_ISSUER}",
                        f"audience={_ACTIONS_AUDIENCE}",
                        f"client_id={planned.identity.client_id}",
                    ),
                )
            )
        for role in planned.roles:
            actions.append(
                BootstrapAction(
                    action_id=f"azure-rbac-{role.role_key}-{role.assignment_id}",
                    phase="azure",
                    stage="planned",
                    kind="role-assignment",
                    diagnostics=(
                        f"scope={role.scope}",
                        f"role={role.role_key}",
                        f"role_definition_id={role.role_definition_id}",
                        f"role_assignment_id={role.assignment_id}",
                        f"approved_role_sha256={role.fingerprint}",
                        f"client_id={planned.identity.client_id}",
                    ),
                )
            )
        if planned.identity.kind == "user_assigned_managed_identity" and not planned.identity.adopted:
            actions.append(
                BootstrapAction(
                    action_id="azure-uami-create",
                    phase="azure",
                    stage="planned",
                    kind="managed-identity",
                    diagnostics=(
                        f"resource_id={planned.identity.resource_id}",
                        f"location={planned.identity.location}",
                    ),
                )
            )
        return tuple(actions)

    def apply_bindings(self, plan: BootstrapPlan) -> BootstrapReceipt:
        identity_hint = self._select_identity(plan)
        planned = self._planned_bindings(plan)
        before = self._capture_state(plan, planned)
        created_actions: list[str] = []
        adopted_actions: list[str] = []
        changed_actions: list[str] = []
        compensation: list[str] = []
        identity_for_rollback: AzureIdentityReference | None = None
        try:
            identity = self._resolve_live_identity(identity_hint)
            self._assert_identity_matches_plan(planned.identity, identity)
            identity_for_rollback = identity
            if identity.kind == "user_assigned_managed_identity" and not identity.adopted:
                identity = self._create_or_get_uami(identity)
                self._assert_identity_matches_plan(planned.identity, identity)
                created_actions.append("azure-uami-create")
                compensation.append("azure-uami-create")
            elif identity.kind == "user_assigned_managed_identity":
                identity = self._get_uami(identity.resource_id or "")
            else:
                identity = self._get_application(identity.object_id or "")
            self._assert_identity_matches_plan(planned.identity, identity)
            identity_for_rollback = identity
            for subject in planned.subjects:
                action_id = f"azure-fic-{identity.client_id[:8]}-{subject.rsplit(':',1)[-1]}"
                disposition = self._apply_fic(identity, subject)
                if disposition == "created":
                    created_actions.append(action_id)
                    compensation.append(action_id)
                elif disposition == "changed":
                    changed_actions.append(action_id)
                    compensation.append(action_id)
                else:
                    adopted_actions.append(action_id)
            for role in planned.roles:
                action_id, disposition = self._apply_role_assignment(identity, role)
                if disposition == "created":
                    created_actions.append(action_id)
                    compensation.append(action_id)
                elif disposition == "changed":
                    changed_actions.append(action_id)
                    compensation.append(action_id)
                else:
                    adopted_actions.append(action_id)
            after = self._capture_state(plan, PlannedBindingSet(identity=identity, roles=planned.roles, subjects=planned.subjects))
            self._verify_read_only(plan, PlannedBindingSet(identity=identity, roles=planned.roles, subjects=planned.subjects))
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
                changed_actions=tuple(changed_actions),
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
                changed_actions=tuple(changed_actions),
                compensation_required_actions=tuple(compensation),
                error_info=RedactedStatusInfo(code="azure_apply_failed", summary=str(exc)),
                resume_info=RedactedStatusInfo(code="azure_compensation_state", summary="Rollback created UAMI/FIC/RBAC and revert operation-changed Azure resources only."),
            )

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
                return AzureIdentityReference(
                    kind="user_assigned_managed_identity",
                    client_id=_text(data.get("client_id"), field="client_id"),
                    resource_id=_canonical_resource_id(_text(data.get("resource_id"), field="resource_id")),
                    object_id=_optional_text(data.get("object_id")),
                    principal_id=_optional_text(data.get("principal_id")),
                    tenant_id=_optional_text(data.get("tenant_id")),
                    subscription_id=_optional_text(data.get("subscription_id")),
                    name=_text(data.get("name", "shared"), field="name"),
                    adopted=data.get("adopted", "false") == "true",
                    location=_optional_text(data.get("location")) or "eastus",
                )
            if action.kind == "entra-application":
                return AzureIdentityReference(
                    kind="entra_application",
                    client_id=_text(data.get("client_id"), field="client_id"),
                    resource_id=None,
                    object_id=_optional_text(data.get("object_id")),
                    principal_id=_optional_text(data.get("service_principal_id")),
                    tenant_id=_optional_text(data.get("tenant_id")),
                    subscription_id=_optional_text(data.get("subscription_id")),
                    name=_text(data.get("name", "shared"), field="name"),
                    adopted=True,
                )
        raise AzureProviderError("plan does not contain an Azure identity action")

    def _planned_bindings(self, plan: BootstrapPlan) -> PlannedBindingSet:
        identity = self._select_identity(plan)
        if not identity.subscription_id:
            raise AzureProviderError("identity must include subscription_id")
        roles = tuple(self._selected_role_scopes(plan, identity.subscription_id, identity.principal_id or identity.object_id or identity.client_id))
        return PlannedBindingSet(identity=identity, roles=roles, subjects=_action_subjects(plan.repository_identity))

    def _selected_role_scopes(self, plan: BootstrapPlan, subscription_id: str, principal_id: str) -> Iterable[PlannedRoleAssignment]:
        seen: set[str] = set()
        subscription_prefix = f"/subscriptions/{subscription_id}".lower()
        for action in plan.actions:
            if action.phase != "azure" or action.kind != "role-assignment":
                continue
            data = _diagnostics_map(action)
            role_key = _text(data.get("role"), field="role")
            scope = _canonical_resource_id(_text(data.get("scope"), field="scope"))
            if scope.lower().startswith("/providers/microsoft.management/managementgroups/"):
                raise AzureProviderError("management-group scopes are not allowed")
            if not scope.startswith(subscription_prefix):
                raise AzureProviderError("cross-subscription scopes are not allowed")
            if scope == subscription_prefix:
                raise AzureProviderError("subscription-wide role assignment scopes are not allowed")
            role_definition_id = _canonical_role_definition_id(self._approved_role_definition(role_key), subscription_id)
            role_guid = _role_guid(role_definition_id)
            if role_guid in {_OWNER_ROLE_GUID, _CONTRIBUTOR_ROLE_GUID}:
                raise AzureProviderError("Owner and Contributor role definitions are not allowed")
            assignment_id = _role_assignment_id(scope, principal_id, role_definition_id)
            fingerprint = hashlib.sha256(_canonical_bytes({"role_definition_id": role_definition_id, "scope": scope})).hexdigest()
            token = f"{role_key}|{scope}|{role_definition_id}"
            if token in seen:
                continue
            seen.add(token)
            yield PlannedRoleAssignment(role_key=role_key, scope=scope, role_definition_id=role_definition_id, assignment_id=assignment_id, fingerprint=fingerprint)

    def _approved_role_definition(self, role_key: str) -> str:
        role_definition_id = self._approved_role_definitions.get(role_key)
        if not role_definition_id:
            raise AzureProviderError(f"role {role_key!r} is not in the approved role-definition map")
        return role_definition_id

    def _capture_state(self, plan: BootstrapPlan, planned: PlannedBindingSet) -> tuple[FingerprintRecord, ...]:
        payload = {
            "repository": plan.repository_identity,
            "identity": {
                "kind": planned.identity.kind,
                "client_id": planned.identity.client_id,
                "resource_id": planned.identity.resource_id,
                "principal_id": planned.identity.principal_id,
                "tenant_id": planned.identity.tenant_id,
            },
            "subjects": planned.subjects,
            "roles": [{"scope": role.scope, "role_definition_id": role.role_definition_id, "assignment_id": role.assignment_id} for role in planned.roles],
        }
        return (_fingerprint("azure-bindings", payload),)

    def _assert_identity_matches_plan(self, planned: AzureIdentityReference, actual: AzureIdentityReference) -> None:
        if planned.kind != actual.kind:
            raise AzureProviderError("live Azure identity kind did not match the planned identity")
        if planned.client_id != actual.client_id:
            raise AzureProviderError("live Azure identity client_id did not match the planned identity")
        if planned.tenant_id and actual.tenant_id and planned.tenant_id != actual.tenant_id:
            raise AzureProviderError("live Azure identity tenant_id did not match the planned identity")
        if planned.resource_id and actual.resource_id and _canonical_resource_id(planned.resource_id) != _canonical_resource_id(actual.resource_id):
            raise AzureProviderError("live Azure identity resource_id did not match the planned identity")

    def _resolve_live_identity(self, identity: AzureIdentityReference) -> AzureIdentityReference:
        if identity.kind == "user_assigned_managed_identity":
            if identity.adopted:
                return self._get_uami(identity.resource_id or "")
            return identity
        return self._get_application(identity.object_id or "")

    def _get_uami(self, resource_id: str) -> AzureIdentityReference:
        response = self._request("GET", f"https://management.azure.com{_canonical_resource_id(resource_id)}", scope=_ARM_SCOPE, params={"api-version": _MANAGED_IDENTITY_API_VERSION})
        props = _require_mapping(response.get("properties"), field="identity.properties")
        resource_id_live = _canonical_resource_id(_text(response.get("id"), field="identity.id"))
        subscription_id = resource_id_live.split("/")[2]
        return AzureIdentityReference(
            kind="user_assigned_managed_identity",
            client_id=_text(props.get("clientId"), field="identity.clientId"),
            resource_id=resource_id_live,
            object_id=_optional_text(props.get("principalId")),
            principal_id=_optional_text(props.get("principalId")),
            tenant_id=_optional_text(props.get("tenantId")),
            subscription_id=subscription_id,
            name=_text(response.get("name"), field="identity.name"),
            adopted=True,
            location=_optional_text(response.get("location")),
        )

    def _create_or_get_uami(self, identity: AzureIdentityReference) -> AzureIdentityReference:
        assert identity.resource_id is not None
        url = f"https://management.azure.com{identity.resource_id}"
        response = self._response("PUT", url, scope=_ARM_SCOPE, params={"api-version": _MANAGED_IDENTITY_API_VERSION}, json_body={"location": identity.location})
        if response.status_code not in {200, 201}:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")
        body = _json_response(response)
        props = _require_mapping(body.get("properties"), field="identity.properties")
        return AzureIdentityReference(
            kind="user_assigned_managed_identity",
            client_id=_text(props.get("clientId"), field="identity.clientId"),
            resource_id=_canonical_resource_id(_text(body.get("id"), field="identity.id")),
            object_id=_optional_text(props.get("principalId")),
            principal_id=_optional_text(props.get("principalId")),
            tenant_id=_optional_text(props.get("tenantId")),
            subscription_id=_canonical_resource_id(_text(body.get("id"), field="identity.id")).split("/")[2],
            name=_text(body.get("name"), field="identity.name"),
            adopted=False,
            location=_optional_text(body.get("location")),
        )

    def _get_application(self, object_id: str) -> AzureIdentityReference:
        body = self._request("GET", f"{_GRAPH_APPLICATIONS}/{object_id}", scope=_GRAPH_SCOPE)
        app_id = _text(body.get("appId"), field="application.appId")
        return AzureIdentityReference(
            kind="entra_application",
            client_id=app_id,
            resource_id=None,
            object_id=_text(body.get("id"), field="application.id"),
            principal_id=_optional_text(body.get("appId")),
            tenant_id=_optional_text(body.get("publisherDomain")),
            subscription_id=None,
            name=_text(body.get("displayName"), field="application.displayName"),
            adopted=True,
        )

    def _fic_url(self, identity: AzureIdentityReference, name: str) -> tuple[str, str]:
        if identity.kind == "user_assigned_managed_identity":
            assert identity.resource_id is not None
            return (f"https://management.azure.com{identity.resource_id}/federatedIdentityCredentials/{name}", _ARM_SCOPE)
        if not identity.object_id:
            raise AzureProviderError("Entra application identity is missing object_id")
        return (f"{_GRAPH_APPLICATIONS}/{identity.object_id}/federatedIdentityCredentials/{name}", _GRAPH_SCOPE)

    def _read_fic(self, identity: AzureIdentityReference, subject: str) -> Mapping[str, object] | None:
        name = _fic_name(subject)
        url, scope = self._fic_url(identity, name)
        response = self._response("GET", url, scope=scope, params={"api-version": _FIC_API_VERSION} if scope == _ARM_SCOPE else None)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")
        return _json_response(response)

    def _apply_fic(self, identity: AzureIdentityReference, subject: str) -> str:
        existing = self._read_fic(identity, subject)
        name = _fic_name(subject)
        expected = {"issuer": _ACTIONS_ISSUER, "subject": subject, "audiences": [_ACTIONS_AUDIENCE]}
        if existing is not None:
            props = _require_mapping(existing.get("properties", existing), field="federatedCredential")
            if props.get("issuer") == _ACTIONS_ISSUER and props.get("subject") == subject and props.get("audiences") == [_ACTIONS_AUDIENCE]:
                return "adopted"
        url, scope = self._fic_url(identity, name)
        payload = {"name": name, **expected} if scope == _GRAPH_SCOPE else {"properties": expected}
        response = self._response("PUT" if scope == _ARM_SCOPE else "POST", url if scope == _ARM_SCOPE else f"{_GRAPH_APPLICATIONS}/{identity.object_id}/federatedIdentityCredentials", scope=scope, params={"api-version": _FIC_API_VERSION} if scope == _ARM_SCOPE else None, json_body=payload)
        if response.status_code not in {200, 201}:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")
        body = _json_response(response)
        props = _require_mapping(body.get("properties", body), field="federatedCredential")
        if props.get("issuer") != _ACTIONS_ISSUER or props.get("subject") != subject or props.get("audiences") != [_ACTIONS_AUDIENCE]:
            raise AzureProviderError("federated credential claims drifted from the required exact issuer/audience/subject")
        return "created" if existing is None else "changed"

    def _read_role_assignment(self, role: PlannedRoleAssignment) -> Mapping[str, object] | None:
        url = f"https://management.azure.com{role.scope}/{_ROLE_ASSIGNMENTS_SEGMENT}/{role.assignment_id}"
        response = self._response("GET", url, scope=_ARM_SCOPE, params={"api-version": _AUTHZ_API_VERSION})
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")
        return _json_response(response)

    def _apply_role_assignment(self, identity: AzureIdentityReference, role: PlannedRoleAssignment) -> tuple[str, str]:
        principal_id = identity.principal_id or identity.object_id
        if not principal_id:
            raise AzureProviderError("identity is missing a principal/object id for RBAC")
        existing = self._read_role_assignment(role)
        action_id = f"azure-rbac-{role.role_key}-{role.assignment_id}"
        if existing is not None:
            props = _require_mapping(existing.get("properties"), field="roleAssignment.properties")
            existing_role = _canonical_role_definition_id(_text(props.get("roleDefinitionId"), field="roleAssignment.roleDefinitionId"), identity.subscription_id or role.scope.split("/")[2])
            if existing_role == role.role_definition_id and _text(props.get("principalId"), field="roleAssignment.principalId").lower() == principal_id.lower():
                return action_id, "adopted"
        payload = {"properties": {"principalId": principal_id, "roleDefinitionId": role.role_definition_id, "principalType": "ServicePrincipal"}}
        url = f"https://management.azure.com{role.scope}/{_ROLE_ASSIGNMENTS_SEGMENT}/{role.assignment_id}"
        response = self._response("PUT", url, scope=_ARM_SCOPE, params={"api-version": _AUTHZ_API_VERSION}, json_body=payload)
        if response.status_code == 403:
            raise AzureProviderError("executor is missing Microsoft.Authorization/roleAssignments/write; compensation may be required for previously created Azure resources")
        if response.status_code not in {200, 201}:
            raise AzureProviderError(f"Azure request failed with HTTP {response.status_code}")
        body = _json_response(response)
        props = _require_mapping(body.get("properties"), field="roleAssignment.properties")
        actual_role = _canonical_role_definition_id(_text(props.get("roleDefinitionId"), field="roleAssignment.roleDefinitionId"), identity.subscription_id or role.scope.split("/")[2])
        if actual_role != role.role_definition_id:
            raise AzureProviderError("role assignment roleDefinitionId drifted from the planned approved role")
        return action_id, "created" if existing is None else "changed"

    def _verify_read_only(self, plan: BootstrapPlan, planned: PlannedBindingSet) -> None:
        if planned.identity.kind == "user_assigned_managed_identity":
            identity = self._get_uami(planned.identity.resource_id or "")
        else:
            identity = self._get_application(planned.identity.object_id or "")
        self._assert_identity_matches_plan(planned.identity, identity)
        for subject in planned.subjects:
            fic = self._read_fic(identity, subject)
            if fic is None:
                raise AzureProviderError("federated credential verification drift: credential missing")
            props = _require_mapping(fic.get("properties", fic), field="federatedCredential")
            if props.get("issuer") != _ACTIONS_ISSUER or props.get("subject") != subject or props.get("audiences") != [_ACTIONS_AUDIENCE]:
                raise AzureProviderError("federated credential verification drifted from exact claims")
        for role in planned.roles:
            assignment = self._read_role_assignment(role)
            if assignment is None:
                raise AzureProviderError("role assignment verification drift: assignment missing")
            props = _require_mapping(assignment.get("properties"), field="roleAssignment.properties")
            actual_role = _canonical_role_definition_id(_text(props.get("roleDefinitionId"), field="roleAssignment.roleDefinitionId"), planned.identity.subscription_id or role.scope.split("/")[2])
            if actual_role != role.role_definition_id:
                raise AzureProviderError("role assignment verification drifted from planned role definition")


__all__ = ["AzureArmRestProvider", "AzureIdentityReference", "AzureProviderError", "AzureToken"]
