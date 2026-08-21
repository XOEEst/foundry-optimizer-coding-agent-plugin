from __future__ import annotations

import pytest
import yaml

from foundry_opt.bootstrap.errors import BootstrapPlanError
from foundry_opt.bootstrap.input_contracts import BootstrapPlanInput, TrustedTemplateManifest
from foundry_opt.bootstrap.plan_factory import build_phase_actions, load_trusted_manifest

_SHARED_CLIENT_ID = "11111111-1111-1111-1111-111111111111"
_TENANT_ID = "22222222-2222-2222-2222-222222222222"
_SUBSCRIPTION_ID = "33333333-3333-3333-3333-333333333333"


def _manifest_hash() -> str:
    return TrustedTemplateManifest.load_pinned_manifest().manifest_hash


def _selected_profile_payload(*, package_root: str = "agent") -> dict[str, object]:
    return {
        "schema_version": 1,
        "package_root": package_root,
        "runtime": {
            "schema_version": 1,
            "kind": "hosted",
            "runtime": "python_3_13",
            "entrypoint": ["python", "main.py"],
            "dependency_resolution": "remote_build",
            "protocol_name": "responses",
            "protocol_version": "2.0.0",
        },
        "foundry_project": {
            "schema_version": 1,
            "project_endpoint": "https://example.services.ai.azure.com/api/projects/example",
            "account_resource_id": (
                f"/subscriptions/{_SUBSCRIPTION_ID}/resourceGroups/example-rg/"
                "providers/Microsoft.CognitiveServices/accounts/example"
            ),
            "agent_name": "example-agent",
            "model_deployment_aliases": ["baseline-model"],
        },
        "baseline_model": "baseline-model",
        "allowed_models": ["baseline-model"],
        "min_candidates": 1,
        "max_candidates": 2,
        "primary_metric": "quality",
        "decision_policy": {
            "schema_version": 1,
            "minimum_aggregate_delta": 0.01,
            "focused_cases_required": True,
            "max_regressions": 0,
        },
        "hard_guardrails": [
            {
                "schema_version": 1,
                "evaluator_name": "safety",
                "required_pass_rate": 1.0,
                "required": True,
            }
        ],
        "deployment": {
            "schema_version": 1,
            "environment": "foundry-production",
            "enabled": True,
            "require_aligned_binding": True,
        },
    }


def _plan_input(
    *,
    optimizer_environment: str,
    deployment_environment: str,
    client_id_variable_name: str = "AZURE_OPTIMIZER_CLIENT_ID",
    shared_client_id: str = _SHARED_CLIENT_ID,
    branch_policy_intent: str = "preserve_repository_default",
    default_branch: str = "main",
    include_azure: bool = False,
    oidc_subject_prefix: str | None = None,
    selected_root: str = "agent",
    discovery_root: str | None = None,
    include_profile: bool = False,
    selected_enabled: bool | None = None,
    reviewed_target: dict[str, object] | None = None,
) -> BootstrapPlanInput:
    payload: dict[str, object] = {
        "schema_version": 1,
        "repository": {
            "schema_version": 1,
            "repository_id": "example-org/example-repo",
            "repository_url": "https://github.com/example-org/example-repo.git",
            "default_branch": default_branch,
            "root": ".",
            "selected_agents": [
                {
                    "schema_version": 1,
                    "repo_agent_id": "example-agent",
                    "root": selected_root,
                    **(
                        {"discovery_root": discovery_root}
                        if discovery_root is not None
                        else {}
                    ),
                    "config_path": "agent/.foundry/foundry-opt.yaml",
                    "editable_paths": ["agent/main.py"],
                    **(
                        {"enabled": selected_enabled}
                        if selected_enabled is not None
                        else {}
                    ),
                    **(
                        {
                            "profile": _selected_profile_payload(
                                package_root="agent"
                            )
                        }
                        if include_profile
                        else {}
                    ),
                    **(
                        {"foundry_target": reviewed_target}
                        if reviewed_target is not None
                        else {}
                    ),
                }
            ],
        },
        "runtime_provenance": {
            "schema_version": 1,
            "runtime_repository_url": "https://github.com/XOEEst/foundry-optimizer-coding-agent-plugin-runtime.git",
            "runtime_commit": "c899b718f3baebcfd08209ee5184d0cf61d8153d",
            "uv_lock_sha256": "74d7bb534c53e71a61ce197f3d5fa3169f2413373c2e42617280e78e83d6c681",
        },
        "repository_phase": {
            "schema_version": 1,
            "trusted_manifest_id": "foundry-v1-managed-payloads",
            "trusted_manifest_version": "1.0.0",
            "trusted_manifest_hash": _manifest_hash(),
            "agent_render_contexts": [
                {
                    "schema_version": 1,
                    "repo_agent_id": "example-agent",
                    "values": [{"schema_version": 1, "key": "selectedRoot", "value": "agent"}],
                }
            ],
        },
        "offline_plan": False,
        "required_phases": ["github", *(["azure"] if include_azure else [])],
        "github_phase": {
            "schema_version": 1,
            "optimizer_environment": optimizer_environment,
            "deployment_environment": deployment_environment,
            "shared_client_id": shared_client_id,
            "client_id_variable_name": client_id_variable_name,
            "oidc_subject_prefix": oidc_subject_prefix,
            "default_branch_policy_intent": branch_policy_intent,
        },
    }
    if include_azure:
        payload["azure_phase"] = {
            "schema_version": 1,
            "tenant_id": _TENANT_ID,
            "subscription_id": _SUBSCRIPTION_ID,
            "identity": {
                "schema_version": 1,
                "identity_kind": "user_assigned_managed_identity",
                "existing_resource_id": (
                    f"/subscriptions/{_SUBSCRIPTION_ID}/resourceGroups/example-rg/"
                    "providers/Microsoft.ManagedIdentity/userAssignedIdentities/example"
                ),
                "existing_client_id": _SHARED_CLIENT_ID,
                "existing_object_id": (
                    "44444444-4444-4444-4444-444444444444"
                ),
                "create_if_missing": False,
            },
            "resource_group": "example-rg",
            "location": "eastus2",
            "github_repository_id": "example-org/example-repo",
            "approved_role_assignments": [],
        }
    return BootstrapPlanInput.model_validate(payload)


