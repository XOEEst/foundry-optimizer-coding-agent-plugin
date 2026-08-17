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


def _provider(recorder: AzureTransportRecorder, roles: dict[str, str] | None = None, token: str = "token") -> AzureArmRestProvider:
    return AzureArmRestProvider(token_provider=lambda scope: token, transport=recorder.transport(), approved_role_definitions=roles or {"FoundryProjectReader": ROLE})


def _fic(subject: str) -> str:
    return hashlib.sha256(subject.encode()).hexdigest()[:24]


def _subjects() -> tuple[str, str]:
    return ("repo:octo-org/octo-repo:environment:copilot", "repo:octo-org/octo-repo:environment:foundry-production")


def _setup_success(recorder: AzureTransportRecorder, *, adopted_identity: bool, adopted_copilot_fic: bool = False, adopted_role: bool = False) -> None:
    if adopted_identity:
        recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (200, {"id": UAMI_ID, "name": "shared-uami", "location": "eastus", "properties": {"clientId": CLIENT, "principalId": PRINCIPAL, "tenantId": TENANT}}))
    else:
        recorder.add("PUT", f"{UAMI_URL}?api-version=2023-01-31", (201, {"id": UAMI_ID, "name": "shared-uami", "location": "eastus", "properties": {"clientId": CLIENT, "principalId": PRINCIPAL, "tenantId": TENANT}}))
        recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (200, {"id": UAMI_ID, "name": "shared-uami", "location": "eastus", "properties": {"clientId": CLIENT, "principalId": PRINCIPAL, "tenantId": TENANT}}))
    for index, subject in enumerate(_subjects()):
        url = f"{UAMI_URL}/federatedIdentityCredentials/{_fic(subject)}?api-version=2024-11-30"
        adopted = adopted_copilot_fic and index == 0
        recorder.add_sequence("GET", url, [((200 if adopted else 404), {"properties": {"issuer": "https://token.actions.githubusercontent.com", "subject": subject, "audiences": ["api://AzureADTokenExchange"]}} if adopted else {"error": {}}), (200, {"properties": {"issuer": "https://token.actions.githubusercontent.com", "subject": subject, "audiences": ["api://AzureADTokenExchange"]}})])
        if not adopted:
            recorder.add("PUT", url, (201, {"properties": {"issuer": "https://token.actions.githubusercontent.com", "subject": subject, "audiences": ["api://AzureADTokenExchange"]}}))
    assignment_id = "763b639a-9ee2-570d-b5c4-d6f92b269bbe"
    role_url = f"https://management.azure.com{SCOPE}/providers/Microsoft.Authorization/roleAssignments/{assignment_id}?api-version=2022-04-01"
    role_payload = {"properties": {"principalId": PRINCIPAL, "roleDefinitionId": ROLE}, "id": role_url.replace("?api-version=2022-04-01", "")}
    recorder.add_sequence("GET", role_url, [((200 if adopted_role else 404), role_payload if adopted_role else {"error": {}}), (200, role_payload), (404, {"error": {}})])
    if not adopted_role:
        recorder.add("PUT", role_url, (201, role_payload))
        recorder.add("DELETE", role_url, (204, {}))


def test_preserves_planned_ids_for_adoption_and_blocks_drift_before_rbac() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    plan = _plan(_uami(adopted=True), _role())
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (200, {"id": UAMI_ID, "name": "shared-uami", "properties": {"clientId": "wrong", "principalId": PRINCIPAL, "tenantId": TENANT}}))
    receipt = provider.apply_bindings(plan)
    assert receipt.error_info is not None
    assert "live identity" not in receipt.error_info.summary


def test_new_uami_fills_missing_ids_and_uses_frozen_role_scope_fingerprint() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    plan = _plan(_uami(adopted=False, ids=False), _role())
    _setup_success(recorder, adopted_identity=False)
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
    recorder.add("PUT", f"{UAMI_URL}?api-version=2023-01-31", (201, {"id": UAMI_ID, "name": "shared-uami", "location": "eastus", "properties": {"clientId": CLIENT, "principalId": PRINCIPAL, "tenantId": TENANT}}))
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (200, {"id": UAMI_ID, "name": "shared-uami", "location": "eastus", "properties": {"clientId": CLIENT, "principalId": PRINCIPAL, "tenantId": TENANT}}))
    fic_url = f"{UAMI_URL}/federatedIdentityCredentials/{_fic(_subjects()[0])}?api-version=2024-11-30"
    recorder.add("GET", fic_url, (404, {"error": {}}))
    recorder.add("PUT", fic_url, lambda request: (_ for _ in ()).throw(httpx.TimeoutException("timeout")))
    receipt = provider.apply_bindings(_plan(_uami(adopted=False, ids=False), _role()))
    state = provider.export_provider_state(receipt)
    assert receipt.error_info is not None
    assert "azure-uami-create" in receipt.compensation_required_actions
    assert state["compensation_required_actions"]


