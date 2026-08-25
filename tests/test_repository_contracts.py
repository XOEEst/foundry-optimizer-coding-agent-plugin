from __future__ import annotations

import pytest

from foundry_opt.contract_errors import BootstrapConfigError
from foundry_opt.repository_contracts import (
    EvaluatorReference,
    IssueEvaluatorRequestEntry,
    RootRegistry,
)


def _registry(*, schema_version: int) -> dict[str, object]:
    distribution: dict[str, object] = {
        "repository": "https://github.com/example/foundry-opt.git",
        "channel": "reviewed",
        "pin": "a" * 40,
    }
    if schema_version == 2:
        distribution.update(
            {
                "package_path": ".",
                "uv_lock_sha256": "b" * 64,
                "optimizer_skill_path": "plugins/foundry-agent-optimizer",
            }
        )
    return {
        "schema_version": schema_version,
        "distribution": distribution,
        "github": {
            "optimizer_environment": "copilot",
            "deployment_environment": "foundry-production",
            "client_id_variable": "AZURE_OPTIMIZER_CLIENT_ID",
        },
        "identity": {"kind": "unresolved_migration"},
        "agents": [
            {
                "agent_id": "agent",
                "root": "agent",
                "config_path": "agent/.foundry/foundry-opt.yaml",
                "enabled": True,
            }
        ],
    }


def test_registry_v1_remains_readable_for_skill_migration() -> None:
    registry = RootRegistry.from_document(_registry(schema_version=1))

    assert registry.schema_version == 1
    assert registry.has_exact_runtime_provenance is False
    assert registry.distribution.package_path == "."


def test_registry_v2_requires_complete_runtime_provenance() -> None:
    document = _registry(schema_version=2)
    del document["distribution"]["uv_lock_sha256"]  # type: ignore[index]

    with pytest.raises(
        BootstrapConfigError,
        match="distribution.uv_lock_sha256",
    ):
        RootRegistry.from_document(document)


def test_registry_v2_exposes_complete_runtime_provenance() -> None:
    registry = RootRegistry.from_document(_registry(schema_version=2))

    assert registry.schema_version == 2
    assert registry.has_exact_runtime_provenance is True
    assert registry.distribution.optimizer_skill_path == (
        "plugins/foundry-agent-optimizer"
    )


def test_evaluator_reference_preserves_definition_scoped_string_check_id() -> None:
    reference = EvaluatorReference.from_document(
        {
            "evaluator_id": (
                "policy_coverage_9d3e2d8b-81e6-436b-96a3-b46a46ef6dce"
            ),
            "provenance": "reused_existing",
        }
    )

    assert reference.evaluator_id == (
        "policy_coverage_9d3e2d8b-81e6-436b-96a3-b46a46ef6dce"
    )


def test_issue_evaluator_request_still_requires_immutable_resource_id() -> None:
    with pytest.raises(BootstrapConfigError, match="evaluator_id"):
        IssueEvaluatorRequestEntry.from_document(
            {
                "evaluator_id": (
                    "policy_coverage_9d3e2d8b-81e6-436b-96a3-b46a46ef6dce"
                ),
            }
        )
