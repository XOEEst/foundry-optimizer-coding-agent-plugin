from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from foundry_opt.optimizer_core import (
    ActionBinding,
    ActionProviderManifest,
    ActionProviderSpec,
    ActionRegistry,
    OptimizationCoreError,
    ResourceLease,
    bootstrap_optimization_target,
    derive_action_requirements,
    project_legacy_adapters_to_actions,
)


def _facts(kind: str, surfaces: list[str]) -> dict[str, object]:
    return {
        "agent": {"identity": f"{kind}-agent", "kind": kind},
        "source": {
            "artifact_type": "agent-source",
            "uri": f"source:{kind}",
            "digest": "sha256:" + "1" * 64,
        },
        "baseline": {
            "artifact_type": "agent-baseline",
            "uri": f"baseline:{kind}",
            "digest": "sha256:" + "2" * 64,
        },
        "input_interface": {
            "artifact_type": "agent-request",
            "schema_ref": "schema:request",
        },
        "output_interface": {
            "artifact_type": "agent-output",
            "schema_ref": "schema:output",
        },
        "mutable_surfaces": [
            {"name": surface, "artifact_type": "text"} for surface in surfaces
        ],
        "frozen_surfaces": ["model", "tool_parameters"],
        "invariants": ["preserve_input_output_contract"],
        "datasets": [
            {"artifact_type": "dataset", "uri": "dataset:search"},
            {"artifact_type": "dataset", "uri": "dataset:validation"},
        ],
        "evaluators": [
            {"artifact_type": "evaluator", "uri": "evaluator:primary"}
        ],
    }


@pytest.mark.parametrize(
    ("kind", "surfaces"),
    [
        ("prompt", ["instruction"]),
        ("hosted", ["instruction", "tool_description"]),
        ("local", ["instruction", "tool_description"]),
    ],
)
def test_normalizes_prompt_hosted_and_local_inspection_facts(
    kind: str,
    surfaces: list[str],
) -> None:
    target = bootstrap_optimization_target(_facts(kind, surfaces))

    assert target.agent_kind == kind
    assert [surface.name for surface in target.mutable_surfaces] == surfaces
    assert target.source_identity.artifact_type == "agent-source"
    assert target.baseline_identity.artifact_type == "agent-baseline"
    assert target.input_interface.artifact_type == "agent-request"
    assert target.output_interface.artifact_type == "agent-output"
    assert {item.uri for item in target.datasets} == {
        "dataset:search",
        "dataset:validation",
    }
    assert [item.uri for item in target.evaluators] == ["evaluator:primary"]


@pytest.mark.parametrize(
    "forbidden",
    [
        {"credential": "secret"},
        {"provider_handle": "native:123"},
        {"agent": {"provider_id": "azure-foundry"}},
    ],
)
def test_target_normalization_rejects_credentials_and_provider_handles(
    forbidden: dict[str, object],
) -> None:
    facts = _facts("prompt", ["instruction"])
    facts.update(forbidden)

    with pytest.raises(OptimizationCoreError):
        bootstrap_optimization_target(facts)


def test_derives_required_atomic_actions() -> None:
    target = bootstrap_optimization_target(_facts("prompt", ["instruction"]))

    requirements = derive_action_requirements(target)

    assert [requirement.action for requirement in requirements] == [
        "inspect",
        "workspace_prepare",
        "read_mutate",
        "workspace_finalize",
        "materialize",
        "invoke",
        "evaluate",
        "cleanup",
    ]
    evaluation = next(item for item in requirements if item.action == "evaluate")
    assert {
        "complete_denominator",
        "opaque_confirmation",
        "sealed_validation",
    } <= evaluation.required_capabilities


def _manifest(requirement: object, index: int) -> ActionProviderManifest:
    action = requirement.action
    return ActionProviderManifest(
        provider_id=f"provider-{index}",
        provider_version="1.0.0",
        implementation_id=f"tests.provider.{index}",
        implementation_version="1.0.0",
        actions=(
            ActionProviderSpec(
                action=action,
                capabilities=requirement.required_capabilities,
                accepted_artifact_types=requirement.accepted_artifact_types,
                produced_artifact_types=requirement.produced_artifact_types,
            ),
        ),
    )


def test_binds_a_different_lazy_provider_per_action() -> None:
    target = bootstrap_optimization_target(_facts("hosted", ["instruction"]))
    registry = ActionRegistry()
    created: list[str] = []
    bindings: dict[str, ActionBinding] = {}

    for index, requirement in enumerate(target.required_actions):
        manifest = _manifest(requirement, index)
        registry.register(
            manifest,
            lambda binding, provider=manifest.provider_id: (
                created.append(provider) or object()
            ),
        )
        bindings[requirement.action] = registry.bind(
            requirement.action,
            provider_id=manifest.provider_id,
            provider_version=manifest.provider_version,
            config={"mode": requirement.action},
            credential_reference=(
                "credential-ref:test" if requirement.action in {"invoke", "evaluate"} else None
            ),
        )

    registry.validate_bindings(target, bindings)
    assert len({binding.provider_id for binding in bindings.values()}) == len(bindings)
    assert created == []

    first = bindings["inspect"]
    assert registry.client_for(first) is registry.client_for(first)
    assert created == ["provider-0"]


