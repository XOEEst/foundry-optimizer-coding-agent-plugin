from __future__ import annotations

import hashlib

import httpx
import pytest

from foundry_opt.bootstrap.contracts import BootstrapAction, BootstrapPlan
from foundry_opt.bootstrap.providers.azure import AzureArmRestProvider, AzureProviderError
from tests.bootstrap.fakes import AzureTransportRecorder, json_body


SUB = "55555555-5555-5555-5555-555555555555"
TENANT = "44444444-4444-4444-4444-444444444444"
CLIENT = "11111111-1111-1111-1111-111111111111"
PRINCIPAL = "22222222-2222-2222-2222-222222222222"
UAMI_ID = f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/shared-uami"
UAMI_URL = f"https://management.azure.com{UAMI_ID}"
FOUNDRY_SCOPE = f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acct/projects/proj"
TELEMETRY_SCOPE = f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.Insights/components/appi"
FOUNDRY_ROLE = f"/subscriptions/{SUB}/providers/Microsoft.Authorization/roleDefinitions/00000000-0000-0000-0000-000000000111"
TELEMETRY_ROLE = f"/providers/Microsoft.Authorization/roleDefinitions/00000000-0000-0000-0000-000000000222"


def _plan(*actions: BootstrapAction) -> BootstrapPlan:
    return BootstrapPlan.create(
        operation_id="op-azure-1",
        runtime_repository="https://github.com/octo-org/octo-repo.git",
        runtime_commit="a" * 40,
        repository_identity="octo-org/octo-repo",
        actions=actions,
    )


def _uami_action(*, adopted: bool = False) -> BootstrapAction:
    return BootstrapAction(
        action_id="identity",
        phase="azure",
        stage="planned",
        kind="managed-identity",
        diagnostics=(
            f"client_id={CLIENT}",
            f"principal_id={PRINCIPAL}",
            "object_id=33333333-3333-3333-3333-333333333333",
            f"tenant_id={TENANT}",
            f"subscription_id={SUB}",
            f"resource_id={UAMI_ID}",
            "name=shared-uami",
            "location=eastus",
            f"adopted={'true' if adopted else 'false'}",
        ),
    )


def _role_action(role: str, scope: str) -> BootstrapAction:
    return BootstrapAction(
        action_id=f"role-{role}",
        phase="azure",
        stage="planned",
        kind="role-assignment",
        diagnostics=(f"role={role}", f"scope={scope}"),
    )


def _provider(recorder: AzureTransportRecorder, *, roles: dict[str, str] | None = None) -> AzureArmRestProvider:
    return AzureArmRestProvider(
        token_provider=lambda scope: "token-arm" if "management" in scope else "token-graph",
        transport=recorder.transport(),
        approved_role_definitions=roles or {
            "FoundryProjectReader": FOUNDRY_ROLE,
            "TelemetryMetricsReader": TELEMETRY_ROLE,
        },
    )


def _fic_name(subject: str) -> str:
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()[:24]


def _uami_payload() -> dict[str, object]:
    return {
        "id": UAMI_ID,
        "name": "shared-uami",
        "location": "eastus",
        "properties": {
            "clientId": CLIENT,
            "principalId": PRINCIPAL,
            "tenantId": TENANT,
        },
    }


def _assignment_id(scope: str, role_id: str) -> str:
    provider = _provider(AzureTransportRecorder())
    actions = provider.plan_bindings(_plan(_uami_action(adopted=True), _role_action("FoundryProjectReader" if scope == FOUNDRY_SCOPE else "TelemetryMetricsReader", scope)))
    for action in actions:
        if action.kind == "role-assignment":
            data = dict(item.split("=", 1) for item in action.diagnostics if "=" in item)
            if data["scope"] == scope:
                return data["role_assignment_id"]
    raise AssertionError("missing assignment id")


def _install_readonly_identity_routes(recorder: AzureTransportRecorder) -> None:
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (200, _uami_payload()))


def _install_fic_routes(recorder: AzureTransportRecorder, *, create: bool) -> None:
    for subject in (
        "repo:octo-org/octo-repo:environment:copilot",
        "repo:octo-org/octo-repo:environment:foundry-production",
    ):
        fic_url = f"{UAMI_URL}/federatedIdentityCredentials/{_fic_name(subject)}?api-version=2024-11-30"
        recorder.add("GET", fic_url, (200, {"properties": {"issuer": "https://token.actions.githubusercontent.com", "subject": subject, "audiences": ["api://AzureADTokenExchange"]}}) if not create else (404, {"error": {"code": "not_found"}}))
        if create:
            recorder.add("PUT", fic_url, lambda request, s=subject: httpx.Response(201, json={"properties": {"issuer": "https://token.actions.githubusercontent.com", "subject": s, "audiences": ["api://AzureADTokenExchange"]}}, request=request))
            recorder.add_sequence("GET", fic_url, [(404, {"error": {"code": "not_found"}}), (200, {"properties": {"issuer": "https://token.actions.githubusercontent.com", "subject": subject, "audiences": ["api://AzureADTokenExchange"]}})])


def _install_role_routes(recorder: AzureTransportRecorder, *, create: bool) -> None:
    for scope, role_id, key in (
        (FOUNDRY_SCOPE, FOUNDRY_ROLE, "FoundryProjectReader"),
        (TELEMETRY_SCOPE, TELEMETRY_ROLE, "TelemetryMetricsReader"),
    ):
        planned_id = _assignment_id(scope, role_id)
        url = f"https://management.azure.com{scope}/providers/Microsoft.Authorization/roleAssignments/{planned_id}?api-version=2022-04-01"
        body = {"id": f"{scope}/providers/Microsoft.Authorization/roleAssignments/{planned_id}", "properties": {"roleDefinitionId": role_id if role_id.startswith('/subscriptions/') else f"/subscriptions/{SUB}{role_id}", "principalId": PRINCIPAL}}
        recorder.add("GET", url, (200, body) if not create else (404, {"error": {"code": "not_found"}}))
        if create:
            recorder.add("PUT", url, (201, body))
            recorder.add_sequence("GET", url, [(404, {"error": {"code": "not_found"}}), (200, body)])


