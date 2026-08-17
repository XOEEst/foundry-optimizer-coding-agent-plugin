from __future__ import annotations

import hashlib

import httpx
import pytest

from foundry_opt.bootstrap.contracts import BootstrapAction, BootstrapPlan
from foundry_opt.bootstrap.providers.azure import AzureArmRestProvider, AzureProviderError
from tests.bootstrap.fakes import AzureTransportRecorder

SUB = "55555555-5555-5555-5555-555555555555"
TENANT = "44444444-4444-4444-4444-444444444444"
CLIENT = "11111111-1111-1111-1111-111111111111"
PRINCIPAL = "22222222-2222-2222-2222-222222222222"
UAMI_ID = f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/shared-uami"
UAMI_URL = f"https://management.azure.com{UAMI_ID}"
SCOPE = f"/subscriptions/{SUB}/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acct/projects/proj"
ROLE = f"/subscriptions/{SUB}/providers/Microsoft.Authorization/roleDefinitions/00000000-0000-0000-0000-000000000111"
ASSIGNMENT_ID = "763b639a-9ee2-570d-b5c4-d6f92b269bbe"
ROLE_URL = f"https://management.azure.com{SCOPE}/providers/Microsoft.Authorization/roleAssignments/{ASSIGNMENT_ID}?api-version=2022-04-01"


def _plan(*actions: BootstrapAction) -> BootstrapPlan:
    return BootstrapPlan.create(operation_id="op", runtime_repository="https://github.com/octo-org/octo-repo.git", runtime_commit="a" * 40, repository_identity="octo-org/octo-repo", actions=actions)


def _uami(*, adopted: bool, ids: bool = True) -> BootstrapAction:
    diagnostics = [f"resource_id={UAMI_ID}", f"subscription_id={SUB}", "name=shared-uami", "location=eastus", f"adopted={'true' if adopted else 'false'}"]
    if ids:
        diagnostics += [f"client_id={CLIENT}", f"principal_id={PRINCIPAL}", f"tenant_id={TENANT}"]
    return BootstrapAction(action_id="identity", phase="azure", stage="planned", kind="managed-identity", diagnostics=tuple(diagnostics))


def _role(scope: str = SCOPE) -> BootstrapAction:
    return BootstrapAction(action_id="role", phase="azure", stage="planned", kind="role-assignment", diagnostics=("role=FoundryProjectReader", f"scope={scope}"))


def _provider(recorder: AzureTransportRecorder, token: str = "token") -> AzureArmRestProvider:
    return AzureArmRestProvider(token_provider=lambda scope: token, transport=recorder.transport(), approved_role_definitions={"FoundryProjectReader": ROLE})


def _fic(subject: str) -> str:
    return hashlib.sha256(subject.encode()).hexdigest()[:24]


def _subjects() -> tuple[str, str]:
    return ("repo:octo-org/octo-repo:environment:copilot", "repo:octo-org/octo-repo:environment:foundry-production")


def _uami_payload() -> dict[str, object]:
    return {"id": UAMI_ID, "name": "shared-uami", "location": "eastus", "properties": {"clientId": CLIENT, "principalId": PRINCIPAL, "tenantId": TENANT}}


def _role_payload() -> dict[str, object]:
    return {"properties": {"principalId": PRINCIPAL, "roleDefinitionId": ROLE}, "id": ROLE_URL.replace("?api-version=2022-04-01", "")}


def _fic_payload(subject: str) -> dict[str, object]:
    return {"properties": {"issuer": "https://token.actions.githubusercontent.com", "subject": subject, "audiences": ["api://AzureADTokenExchange"]}}


def test_authorization_uses_real_bearer_and_never_persists_token() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder, token="secret-token")
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (404, {"error": {}}))
    recorder.add("PUT", f"{UAMI_URL}?api-version=2023-01-31", (201, _uami_payload()))
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (200, _uami_payload()))
    for subject in _subjects():
        url = f"{UAMI_URL}/federatedIdentityCredentials/{_fic(subject)}?api-version=2024-11-30"
        recorder.add("GET", url, (404, {"error": {}}))
        recorder.add("PUT", url, (201, _fic_payload(subject)))
    recorder.add("GET", ROLE_URL, (404, {"error": {}}))
    recorder.add("PUT", ROLE_URL, (201, _role_payload()))
    receipt = provider.apply_bindings(_plan(_uami(adopted=False, ids=False), _role()))
    state = provider.export_provider_state(receipt)
    assert recorder.requests[0].headers["Authorization"] == "Bearer secret-token"
    assert "secret-token" not in str(state)
    assert "secret-token" not in str(receipt)


