from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal, cast

from foundry_opt.contract_errors import BootstrapConfigError
from foundry_opt.repository_contracts import AgentProfile, RepositoryRegistry
from foundry_opt.poc.config import IssueEvaluatorEntry, validate_repository_relative_path
from foundry_opt.poc.verification import (
    DeploymentVerification,
    resolve_deployment_verification,
)
from foundry_opt.verification import VerificationCheckSpec, VerificationDatasetInput


@dataclass(frozen=True, slots=True)
class RegistrySelection:
    repo_agent_id: str
    root: str
    config_path: str
    sidecar: AgentProfile
    registry_hash: str
    sidecar_hash: str


@dataclass(frozen=True, slots=True)
class WorkflowMatrixEntry:
    changed_root: str
    repo_agent_id: str
    config_path: str


@dataclass(frozen=True, slots=True)
class DeploymentPlan:
    repo_agent_id: str
    changed_root: str
    config_path: str
    exact_source: str
    package_root: str
    registry_hash: str
    sidecar_hash: str
    objective_hash: str | None
    default_evaluator_ids: tuple[str, ...]
    verification: DeploymentVerification
    receipt_inputs: Mapping[str, str]


KnownIssueAuthorPermission = Literal[
    "admin",
    "maintain",
    "write",
    "triage",
    "read",
    "none",
]

_KNOWN_ISSUE_AUTHOR_PERMISSIONS = frozenset(
    {"admin", "maintain", "write", "triage", "read", "none"}
)
_OVERRIDE_ISSUE_AUTHOR_PERMISSIONS = frozenset({"admin", "maintain", "write"})


def normalize_issue_author_permission(
    permission: str,
) -> KnownIssueAuthorPermission:
    normalized = permission.strip().casefold()
    if normalized not in _KNOWN_ISSUE_AUTHOR_PERMISSIONS:
        raise BootstrapConfigError(
            "trusted binding carries an unknown issue author permission"
        )
    return cast(KnownIssueAuthorPermission, normalized)


def _require_issue_override_permission(
    author_permission: str | None,
    *,
    missing_message: str,
    insufficient_message: str,
) -> None:
    if author_permission is None:
        raise BootstrapConfigError(missing_message)
    normalized = normalize_issue_author_permission(author_permission)
    if normalized not in _OVERRIDE_ISSUE_AUTHOR_PERMISSIONS:
        raise BootstrapConfigError(insufficient_message)


def resolve_registry_selection(
    repository_root: Path,
    *,
    repo_agent_id: str | None = None,
    explicit_target: str | None = None,
    content_reader: Callable[[str], bytes] | None = None,
) -> RegistrySelection:
    if explicit_target is not None:
        raise BootstrapConfigError("explicit Foundry targets are not allowed for registry-managed workflow execution")
    reader = (
        content_reader
        if content_reader is not None
        else lambda relative: (repository_root / relative).read_bytes()
    )
    registry_bytes = reader(".foundry-opt/registry.yaml")
    registry = RepositoryRegistry.from_document(registry_bytes.decode("utf-8"))
    enabled = tuple(agent for agent in registry.agents if agent.enabled)
    if repo_agent_id is None:
        if len(enabled) != 1:
            raise BootstrapConfigError("registry selection requires exactly one enabled agent when repoAgentId is omitted")
        selected = enabled[0]
    else:
        matches = [agent for agent in enabled if agent.agent_id == repo_agent_id]
        if len(matches) != 1:
            raise BootstrapConfigError("repoAgentId must resolve exactly one enabled registry agent")
        selected = matches[0]
    try:
        sidecar_bytes = reader(selected.config_path)
    except OSError as exc:
        raise BootstrapConfigError("enabled registry entry requires a profile at config_path") from exc
    sidecar = AgentProfile.from_document(sidecar_bytes.decode("utf-8"))
    if sidecar.repo_agent_id != selected.agent_id:
        raise BootstrapConfigError("registry config_path sidecar repo_agent_id does not match registry agent_id")
    return RegistrySelection(
        repo_agent_id=selected.agent_id,
        root=selected.root,
        config_path=selected.config_path,
        sidecar=sidecar,
        registry_hash=hashlib.sha256(registry_bytes).hexdigest(),
        sidecar_hash=hashlib.sha256(sidecar_bytes).hexdigest(),
    )


def protected_editable_patterns(selection: RegistrySelection) -> tuple[str, ...]:
    protected = {".foundry-opt/**", ".foundry-opt/registry.yaml", selection.config_path}
    protected.add(f"{PurePosixPath(selection.config_path).parent.as_posix()}/**")
    return tuple(sorted(protected))


def protected_editable_patterns_for_repository(
    repository_root: Path,
    *,
    repo_agent_id: str | None = None,
) -> tuple[str, ...]:
    registry = RepositoryRegistry.from_document((repository_root / ".foundry-opt" / "registry.yaml").read_text(encoding="utf-8"))
    protected = {".foundry-opt/**", ".foundry-opt/registry.yaml"}
    enabled = [agent for agent in registry.agents if agent.enabled]
    selected_agents = enabled
    if repo_agent_id is not None:
        selected_agents = [agent for agent in registry.agents if agent.agent_id == repo_agent_id]
        if len(selected_agents) != 1:
            raise BootstrapConfigError("repoAgentId must resolve exactly one registry agent")
    for agent in selected_agents:
        protected.add(agent.config_path)
        protected.add(f"{PurePosixPath(agent.config_path).parent.as_posix()}/**")
    return tuple(sorted(protected))


