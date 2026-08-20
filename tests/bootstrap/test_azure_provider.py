from __future__ import annotations

import hashlib

import httpx
import pytest

from foundry_opt.bootstrap.contracts import BootstrapAction, BootstrapPlan
from foundry_opt.bootstrap.providers.azure import (
    AzureArmRestProvider,
    AzureIdentityReference,
    AzureProviderError,
)
from tests.bootstrap.fakes import AzureTransportRecorder, json_body

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
APPLICATION_OBJECT_ID = "33333333-3333-3333-3333-333333333333"
GRAPH_FIC_COLLECTION = (
    "https://graph.microsoft.com/v1.0/applications/"
    f"{APPLICATION_OBJECT_ID}/federatedIdentityCredentials"
)


def _plan(*actions: BootstrapAction) -> BootstrapPlan:
    planned_actions = list(actions)
    if not any(action.kind == "federated-credential" for action in planned_actions):
        planned_actions.extend(_fic_action(subject) for subject in _subjects())
    return BootstrapPlan.create(operation_id="op", runtime_repository="https://github.com/octo-org/octo-repo.git", runtime_commit="a" * 40, repository_identity="octo-org/octo-repo", actions=tuple(planned_actions))


def _uami(*, adopted: bool, ids: bool = True) -> BootstrapAction:
    diagnostics = [f"resource_id={UAMI_ID}", f"subscription_id={SUB}", "name=shared-uami", "location=eastus", f"adopted={'true' if adopted else 'false'}"]
    if ids:
        diagnostics += [f"client_id={CLIENT}", f"principal_id={PRINCIPAL}", f"tenant_id={TENANT}"]
    return BootstrapAction(action_id="identity", phase="azure", stage="planned", kind="managed-identity", diagnostics=tuple(diagnostics))


def _role(scope: str = SCOPE) -> BootstrapAction:
    return BootstrapAction(action_id="role", phase="azure", stage="planned", kind="role-assignment", diagnostics=("role=FoundryProjectReader", f"scope={scope}"))


def _fic_action(subject: str) -> BootstrapAction:
    return BootstrapAction(
        action_id=f"fic-{hashlib.sha256(subject.encode()).hexdigest()[:8]}",
        phase="azure",
        stage="planned",
        kind="federated-credential",
        diagnostics=(f"subject={subject}",),
    )


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


def _entra_identity() -> AzureIdentityReference:
    return AzureIdentityReference(
        kind="entra_application",
        client_id=CLIENT,
        resource_id=None,
        object_id=APPLICATION_OBJECT_ID,
        principal_id=PRINCIPAL,
        tenant_id=TENANT,
        subscription_id=SUB,
        name="existing-app",
        adopted=True,
    )


def _entra_action() -> BootstrapAction:
    return BootstrapAction(
        action_id="identity",
        phase="azure",
        stage="planned",
        kind="entra-application",
        diagnostics=(
            f"subscription_id={SUB}",
            f"tenant_id={TENANT}",
            f"client_id={CLIENT}",
            f"object_id={APPLICATION_OBJECT_ID}",
            f"name={CLIENT}",
            "adopted=true",
        ),
    )


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


def test_planned_bindings_are_replayable_and_bind_role_approval() -> None:
    provider = _provider(AzureTransportRecorder())
    original = _plan(_uami(adopted=True), _role())

    actions = tuple(provider.plan_bindings(original))
    replay = _plan(*actions)

    assert provider._planned_bindings(replay) == provider._planned_bindings(original)
    role = next(action for action in actions if action.kind == "role-assignment")
    tampered = role.model_copy(
        update={
            "diagnostics": tuple(
                "approved_role_sha256=" + ("0" * 64)
                if item.startswith("approved_role_sha256=")
                else item
                for item in role.diagnostics
            )
        }
    )
    with pytest.raises(
        AzureProviderError,
        match="drifted from planned approval fingerprint",
    ):
        provider._planned_bindings(
            _plan(*(tampered if action is role else action for action in actions))
        )


def test_planned_bindings_preserve_explicit_immutable_subjects() -> None:
    provider = _provider(AzureTransportRecorder())
    prefix = "repo:octo-org@123/octo-repo@456"
    subjects = (
        f"{prefix}:environment:copilot",
        f"{prefix}:environment:foundry-production",
    )
    identity = BootstrapAction(
        action_id="identity",
        phase="azure",
        stage="planned",
        kind="entra-application",
        diagnostics=(
            f"subscription_id={SUB}",
            f"tenant_id={TENANT}",
            f"client_id={CLIENT}",
            f"object_id={PRINCIPAL}",
            f"name={CLIENT}",
            "adopted=true",
        ),
    )

    planned = provider._planned_bindings(
        _plan(identity, *(_fic_action(subject) for subject in subjects))
    )

    assert planned.identity.object_id == PRINCIPAL
    assert planned.subjects == subjects