def test_export_restore_verify_restart_and_no_token_in_state() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder, token="secret-token")
    _setup_success(recorder, adopted_identity=False, adopted_copilot_fic=True, adopted_role=True)
    receipt = provider.apply_bindings(_plan(_uami(adopted=False, ids=False), _role()))
    state = provider.export_provider_state(receipt)
    assert "secret-token" not in str(state)
    restarted = _provider(recorder, token="secret-token")
    restarted.restore_provider_state(state)
    for subject in _subjects():
        url = f"{UAMI_URL}/federatedIdentityCredentials/{_fic(subject)}?api-version=2024-11-30"
        recorder.add("GET", url, (200, {"properties": {"issuer": "https://token.actions.githubusercontent.com", "subject": subject, "audiences": ["api://AzureADTokenExchange"]}}))
    role_url = "https://management.azure.com/subscriptions/55555555-5555-5555-5555-555555555555/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acct/projects/proj/providers/Microsoft.Authorization/roleAssignments/763b639a-9ee2-570d-b5c4-d6f92b269bbe?api-version=2022-04-01"
    recorder.add("GET", role_url, (200, {"properties": {"principalId": PRINCIPAL, "roleDefinitionId": ROLE}, "id": role_url.replace('?api-version=2022-04-01','')}))
    assert restarted.verify_bindings(receipt) is True


def test_verify_bindings_uses_live_exact_claims_and_authorization_header() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder, token="live-token")
    _setup_success(recorder, adopted_identity=False)
    receipt = provider.apply_bindings(_plan(_uami(adopted=False, ids=False), _role()))
    for subject in _subjects():
        url = f"{UAMI_URL}/federatedIdentityCredentials/{_fic(subject)}?api-version=2024-11-30"
        recorder.add("GET", url, (200, {"properties": {"issuer": "https://token.actions.githubusercontent.com", "subject": subject, "audiences": ["api://AzureADTokenExchange"]}}))
    role_url = "https://management.azure.com/subscriptions/55555555-5555-5555-5555-555555555555/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acct/projects/proj/providers/Microsoft.Authorization/roleAssignments/763b639a-9ee2-570d-b5c4-d6f92b269bbe?api-version=2022-04-01"
    recorder.add("GET", role_url, (200, {"properties": {"principalId": PRINCIPAL, "roleDefinitionId": ROLE}, "id": role_url.replace('?api-version=2022-04-01','')}))
    assert provider.verify_bindings(receipt) is True
    assert recorder.requests[0].headers["Authorization"] == "Bearer live-token"


def test_rollback_only_deletes_created_resources_and_keeps_adopted() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    _setup_success(recorder, adopted_identity=False, adopted_copilot_fic=True, adopted_role=True)
    role_url = "https://management.azure.com/subscriptions/55555555-5555-5555-5555-555555555555/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acct/projects/proj/providers/Microsoft.Authorization/roleAssignments/763b639a-9ee2-570d-b5c4-d6f92b269bbe?api-version=2022-04-01"
    created_fic_delete = f"{UAMI_URL}/federatedIdentityCredentials/{_fic(_subjects()[1])}?api-version=2024-11-30"
    recorder.add("DELETE", created_fic_delete, (204, {}))
    recorder.add("DELETE", f"{UAMI_URL}?api-version=2023-01-31", (204, {}))
    receipt = provider.apply_bindings(_plan(_uami(adopted=False, ids=False), _role()))
    provider.rollback_bindings(receipt)
    assert all(str(request.url) != role_url or request.method != "DELETE" for request in recorder.history)
    assert any(str(request.url) == created_fic_delete and request.method == "DELETE" for request in recorder.history)


def test_verify_rollback_confirms_absence_and_adopted_restoration() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    _setup_success(recorder, adopted_identity=False, adopted_copilot_fic=True, adopted_role=True)
    created_fic = f"{UAMI_URL}/federatedIdentityCredentials/{_fic(_subjects()[1])}?api-version=2024-11-30"
    adopted_fic = f"{UAMI_URL}/federatedIdentityCredentials/{_fic(_subjects()[0])}?api-version=2024-11-30"
    recorder.add("DELETE", created_fic, (204, {}))
    recorder.add("DELETE", f"{UAMI_URL}?api-version=2023-01-31", (204, {}))
    recorder.add("GET", created_fic, (404, {"error": {}}))
    recorder.add("GET", adopted_fic, (200, {"properties": {"issuer": "https://token.actions.githubusercontent.com", "subject": _subjects()[0], "audiences": ["api://AzureADTokenExchange"]}}))
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (404, {"error": {}}))
    receipt = provider.apply_bindings(_plan(_uami(adopted=False, ids=False), _role()))
    provider.rollback_bindings(receipt)
    assert provider.verify_rollback(receipt) is True


def test_restore_provider_state_rejects_tampered_hash() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    _setup_success(recorder, adopted_identity=False)
    receipt = provider.apply_bindings(_plan(_uami(adopted=False, ids=False), _role()))
    state = dict(provider.export_provider_state(receipt))
    state["state_hash"] = "0" * 64
    with pytest.raises(AzureProviderError):
        _provider(AzureTransportRecorder()).restore_provider_state(state)


def test_rollback_failure_raises_sanitized_error_and_state_still_exports() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    _setup_success(recorder, adopted_identity=False)
    created_fic = f"{UAMI_URL}/federatedIdentityCredentials/{_fic(_subjects()[0])}?api-version=2024-11-30"
    recorder.add("DELETE", created_fic, (500, {"error": {"message": "sensitive"}}))
    receipt = provider.apply_bindings(_plan(_uami(adopted=False, ids=False), _role()))
    with pytest.raises(AzureProviderError):
        provider.rollback_bindings(receipt)
    exported = provider.export_provider_state(receipt)
    assert "sensitive" not in str(exported)