def test_apply_uses_arm_properties_wrapper_and_read_only_verify() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    recorder.routes.clear()
    plan = _plan(_uami_action(adopted=False), _role_action("FoundryProjectReader", FOUNDRY_SCOPE), _role_action("TelemetryMetricsReader", TELEMETRY_SCOPE))
    recorder.add("PUT", f"{UAMI_URL}?api-version=2023-01-31", (201, _uami_payload()))
    _install_readonly_identity_routes(recorder)
    _install_fic_routes(recorder, create=True)
    _install_role_routes(recorder, create=True)
    receipt = provider.apply_bindings(plan)
    assert receipt.error_info is None
    put_bodies = [json_body(request) for request in recorder.history if request.method == "PUT" and "federatedIdentityCredentials" in str(request.url)]
    assert put_bodies
    assert all(set(body) == {"properties"} for body in put_bodies)
    verify_requests = [request for request in recorder.history if request.method == "GET" and "federatedIdentityCredentials" in str(request.url)]
    assert verify_requests


def test_plan_hashes_canonical_role_ids_and_scopes() -> None:
    provider = _provider(AzureTransportRecorder())
    actions = provider.plan_bindings(_plan(_uami_action(adopted=True), _role_action("TelemetryMetricsReader", TELEMETRY_SCOPE)))
    role_action = next(action for action in actions if action.kind == "role-assignment")
    diag = dict(item.split("=", 1) for item in role_action.diagnostics if "=" in item)
    assert diag["role_definition_id"] == f"/subscriptions/{SUB}/providers/microsoft.authorization/roledefinitions/00000000-0000-0000-0000-000000000222"
    assert len(diag["approved_role_sha256"]) == 64


def test_apply_rejects_identity_mismatch_from_live_state() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    plan = _plan(_uami_action(adopted=True), _role_action("FoundryProjectReader", FOUNDRY_SCOPE))
    payload = _uami_payload()
    payload["properties"] = {**payload["properties"], "clientId": "99999999-9999-9999-9999-999999999999"}
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (200, payload))
    receipt = provider.apply_bindings(plan)
    assert receipt.error_info is not None
    assert "client_id" in receipt.error_info.summary


def test_plan_rejects_owner_alias_and_management_group_or_cross_subscription_scope() -> None:
    provider = _provider(AzureTransportRecorder(), roles={"BadRole": f"/subscriptions/{SUB}/providers/Microsoft.Authorization/roleDefinitions/8e3af657-a8ff-443c-a75c-2fe8c4bcb635"})
    with pytest.raises(AzureProviderError, match="Owner and Contributor"):
        provider.plan_bindings(_plan(_uami_action(adopted=True), _role_action("BadRole", FOUNDRY_SCOPE)))
    provider = _provider(AzureTransportRecorder())
    with pytest.raises(AzureProviderError, match="management-group"):
        provider.plan_bindings(_plan(_uami_action(adopted=True), _role_action("FoundryProjectReader", "/providers/Microsoft.Management/managementGroups/root")))
    with pytest.raises(AzureProviderError, match="cross-subscription"):
        provider.plan_bindings(_plan(_uami_action(adopted=True), _role_action("FoundryProjectReader", "/subscriptions/66666666-6666-6666-6666-666666666666/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acct/projects/proj")))


def test_apply_accepts_canonical_role_response_and_canonical_resource_ids() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    plan = _plan(_uami_action(adopted=True), _role_action("TelemetryMetricsReader", TELEMETRY_SCOPE))
    _install_readonly_identity_routes(recorder)
    _install_fic_routes(recorder, create=False)
    assignment_id = _assignment_id(TELEMETRY_SCOPE, TELEMETRY_ROLE)
    url = f"https://management.azure.com{TELEMETRY_SCOPE}/providers/Microsoft.Authorization/roleAssignments/{assignment_id}?api-version=2022-04-01"
    recorder.add("GET", url, (200, {"id": f"{TELEMETRY_SCOPE}/providers/Microsoft.Authorization/roleAssignments/{assignment_id}", "properties": {"roleDefinitionId": f"/subscriptions/{SUB}/providers/Microsoft.Authorization/roleDefinitions/00000000-0000-0000-0000-000000000222", "principalId": PRINCIPAL}}))
    receipt = provider.apply_bindings(plan)
    assert receipt.error_info is None


def test_apply_reports_timeout_compensation_for_created_uami() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    plan = _plan(_uami_action(adopted=False), _role_action("FoundryProjectReader", FOUNDRY_SCOPE))
    recorder.add("PUT", f"{UAMI_URL}?api-version=2023-01-31", (201, _uami_payload()))
    _install_readonly_identity_routes(recorder)
    fic_url = f"{UAMI_URL}/federatedIdentityCredentials/{_fic_name('repo:octo-org/octo-repo:environment:copilot')}?api-version=2024-11-30"
    recorder.add("GET", fic_url, (404, {"error": {"code": "not_found"}}))
    recorder.add("PUT", fic_url, lambda request: (_ for _ in ()).throw(httpx.TimeoutException("timeout")))
    receipt = provider.apply_bindings(plan)
    assert receipt.error_info is not None
    assert "timed out" in receipt.error_info.summary
    assert "azure-uami-create" in receipt.compensation_required_actions