def test_registry_uses_agent_directory_for_repository_root_discovery() -> None:
    plan_input = _plan_input(
        optimizer_environment="copilot",
        deployment_environment="foundry-production",
        selected_root=".",
    )
    selected = plan_input.repository.selected_agents[0]
    assert selected.discovery_selection_root == "."
    assert selected.root == "agent"

    payloads = load_trusted_manifest(plan_input)
    registry_payload = next(
        payload
        for payload in payloads
        if payload.template_id == "registry"
    )
    registry = yaml.safe_load(registry_payload.rendered_template)

    assert registry["agents"] == [
        {
            "schema_version": 1,
            "agent_id": "example-agent",
            "root": "agent",
            "config_path": "agent/.foundry/foundry-opt.yaml",
            "enabled": False,
        }
    ]


def test_sidecar_payload_renders_quick_profile_and_explicit_enabled_state() -> None:
    payloads = load_trusted_manifest(
        _plan_input(
            optimizer_environment="copilot",
            deployment_environment="foundry-production",
            include_profile=True,
            selected_enabled=True,
        )
    )
    sidecar = next(payload for payload in payloads if payload.template_id == "sidecar")
    registry = next(payload for payload in payloads if payload.template_id == "registry")
    sidecar_document = yaml.safe_load(sidecar.rendered_template)
    registry_document = yaml.safe_load(registry.rendered_template)

    assert sidecar_document["schema_version"] == 2
    assert sidecar_document["verification"] == {
        "schema_version": 1,
        "mode": "off",
        "repository_checks": [],
        "evaluation_gate_policy": "allow_no_evidence",
        "bundle": None,
        "lineage": None,
    }
    assert registry_document["agents"][0]["enabled"] is True


def test_sidecar_payload_persists_the_reviewed_foundry_target() -> None:
    endpoint = "https://reviewed.services.ai.azure.com/api/projects/reviewed"
    account_resource_id = (
        f"/subscriptions/{_SUBSCRIPTION_ID}/resourceGroups/reviewed-rg/"
        "providers/Microsoft.CognitiveServices/accounts/reviewed"
    )
    payloads = load_trusted_manifest(
        _plan_input(
            optimizer_environment="copilot",
            deployment_environment="foundry-production",
            include_profile=True,
            selected_enabled=True,
            reviewed_target={
                "state": "new_target",
                "project_endpoint": endpoint,
                "project_endpoint_source": "owner_answer",
                "agent_name": "reviewed-agent",
                "agent_name_source": "owner_answer",
                "account_resource_id": account_resource_id,
                "deployment_ready": True,
                "detail": "project access succeeded and the name is available",
            },
        )
    )

    sidecar = next(payload for payload in payloads if payload.template_id == "sidecar")
    document = yaml.safe_load(sidecar.rendered_template)

    assert document["foundry_project"]["project_endpoint"] == endpoint
    assert document["foundry_project"]["account_resource_id"] == account_resource_id
    assert document["foundry_project"]["agent_name"] == "reviewed-agent"
    assert document["foundry_target"]["state"] == "new_target"
    assert document["foundry_target"]["project_endpoint_source"] == "owner_answer"


