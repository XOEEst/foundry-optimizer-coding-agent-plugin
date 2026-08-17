from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from foundry_opt.bootstrap.contracts import BootstrapSidecar, RootRegistry
from foundry_opt.bootstrap.errors import BootstrapConfigError
from foundry_opt.poc.config import IssueEvaluatorEntry, validate_repository_relative_path


@dataclass(frozen=True, slots=True)
class RegistrySelection:
    repo_agent_id: str
    root: str
    config_path: str
    sidecar: BootstrapSidecar
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
    objective_hash: str
    default_evaluator_ids: tuple[str, ...]
    receipt_inputs: Mapping[str, str]


AuthorPermissionResolver = Callable[[str, Sequence[str]], bool]


def resolve_registry_selection(
    repository_root: Path,
    *,
    repo_agent_id: str | None = None,
    explicit_target: str | None = None,
) -> RegistrySelection:
    if explicit_target is not None:
        raise BootstrapConfigError("explicit Foundry targets are not allowed for registry-managed workflow execution")
    registry_path = repository_root / ".foundry-opt" / "registry.yaml"
    registry_bytes = registry_path.read_bytes()
    registry = RootRegistry.from_document(registry_bytes.decode("utf-8"))
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
    sidecar_path = repository_root / selected.config_path
    sidecar_bytes = sidecar_path.read_bytes()
    sidecar = BootstrapSidecar.from_document(sidecar_bytes.decode("utf-8"))
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


def verify_issue_evaluator_authority(
    author_login: str,
    evaluators: Sequence[IssueEvaluatorEntry] | None,
    *,
    resolver: AuthorPermissionResolver | None,
) -> None:
    if not evaluators:
        return
    if resolver is None:
        raise BootstrapConfigError("issue-supplied evaluator IDs require an injected write-authority resolver")
    ids = [entry.evaluator_id for entry in evaluators]
    if not resolver(author_login, ids):
        raise BootstrapConfigError("issue author is not authorized to request arbitrary evaluator IDs")


def build_changed_path_matrix(
    repository_root: Path,
    *,
    changed_paths: Sequence[str],
    manual_repo_agent_id: str | None = None,
) -> tuple[WorkflowMatrixEntry, ...]:
    registry = RootRegistry.from_document((repository_root / ".foundry-opt" / "registry.yaml").read_text(encoding="utf-8"))
    enabled = [agent for agent in registry.agents if agent.enabled]
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
        path.startswith(".foundry-opt/")
        or path.startswith(".github/workflows/")
        or path == ".github/foundry-opt.lock.yml"
        for path in normalized
    )
    sidecars = {
        agent.agent_id: BootstrapSidecar.from_document((repository_root / agent.config_path).read_text(encoding="utf-8"))
        for agent in enabled
    }
    include: list[WorkflowMatrixEntry] = []
    for agent in sorted(enabled, key=lambda item: (item.root.casefold(), item.agent_id.casefold(), item.config_path.casefold())):
        sidecar = sidecars[agent.agent_id]
        relations = {relation.agent_id for relation in sidecar.shared_source_relations}
        roots = {agent.root, *(registry_agent.root for registry_agent in enabled if registry_agent.agent_id in relations)}
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
    if not use_repository_default_evaluators:
        raise BootstrapConfigError("deployment plans must use the repository default evaluator bundle")
    active = selection.sidecar.default_evaluator_bundle
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
        objective_hash=active.objective.objective_hash,
        default_evaluator_ids=tuple(item.reference.evaluator_id for item in active.objective.evaluators),
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
