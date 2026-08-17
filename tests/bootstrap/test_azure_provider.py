from __future__ import annotations

import hashlib

import httpx
import pytest

from foundry_opt.bootstrap.contracts import BootstrapAction, BootstrapPlan, BootstrapReceipt
from foundry_opt.bootstrap.providers.azure import AzureArmRestProvider, AzureProviderError
from tests.bootstrap.fakes import AzureTransportRecorder, json_body


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
            "client_id=11111111-1111-1111-1111-111111111111",
            "principal_id=22222222-2222-2222-2222-222222222222",
            "object_id=33333333-3333-3333-3333-333333333333",
            "tenant_id=44444444-4444-4444-4444-444444444444",
            "subscription_id=55555555-5555-5555-5555-555555555555",
            "resource_id=/subscriptions/55555555-5555-5555-5555-555555555555/resourceGroups/rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/shared-uami",
            "name=shared-uami",
            f"adopted={'true' if adopted else 'false'}",
        ),
    )


def _app_action() -> BootstrapAction:
    return BootstrapAction(
        action_id="identity-app",
        phase="azure",
        stage="planned",
        kind="entra-application",
        diagnostics=(
            "client_id=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "object_id=bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "service_principal_id=cccccccc-cccc-cccc-cccc-cccccccccccc",
            "tenant_id=44444444-4444-4444-4444-444444444444",
            "subscription_id=55555555-5555-5555-5555-555555555555",
            "name=shared-app",
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


def _provider(recorder: AzureTransportRecorder) -> AzureArmRestProvider:
    return AzureArmRestProvider(
        token_provider=lambda scope: "token-arm" if "management" in scope else "token-graph",
        transport=recorder.transport(),
        approved_role_definitions={
            "FoundryProjectReader": "/subscriptions/55555555-5555-5555-5555-555555555555/providers/Microsoft.Authorization/roleDefinitions/role-foundry",
            "TelemetryMetricsReader": "/subscriptions/55555555-5555-5555-5555-555555555555/providers/Microsoft.Authorization/roleDefinitions/role-telemetry",
        },
    )


def _fic_name(subject: str) -> str:
    return hashlib.sha256(subject.encode("utf-8")).hexdigest()[:24]


def test_apply_bindings_creates_exact_uami_fics_and_role_assignments() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    plan = _plan(
        _uami_action(adopted=False),
        _role_action("FoundryProjectReader", "/subscriptions/55555555-5555-5555-5555-555555555555/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acct/projects/proj"),
        _role_action("TelemetryMetricsReader", "/subscriptions/55555555-5555-5555-5555-555555555555/resourceGroups/rg/providers/Microsoft.Insights/components/appi"),
    )
    uami_resource = "https://management.azure.com/subscriptions/55555555-5555-5555-5555-555555555555/resourceGroups/rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/shared-uami"
    recorder.add("PUT", f"{uami_resource}?api-version=2023-01-31", (201, {"id": uami_resource, "properties": {"clientId": "11111111-1111-1111-1111-111111111111"}}))
    for subject in (
        "repo:octo-org/octo-repo:environment:copilot",
        "repo:octo-org/octo-repo:environment:foundry-production",
    ):
        name = _fic_name(subject)
        fic_url = f"{uami_resource}/federatedIdentityCredentials/{name}?api-version=2024-11-30"
        recorder.add("PUT", fic_url, lambda request, s=subject, n=name: httpx.Response(201, json={"name": n, "properties": {"issuer": "https://token.actions.githubusercontent.com", "subject": s, "audiences": ["api://AzureADTokenExchange"]}}, request=request))
    for assignment_id, role in (
        ("db584d50-0c1e-57f1-93c1-ea1b6a5a7e8d", "role-foundry"),
        ("25e80292-7cc4-5806-b423-6acf3abf9060", "role-telemetry"),
    ):
        scope = "/subscriptions/55555555-5555-5555-5555-555555555555/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acct/projects/proj" if role == "role-foundry" else "/subscriptions/55555555-5555-5555-5555-555555555555/resourceGroups/rg/providers/Microsoft.Insights/components/appi"
        recorder.add(
            "PUT",
            f"https://management.azure.com{scope}/providers/Microsoft.Authorization/roleAssignments/{assignment_id}?api-version=2022-04-01",
            (201, {"id": f"{scope}/providers/Microsoft.Authorization/roleAssignments/{assignment_id}", "properties": {"roleDefinitionId": f"/subscriptions/55555555-5555-5555-5555-555555555555/providers/Microsoft.Authorization/roleDefinitions/{role}"}}),
        )
    receipt = provider.apply_bindings(plan)
    assert isinstance(receipt, BootstrapReceipt)
    assert set(receipt.created_actions) >= {"azure-uami-create", "azure-fic-11111111-copilot", "azure-fic-11111111-foundry-production"}
    assert receipt.error_info is None
    fic_bodies = [json_body(request) for request in recorder.requests if "federatedIdentityCredentials" in str(request.url)]
    assert {body["subject"] for body in fic_bodies} == {
        "repo:octo-org/octo-repo:environment:copilot",
        "repo:octo-org/octo-repo:environment:foundry-production",
    }
    assert all(body["issuer"] == "https://token.actions.githubusercontent.com" for body in fic_bodies)
    assert all(body["audiences"] == ["api://AzureADTokenExchange"] for body in fic_bodies)


def test_apply_bindings_adopts_existing_identity_and_assignments() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    plan = _plan(_uami_action(adopted=True), _role_action("FoundryProjectReader", "/subscriptions/55555555-5555-5555-5555-555555555555/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acct/projects/proj"))
    uami_resource = "https://management.azure.com/subscriptions/55555555-5555-5555-5555-555555555555/resourceGroups/rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/shared-uami"
    subject = "repo:octo-org/octo-repo:environment:copilot"
    recorder.add("PUT", f"{uami_resource}/federatedIdentityCredentials/{_fic_name(subject)}?api-version=2024-11-30", (200, {"properties": {"issuer": "https://token.actions.githubusercontent.com", "subject": subject, "audiences": ["api://AzureADTokenExchange"]}}))
    subject2 = "repo:octo-org/octo-repo:environment:foundry-production"
    recorder.add("PUT", f"{uami_resource}/federatedIdentityCredentials/{_fic_name(subject2)}?api-version=2024-11-30", (200, {"properties": {"issuer": "https://token.actions.githubusercontent.com", "subject": subject2, "audiences": ["api://AzureADTokenExchange"]}}))
    assignment_id = "db584d50-0c1e-57f1-93c1-ea1b6a5a7e8d"
    scope = "/subscriptions/55555555-5555-5555-5555-555555555555/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acct/projects/proj"
    recorder.add("PUT", f"https://management.azure.com{scope}/providers/Microsoft.Authorization/roleAssignments/{assignment_id}?api-version=2022-04-01", (200, {"id": f"{scope}/providers/Microsoft.Authorization/roleAssignments/{assignment_id}", "properties": {"roleDefinitionId": "/subscriptions/55555555-5555-5555-5555-555555555555/providers/Microsoft.Authorization/roleDefinitions/role-foundry"}}))
    receipt = provider.apply_bindings(plan)
    assert "azure-uami-create" not in receipt.created_actions
    assert "azure-fic-11111111-copilot" in receipt.adopted_actions


@pytest.mark.parametrize("issuer,audience", [("https://wrong.example", "api://AzureADTokenExchange"), ("https://token.actions.githubusercontent.com", "api://wrong")])
def test_apply_bindings_rejects_wrong_issuer_or_audience(issuer: str, audience: str) -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    plan = _plan(_uami_action(adopted=True), _role_action("FoundryProjectReader", "/subscriptions/55555555-5555-5555-5555-555555555555/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acct/projects/proj"))
    uami_resource = "https://management.azure.com/subscriptions/55555555-5555-5555-5555-555555555555/resourceGroups/rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/shared-uami"
    subject = "repo:octo-org/octo-repo:environment:copilot"
    recorder.add("PUT", f"{uami_resource}/federatedIdentityCredentials/{_fic_name(subject)}?api-version=2024-11-30", (200, {"properties": {"issuer": issuer, "subject": subject, "audiences": [audience]}}))
    recorder.add("PUT", f"{uami_resource}/federatedIdentityCredentials/{_fic_name('repo:octo-org/octo-repo:environment:foundry-production')}?api-version=2024-11-30", (200, {"properties": {"issuer": "https://token.actions.githubusercontent.com", "subject": "repo:octo-org/octo-repo:environment:foundry-production", "audiences": ["api://AzureADTokenExchange"]}}))
    receipt = provider.apply_bindings(plan)
    assert receipt.error_info is not None
    assert "exact" in receipt.error_info.summary or "drifted" in receipt.error_info.summary


def test_plan_bindings_rejects_subscription_scope_and_unapproved_role() -> None:
    provider = AzureArmRestProvider(token_provider=lambda scope: "token", approved_role_definitions={})
    with pytest.raises(AzureProviderError, match="approved role-definition map"):
        provider.plan_bindings(_plan(_uami_action(adopted=True), _role_action("FoundryProjectReader", "/subscriptions/55555555-5555-5555-5555-555555555555/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acct/projects/proj")))
    with pytest.raises(AzureProviderError, match="subscription-wide"):
        provider.plan_bindings(_plan(_uami_action(adopted=True), _role_action("TelemetryMetricsReader", "/subscriptions/55555555-5555-5555-5555-555555555555")))


def test_apply_bindings_reports_partial_apply_and_compensation_state() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    plan = _plan(_uami_action(adopted=False), _role_action("FoundryProjectReader", "/subscriptions/55555555-5555-5555-5555-555555555555/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acct/projects/proj"))
    uami_resource = "https://management.azure.com/subscriptions/55555555-5555-5555-5555-555555555555/resourceGroups/rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/shared-uami"
    recorder.add("PUT", f"{uami_resource}?api-version=2023-01-31", (201, {"id": uami_resource}))
    recorder.add("PUT", f"{uami_resource}/federatedIdentityCredentials/{_fic_name('repo:octo-org/octo-repo:environment:copilot')}?api-version=2024-11-30", (201, {"properties": {"issuer": "https://token.actions.githubusercontent.com", "subject": "repo:octo-org/octo-repo:environment:copilot", "audiences": ["api://AzureADTokenExchange"]}}))
    recorder.add("PUT", f"{uami_resource}/federatedIdentityCredentials/{_fic_name('repo:octo-org/octo-repo:environment:foundry-production')}?api-version=2024-11-30", (500, {"error": {"message": "boom"}}))
    receipt = provider.apply_bindings(plan)
    assert receipt.error_info is not None
    assert receipt.compensation_required_actions
    assert "Rollback only operation-created" in receipt.resume_info.summary


def test_apply_bindings_detects_executor_missing_role_assignments_write() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    plan = _plan(_uami_action(adopted=True), _role_action("FoundryProjectReader", "/subscriptions/55555555-5555-5555-5555-555555555555/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acct/projects/proj"))
    uami_resource = "https://management.azure.com/subscriptions/55555555-5555-5555-5555-555555555555/resourceGroups/rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/shared-uami"
    for subject in (
        "repo:octo-org/octo-repo:environment:copilot",
        "repo:octo-org/octo-repo:environment:foundry-production",
    ):
        recorder.add("PUT", f"{uami_resource}/federatedIdentityCredentials/{_fic_name(subject)}?api-version=2024-11-30", (200, {"properties": {"issuer": "https://token.actions.githubusercontent.com", "subject": subject, "audiences": ["api://AzureADTokenExchange"]}}))
    assignment_id = "db584d50-0c1e-57f1-93c1-ea1b6a5a7e8d"
    scope = "/subscriptions/55555555-5555-5555-5555-555555555555/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acct/projects/proj"
    recorder.add("PUT", f"https://management.azure.com{scope}/providers/Microsoft.Authorization/roleAssignments/{assignment_id}?api-version=2022-04-01", (403, {"error": {"code": "AuthorizationFailed"}}))
    receipt = provider.apply_bindings(plan)
    assert receipt.error_info is not None
    assert "roleAssignments/write" in receipt.error_info.summary


def test_plan_bindings_for_entra_app_uses_shared_client_id() -> None:
    provider = AzureArmRestProvider(token_provider=lambda scope: "token", approved_role_definitions={"FoundryProjectReader": "/subscriptions/55555555-5555-5555-5555-555555555555/providers/Microsoft.Authorization/roleDefinitions/role-foundry"})
    actions = provider.plan_bindings(_plan(_app_action(), _role_action("FoundryProjectReader", "/subscriptions/55555555-5555-5555-5555-555555555555/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acct/projects/proj")))
    fic_actions = [action for action in actions if action.kind == "federated-credential"]
    assert len(fic_actions) == 2
    assert all("client_id=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" in action.diagnostics for action in fic_actions)
