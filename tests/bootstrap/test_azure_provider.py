from __future__ import annotations

import hashlib

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


def _fic_payload(subject: str) -> dict[str, object]:
    return {"properties": {"issuer": "https://token.actions.githubusercontent.com", "subject": subject, "audiences": ["api://AzureADTokenExchange"]}}


def _role_payload(*, condition: str | None = None, condition_version: str | None = None, delegated_id: str | None = None) -> dict[str, object]:
    return {
        "properties": {
            "principalId": PRINCIPAL,
            "roleDefinitionId": ROLE,
            "condition": condition,
            "conditionVersion": condition_version,
            "delegatedManagedIdentityResourceId": delegated_id,
        },
        "id": ROLE_URL.replace("?api-version=2022-04-01", ""),
    }


def test_real_bearer_header_and_no_token_persistence() -> None:
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


def test_restore_reconciles_ambiguous_uami_before_verify_rollback() -> None:
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
    exported = provider.export_provider_state(receipt)
    state = dict(exported)
    state["attempts"][0]["disposition"] = "ambiguous"
    state["identity"]["disposition"] = "created"
    from foundry_opt.bootstrap.canonical import canonical_sha256
    state["state_hash"] = canonical_sha256({k: v for k, v in state.items() if k != "state_hash"})
    restarted = _provider(recorder)
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (200, _uami_payload()))
    restarted.restore_provider_state(state)
    recorder.add("DELETE", ROLE_URL, (204, {}))
    for subject in reversed(_subjects()):
        recorder.add("DELETE", f"{UAMI_URL}/federatedIdentityCredentials/{_fic(subject)}?api-version=2024-11-30", (204, {}))
    recorder.add("DELETE", f"{UAMI_URL}?api-version=2023-01-31", (204, {}))
    restarted.rollback_bindings(receipt)
    recorder.add("GET", ROLE_URL, (404, {"error": {}}))
    for subject in _subjects():
        recorder.add("GET", f"{UAMI_URL}/federatedIdentityCredentials/{_fic(subject)}?api-version=2024-11-30", (404, {"error": {}}))
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (404, {"error": {}}))
    assert restarted.verify_rollback(receipt) is True


def test_fic_http_200_is_changed_and_restored_from_preimage() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (200, _uami_payload()))
    subject = _subjects()[0]
    fic_url = f"{UAMI_URL}/federatedIdentityCredentials/{_fic(subject)}?api-version=2024-11-30"
    recorder.add("GET", fic_url, (200, {"properties": {"issuer": "old", "subject": subject, "audiences": ["old"]}}))
    recorder.add("PUT", fic_url, (200, _fic_payload(subject)))
    other = _subjects()[1]
    other_url = f"{UAMI_URL}/federatedIdentityCredentials/{_fic(other)}?api-version=2024-11-30"
    recorder.add("GET", other_url, (404, {"error": {}}))
    recorder.add("PUT", other_url, (201, _fic_payload(other)))
    recorder.add("GET", ROLE_URL, (200, _role_payload()))
    receipt = provider.apply_bindings(_plan(_uami(adopted=False), _role()))
    state = provider.export_provider_state(receipt)
    fic_state = next(item for item in state["federated_credentials"] if item["action_id"] == "azure-fic-copilot")
    assert fic_state["disposition"] == "changed"
    recorder.add("PUT", fic_url, (200, {"properties": {"issuer": "old", "subject": subject, "audiences": ["old"]}}))
    recorder.add("DELETE", other_url, (204, {}))
    provider.rollback_bindings(receipt)


def test_rbac_http_200_is_changed_and_restored_from_preimage() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (200, _uami_payload()))
    for subject in _subjects():
        url = f"{UAMI_URL}/federatedIdentityCredentials/{_fic(subject)}?api-version=2024-11-30"
        recorder.add("GET", url, (200, _fic_payload(subject)))
    recorder.add("GET", ROLE_URL, (404, {"error": {}}))
    recorder.add("GET", ROLE_URL, (200, _role_payload(condition="old", condition_version="1.0")))
    recorder.add("PUT", ROLE_URL, (200, _role_payload()))
    receipt = provider.apply_bindings(_plan(_uami(adopted=False), _role()))
    state = provider.export_provider_state(receipt)
    role_state = state["role_assignments"][0]
    assert role_state["disposition"] == "changed"
    recorder.add("PUT", ROLE_URL, (200, _role_payload(condition="old", condition_version="1.0")))
    provider.rollback_bindings(receipt)


def test_uami_http_200_change_without_preimage_fails_closed() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (404, {"error": {}}))
    recorder.add("PUT", f"{UAMI_URL}?api-version=2023-01-31", (200, _uami_payload()))
    with pytest.raises(AzureProviderError):
        provider.apply_bindings(_plan(_uami(adopted=False, ids=False), _role()))


def test_adopted_role_verification_requires_default_condition_fields() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (200, _uami_payload()))
    for subject in _subjects():
        url = f"{UAMI_URL}/federatedIdentityCredentials/{_fic(subject)}?api-version=2024-11-30"
        recorder.add("GET", url, (200, _fic_payload(subject)))
    recorder.add("GET", ROLE_URL, (200, _role_payload(condition="x", condition_version="2.0", delegated_id="/subscriptions/x")))
    recorder.add("PUT", ROLE_URL, (200, _role_payload()))
    receipt = provider.apply_bindings(_plan(_uami(adopted=False), _role()))
    recorder.add("GET", ROLE_URL, (200, _role_payload(condition="x", condition_version="2.0", delegated_id="/subscriptions/x")))
    with pytest.raises(AzureProviderError):
        provider.verify_bindings(receipt)


def test_scope_comparison_is_casefolded() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    recorder.add("GET", f"{UAMI_URL}?api-version=2023-01-31", (200, _uami_payload()))
    for subject in _subjects():
        url = f"{UAMI_URL}/federatedIdentityCredentials/{_fic(subject)}?api-version=2024-11-30"
        recorder.add("GET", url, (200, _fic_payload(subject)))
    mixed_scope_url = f"https://management.azure.com/subscriptions/{SUB.upper()}/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acct/projects/proj/providers/Microsoft.Authorization/roleAssignments/{ASSIGNMENT_ID}?api-version=2022-04-01"
    recorder.add("GET", mixed_scope_url, (200, {"properties": {"principalId": PRINCIPAL, "roleDefinitionId": ROLE, "condition": None, "conditionVersion": None, "delegatedManagedIdentityResourceId": None}, "id": mixed_scope_url.replace('?api-version=2022-04-01', '')}))
    receipt = provider.apply_bindings(_plan(_uami(adopted=False), _role()))
    assert provider.verify_bindings(receipt) is True