def verify_issue_evaluator_authority(
    author_permission: str | None,
    evaluators: Sequence[IssueEvaluatorEntry] | None,
) -> None:
    if not evaluators:
        return
    _require_issue_override_permission(
        author_permission,
        missing_message=(
            "issue-supplied evaluator IDs require a trusted issue author "
            "permission in the binding"
        ),
        insufficient_message=(
            "issue author requires write, maintain, or admin repository "
            "permission to request arbitrary evaluator IDs"
        ),
    )


def verify_issue_dataset_authority(
    author_permission: str | None,
    dataset: VerificationDatasetInput | None,
) -> None:
    if dataset is None:
        return
    _require_issue_override_permission(
        author_permission,
        missing_message=(
            "issue-supplied verification dataset requires a trusted issue "
            "author permission in the binding"
        ),
        insufficient_message=(
            "issue author requires write, maintain, or admin repository "
            "permission to request arbitrary verification datasets"
        ),
    )


def verify_issue_check_authority(
    author_permission: str | None,
    checks: Sequence[VerificationCheckSpec] | None,
) -> None:
    if not checks:
        return
    _require_issue_override_permission(
        author_permission,
        missing_message=(
            "issue-supplied verification commands/checks require a trusted "
            "issue author permission in the binding"
        ),
        insufficient_message=(
            "issue author requires write, maintain, or admin repository "
            "permission to request arbitrary verification commands/checks"
        ),
    )


def build_changed_path_matrix(
    repository_root: Path,
    *,
    changed_paths: Sequence[str],
    manual_repo_agent_id: str | None = None,
) -> tuple[WorkflowMatrixEntry, ...]:
    registry = RepositoryRegistry.from_document((repository_root / ".foundry-opt" / "registry.yaml").read_text(encoding="utf-8"))
    enabled = [agent for agent in registry.agents if agent.enabled]
    by_id = {agent.agent_id: agent for agent in registry.agents}
    if manual_repo_agent_id is not None:
        chosen = [agent for agent in enabled if agent.agent_id == manual_repo_agent_id]
        if len(chosen) != 1:
            raise BootstrapConfigError("manual repoAgentId must identify exactly one enabled agent")
        agent = chosen[0]
        return (WorkflowMatrixEntry(changed_root=agent.root, repo_agent_id=agent.agent_id, config_path=agent.config_path),)
    normalized = tuple(
        sorted({validate_repository_relative_path(path, field="changed_path") for path in changed_paths}, key=lambda value: (value.casefold(), value))
    )
    shared_contract_changed = any(
        # `.foundry-opt/` already covers the authoritative committed lock
        # (`.foundry-opt/bootstrap.lock.json`) and the registry.
        path.startswith(".foundry-opt/") or path.startswith(".github/workflows/")
        for path in normalized
    )
    sidecars = {
        agent.agent_id: AgentProfile.from_document((repository_root / agent.config_path).read_text(encoding="utf-8"))
        for agent in enabled
    }
    include: list[WorkflowMatrixEntry] = []
    for agent in sorted(enabled, key=lambda item: (item.root.casefold(), item.agent_id.casefold(), item.config_path.casefold())):
        sidecar = sidecars[agent.agent_id]
        relations = {relation.agent_id for relation in sidecar.shared_source_relations}
        unknown = sorted(relation for relation in relations if relation not in by_id)
        if unknown:
            raise BootstrapConfigError(f"shared_source_relations references unknown agent_id: {unknown[0]}")
        roots = {agent.root, *(by_id[relation].root for relation in relations)}
        prefixes = tuple(sorted({f"{root}/" for root in roots} | {agent.config_path}))
        if shared_contract_changed or any(path == agent.config_path or any(path.startswith(prefix) for prefix in prefixes) for path in normalized):
            include.append(WorkflowMatrixEntry(changed_root=agent.root, repo_agent_id=agent.agent_id, config_path=agent.config_path))
    return tuple(include)


def build_registered_deployment_plan(
    selection: RegistrySelection,
    *,
    changed_root: str,
    exact_source: str,
    use_repository_default_evaluators: bool,
) -> DeploymentPlan:
    if (
        selection.sidecar.verification.evaluation_gate_policy
        == "require_foundry_evaluation"
        and not use_repository_default_evaluators
    ):
        raise BootstrapConfigError("deployment plans must use the repository default evaluator bundle")
    verification = resolve_deployment_verification(profile=selection.sidecar)
    active = (
        selection.sidecar.default_evaluator_bundle
        if verification.mode == "foundry_evaluation"
        else None
    )
    receipt_inputs = {
        "repo_agent_id": selection.repo_agent_id,
        "changed_root": changed_root,
        "exact_source": exact_source,
        "config_path": selection.config_path,
    }
    return DeploymentPlan(
        repo_agent_id=selection.repo_agent_id,
        changed_root=changed_root,
        config_path=selection.config_path,
        exact_source=exact_source,
        package_root=selection.sidecar.package_root,
        registry_hash=selection.registry_hash,
        sidecar_hash=selection.sidecar_hash,
        objective_hash=(
            None if active is None else active.objective.objective_hash
        ),
        default_evaluator_ids=(
            ()
            if active is None
            else tuple(
                item.reference.evaluator_id
                for item in active.objective.evaluators
            )
        ),
        verification=verification,
        receipt_inputs=receipt_inputs,
    )


def matrix_to_json(entries: Sequence[WorkflowMatrixEntry]) -> str:
    payload = {
        "include": [
            {
                "changed_root": entry.changed_root,
                "repo_agent_id": entry.repo_agent_id,
                "config_path": entry.config_path,
            }
            for entry in entries
        ]
    }
    return json.dumps(payload, sort_keys=True)