def test_rejects_incompatible_cross_provider_artifact_types() -> None:
    target = bootstrap_optimization_target(_facts("local", ["instruction"]))
    registry = ActionRegistry()
    bindings: dict[str, ActionBinding] = {}

    for index, requirement in enumerate(target.required_actions):
        manifest = _manifest(requirement, index)
        registry.register(manifest, lambda binding: object())
        bindings[requirement.action] = registry.bind(
            requirement.action,
            provider_id=manifest.provider_id,
            provider_version=manifest.provider_version,
        )

    bindings["materialize"] = replace(
        bindings["materialize"],
        produced_artifact_types=frozenset({"provider-private-handle"}),
    )
    with pytest.raises(OptimizationCoreError, match="does not produce"):
        registry.validate_bindings(target, bindings)


def test_same_provider_uses_distinct_clients_for_distinct_bindings() -> None:
    target = bootstrap_optimization_target(_facts("hosted", ["instruction"]))
    registry = ActionRegistry()
    requirements = {
        requirement.action: requirement for requirement in target.required_actions
    }
    manifest = ActionProviderManifest(
        provider_id="multi-action",
        provider_version="1.0.0",
        implementation_id="tests.multi-action",
        implementation_version="1.0.0",
        actions=tuple(
            ActionProviderSpec(
                action=requirement.action,
                capabilities=requirement.required_capabilities,
                accepted_artifact_types=requirement.accepted_artifact_types,
                produced_artifact_types=requirement.produced_artifact_types,
            )
            for requirement in requirements.values()
        ),
    )
    created: list[ActionBinding] = []
    registry.register(
        manifest,
        lambda binding: created.append(binding) or object(),
    )
    inspect = registry.bind(
        "inspect",
        provider_id="multi-action",
        provider_version="1.0.0",
        config={"mode": "inspect"},
    )
    evaluate = registry.bind(
        "evaluate",
        provider_id="multi-action",
        provider_version="1.0.0",
        config={"mode": "evaluate"},
        credential_reference="credential-ref:evaluation",
    )

    assert inspect.manifest_hash == evaluate.manifest_hash
    assert inspect.config_hash != evaluate.config_hash
    assert registry.client_for(inspect) is registry.client_for(inspect)
    assert registry.client_for(evaluate) is registry.client_for(evaluate)
    assert len(created) == 2


def test_resource_lease_cleanup_is_ownership_safe() -> None:
    lease = ResourceLease(
        lease_id="lease-1",
        resource_type="candidate-workspace",
        owner_id="run-1",
        ownership_token="ownership-1",
        restricted_reference="restricted:workspace",
    )

    lease.validate_cleanup(owner_id="run-1", ownership_token="ownership-1")
    with pytest.raises(OptimizationCoreError, match="ownership"):
        lease.validate_cleanup(owner_id="run-2", ownership_token="ownership-1")
    with pytest.raises(OptimizationCoreError, match="ownership"):
        lease.validate_cleanup(owner_id="run-1", ownership_token="wrong")


def test_projects_legacy_adapter_selection_after_normalization() -> None:
    adapters = {
        kind: {
            "id": f"legacy-{kind}",
            "version": "1.0.0",
            "implementation_id": f"legacy.{kind}",
            "implementation_version": "1.0.0",
            "config_hash": "sha256:" + str(index) * 64,
            "capabilities": {"apply_winner": kind == "target"},
            "resolved_config": (
                {"credential_provider": "test-identity"}
                if kind in {"target", "evaluation"}
                else {}
            ),
        }
        for index, kind in enumerate(("workspace", "target", "evaluation"), start=1)
    }

    bindings = project_legacy_adapters_to_actions(adapters)

    assert bindings["workspace_prepare"].provider_id == "legacy-workspace"
    assert bindings["materialize"].provider_id == "legacy-target"
    assert bindings["evaluate"].provider_id == "legacy-evaluation"
    assert bindings["invoke"].credential_reference == "credential-ref:test-identity"
    assert bindings["apply"].provider_id == "legacy-target"


def test_bootstrap_imports_without_azure_sdk_initialization() -> None:
    script = """
import importlib.abc
import sys

class RejectAzure(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "azure" or fullname.startswith("azure."):
            raise RuntimeError("Azure import attempted")
        return None

sys.meta_path.insert(0, RejectAzure())
from foundry_opt.optimizer_core import bootstrap_optimization_target

facts = {
    "agent_id": "prompt-agent",
    "agent_kind": "prompt",
    "source_identity": "source:prompt",
    "baseline_identity": "baseline:prompt",
    "input_artifact_type": "agent-request",
    "output_artifact_type": "agent-output",
    "mutable_surfaces": ["instruction"],
    "frozen_surfaces": ["model"],
    "invariants": ["preserve-interface"],
    "datasets": [],
    "evaluators": [],
}
assert bootstrap_optimization_target(facts).agent_kind == "prompt"
assert not any(name == "azure" or name.startswith("azure.") for name in sys.modules)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
