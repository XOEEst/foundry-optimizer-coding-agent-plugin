from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PROVIDERS_ROOT = (
    REPOSITORY_ROOT / "src" / "foundry_opt" / "templates" / "providers"
)
ATOMIC_OPERATIONS = {
    "inspect_baseline",
    "open_candidate",
    "apply_mutation",
    "freeze_candidate",
    "invoke_candidate",
    "evaluate",
    "cleanup_candidate",
    "apply_winner",
}
BINDINGS = ("foundry-hosted", "foundry-prompt")


def _load_binding(binding_id: str) -> dict[str, object]:
    path = PROVIDERS_ROOT / binding_id / "binding.yaml"
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict), path
    return value


def _resolve(entrypoint: str) -> object:
    module_name, _, qualname = entrypoint.partition(":")
    assert qualname, f"entrypoint must be module:qualname, got {entrypoint!r}"
    obj: object = importlib.import_module(module_name)
    for part in qualname.split("."):
        obj = getattr(obj, part)
    return obj


def _binding_entrypoints(binding: dict[str, object]) -> list[str]:
    entrypoints: list[str] = []
    runtime = binding["runtime"]
    assert isinstance(runtime, dict)
    entrypoints.extend(str(value) for value in runtime.values())
    entrypoints.append(str(binding["target_adapter"]))
    entrypoints.append(str(binding["evaluation_adapter"]))
    operations = binding["operations"]
    assert isinstance(operations, dict)
    for spec in operations.values():
        assert isinstance(spec, dict)
        for entrypoint in spec["entrypoints"]:
            entrypoints.append(str(entrypoint))
    return entrypoints


@pytest.mark.parametrize("binding_id", BINDINGS)
def test_binding_covers_every_atomic_operation(binding_id: str) -> None:
    binding = _load_binding(binding_id)
    assert binding["kind"] == "provider_binding"
    assert binding["provider"] == "foundry"
    assert binding["id"] == binding_id
    assert binding["isolation_mechanism"]
    operations = binding["operations"]
    assert isinstance(operations, dict)
    assert set(operations) == ATOMIC_OPERATIONS
    assert operations["apply_winner"].get("requires_explicit_approval") is True


@pytest.mark.parametrize("binding_id", BINDINGS)
def test_binding_entrypoints_are_importable(binding_id: str) -> None:
    binding = _load_binding(binding_id)
    for entrypoint in _binding_entrypoints(binding):
        assert _resolve(entrypoint) is not None, entrypoint


@pytest.mark.parametrize("binding_id", BINDINGS)
def test_binding_legacy_descriptors_exist(binding_id: str) -> None:
    binding = _load_binding(binding_id)
    descriptors = binding["legacy_descriptors"]
    assert isinstance(descriptors, dict)
    base = PROVIDERS_ROOT / binding_id
    for relative in descriptors.values():
        assert (base / str(relative)).resolve().is_file(), relative


def test_providers_live_outside_the_neutral_skill() -> None:
    operations_guide = (
        REPOSITORY_ROOT
        / "src"
        / "foundry_opt"
        / "templates"
        / "skills"
        / "agent-optimizer-v2"
        / "guides"
        / "operations.md"
    ).read_text(encoding="utf-8")
    assert "foundry_opt" not in operations_guide
    assert "binding.yaml" not in operations_guide