def test_planned_bindings_require_exactly_two_explicit_subjects() -> None:
    provider = _provider(AzureTransportRecorder())
    identity = _uami(adopted=True)
    one_subject = _fic_action(_subjects()[0])

    with pytest.raises(
        AzureProviderError,
        match="exactly two federated credential subjects",
    ):
        provider._planned_bindings(
            BootstrapPlan.create(
                operation_id="missing-fic",
                runtime_repository="https://github.com/octo-org/octo-repo.git",
                runtime_commit="a" * 40,
                repository_identity="octo-org/octo-repo",
                actions=(identity, one_subject),
            )
        )


def test_entra_fic_adopts_exact_subject_regardless_of_legacy_name() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    subject = _subjects()[0]
    existing = {
        "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "name": "github-octo-repo-copilot",
        "issuer": "https://token.actions.githubusercontent.com",
        "subject": subject,
        "audiences": ["api://AzureADTokenExchange"],
    }
    recorder.add(
        "GET",
        GRAPH_FIC_COLLECTION,
        (200, {"value": [existing]}),
    )

    observed = provider._get_fic(_entra_identity(), subject)

    assert observed == existing
    assert [str(request.url) for request in recorder.requests] == [
        GRAPH_FIC_COLLECTION
    ]


def test_entra_apply_adopts_legacy_name_and_creates_only_missing_subject() -> None:
    subjects = _subjects()
    credentials: list[dict[str, object]] = [
        {
            "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "name": "github-octo-repo-copilot",
            "issuer": "https://token.actions.githubusercontent.com",
            "subject": subjects[0],
            "audiences": ["api://AzureADTokenExchange"],
        }
    ]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        url = str(request.url)
        if request.method == "GET" and url == (
            "https://graph.microsoft.com/v1.0/applications/"
            f"{APPLICATION_OBJECT_ID}"
        ):
            return httpx.Response(
                200,
                json={
                    "id": APPLICATION_OBJECT_ID,
                    "appId": CLIENT,
                    "displayName": "existing-app",
                },
                request=request,
            )
        if request.method == "GET" and request.url.path.endswith(
            "/servicePrincipals"
        ):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": PRINCIPAL,
                            "appOwnerOrganizationId": TENANT,
                        }
                    ]
                },
                request=request,
            )
        if request.method == "GET" and url == GRAPH_FIC_COLLECTION:
            return httpx.Response(
                200,
                json={"value": list(credentials)},
                request=request,
            )
        if request.method == "POST" and url == GRAPH_FIC_COLLECTION:
            body = dict(json_body(request))
            created = {
                "id": "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
                **body,
            }
            credentials.append(created)
            return httpx.Response(201, json=created, request=request)
        return httpx.Response(
            404,
            json={"error": {"code": "not_found"}},
            request=request,
        )

    provider = AzureArmRestProvider(
        token_provider=lambda scope: "token",
        transport=httpx.MockTransport(handler),
    )

    receipt = provider.apply_bindings(
        _plan(
            _entra_action(),
            *(_fic_action(subject) for subject in subjects),
        )
    )
    state = provider.export_provider_state(receipt)

    assert receipt.adopted_actions == ("azure-fic-copilot",)
    assert receipt.created_actions == ("azure-fic-foundry-production",)
    assert len([request for request in requests if request.method == "POST"]) == 1
    assert state["federated_credentials"][0]["resource_id"].endswith(
        "/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    )
    assert state["federated_credentials"][1]["resource_id"].endswith(
        "/bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    )


def test_entra_fic_inventory_fails_closed_on_subject_mismatch() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    subject = _subjects()[0]
    recorder.add(
        "GET",
        GRAPH_FIC_COLLECTION,
        (
            200,
            {
                "value": [
                    {
                        "id": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                        "name": "legacy",
                        "issuer": "https://token.actions.githubusercontent.com",
                        "subject": subject,
                        "audiences": ["unexpected"],
                    }
                ]
            },
        ),
    )

    with pytest.raises(
        AzureProviderError,
        match="unexpected issuer or audience",
    ):
        provider._get_fic(_entra_identity(), subject)


def test_entra_fic_delete_uses_graph_credential_id() -> None:
    recorder = AzureTransportRecorder()
    provider = _provider(recorder)
    subject = _subjects()[0]
    credential_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    recorder.add(
        "GET",
        GRAPH_FIC_COLLECTION,
        (
            200,
            {
                "value": [
                    {
                        "id": credential_id,
                        "name": "legacy",
                        "issuer": "https://token.actions.githubusercontent.com",
                        "subject": subject,
                        "audiences": ["api://AzureADTokenExchange"],
                    }
                ]
            },
        ),
    )
    recorder.add(
        "DELETE",
        f"{GRAPH_FIC_COLLECTION}/{credential_id}",
        (204, {}),
    )

    provider._delete_fic(_entra_identity(), subject)

    assert str(recorder.requests[-1].url) == (
        f"{GRAPH_FIC_COLLECTION}/{credential_id}"
    )


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
