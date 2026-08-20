"""Azure identity planning must name the exact resource, never a placeholder."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from foundry_opt.bootstrap.contracts import BootstrapPlan
from foundry_opt.bootstrap.drivers import AzurePhaseDriver
from foundry_opt.bootstrap.errors import BootstrapConfigError
from foundry_opt.bootstrap.input_contracts import (
    AzureIdentityInput,
    BootstrapPlanInput,
    TrustedTemplateManifest,
)
from foundry_opt.bootstrap.plan_factory import build_phase_actions
from foundry_opt.bootstrap.providers.azure import AzureArmRestProvider

SUBSCRIPTION = "33333333-3333-3333-3333-333333333333"
TENANT = "22222222-2222-2222-2222-222222222222"
CLIENT_ID = "44444444-4444-4444-4444-444444444444"
OBJECT_ID = "55555555-5555-5555-5555-555555555555"
CONTRACT_ERRORS = (BootstrapConfigError, ValidationError)


def _resource_id(name: str, *, resource_group: str = "example-rg") -> str:
    return (
        f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{resource_group}"
        f"/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{name}"
    )


def _plan_input(
    identity: dict[str, object],
    *,
    resource_group: str = "example-rg",
    oidc_subject_prefix: str | None = None,
) -> BootstrapPlanInput:
    manifest = TrustedTemplateManifest.load_pinned_manifest()
    payload = {
        "schema_version": 1,
        "repository": {
            "schema_version": 1,
            "repository_id": "org/repo",
            "repository_url": "https://github.com/org/repo.git",
            "default_branch": "main",
            "root": ".",
            "selected_agents": [
                {
                    "schema_version": 1,
                    "repo_agent_id": "app",
                    "root": "app",
                    "config_path": "app/.foundry/foundry-opt.yaml",
                    "editable_paths": ["app/main.py"],
                }
            ],
        },
        "runtime_provenance": {
            "schema_version": 1,
            "runtime_repository_url": "https://github.com/XOEEst/foundry-optimizer-coding-agent-plugin.git",
            "runtime_commit": "a" * 40,
            "uv_lock_sha256": "0" * 64,
        },
        "repository_phase": {
            "schema_version": 1,
            "trusted_manifest_id": manifest.manifest_id,
            "trusted_manifest_version": manifest.manifest_version,
            "trusted_manifest_hash": manifest.manifest_hash,
            "agent_render_contexts": [{"schema_version": 1, "repo_agent_id": "app", "values": []}],
        },
        "offline_plan": False,
        "required_phases": ["repository", "azure"],
        "azure_phase": {
            "schema_version": 1,
            "tenant_id": TENANT,
            "subscription_id": SUBSCRIPTION,
            "identity": {"schema_version": 1, **identity},
            "resource_group": resource_group,
            "location": "eastus2",
            "github_repository_id": "org/repo",
            "approved_role_assignments": [
                {
                    "schema_version": 1,
                    "alias": "foundry-user",
                    "role_definition_id": (
                        f"/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Authorization"
                        "/roleDefinitions/53ca6127-db72-4b80-b1b0-d745d6d5456d"
                    ),
                    "scope": (
                        f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{resource_group}"
                        "/providers/Microsoft.CognitiveServices/accounts/example"
                    ),
                }
            ],
        },
    }
    if oidc_subject_prefix is not None:
        payload["required_phases"] = ["repository", "github", "azure"]
        payload["github_phase"] = {
            "schema_version": 1,
            "optimizer_environment": "copilot",
            "deployment_environment": "foundry-production",
            "shared_client_id": identity["existing_client_id"],
            "client_id_variable_name": "AZURE_OPTIMIZER_CLIENT_ID",
            "oidc_subject_prefix": oidc_subject_prefix,
            "default_branch_policy_intent": "preserve_repository_default",
        }
    return BootstrapPlanInput.model_validate(payload)


def _identity_diagnostics(plan_input: BootstrapPlanInput) -> dict[str, str]:
    action = next(action for action in build_phase_actions(plan_input) if action.action_id == "azure-identity")
    return {entry.split("=", 1)[0]: entry.split("=", 1)[1] for entry in action.diagnostics}


def test_adopted_identity_name_comes_from_the_resource_id() -> None:
    plan_input = _plan_input(
        {
            "identity_kind": "user_assigned_managed_identity",
            "existing_resource_id": _resource_id("pilot-foundry-opt"),
            "existing_client_id": CLIENT_ID,
            "existing_object_id": OBJECT_ID,
        }
    )

    diagnostics = _identity_diagnostics(plan_input)

    assert diagnostics["name"] == "pilot-foundry-opt"
    assert diagnostics["name"] != "shared-uami"
    assert diagnostics["adopted"] == "true"
    assert diagnostics["resource_id"] == _resource_id("pilot-foundry-opt")


def test_create_if_missing_names_the_reviewed_creation_target() -> None:
    plan_input = _plan_input(
        {
            "identity_kind": "user_assigned_managed_identity",
            "existing_resource_id": _resource_id("new-pilot-identity"),
            "create_if_missing": True,
        }
    )

    diagnostics = _identity_diagnostics(plan_input)

    assert diagnostics["name"] == "new-pilot-identity"
    assert diagnostics["adopted"] == "false"
    assert diagnostics["resource_id"] == _resource_id("new-pilot-identity")


@pytest.mark.parametrize("name", ["shared-uami", "customer_identity", "Identity01"])
def test_every_identity_name_round_trips_into_the_plan(name: str) -> None:
    plan_input = _plan_input(
        {
            "identity_kind": "user_assigned_managed_identity",
            "existing_resource_id": _resource_id(name),
            "create_if_missing": True,
        }
    )

    assert _identity_diagnostics(plan_input)["name"] == name
    assert plan_input.azure_phase is not None
    assert plan_input.azure_phase.identity.identity_name == name


def test_identity_name_matches_the_provider_live_document_name() -> None:
    # The ARM provider records the live `name` returned by Azure; a planned placeholder would
    # disagree with the created or adopted resource.
    identity = AzureIdentityInput(
        identity_kind="user_assigned_managed_identity",
        existing_resource_id=_resource_id("exact-name"),
        create_if_missing=True,
    )
    live_document = {"id": identity.existing_resource_id, "name": "exact-name"}

    assert identity.identity_name == live_document["name"]
    assert identity.existing_resource_id.rsplit("/", 1)[-1] == identity.identity_name


def test_adopted_entra_application_is_labelled_by_its_client_id() -> None:
    plan_input = _plan_input(
        {
            "identity_kind": "entra_application",
            "existing_client_id": CLIENT_ID,
            "existing_object_id": OBJECT_ID,
        }
    )

    diagnostics = _identity_diagnostics(plan_input)

    assert diagnostics["name"] == CLIENT_ID
    assert diagnostics["client_id"] == CLIENT_ID
    assert "resource_id" not in diagnostics


def test_adopted_entra_application_emits_application_object_id() -> None:
    plan_input = _plan_input(
        {
            "identity_kind": "entra_application",
            "existing_client_id": CLIENT_ID,
            "existing_object_id": OBJECT_ID,
        }
    )

    diagnostics = _identity_diagnostics(plan_input)

    assert diagnostics["object_id"] == OBJECT_ID
    assert "principal_id" not in diagnostics


def test_entra_application_and_immutable_subjects_round_trip_through_driver() -> None:
    prefix = "repo:org@123/repo@456"
    plan_input = _plan_input(
        {
            "identity_kind": "entra_application",
            "existing_client_id": CLIENT_ID,
            "existing_object_id": OBJECT_ID,
        },
        oidc_subject_prefix=prefix,
    )
    provider = AzureArmRestProvider(
        token_provider=lambda scope: "token",
        approved_role_definitions={
            "foundry-user": (
                f"/subscriptions/{SUBSCRIPTION}/providers/"
                "Microsoft.Authorization/roleDefinitions/"
                "53ca6127-db72-4b80-b1b0-d745d6d5456d"
            )
        },
    )
    driver = AzurePhaseDriver(plan_input=plan_input, provider=provider)

    actions = driver.plan(
        {
            "operation_id": "immutable-entra-plan",
            "runtime_repository": "https://github.com/org/runtime.git",
            "runtime_commit": "a" * 40,
            "repository_id": "org/repo",
        }
    )

    identity = next(action for action in actions if action.kind == "entra-application")
    diagnostics = {
        entry.split("=", 1)[0]: entry.split("=", 1)[1]
        for entry in identity.diagnostics
    }
    subjects = tuple(
        action.diagnostics[0].removeprefix("subject=")
        for action in actions
        if action.kind == "federated-credential"
    )
    assert diagnostics["object_id"] == OBJECT_ID
    assert subjects == (
        f"{prefix}:environment:copilot",
        f"{prefix}:environment:foundry-production",
    )


def test_non_managed_identity_resource_ids_fail_closed() -> None:
    with pytest.raises(CONTRACT_ERRORS, match="Microsoft.ManagedIdentity/userAssignedIdentities"):
        AzureIdentityInput(
            identity_kind="user_assigned_managed_identity",
            existing_resource_id=(
                f"/subscriptions/{SUBSCRIPTION}/resourceGroups/example-rg"
                "/providers/Microsoft.CognitiveServices/accounts/example"
            ),
        )
    with pytest.raises(CONTRACT_ERRORS, match="Microsoft.ManagedIdentity/userAssignedIdentities"):
        AzureIdentityInput(
            identity_kind="user_assigned_managed_identity",
            existing_resource_id=_resource_id("nested/name"),
        )


def test_unresolvable_identity_name_fails_closed() -> None:
    identity = AzureIdentityInput(identity_kind="unresolved_migration")

    with pytest.raises(BootstrapConfigError, match="resolved identity resource id or client id"):
        _ = identity.identity_name


def test_plan_factory_no_longer_hardcodes_an_identity_name() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "src" / "foundry_opt" / "bootstrap" / "plan_factory.py"
    ).read_text(encoding="utf-8")

    assert "shared-uami" not in source
    assert "name={identity.identity_name}" in source


def test_identity_diagnostics_stay_json_safe_and_bounded() -> None:
    plan_input = _plan_input(
        {
            "identity_kind": "user_assigned_managed_identity",
            "existing_resource_id": _resource_id("pilot-foundry-opt"),
            "create_if_missing": True,
        }
    )
    action = next(action for action in build_phase_actions(plan_input) if action.action_id == "azure-identity")

    encoded = json.dumps(action.model_dump(mode="json"))
    assert "pilot-foundry-opt" in encoded
    assert all("=" in entry for entry in action.diagnostics)


def test_fresh_apply_process_restores_approved_role_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan_input = _plan_input(
        {
            "identity_kind": "user_assigned_managed_identity",
            "existing_resource_id": _resource_id("pilot-identity"),
            "create_if_missing": True,
        }
    )
    captured: dict[str, str] = {}

    class _Provider:
        def __init__(
            self,
            *,
            token_provider,
            approved_role_definitions,
        ) -> None:
            del token_provider
            captured.update(approved_role_definitions)

        def apply_bindings(self, plan: BootstrapPlan) -> BootstrapPlan:
            return plan

    monkeypatch.setattr(
        "foundry_opt.bootstrap.drivers.AzureArmRestProvider",
        _Provider,
    )
    driver = AzurePhaseDriver(plan_input=plan_input)
    phase_plan = BootstrapPlan.create(
        operation_id="apply-in-new-process",
        runtime_repository=(
            "https://github.com/XOEEst/"
            "foundry-optimizer-coding-agent-plugin.git"
        ),
        runtime_commit="a" * 40,
        repository_identity="org/repo",
        actions=(),
    )

    assert driver.apply(phase_plan) is phase_plan
    assert captured == {
        "foundry-user": (
            f"/subscriptions/{SUBSCRIPTION}/providers/"
            "Microsoft.Authorization/roleDefinitions/"
            "53ca6127-db72-4b80-b1b0-d745d6d5456d"
        )
    }