def test_verify_rejects_failed_receipt() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (200, {"id": UAMI_ID, "name": "shared-uami", "properties": {"clientId": "wrong", "principalId": PRINCIPAL, "tenantId": TENANT}}))
    receipt = provider.apply_bindings(_plan(_uami(adopted=True), _role()))
    with pytest.raises(AzureProviderError):
        provider.verify_bindings(receipt)


def test_timeout_reconciles_ambiguous_fic_write_as_created_and_rollbackable() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (404, {"error": {}}))
    recorder.add("PUT", f"{UAMI_URL}?api-version=2023-01-31", (201, _uami_payload()))
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (200, _uami_payload()))
    subject = _subjects()[0]
    fic_url = f"{UAMI_URL}/federatedIdentityCredentials/{_fic(subject)}?api-version=2024-11-30"
    recorder.add("GET", fic_url, (404, {"error": {}}))
    recorder.add("PUT", fic_url, lambda request: (_ for _ in ()).throw(httpx.TimeoutException("timeout")))
    recorder.add("GET", fic_url, (200, _fic_payload(subject)))
    recorder.add("GET", f"{UAMI_URL}/federatedIdentityCredentials/{_fic(_subjects()[1])}?api-version=2024-11-30", (404, {"error": {}}))
    recorder.add("PUT", f"{UAMI_URL}/federatedIdentityCredentials/{_fic(_subjects()[1])}?api-version=2024-11-30", (201, _fic_payload(_subjects()[1])))
    recorder.add("GET", ROLE_URL, (404, {"error": {}}))
    recorder.add("PUT", ROLE_URL, (201, _role_payload()))
    receipt = provider.apply_bindings(_plan(_uami(adopted=False, ids=False), _role()))
    state = provider.export_provider_state(receipt)
    assert state["attempts"]
    assert any(attempt["action_id"] == "azure-fic-copilot" for attempt in state["attempts"])
    assert any(attempt["action_id"] == "azure-fic-copilot" for attempt in state["federated_credentials"]) or any(attempt["action_id"] == "azure-fic-copilot" for attempt in state["attempts"])
    assert any(item.startswith("azure-fic-") for item in receipt.compensation_required_actions)


def test_existing_uami_is_never_created_and_is_adopted() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (200, _uami_payload()))
    for subject in _subjects():
        url = f"{UAMI_URL}/federatedIdentityCredentials/{_fic(subject)}?api-version=2024-11-30"
        recorder.add("GET", url, (404, {"error": {}}))
        recorder.add("PUT", url, (201, _fic_payload(subject)))
    recorder.add("GET", ROLE_URL, (404, {"error": {}}))
    recorder.add("PUT", ROLE_URL, (201, _role_payload()))
    receipt = provider.apply_bindings(_plan(_uami(adopted=False), _role()))
    state = provider.export_provider_state(receipt)
    assert "azure-uami-create" not in receipt.created_actions
    assert state["identity"]["disposition"] == "adopted"


def test_successful_receipt_exports_rollback_targets() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (404, {"error": {}}))
    recorder.add("PUT", f"{UAMI_URL}?api-version=2023-01-31", (201, _uami_payload()))
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (200, _uami_payload()))
    for subject in _subjects():
        url = f"{UAMI_URL}/federatedIdentityCredentials/{_fic(subject)}?api-version=2024-11-30"
        recorder.add("GET", url, (404, {"error": {}}))
        recorder.add("PUT", url, (201, _fic_payload(subject)))
    recorder.add("GET", ROLE_URL, (404, {"error": {}}))
    recorder.add("PUT", ROLE_URL, (201, _role_payload()))
    receipt = provider.apply_bindings(_plan(_uami(adopted=False, ids=False), _role()))
    state = provider.export_provider_state(receipt)
    assert receipt.error_info is None
    assert set(state["compensation_required_actions"]) == set(receipt.compensation_required_actions)
    assert any(str(item).startswith("azure-rbac-FoundryProjectReader-") for item in state["compensation_required_actions"])