def test_build_phase_actions_emits_a_variable_action_for_each_distinct_environment() -> None:
    plan_input = _plan_input(
        optimizer_environment="copilot",
        deployment_environment="foundry-production",
        client_id_variable_name="AZURE_SHARED_UAMI_CLIENT_ID",
    )
    actions = build_phase_actions(plan_input)
    variable_actions = [action for action in actions if action.kind == "github-variable"]

    assert len(variable_actions) == 2
    by_environment = {action.diagnostics[0]: action for action in variable_actions}
    assert set(by_environment) == {"copilot", "foundry-production"}
    for environment, action in by_environment.items():
        assert action.action_id == f"github-variable-client-id-{environment}"
        assert action.diagnostics == (environment, "AZURE_SHARED_UAMI_CLIENT_ID", _SHARED_CLIENT_ID)


def test_build_phase_actions_dedupes_identical_optimizer_and_deployment_environment() -> None:
    plan_input = _plan_input(optimizer_environment="shared-env", deployment_environment="shared-env")
    actions = build_phase_actions(plan_input)
    variable_actions = [action for action in actions if action.kind == "github-variable"]

    assert len(variable_actions) == 1
    assert variable_actions[0].action_id == "github-variable-client-id-shared-env"
    assert variable_actions[0].diagnostics == ("shared-env", "AZURE_OPTIMIZER_CLIENT_ID", _SHARED_CLIENT_ID)


def test_build_phase_actions_provisions_tenant_id_in_each_environment() -> None:
    actions = build_phase_actions(
        _plan_input(
            optimizer_environment="copilot",
            deployment_environment="foundry-production",
            include_azure=True,
        )
    )
    tenant_actions = [
        action
        for action in actions
        if action.action_id.startswith("github-variable-tenant-id-")
    ]

    assert len(tenant_actions) == 2
    assert {
        action.diagnostics
        for action in tenant_actions
    } == {
        ("copilot", "AZURE_TENANT_ID", _TENANT_ID),
        ("foundry-production", "AZURE_TENANT_ID", _TENANT_ID),
    }


def test_azure_federation_uses_immutable_github_subject_prefix() -> None:
    prefix = "repo:example-org@123/example-repo@456"
    actions = build_phase_actions(
        _plan_input(
            optimizer_environment="copilot",
            deployment_environment="foundry-production",
            include_azure=True,
            oidc_subject_prefix=prefix,
        )
    )
    federation = {
        action.action_id: action.diagnostics
        for action in actions
        if action.kind == "federated-credential"
    }

    assert federation == {
        "azure-fic-copilot": (
            f"subject={prefix}:environment:copilot",
        ),
        "azure-fic-foundry-production": (
            f"subject={prefix}:environment:foundry-production",
        ),
    }


def test_deploy_workflow_renders_custom_client_id_variable() -> None:
    payloads = load_trusted_manifest(
        _plan_input(
            optimizer_environment="copilot",
            deployment_environment="foundry-production",
            client_id_variable_name="AZURE_SHARED_UAMI_CLIENT_ID",
            default_branch="release",
        )
    )
    workflow = next(
        payload
        for payload in payloads
        if payload.template_id == "deploy-workflow"
    )

    assert "vars.AZURE_SHARED_UAMI_CLIENT_ID" in workflow.rendered_template
    assert "vars.AZURE_OPTIMIZER_CLIENT_ID" not in workflow.rendered_template
    assert "      - release" in workflow.rendered_template
    assert 'test "$GITHUB_REF" = "refs/heads/release"' in workflow.rendered_template


def test_preserve_repository_default_does_not_create_environment_branch_policy() -> None:
    actions = build_phase_actions(
        _plan_input(
            optimizer_environment="copilot",
            deployment_environment="foundry-production",
            branch_policy_intent="preserve_repository_default",
        )
    )

    assert not any(action.kind == "github-branch-policy" for action in actions)


def test_explicit_policy_intents_create_the_reviewed_branch_policy() -> None:
    actions = build_phase_actions(
        _plan_input(
            optimizer_environment="copilot",
            deployment_environment="foundry-production",
            branch_policy_intent="require_explicit",
            default_branch="release",
        )
    )
    policy = next(action for action in actions if action.kind == "github-branch-policy")

    assert policy.diagnostics == ("foundry-production", "release")


def test_require_main_refuses_a_non_main_default_branch() -> None:
    with pytest.raises(
        BootstrapPlanError,
        match="requires repository default_branch=main",
    ):
        build_phase_actions(
            _plan_input(
                optimizer_environment="copilot",
                deployment_environment="foundry-production",
                branch_policy_intent="require_main",
                default_branch="release",
            )
        )
