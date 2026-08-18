from __future__ import annotations

import pytest

from foundry_opt.bootstrap.errors import BootstrapPlanError
from foundry_opt.bootstrap.input_contracts import BootstrapPlanInput, TrustedTemplateManifest
from foundry_opt.bootstrap.plan_factory import build_phase_actions

_SHARED_CLIENT_ID = "11111111-1111-1111-1111-111111111111"


def _manifest_hash() -> str:
    return TrustedTemplateManifest.load_pinned_manifest().manifest_hash


def _plan_input(
    *,
    optimizer_environment: str,
    deployment_environment: str,
    client_id_variable_name: str = "AZURE_OPTIMIZER_CLIENT_ID",
    shared_client_id: str = _SHARED_CLIENT_ID,
    branch_policy_intent: str = "preserve_repository_default",
    default_branch: str = "main",
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
                    "root": "agent",
                    "config_path": "agent/.foundry/foundry-opt.yaml",
                    "editable_paths": ["agent/main.py"],
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
        "required_phases": ["github"],
        "github_phase": {
            "schema_version": 1,
            "optimizer_environment": optimizer_environment,
            "deployment_environment": deployment_environment,
            "shared_client_id": shared_client_id,
            "client_id_variable_name": client_id_variable_name,
            "default_branch_policy_intent": branch_policy_intent,
        },
    }
    return BootstrapPlanInput.model_validate(payload)


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
