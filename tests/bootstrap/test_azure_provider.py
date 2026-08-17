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
NEW_CLIENT = "aaaaaaaa-1111-1111-1111-111111111111"
NEW_PRINCIPAL = "bbbbbbbb-2222-2222-2222-222222222222"
UAMI_ID = f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/shared-uami"
UAMI_URL = f"https://management.azure.com{UAMI_ID}"
FOUNDRY_SCOPE = f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acct/projects/proj"
FOUNDRY_ROLE = f"/subscriptions/{SUB}/providers/Microsoft.Authorization/roleDefinitions/00000000-0000-0000-0000-000000000111"


def _plan(*actions: BootstrapAction) -> BootstrapPlan:
    return BootstrapPlan.create(operation_id="op", runtime_repository="https://github.com/octo-org/octo-repo.git", runtime_commit="a" * 40, repository_identity="octo-org/octo-repo", actions=actions)


def _uami_action(*, adopted: bool, include_ids: bool = True) -> BootstrapAction:
    diagnostics = [f"subscription_id={SUB}", f"resource_id={UAMI_ID}", "name=shared-uami", "location=eastus", f"adopted={'true' if adopted else 'false'}"]
    if include_ids:
        diagnostics.extend([f"client_id={CLIENT}", f"principal_id={PRINCIPAL}", f"tenant_id={TENANT}"])
    return BootstrapAction(action_id="identity", phase="azure", stage="planned", kind="managed-identity", diagnostics=tuple(diagnostics))


def _app_action() -> BootstrapAction:
    return BootstrapAction(action_id="app", phase="azure", stage="planned", kind="entra-application", diagnostics=(f"object_id=app-object", f"subscription_id={SUB}", f"tenant_id={TENANT}", "name=shared-app"))


def _role_action() -> BootstrapAction:
    return BootstrapAction(action_id="role", phase="azure", stage="planned", kind="role-assignment", diagnostics=(f"role=FoundryProjectReader", f"scope={FOUNDRY_SCOPE}"))


def _provider(recorder: AzureTransportRecorder) -> AzureArmRestProvider:
    return AzureArmRestProvider(token_provider=lambda scope: "token", transport=recorder.transport(), approved_role_definitions={"FoundryProjectReader": FOUNDRY_ROLE})


def _fic_name(subject: str) -> str:
    return hashlib.sha256(subject.encode()).hexdigest()[:24]


def _uami_payload(client: str = CLIENT, principal: str = PRINCIPAL) -> dict[str, object]:
    return {"id": UAMI_ID, "name": "shared-uami", "location": "eastus", "properties": {"clientId": client, "principalId": principal, "tenantId": TENANT}}


def test_new_uami_allows_azure_generated_ids_and_binds_later_actions_to_live_ids() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    plan = _plan(_uami_action(adopted=False, include_ids=False), _role_action())
    recorder.add("PUT", f"{UAMI_URL}?api-version=2023-01-31", (201, _uami_payload(NEW_CLIENT, NEW_PRINCIPAL)))
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (200, _uami_payload(NEW_CLIENT, NEW_PRINCIPAL)))
    for subject in ("repo:octo-org/octo-repo:environment:copilot", "repo:octo-org/octo-repo:environment:foundry-production"):
        url = f"{UAMI_URL}/federatedIdentityCredentials/{_fic_name(subject)}?api-version=2024-11-30"
        recorder.add_sequence("GET", url, [(404, {"error": {}}), (200, {"properties": {"issuer": "https://token.actions.githubusercontent.com", "subject": subject, "audiences": ["api://AzureADTokenExchange"]}}), (200, {"properties": {"issuer": "https://token.actions.githubusercontent.com", "subject": subject, "audiences": ["api://AzureADTokenExchange"]}})])
        recorder.add("PUT", url, (201, {"properties": {"issuer": "https://token.actions.githubusercontent.com", "subject": subject, "audiences": ["api://AzureADTokenExchange"]}}))
    assignment_id = "7d28535f-c478-51ed-b2fe-fc902050bcd9"
    role_url = f"https://management.azure.com{FOUNDRY_SCOPE}/providers/Microsoft.Authorization/roleAssignments/{assignment_id}?api-version=2022-04-01"
    recorder.add_sequence("GET", role_url, [(404, {"error": {}}), (200, {"properties": {"principalId": NEW_PRINCIPAL, "roleDefinitionId": FOUNDRY_ROLE}, "id": role_url.replace('?api-version=2022-04-01','')}), (200, {"properties": {"principalId": NEW_PRINCIPAL, "roleDefinitionId": FOUNDRY_ROLE}, "id": role_url.replace('?api-version=2022-04-01','')})])
    recorder.add("PUT", role_url, (201, {"properties": {"principalId": NEW_PRINCIPAL, "roleDefinitionId": FOUNDRY_ROLE}, "id": role_url.replace('?api-version=2022-04-01','')}))
    receipt = provider.apply_bindings(plan)
    assert receipt.error_info is None
    role_put = next(json_body(r) for r in recorder.history if r.method == "PUT" and "/roleAssignments/" in str(r.url))
    assert role_put["properties"]["principalId"] == NEW_PRINCIPAL


