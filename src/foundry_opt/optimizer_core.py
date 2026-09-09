"""Provider-neutral contracts for the agent-optimizer-v2 bootstrap path.

This module intentionally uses only the Python standard library.  Agent
understanding, target normalization, action derivation, and binding validation
therefore do not initialize provider SDKs or credentials.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Protocol, TypeAlias, runtime_checkable


ActionName: TypeAlias = Literal[
    "inspect",
    "workspace_prepare",
    "read_mutate",
    "workspace_finalize",
    "materialize",
    "invoke",
    "evaluate",
    "cleanup",
    "apply",
]

REQUIRED_ACTION_ORDER: Final[tuple[ActionName, ...]] = (
    "inspect",
    "workspace_prepare",
    "read_mutate",
    "workspace_finalize",
    "materialize",
    "invoke",
    "evaluate",
    "cleanup",
)
OPTIONAL_ACTIONS: Final[tuple[ActionName, ...]] = ("apply",)
_SHA256_PATTERN: Final = re.compile(r"^sha256:[0-9a-f]{64}$")
_FORBIDDEN_FACT_KEYS: Final = {
    "access_key",
    "api_key",
    "client_secret",
    "credential",
    "credential_reference",
    "credentials",
    "native_handle",
    "password",
    "provider",
    "provider_handle",
    "provider_id",
    "secret",
    "token",
}


class OptimizationCoreError(ValueError):
    """Provider-neutral bootstrap or action binding is invalid."""


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """Portable reference to a typed artifact crossing action boundaries."""

    artifact_type: str
    uri: str
    digest: str | None = None
    media_type: str | None = None
    producer_action: ActionName | None = None

    def __post_init__(self) -> None:
        _require_text(self.artifact_type, "artifact_type")
        _require_text(self.uri, "uri")
        if self.digest is not None and _SHA256_PATTERN.fullmatch(self.digest) is None:
            raise OptimizationCoreError("artifact digest must be sha256:<lowercase hex>")
        if self.media_type is not None:
            _require_text(self.media_type, "media_type")


@dataclass(frozen=True, slots=True)
class ResourceLease:
    """Ownership proof for a resource that an action may later clean up."""

    lease_id: str
    resource_type: str
    owner_id: str
    ownership_token: str
    cleanup_action: ActionName = "cleanup"
    restricted_reference: str | None = None

    def __post_init__(self) -> None:
        for name in ("lease_id", "resource_type", "owner_id", "ownership_token"):
            _require_text(getattr(self, name), name)
        if self.cleanup_action != "cleanup":
            raise OptimizationCoreError("resource leases must use the cleanup action")
        if self.restricted_reference is not None:
            _require_text(self.restricted_reference, "restricted_reference")

    def validate_cleanup(self, *, owner_id: str, ownership_token: str) -> None:
        if owner_id != self.owner_id or ownership_token != self.ownership_token:
            raise OptimizationCoreError(
                "cleanup rejected because resource ownership does not match the lease"
            )


@dataclass(frozen=True, slots=True)
class MutableSurface:
    """One provider-neutral agent surface that an experiment may mutate."""

    name: str
    artifact_type: str
    locator: str | None = None
    constraints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_text(self.name, "mutable surface name")
        _require_text(self.artifact_type, "mutable surface artifact_type")
        if self.locator is not None:
            _require_text(self.locator, "mutable surface locator")
        _require_unique_text(self.constraints, "mutable surface constraints")


@dataclass(frozen=True, slots=True)
class InterfaceContract:
    """Typed input or output boundary understood without a provider SDK."""

    artifact_type: str
    schema_ref: str | None = None

    def __post_init__(self) -> None:
        _require_text(self.artifact_type, "interface artifact_type")
        if self.schema_ref is not None:
            _require_text(self.schema_ref, "interface schema_ref")


@dataclass(frozen=True, slots=True)
class ActionRequirement:
    """Minimum contract an independently selected action provider must satisfy."""

    action: ActionName
    required_capabilities: frozenset[str] = field(default_factory=frozenset)
    accepted_artifact_types: frozenset[str] = field(default_factory=frozenset)
    produced_artifact_types: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        _require_action(self.action)
        _require_unique_text(self.required_capabilities, "required capabilities")
        _require_unique_text(
            self.accepted_artifact_types,
            "accepted artifact types",
        )
        _require_unique_text(
            self.produced_artifact_types,
            "produced artifact types",
        )


@dataclass(frozen=True, slots=True)
class OptimizationTarget:
    """Provider-neutral, credential-free description of the agent under test."""

    target_id: str
    agent_identity: str
    agent_kind: str
    source_identity: ArtifactRef
    baseline_identity: ArtifactRef
    input_interface: InterfaceContract
    output_interface: InterfaceContract
    mutable_surfaces: tuple[MutableSurface, ...]
    frozen_surfaces: tuple[str, ...]
    invariants: tuple[str, ...]
    datasets: tuple[ArtifactRef, ...]
    evaluators: tuple[ArtifactRef, ...]
    required_actions: tuple[ActionRequirement, ...]

    def __post_init__(self) -> None:
        _require_text(self.target_id, "target_id")
        _require_text(self.agent_identity, "agent_identity")
        _require_text(self.agent_kind, "agent_kind")
        if not self.mutable_surfaces:
            raise OptimizationCoreError("at least one mutable surface is required")
        _require_unique_text(
            (surface.name for surface in self.mutable_surfaces),
            "mutable surface names",
        )
        _require_unique_text(self.frozen_surfaces, "frozen surfaces")
        _require_unique_text(self.invariants, "invariants")
        action_names = tuple(requirement.action for requirement in self.required_actions)
        _require_unique_text(action_names, "required actions")
        missing = set(REQUIRED_ACTION_ORDER) - set(action_names)
        if missing:
            raise OptimizationCoreError(
                "optimization target is missing required actions: "
                + ", ".join(sorted(missing))
            )


@dataclass(frozen=True, slots=True)
class ActionBinding:
    """Frozen identity and artifact contract for one atomic action."""

    action: ActionName
    provider_id: str
    provider_version: str
    implementation_id: str
    implementation_version: str
    manifest_hash: str
    config_hash: str
    capabilities: frozenset[str]
    accepted_artifact_types: frozenset[str]
    produced_artifact_types: frozenset[str]
    credential_reference: str | None = None

    def __post_init__(self) -> None:
        _require_action(self.action)
        for name in (
            "provider_id",
            "provider_version",
            "implementation_id",
            "implementation_version",
        ):
            _require_text(getattr(self, name), name)
        for name in ("manifest_hash", "config_hash"):
            if _SHA256_PATTERN.fullmatch(getattr(self, name)) is None:
                raise OptimizationCoreError(
                    f"{name} must be sha256:<lowercase hex>"
                )
        _require_unique_text(self.capabilities, "binding capabilities")
        _require_unique_text(
            self.accepted_artifact_types,
            "binding accepted artifact types",
        )
        _require_unique_text(
            self.produced_artifact_types,
            "binding produced artifact types",
        )
        if self.credential_reference is not None:
            _require_text(self.credential_reference, "credential_reference")
            if not self.credential_reference.startswith("credential-ref:"):
                raise OptimizationCoreError(
                    "credential_reference must be an opaque credential-ref: value"
                )


@dataclass(frozen=True, slots=True)
class ActionProviderSpec:
    action: ActionName
    capabilities: frozenset[str]
    accepted_artifact_types: frozenset[str]
    produced_artifact_types: frozenset[str]

    def __post_init__(self) -> None:
        _require_action(self.action)
        _require_unique_text(self.capabilities, "provider capabilities")
        _require_unique_text(
            self.accepted_artifact_types,
            "provider accepted artifact types",
        )
        _require_unique_text(
            self.produced_artifact_types,
            "provider produced artifact types",
        )


@dataclass(frozen=True, slots=True)
class ActionProviderManifest:
    provider_id: str
    provider_version: str
    implementation_id: str
    implementation_version: str
    actions: tuple[ActionProviderSpec, ...]

    def __post_init__(self) -> None:
        for name in (
            "provider_id",
            "provider_version",
            "implementation_id",
            "implementation_version",
        ):
            _require_text(getattr(self, name), name)
        if not self.actions:
            raise OptimizationCoreError("an action provider must implement an action")
        _require_unique_text(
            (spec.action for spec in self.actions),
            "provider action names",
        )

    def spec_for(self, action: ActionName) -> ActionProviderSpec:
        for spec in self.actions:
            if spec.action == action:
                return spec
        raise OptimizationCoreError(
            f"{self.provider_id}@{self.provider_version} does not implement {action}"
        )


@runtime_checkable
class AgentInspector(Protocol):
    """Read-only discovery boundary used before action-provider binding."""

    def inspect(self) -> Mapping[str, object]: ...


@runtime_checkable
class TargetNormalizer(Protocol):
    def normalize(self, inspection_facts: Mapping[str, object]) -> OptimizationTarget: ...


@runtime_checkable
class InspectAction(Protocol):
    def inspect(self, target: OptimizationTarget) -> ArtifactRef: ...


@runtime_checkable
class WorkspacePrepareAction(Protocol):
    def workspace_prepare(self, target: OptimizationTarget) -> tuple[ArtifactRef, ResourceLease]: ...


@runtime_checkable
class ReadMutateAction(Protocol):
    def read_mutate(self, workspace: ArtifactRef) -> ArtifactRef: ...


@runtime_checkable
class WorkspaceFinalizeAction(Protocol):
    def workspace_finalize(self, candidate: ArtifactRef) -> ArtifactRef: ...


@runtime_checkable
class MaterializeAction(Protocol):
    def materialize(self, candidate: ArtifactRef) -> tuple[ArtifactRef, ResourceLease]: ...


@runtime_checkable
class InvokeAction(Protocol):
    def invoke(self, agent: ArtifactRef, request: ArtifactRef) -> ArtifactRef: ...


@runtime_checkable
class EvaluateAction(Protocol):
    def evaluate(
        self,
        output: ArtifactRef,
        datasets: Sequence[ArtifactRef],
        evaluators: Sequence[ArtifactRef],
    ) -> ArtifactRef: ...


@runtime_checkable
class CleanupAction(Protocol):
    def cleanup(self, lease: ResourceLease) -> ArtifactRef: ...


@runtime_checkable
class ApplyAction(Protocol):
    def apply(self, candidate: ArtifactRef, approval_reference: str) -> ArtifactRef: ...


class DefaultTargetNormalizer:
    """Normalize simple inspection facts into the stable v2 target contract."""

    def normalize(self, inspection_facts: Mapping[str, object]) -> OptimizationTarget:
        facts = _object(inspection_facts, "inspection facts")
        _reject_sensitive_or_provider_native_facts(facts)

        agent = _optional_object(facts.get("agent"), "agent")
        identity = _first_text(
            agent.get("identity"),
            agent.get("id"),
            facts.get("agent_identity"),
            facts.get("agent_id"),
        )
        kind = _first_text(agent.get("kind"), facts.get("agent_kind"), "generic")
        target_id = _first_text(facts.get("target_id"), identity)

        source = _artifact(
            facts.get("source") or facts.get("source_identity"),
            default_type="agent-source",
            field_name="source identity",
        )
        baseline = _artifact(
            facts.get("baseline") or facts.get("baseline_identity"),
            default_type="agent-baseline",
            field_name="baseline identity",
        )
        input_interface = _interface(
            facts.get("input_interface"),
            fallback_type=facts.get("input_artifact_type"),
            field_name="input interface",
        )
        output_interface = _interface(
            facts.get("output_interface"),
            fallback_type=facts.get("output_artifact_type"),
            field_name="output interface",
        )

        raw_surfaces = facts.get("mutable_surfaces")
        if not isinstance(raw_surfaces, Sequence) or isinstance(raw_surfaces, (str, bytes)):
            raise OptimizationCoreError("mutable_surfaces must be a sequence")
        mutable_surfaces = tuple(
            _mutable_surface(value, index)
            for index, value in enumerate(raw_surfaces)
        )
        frozen_surfaces = _text_tuple(facts.get("frozen_surfaces"), "frozen_surfaces")
        invariants = _text_tuple(facts.get("invariants"), "invariants")
        datasets = _artifact_tuple(facts.get("datasets", ()), "dataset")
        evaluators = _artifact_tuple(facts.get("evaluators", ()), "evaluator")
        include_apply = bool(facts.get("require_apply", False))

        provisional = OptimizationTarget(
            target_id=target_id,
            agent_identity=identity,
            agent_kind=kind,
            source_identity=source,
            baseline_identity=baseline,
            input_interface=input_interface,
            output_interface=output_interface,
            mutable_surfaces=mutable_surfaces,
            frozen_surfaces=frozen_surfaces,
            invariants=invariants,
            datasets=datasets,
            evaluators=evaluators,
            required_actions=derive_action_requirements(
                source_artifact_type=source.artifact_type,
                output_artifact_type=output_interface.artifact_type,
                include_apply=include_apply,
            ),
        )
        return provisional


def bootstrap_optimization_target(
    inspection_facts: Mapping[str, object],
    *,
    normalizer: TargetNormalizer | None = None,
) -> OptimizationTarget:
    """Normalize already-collected read-only facts before provider binding."""

    return (normalizer or DefaultTargetNormalizer()).normalize(inspection_facts)


def understand_and_normalize(
    inspector: AgentInspector,
    *,
    normalizer: TargetNormalizer | None = None,
) -> OptimizationTarget:
    """Run read-only understanding and normalization without loading providers."""

    return bootstrap_optimization_target(
        inspector.inspect(),
        normalizer=normalizer,
    )


def derive_action_requirements(
    target: OptimizationTarget | None = None,
    *,
    source_artifact_type: str | None = None,
    output_artifact_type: str | None = None,
    include_apply: bool = False,
) -> tuple[ActionRequirement, ...]:
    """Derive atomic action contracts from a provider-neutral target."""

    if target is not None:
        source_artifact_type = target.source_identity.artifact_type
        output_artifact_type = target.output_interface.artifact_type
        include_apply = any(
            requirement.action == "apply" for requirement in target.required_actions
        )
    source_type = _require_text(
        source_artifact_type,
        "source_artifact_type",
    )
    output_type = _require_text(
        output_artifact_type,
        "output_artifact_type",
    )
    requirements: list[ActionRequirement] = [
        ActionRequirement(
            "inspect",
            frozenset({"read_only"}),
            frozenset({source_type}),
            frozenset({"optimization-target-snapshot"}),
        ),
        ActionRequirement(
            "workspace_prepare",
            frozenset({"isolated_workspace", "parent_commit_lineage"}),
            frozenset({source_type}),
            frozenset({"candidate-workspace"}),
        ),
        ActionRequirement(
            "read_mutate",
            frozenset({"bounded_mutation"}),
            frozenset({"candidate-workspace"}),
            frozenset({"mutable-candidate"}),
        ),
        ActionRequirement(
            "workspace_finalize",
            frozenset({"exact_readback", "immutable_candidate"}),
            frozenset({"mutable-candidate"}),
            frozenset({"candidate-artifact"}),
        ),
        ActionRequirement(
            "materialize",
            frozenset({"verified_candidate_input"}),
            frozenset({"candidate-artifact"}),
            frozenset({"invokable-agent"}),
        ),
        ActionRequirement(
            "invoke",
            frozenset({"frozen_interface"}),
            frozenset({"invokable-agent"}),
            frozenset({output_type}),
        ),
        ActionRequirement(
            "evaluate",
            frozenset(
                {
                    "complete_denominator",
                    "opaque_confirmation",
                    "sealed_validation",
                }
            ),
            frozenset({output_type}),
            frozenset({"evaluation-result"}),
        ),
        ActionRequirement(
            "cleanup",
            frozenset({"ownership_safe"}),
            frozenset({"resource-lease"}),
            frozenset({"cleanup-receipt"}),
        ),
    ]
    if include_apply:
        requirements.append(
            ActionRequirement(
                "apply",
                frozenset({"explicit_approval"}),
                frozenset({"candidate-artifact"}),
                frozenset({"applied-target"}),
            )
        )
    return tuple(requirements)


class ActionRegistry:
    """Registry that binds each atomic action independently and lazily."""

    def __init__(self) -> None:
        self._providers: dict[
            tuple[str, str],
            tuple[ActionProviderManifest, Callable[[ActionBinding], object]],
        ] = {}
        self._clients: dict[tuple[object, ...], object] = {}

    def register(
        self,
        manifest: ActionProviderManifest,
        client_factory: Callable[[ActionBinding], object],
    ) -> None:
        key = (manifest.provider_id, manifest.provider_version)
        if key in self._providers:
            raise OptimizationCoreError(
                f"action provider {manifest.provider_id}@{manifest.provider_version} "
                "is already registered"
            )
        self._providers[key] = (manifest, client_factory)

    def bind(
        self,
        action: ActionName,
        *,
        provider_id: str,
        provider_version: str,
        config: Mapping[str, object] | None = None,
        credential_reference: str | None = None,
    ) -> ActionBinding:
        key = (provider_id, provider_version)
        if key not in self._providers:
            raise OptimizationCoreError(
                f"unknown action provider {provider_id}@{provider_version}"
            )
        manifest, _ = self._providers[key]
        spec = manifest.spec_for(action)
        return ActionBinding(
            action=action,
            provider_id=manifest.provider_id,
            provider_version=manifest.provider_version,
            implementation_id=manifest.implementation_id,
            implementation_version=manifest.implementation_version,
            manifest_hash=_manifest_hash(manifest),
            config_hash=_config_hash(config or {}),
            capabilities=spec.capabilities,
            accepted_artifact_types=spec.accepted_artifact_types,
            produced_artifact_types=spec.produced_artifact_types,
            credential_reference=credential_reference,
        )

    def client_for(self, binding: ActionBinding) -> object:
        provider_key = (binding.provider_id, binding.provider_version)
        if provider_key not in self._providers:
            raise OptimizationCoreError(
                f"unknown action provider {binding.provider_id}@{binding.provider_version}"
            )
        manifest, factory = self._providers[provider_key]
        if (
            binding.implementation_id != manifest.implementation_id
            or binding.implementation_version != manifest.implementation_version
        ):
            raise OptimizationCoreError("binding implementation identity has drifted")
        if binding.manifest_hash != _manifest_hash(manifest):
            raise OptimizationCoreError("binding provider manifest has drifted")
        key = (
            binding.action,
            binding.provider_id,
            binding.provider_version,
            binding.implementation_id,
            binding.implementation_version,
            binding.manifest_hash,
            binding.config_hash,
            binding.credential_reference,
        )
        if key not in self._clients:
            self._clients[key] = factory(binding)
        return self._clients[key]

    def validate_bindings(
        self,
        target: OptimizationTarget,
        bindings: Mapping[ActionName, ActionBinding],
    ) -> None:
        validate_action_bindings(target, bindings)
        for binding in bindings.values():
            key = (binding.provider_id, binding.provider_version)
            if key not in self._providers:
                raise OptimizationCoreError(
                    f"binding uses unregistered provider {binding.provider_id}"
                )
            manifest, _ = self._providers[key]
            spec = manifest.spec_for(binding.action)
            if (
                binding.implementation_id != manifest.implementation_id
                or binding.implementation_version != manifest.implementation_version
                or binding.capabilities != spec.capabilities
                or binding.accepted_artifact_types != spec.accepted_artifact_types
                or binding.produced_artifact_types != spec.produced_artifact_types
            ):
                raise OptimizationCoreError(
                    f"binding for {binding.action} differs from its provider manifest"
                )


def validate_action_bindings(
    target: OptimizationTarget,
    bindings: Mapping[ActionName, ActionBinding],
) -> None:
    """Validate independently selected providers and cross-action artifacts."""

    required = {requirement.action: requirement for requirement in target.required_actions}
    missing = set(required) - set(bindings)
    if missing:
        raise OptimizationCoreError(
            "missing action bindings: " + ", ".join(sorted(missing))
        )
    unexpected = set(bindings) - set(required)
    if unexpected:
        raise OptimizationCoreError(
            "unexpected action bindings: " + ", ".join(sorted(unexpected))
        )

    for action, requirement in required.items():
        binding = bindings[action]
        if binding.action != action:
            raise OptimizationCoreError(f"binding key does not match action {action}")
        missing_capabilities = requirement.required_capabilities - binding.capabilities
        if missing_capabilities:
            raise OptimizationCoreError(
                f"{action} lacks capabilities: "
                + ", ".join(sorted(missing_capabilities))
            )
        if not requirement.accepted_artifact_types <= binding.accepted_artifact_types:
            raise OptimizationCoreError(
                f"{action} does not accept all required artifact types"
            )
        if not requirement.produced_artifact_types <= binding.produced_artifact_types:
            raise OptimizationCoreError(
                f"{action} does not produce all required artifact types"
            )

    for producer, consumer in (
        ("workspace_prepare", "read_mutate"),
        ("read_mutate", "workspace_finalize"),
        ("workspace_finalize", "materialize"),
        ("materialize", "invoke"),
        ("invoke", "evaluate"),
    ):
        produced = bindings[producer].produced_artifact_types
        accepted = bindings[consumer].accepted_artifact_types
        if not produced & accepted:
            raise OptimizationCoreError(
                f"artifact types are incompatible between {producer} and {consumer}"
            )
    if "apply" in bindings and not (
        bindings["workspace_finalize"].produced_artifact_types
        & bindings["apply"].accepted_artifact_types
    ):
        raise OptimizationCoreError(
            "artifact types are incompatible between workspace_finalize and apply"
        )


def project_legacy_adapters_to_actions(
    adapters: Mapping[str, Mapping[str, object]],
) -> dict[ActionName, ActionBinding]:
    """Project a frozen v1 adapter bundle into best-effort atomic bindings.

    This bridge preserves existing Foundry execution implementations.  It does
    not split cleanup inside a coarse adapter and must not be used to infer a
    provider-neutral target.
    """

    workspace = _object(adapters.get("workspace"), "legacy workspace adapter")
    target = _object(adapters.get("target"), "legacy target adapter")
    evaluation = _object(adapters.get("evaluation"), "legacy evaluation adapter")
    bindings = {
        "inspect": _legacy_binding(
            "inspect",
            target,
            {"read_only"},
            {"agent-source"},
            {"optimization-target-snapshot"},
        ),
        "workspace_prepare": _legacy_binding(
            "workspace_prepare",
            workspace,
            {"isolated_workspace", "parent_commit_lineage"},
            {"agent-source"},
            {"candidate-workspace"},
        ),
        "read_mutate": _legacy_binding(
            "read_mutate",
            workspace,
            {"bounded_mutation"},
            {"candidate-workspace"},
            {"mutable-candidate"},
        ),
        "workspace_finalize": _legacy_binding(
            "workspace_finalize",
            workspace,
            {"exact_readback", "immutable_candidate"},
            {"mutable-candidate"},
            {"candidate-artifact"},
        ),
        "materialize": _legacy_binding(
            "materialize",
            target,
            {"verified_candidate_input"},
            {"candidate-artifact"},
            {"invokable-agent"},
        ),
        "invoke": _legacy_binding(
            "invoke",
            target,
            {"frozen_interface"},
            {"invokable-agent"},
            {"agent-output"},
        ),
        "evaluate": _legacy_binding(
            "evaluate",
            evaluation,
            {"complete_denominator", "opaque_confirmation", "sealed_validation"},
            {"agent-output"},
            {"evaluation-result"},
        ),
        "cleanup": _legacy_binding(
            "cleanup",
            target,
            {"ownership_safe"},
            {"resource-lease"},
            {"cleanup-receipt"},
        ),
    }
    target_capabilities = _string_bool_capabilities(target.get("capabilities"))
    if target_capabilities.get("apply_winner") is True:
        bindings["apply"] = _legacy_binding(
            "apply",
            target,
            {"explicit_approval"},
            {"candidate-artifact"},
            {"applied-target"},
        )
    return bindings


def _legacy_binding(
    action: ActionName,
    adapter: Mapping[str, object],
    capabilities: set[str],
    accepts: set[str],
    produces: set[str],
) -> ActionBinding:
    provider_id = _first_text(adapter.get("id"))
    provider_version = _first_text(adapter.get("version"))
    implementation_id = _first_text(adapter.get("implementation_id"), provider_id)
    implementation_version = _first_text(
        adapter.get("implementation_version"),
        provider_version,
    )
    config_hash = adapter.get("config_hash")
    if not isinstance(config_hash, str) or _SHA256_PATTERN.fullmatch(config_hash) is None:
        config_hash = _config_hash(_optional_object(adapter.get("resolved_config"), "config"))
    resolved_config = _optional_object(adapter.get("resolved_config"), "config")
    credential_provider = resolved_config.get("credential_provider")
    credential_reference = (
        f"credential-ref:{credential_provider}"
        if isinstance(credential_provider, str) and credential_provider
        else None
    )
    return ActionBinding(
        action=action,
        provider_id=provider_id,
        provider_version=provider_version,
        implementation_id=implementation_id,
        implementation_version=implementation_version,
        manifest_hash=_config_hash(
            {
                "legacy_adapter_id": provider_id,
                "legacy_adapter_version": provider_version,
                "implementation_id": implementation_id,
                "implementation_version": implementation_version,
                "action": action,
                "capabilities": sorted(capabilities),
                "accepted_artifact_types": sorted(accepts),
                "produced_artifact_types": sorted(produces),
            }
        ),
        config_hash=config_hash,
        capabilities=frozenset(capabilities),
        accepted_artifact_types=frozenset(accepts),
        produced_artifact_types=frozenset(produces),
        credential_reference=credential_reference,
    )


def _artifact(value: object, *, default_type: str, field_name: str) -> ArtifactRef:
    if isinstance(value, str):
        return ArtifactRef(default_type, value)
    document = _object(value, field_name)
    artifact_type = _first_text(document.get("artifact_type"), default_type)
    uri = _first_text(
        document.get("uri"),
        document.get("reference"),
        document.get("identity"),
        document.get("id"),
    )
    digest = document.get("digest")
    media_type = document.get("media_type")
    return ArtifactRef(
        artifact_type=artifact_type,
        uri=uri,
        digest=digest if isinstance(digest, str) else None,
        media_type=media_type if isinstance(media_type, str) else None,
    )


def _artifact_tuple(value: object, label: str) -> tuple[ArtifactRef, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise OptimizationCoreError(f"{label}s must be a sequence")
    return tuple(
        _artifact(item, default_type=label, field_name=f"{label} {index}")
        for index, item in enumerate(value)
    )


def _interface(
    value: object,
    *,
    fallback_type: object,
    field_name: str,
) -> InterfaceContract:
    if isinstance(value, str):
        return InterfaceContract(value)
    if isinstance(value, Mapping):
        artifact_type = _first_text(
            value.get("artifact_type"),
            value.get("type"),
        )
        schema_ref = value.get("schema_ref") or value.get("schema")
        return InterfaceContract(
            artifact_type=artifact_type,
            schema_ref=schema_ref if isinstance(schema_ref, str) else None,
        )
    return InterfaceContract(_first_text(fallback_type))


def _mutable_surface(value: object, index: int) -> MutableSurface:
    if isinstance(value, str):
        return MutableSurface(value, "text")
    document = _object(value, f"mutable surface {index}")
    raw_constraints = document.get("constraints", ())
    constraints = _text_tuple(raw_constraints, "mutable surface constraints")
    locator = document.get("locator")
    return MutableSurface(
        name=_first_text(document.get("name")),
        artifact_type=_first_text(document.get("artifact_type"), "text"),
        locator=locator if isinstance(locator, str) else None,
        constraints=constraints,
    )


def _reject_sensitive_or_provider_native_facts(value: object, path: str = "facts") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if normalized in _FORBIDDEN_FACT_KEYS:
                raise OptimizationCoreError(
                    f"{path}.{key} is credential-bearing or provider-native"
                )
            _reject_sensitive_or_provider_native_facts(item, f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for index, item in enumerate(value):
            _reject_sensitive_or_provider_native_facts(item, f"{path}[{index}]")


def _config_hash(config: Mapping[str, object]) -> str:
    encoded = json.dumps(
        config,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _manifest_hash(manifest: ActionProviderManifest) -> str:
    return _config_hash(
        {
            "provider_id": manifest.provider_id,
            "provider_version": manifest.provider_version,
            "implementation_id": manifest.implementation_id,
            "implementation_version": manifest.implementation_version,
            "actions": [
                {
                    "action": spec.action,
                    "capabilities": sorted(spec.capabilities),
                    "accepted_artifact_types": sorted(
                        spec.accepted_artifact_types
                    ),
                    "produced_artifact_types": sorted(
                        spec.produced_artifact_types
                    ),
                }
                for spec in manifest.actions
            ],
        }
    )


def _object(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise OptimizationCoreError(f"{name} must be an object")
    return value


def _optional_object(value: object, name: str) -> Mapping[str, object]:
    if value is None:
        return {}
    return _object(value, name)


def _first_text(*values: object) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value
    raise OptimizationCoreError("required text value is missing")


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OptimizationCoreError(f"{name} must be a nonempty string")
    return value


def _text_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise OptimizationCoreError(f"{name} must be a sequence")
    result = tuple(_require_text(item, name) for item in value)
    _require_unique_text(result, name)
    return result


def _require_unique_text(values: Sequence[str] | frozenset[str] | Any, name: str) -> None:
    materialized = tuple(values)
    if any(not isinstance(value, str) or not value.strip() for value in materialized):
        raise OptimizationCoreError(f"{name} must contain nonempty strings")
    if len(materialized) != len(set(materialized)):
        raise OptimizationCoreError(f"{name} must be unique")


def _require_action(action: str) -> None:
    if action not in REQUIRED_ACTION_ORDER + OPTIONAL_ACTIONS:
        raise OptimizationCoreError(f"unknown atomic action: {action}")


def _string_bool_capabilities(value: object) -> dict[str, bool]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): item
        for key, item in value.items()
        if isinstance(item, bool)
    }
