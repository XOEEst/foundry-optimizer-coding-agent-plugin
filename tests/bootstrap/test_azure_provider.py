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
SCOPE = f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acct/projects/proj"
ROLE = f"/subscriptions/{SUB}/providers/Microsoft.Authorization/roleDefinitions/00000000-0000-0000-0000-000000000111"


def _plan(*actions: BootstrapAction) -> BootstrapPlan:
    return BootstrapPlan.create(operation_id="op", runtime_repository="https://github.com/octo-org/octo-repo.git", runtime_commit="a" * 40, repository_identity="octo-org/octo-repo", actions=actions)


def _uami(*, adopted: bool, ids: bool = True) -> BootstrapAction:
    diagnostics = [f"resource_id={UAMI_ID}", f"subscription_id={SUB}", "name=shared-uami", "location=eastus", f"adopted={'true' if adopted else 'false'}"]
    if ids:
        diagnostics += [f"client_id={CLIENT}", f"principal_id={PRINCIPAL}", f"tenant_id={TENANT}"]
    return BootstrapAction(action_id="identity", phase="azure", stage="planned", kind="managed-identity", diagnostics=tuple(diagnostics))


def _role(scope: str = SCOPE) -> BootstrapAction:
    return BootstrapAction(action_id="role", phase="azure", stage="planned", kind="role-assignment", diagnostics=("role=FoundryProjectReader", f"scope={scope}"))


def _provider(recorder: AzureTransportRecorder, roles: dict[str, str] | None = None) -> AzureArmRestProvider:
    return AzureArmRestProvider(token_provider=lambda scope: "token", transport=recorder.transport(), approved_role_definitions=roles or {"FoundryProjectReader": ROLE})


def _fic(subject: str) -> str:
    return hashlib.sha256(subject.encode()).hexdigest()[:24]


def test_preserves_planned_ids_for_adoption_and_blocks_drift_before_rbac() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    plan = _plan(_uami(adopted=True), _role())
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (200, {"id": UAMI_ID, "name": "shared-uami", "properties": {"clientId": "wrong", "principalId": PRINCIPAL, "tenantId": TENANT}}))
    receipt = provider.apply_bindings(plan)
    assert receipt.error_info is not None
    assert "client_id" in receipt.error_info.summary


def test_new_uami_fills_missing_ids_and_uses_frozen_role_scope_fingerprint() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    plan = _plan(_uami(adopted=False, ids=False), _role())
    recorder.add("PUT", f"{UAMI_URL}?api-version=2023-01-31", (201, {"id": UAMI_ID, "name": "shared-uami", "properties": {"clientId": CLIENT, "principalId": PRINCIPAL, "tenantId": TENANT}}))
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (200, {"id": UAMI_ID, "name": "shared-uami", "properties": {"clientId": CLIENT, "principalId": PRINCIPAL, "tenantId": TENANT}}))
    for subject in ("repo:octo-org/octo-repo:environment:copilot", "repo:octo-org/octo-repo:environment:foundry-production"):
        url = f"{UAMI_URL}/federatedIdentityCredentials/{_fic(subject)}?api-version=2024-11-30"
        recorder.add_sequence("GET", url, [(404, {"error": {}}), (200, {"properties": {"issuer": "https://token.actions.githubusercontent.com", "subject": subject, "audiences": ["api://AzureADTokenExchange"]}})])
        recorder.add("PUT", url, (201, {"properties": {"issuer": "https://token.actions.githubusercontent.com", "subject": subject, "audiences": ["api://AzureADTokenExchange"]}}))
    assignment_id = "763b639a-9ee2-570d-b5c4-d6f92b269bbe"
    role_url = f"https://management.azure.com{SCOPE}/providers/Microsoft.Authorization/roleAssignments/{assignment_id}?api-version=2022-04-01"
    recorder.add_sequence("GET", role_url, [(404, {"error": {}}), (200, {"properties": {"principalId": PRINCIPAL, "roleDefinitionId": ROLE}, "id": role_url.replace('?api-version=2022-04-01','')})])
    recorder.add("PUT", role_url, (201, {"properties": {"principalId": PRINCIPAL, "roleDefinitionId": ROLE}, "id": role_url.replace('?api-version=2022-04-01','')}))
    receipt = provider.apply_bindings(plan)
    assert receipt.error_info is None
    role_body = next(json_body(r) for r in recorder.history if r.method == "PUT" and "/roleAssignments/" in str(r.url))
    assert role_body["properties"]["roleDefinitionId"] == ROLE


def test_scope_validator_rejects_encoded_separators_traversal_and_nonapproved_pairs() -> None:
    provider = _provider(AzureTransportRecorder())
    for scope in (f"{SCOPE}%2Fchild", f"{SCOPE}%2e%2e", f"{SCOPE}//child", "/providers/Microsoft.Management/managementGroups/root", "/subscriptions/666/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acct/projects/proj"):
        with pytest.raises(AzureProviderError):
            provider.plan_bindings(_plan(_uami(adopted=True), _role(scope)))


def test_timeout_records_committed_resource_for_reverse_compensation() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    plan = _plan(_uami(adopted=False, ids=False), _role())
    recorder.add("PUT", f"{UAMI_URL}?api-version=2023-01-31", (201, {"id": UAMI_ID, "name": "shared-uami", "properties": {"clientId": CLIENT, "principalId": PRINCIPAL, "tenantId": TENANT}}))
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (200, {"id": UAMI_ID, "name": "shared-uami", "properties": {"clientId": CLIENT, "principalId": PRINCIPAL, "tenantId": TENANT}}))
    fic_url = f"{UAMI_URL}/federatedIdentityCredentials/{_fic('repo:octo-org/octo-repo:environment:copilot')}?api-version=2024-11-30"
    recorder.add("GET", fic_url, (404, {"error": {}}))
    recorder.add("PUT", fic_url, lambda request: (_ for _ in ()).throw(httpx.TimeoutException("timeout")))
    receipt = provider.apply_bindings(plan)
    assert receipt.error_info is not None
    assert receipt.compensation_required_actions[0].startswith("azure-fic-") or receipt.compensation_required_actions[0] == "azure-uami-create"
    assert "azure-uami-create" in receipt.compensation_required_actions