def test_verify_compares_live_to_frozen_planned_identity() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (404, {"error": {}}))
    recorder.add("PUT", f"{UAMI_URL}?api-version=2023-01-31", (201, _uami_payload()))
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (200, _uami_payload()))
    for subject in _subjects():
        url = f"{UAMI_URL}/federatedIdentityCredentials/{_fic(subject)}?api-version=2024-11-30"
        recorder.add("GET", url, (404, {"error": {}}))
        recorder.add("PUT", url, (201, _fic_payload(subject)))
    recorder.add("GET", ROLE_URL, (404, {"error": {}}))
    recorder.add("PUT", ROLE_URL, (201, _role_payload()))
    receipt = provider.apply_bindings(_plan(_uami(adopted=False), _role()))
    state = provider.export_provider_state(receipt)
    restarted = _provider(recorder)
    restarted.restore_provider_state(state)
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (200, {"id": UAMI_ID, "name": "shared-uami", "location": "eastus", "properties": {"clientId": "different", "principalId": PRINCIPAL, "tenantId": TENANT}}))
    with pytest.raises(AzureProviderError):
        restarted.verify_bindings(receipt)


def test_verify_rollback_proves_created_absent_and_adopted_exact() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (200, _uami_payload()))
    adopted_subject = _subjects()[0]
    created_subject = _subjects()[1]
    recorder.add("GET", f"{UAMI_URL}/federatedIdentityCredentials/{_fic(adopted_subject)}?api-version=2024-11-30", (200, _fic_payload(adopted_subject)))
    recorder.add("GET", f"{UAMI_URL}/federatedIdentityCredentials/{_fic(created_subject)}?api-version=2024-11-30", (404, {"error": {}}))
    recorder.add("PUT", f"{UAMI_URL}/federatedIdentityCredentials/{_fic(created_subject)}?api-version=2024-11-30", (201, _fic_payload(created_subject)))
    recorder.add("GET", ROLE_URL, (200, _role_payload()))
    receipt = provider.apply_bindings(_plan(_uami(adopted=False), _role()))
    recorder.add("DELETE", f"{UAMI_URL}/federatedIdentityCredentials/{_fic(created_subject)}?api-version=2024-11-30", (204, {}))
    provider.rollback_bindings(receipt)
    recorder.add("GET", ROLE_URL, (200, _role_payload()))
    recorder.add("GET", f"{UAMI_URL}/federatedIdentityCredentials/{_fic(adopted_subject)}?api-version=2024-11-30", (200, _fic_payload(adopted_subject)))
    recorder.add("GET", f"{UAMI_URL}/federatedIdentityCredentials/{_fic(created_subject)}?api-version=2024-11-30", (404, {"error": {}}))
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (200, _uami_payload()))
    assert provider.verify_rollback(receipt) is True


def test_restore_provider_state_rejects_tampering() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (404, {"error": {}}))
    recorder.add("PUT", f"{UAMI_URL}?api-version=2023-01-31", (201, _uami_payload()))
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (200, _uami_payload()))
    for subject in _subjects():
        url = f"{UAMI_URL}/federatedIdentityCredentials/{_fic(subject)}?api-version=2024-11-30"
        recorder.add("GET", url, (404, {"error": {}}))
        recorder.add("PUT", url, (201, _fic_payload(subject)))
    recorder.add("GET", ROLE_URL, (404, {"error": {}}))
    recorder.add("PUT", ROLE_URL, (201, _role_payload()))
    receipt = provider.apply_bindings(_plan(_uami(adopted=False, ids=False), _role()))
    state = dict(provider.export_provider_state(receipt))
    state["state_hash"] = "0" * 64
    with pytest.raises(AzureProviderError):
        _provider(AzureTransportRecorder()).restore_provider_state(state)