def test_entra_app_uses_service_principal_for_rbac_and_tenant_guid() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    plan = _plan(_app_action(), _role_action())
    recorder.add("GET", "https://graph.microsoft.com/v1.0/applications/app-object", (200, {"id": "app-object", "appId": CLIENT, "displayName": "shared-app"}))
    recorder.add("GET", "https://graph.microsoft.com/v1.0/servicePrincipals?%24filter=appId+eq+%2711111111-1111-1111-1111-111111111111%27", (200, {"value": [{"id": PRINCIPAL, "appOwnerOrganizationId": TENANT}]}))
    for subject in ("repo:octo-org/octo-repo:environment:copilot", "repo:octo-org/octo-repo:environment:foundry-production"):
        base = f"https://graph.microsoft.com/v1.0/applications/app-object/federatedIdentityCredentials/{_fic_name(subject)}"
        recorder.add_sequence("GET", base, [(404, {"error": {}}), (200, {"issuer": "https://token.actions.githubusercontent.com", "subject": subject, "audiences": ["api://AzureADTokenExchange"]}), (200, {"issuer": "https://token.actions.githubusercontent.com", "subject": subject, "audiences": ["api://AzureADTokenExchange"]})])
        recorder.add("POST", "https://graph.microsoft.com/v1.0/applications/app-object/federatedIdentityCredentials", (201, {"issuer": "https://token.actions.githubusercontent.com", "subject": subject, "audiences": ["api://AzureADTokenExchange"]}))
    assignment_id = "763b639a-9ee2-570d-b5c4-d6f92b269bbe"
    role_url = f"https://management.azure.com{FOUNDRY_SCOPE}/providers/Microsoft.Authorization/roleAssignments/{assignment_id}?api-version=2022-04-01"
    recorder.add_sequence("GET", role_url, [(404, {"error": {}}), (200, {"properties": {"principalId": PRINCIPAL, "roleDefinitionId": FOUNDRY_ROLE}, "id": role_url.replace('?api-version=2022-04-01','')}), (200, {"properties": {"principalId": PRINCIPAL, "roleDefinitionId": FOUNDRY_ROLE}, "id": role_url.replace('?api-version=2022-04-01','')})])
    recorder.add("PUT", role_url, (201, {"properties": {"principalId": PRINCIPAL, "roleDefinitionId": FOUNDRY_ROLE}, "id": role_url.replace('?api-version=2022-04-01','')}))
    receipt = provider.apply_bindings(plan)
    assert receipt.error_info is None
    body = next(json_body(r) for r in recorder.history if r.method == "PUT" and "/roleAssignments/" in str(r.url))
    assert body["properties"]["principalId"] == PRINCIPAL


def test_plan_rejects_dot_segments_cross_subscription_and_unapproved_pair() -> None:
    provider = _provider(AzureTransportRecorder())
    with pytest.raises(AzureProviderError, match="dot segments"):
        provider.plan_bindings(_plan(_uami_action(adopted=True), BootstrapAction(action_id="role", phase="azure", stage="planned", kind="role-assignment", diagnostics=("role=FoundryProjectReader", f"scope={FOUNDRY_SCOPE}/../bad"))))
    with pytest.raises(AzureProviderError, match="cross-subscription"):
        provider.plan_bindings(_plan(_uami_action(adopted=True), BootstrapAction(action_id="role", phase="azure", stage="planned", kind="role-assignment", diagnostics=("role=FoundryProjectReader", "scope=/subscriptions/666/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acct/projects/proj"))))
    bad = AzureArmRestProvider(token_provider=lambda scope: "token", approved_role_definitions={}, transport=AzureTransportRecorder().transport())
    with pytest.raises(AzureProviderError, match="approved_role_definitions"):
        bad.plan_bindings(_plan(_uami_action(adopted=True), _role_action()))


def test_verify_checks_principal_and_canonical_role() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    plan = _plan(_uami_action(adopted=True), _role_action())
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (200, _uami_payload()))
    for subject in ("repo:octo-org/octo-repo:environment:copilot", "repo:octo-org/octo-repo:environment:foundry-production"):
        url = f"{UAMI_URL}/federatedIdentityCredentials/{_fic_name(subject)}?api-version=2024-11-30"
        recorder.add("GET", url, (200, {"properties": {"issuer": "https://token.actions.githubusercontent.com", "subject": subject, "audiences": ["api://AzureADTokenExchange"]}}))
    assignment_id = "763b639a-9ee2-570d-b5c4-d6f92b269bbe"
    role_url = f"https://management.azure.com{FOUNDRY_SCOPE}/providers/Microsoft.Authorization/roleAssignments/{assignment_id}?api-version=2022-04-01"
    recorder.add("GET", role_url, (200, {"properties": {"principalId": "wrong", "roleDefinitionId": FOUNDRY_ROLE}, "id": role_url.replace('?api-version=2022-04-01','')}))
    receipt = provider.apply_bindings(plan)
    assert receipt.error_info is not None
    assert "principalId" in receipt.error_info.summary


def test_timeout_reconciliation_records_reverse_compensation() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    plan = _plan(_uami_action(adopted=False, include_ids=False), _role_action())
    recorder.add("PUT", f"{UAMI_URL}?api-version=2023-01-31", (201, _uami_payload(NEW_CLIENT, NEW_PRINCIPAL)))
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (200, _uami_payload(NEW_CLIENT, NEW_PRINCIPAL)))
    url = f"{UAMI_URL}/federatedIdentityCredentials/{_fic_name('repo:octo-org/octo-repo:environment:copilot')}?api-version=2024-11-30"
    recorder.add("GET", url, (404, {"error": {}}))
    recorder.add("PUT", url, lambda request: (_ for _ in ()).throw(httpx.TimeoutException("timeout")))
    receipt = provider.apply_bindings(plan)
    assert receipt.error_info is not None
    assert receipt.compensation_required_actions[0].startswith("azure-fic-") or receipt.compensation_required_actions[0] == "azure-uami-create"
    assert "azure-uami-create" in receipt.compensation_required_actions
