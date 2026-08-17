from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from foundry_opt.bootstrap.canonical import canonical_json_bytes
from foundry_opt.bootstrap.contracts import BindingAssessment
from foundry_opt.bootstrap.errors import BootstrapConfigError
from foundry_opt.poc.config import validate_repository_relative_path

_TEXT_SUFFIXES = frozenset(
    {
        ".cs",
        ".js",
        ".json",
        ".md",
        ".mjs",
        ".py",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".yaml",
        ".yml",
    }
)
_PACKAGE_FILE_NAMES = frozenset({"package.json", "pyproject.toml", "requirements.txt", "Dockerfile", "Dockerfile.app"})
_ENTRYPOINT_FILE_NAMES = frozenset({"main.py", "app.py", "server.py", "index.js", "index.ts", "program.cs"})
_INSTRUCTION_FILE_NAMES = frozenset({"AGENTS.md", "CLAUDE.md", "README.md"})
_WORKFLOW_DIR = PurePosixPath(".github/workflows")
_ALLOWED_TOP_LEVEL_DIRS = frozenset(
    {
        ".foundry",
        ".github",
        "agents",
        "app",
        "apps",
        "services",
        "service",
        "src",
        "skills",
        "tests",
        "packages",
    }
)
_BLOCKED_EXACT_NAMES = frozenset(
    {
        ".env",
        ".env.local",
        ".env.development",
        ".env.production",
        "credentials.json",
        "secrets.json",
        "trace.json",
        "trace.ndjson",
        "trace.jsonl",
        "dataset.csv",
        "dataset.json",
        "dataset.jsonl",
        "dataset.parquet",
        "prompt.txt",
        "prompts.txt",
    }
)
_BLOCKED_SEGMENTS = frozenset(
    {
        ".git",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".venv",
        "venv",
        "node_modules",
        "datasets",
        "traces",
        "prompts",
    }
)
_MAX_TEXT_BYTES = 512 * 1024
_MAX_HASH_BYTES = 2 * 1024 * 1024
_FRAMEWORK_MARKERS: tuple[tuple[str, str], ...] = (
    ("fastapi", "fastapi"),
    ("flask", "flask"),
    ("@azure/functions", "azure-functions"),
    ("azure.functions", "azure-functions"),
    ("express", "express"),
    ("microsoft.semantickernel", "semantic-kernel"),
)
_HANDLER_MARKERS: tuple[str, ...] = ("responses.create", "invoke(", "handler(", "app.route", "MapPost(", "Function(")


def _repo_rel(path: Path, repository_root: Path) -> str:
    try:
        relative = path.relative_to(repository_root)
    except ValueError as exc:
        raise BootstrapConfigError(f"path is outside repository root: {path}") from exc
    if relative == Path("."):
        return "."
    return validate_repository_relative_path(relative.as_posix(), field="repository path")


def _resolve_repository_relative(repository_root: Path, value: str, *, field: str, allow_dot: bool = False) -> Path:
    if allow_dot and value == ".":
        return repository_root
    try:
        relative = validate_repository_relative_path(value, field=field)
    except Exception as exc:
        raise BootstrapConfigError(f"{field} resolves outside repository root: {value!r}") from exc
    target = (repository_root / PurePosixPath(relative)).resolve()
    try:
        target.relative_to(repository_root)
    except ValueError as exc:
        raise BootstrapConfigError(f"{field} resolves outside repository root: {value!r}") from exc
    if not target.exists():
        raise BootstrapConfigError(f"{field} does not exist: {value!r}")
    if target.is_symlink():
        raise BootstrapConfigError(f"{field} must not be a symlink: {value!r}")
    if not target.is_dir():
        raise BootstrapConfigError(f"{field} must resolve to a directory: {value!r}")
    return target


def _normalize_repo_agent_id(value: str) -> str:
    normalized = "-".join(part for part in "".join(ch.lower() if ch.isalnum() or ch in "._-" else "-" for ch in value).strip("-._").split("-") if part)
    if not normalized:
        raise BootstrapConfigError("repoAgentId cannot be empty after normalization")
    return normalized


def _safe_lstat(path: Path) -> Any:
    try:
        return path.lstat()
    except OSError as exc:
        raise BootstrapConfigError(f"unable to stat repository path: {path}") from exc


def _is_blocked_path(relative: PurePosixPath) -> bool:
    parts = relative.parts
    if any(part.casefold() in _BLOCKED_SEGMENTS for part in parts):
        return True
    name = relative.name.casefold()
    return name in _BLOCKED_EXACT_NAMES or name.startswith(".env")


def _iter_repository_files(repository_root: Path) -> Iterable[tuple[PurePosixPath, Path]]:
    pending = [repository_root]
    while pending:
        current = pending.pop()
        children = sorted(current.iterdir(), key=lambda p: p.name.casefold(), reverse=True)
        for child in children:
            lst = _safe_lstat(child)
            relative = PurePosixPath(_repo_rel(child, repository_root))
            if stat.S_ISLNK(lst.st_mode):
                raise BootstrapConfigError(f"repository contains symlinked path: {relative.as_posix()}")
            if stat.S_ISDIR(lst.st_mode):
                if _is_blocked_path(relative):
                    continue
                pending.append(child)
                continue
            if not stat.S_ISREG(lst.st_mode):
                raise BootstrapConfigError(f"repository contains unsupported special file: {relative.as_posix()}")
            if _is_blocked_path(relative):
                continue
            yield relative, child


def _read_text_file(path: Path) -> str:
    if path.stat().st_size > _MAX_TEXT_BYTES:
        return ""
    if path.suffix.casefold() not in _TEXT_SUFFIXES and path.name not in _PACKAGE_FILE_NAMES:
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def _hash_file(relative: PurePosixPath, path: Path) -> dict[str, str]:
    size = path.stat().st_size
    if size > _MAX_HASH_BYTES:
        raise BootstrapConfigError(f"fingerprint input exceeds size limit: {relative.as_posix()}")
    return {"path": relative.as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


class DiscoveryEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: str
    path: str
    detail: str
    confidence: float


class DiscoveryBlocker(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    detail: str


class RuntimeFacts(BaseModel):
    model_config = ConfigDict(frozen=True)

    entrypoints: tuple[str, ...] = ()
    frameworks: tuple[str, ...] = ()
    handlers: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return bool(self.entrypoints and (self.frameworks or self.handlers))


class BindingEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_endpoint: str | None = None
    agent_name: str | None = None
    expected_version: str | None = None
    source_fingerprint: str | None = None
    package_fingerprint: str | None = None


class DiscoveredAgent(BaseModel):
    repoAgentId: str
    root: str
    configPath: str | None = None
    sourceRoot: str
    packageRoot: str
    evidence: tuple[DiscoveryEvidence, ...]
    confidence: float
    packageFingerprint: str
    sourceFingerprint: str
    approvedSharedSourceRepoAgentIds: tuple[str, ...] = ()
    bindingAssessment: BindingAssessment
    blockers: tuple[DiscoveryBlocker, ...] = ()

    @field_validator("root", "sourceRoot", "packageRoot")
    @classmethod
    def _validate_root(cls, value: str) -> str:
        if value == ".":
            return value
        return validate_repository_relative_path(value)

    @field_validator("configPath")
    @classmethod
    def _validate_config(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_repository_relative_path(value)


class DiscoveryResult(BaseModel):
    repositoryRoot: str
    agents: tuple[DiscoveredAgent, ...]


@dataclass
class _Candidate:
    canonical_root: str
    source_root: str
    package_root: str
    config_path: str | None = None
    runtime_facts: RuntimeFacts = field(default_factory=RuntimeFacts)
    binding_evidence: BindingEvidence = field(default_factory=BindingEvidence)
    evidence: list[DiscoveryEvidence] = field(default_factory=list)
    include_runtime_descendants: bool = True


def _workflow_evidence(repository_root: Path) -> list[DiscoveryEvidence]:
    workflows = repository_root / _WORKFLOW_DIR
    if not workflows.exists():
        return []
    evidence: list[DiscoveryEvidence] = []
    for relative, path in sorted(_iter_repository_files(workflows), key=lambda item: item[0].as_posix()):
        if "foundry" in _read_text_file(path).casefold() or "deploy" in _read_text_file(path).casefold():
            evidence.append(DiscoveryEvidence(kind="workflow", path=relative.as_posix(), detail="foundry/deploy workflow", confidence=0.2))
    return evidence


def _instruction_evidence(repository_root: Path) -> list[DiscoveryEvidence]:
    evidence: list[DiscoveryEvidence] = []
    for name in sorted(_INSTRUCTION_FILE_NAMES):
        path = repository_root / name
        if path.exists():
            evidence.append(DiscoveryEvidence(kind="instructions", path=_repo_rel(path, repository_root), detail=name, confidence=0.15))
    return evidence


def _runtime_facts_for_root(repository_root: Path, relative_root: str) -> RuntimeFacts:
    root_path = repository_root if relative_root == "." else repository_root / PurePosixPath(relative_root)
    entrypoints: set[str] = set()
    frameworks: set[str] = set()
    handlers: set[str] = set()
    for relative, path in _iter_repository_files(root_path):
        local_relative = relative if relative_root == "." else PurePosixPath(relative_root) / relative
        if path.name in _ENTRYPOINT_FILE_NAMES:
            entrypoints.add(local_relative.as_posix())
        text = _read_text_file(path).casefold()
        frameworks.update(label for needle, label in _FRAMEWORK_MARKERS if needle in text)
        handlers.update(marker for marker in _HANDLER_MARKERS if marker.casefold() in text)
    return RuntimeFacts(
        entrypoints=tuple(sorted(entrypoints, key=str.casefold)),
        frameworks=tuple(sorted(frameworks, key=str.casefold)),
        handlers=tuple(sorted(handlers, key=str.casefold)),
    )


def _discover_runtime_candidate(relative: PurePosixPath, path: Path, repository_root: Path) -> _Candidate:
    root_path = path.parent
    canonical_root = _repo_rel(root_path, repository_root)
    for probe in path.parents:
        if probe == repository_root:
            break
        if (probe / ".foundry").exists():
            canonical_root = _repo_rel(probe, repository_root)
            break
    text = _read_text_file(path).casefold()
    frameworks = tuple(sorted({label for needle, label in _FRAMEWORK_MARKERS if needle in text}))
    handlers = tuple(sorted({needle for needle in _HANDLER_MARKERS if needle.casefold() in text}))
    evidence = [DiscoveryEvidence(kind="entrypoint", path=relative.as_posix(), detail=path.name, confidence=0.6)]
    evidence.extend(DiscoveryEvidence(kind="framework-import", path=relative.as_posix(), detail=item, confidence=0.4) for item in frameworks)
    evidence.extend(DiscoveryEvidence(kind="handler", path=relative.as_posix(), detail=item, confidence=0.4) for item in handlers)
    return _Candidate(
        canonical_root=canonical_root,
        source_root=canonical_root,
        package_root=canonical_root,
        runtime_facts=RuntimeFacts(entrypoints=(relative.as_posix(),), frameworks=frameworks, handlers=handlers),
        evidence=evidence,
    )


def _discover_metadata_candidate(relative: PurePosixPath, path: Path, repository_root: Path) -> _Candidate:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if payload is None:
        payload = {}
    if not isinstance(payload, Mapping):
        raise BootstrapConfigError(f"metadata file must contain a mapping: {relative.as_posix()}")
    root_dir = path.parent.parent if path.parent.name == ".foundry" else path.parent
    canonical_root = _repo_rel(root_dir, repository_root)
    declared_source = str(payload.get("source_root", canonical_root if canonical_root != "." else "."))
    declared_package = str(payload.get("package_root", declared_source))
    resolved_source = _resolve_repository_relative(repository_root, declared_source, field="source_root", allow_dot=True)
    resolved_package = _resolve_repository_relative(repository_root, declared_package, field="package_root", allow_dot=True)
    binding = BindingEvidence(
        project_endpoint=str(payload.get("project_endpoint")) if payload.get("project_endpoint") is not None else None,
        agent_name=str(payload.get("agent_name")) if payload.get("agent_name") is not None else None,
        expected_version=str(payload.get("expected_version")) if payload.get("expected_version") is not None else None,
    )
    return _Candidate(
        canonical_root=canonical_root,
        source_root=_repo_rel(resolved_source, repository_root),
        package_root=_repo_rel(resolved_package, repository_root),
        config_path=relative.as_posix(),
        binding_evidence=binding,
        evidence=[DiscoveryEvidence(kind="agent-metadata", path=relative.as_posix(), detail=path.name, confidence=0.95)],
        include_runtime_descendants=False,
    )


def _merge_candidates(existing: _Candidate, incoming: _Candidate) -> _Candidate:
    if existing.source_root.casefold() != incoming.source_root.casefold() or existing.package_root.casefold() != incoming.package_root.casefold():
        raise BootstrapConfigError(f"conflicting discovery roots for {existing.canonical_root!r}")
    if existing.config_path and incoming.config_path and existing.config_path.casefold() != incoming.config_path.casefold():
        raise BootstrapConfigError(f"conflicting config paths for {existing.canonical_root!r}")
    runtime = RuntimeFacts(
        entrypoints=tuple(sorted(set(existing.runtime_facts.entrypoints) | set(incoming.runtime_facts.entrypoints), key=str.casefold)),
        frameworks=tuple(sorted(set(existing.runtime_facts.frameworks) | set(incoming.runtime_facts.frameworks), key=str.casefold)),
        handlers=tuple(sorted(set(existing.runtime_facts.handlers) | set(incoming.runtime_facts.handlers), key=str.casefold)),
    )
    binding = existing.binding_evidence
    for field_name in ("project_endpoint", "agent_name", "expected_version"):
        left = getattr(binding, field_name)
        right = getattr(incoming.binding_evidence, field_name)
        if left and right and left != right:
            raise BootstrapConfigError(f"conflicting binding evidence for {existing.canonical_root!r}")
        if not left and right:
            binding = binding.model_copy(update={field_name: right})
    evidence = {(item.kind, item.path.casefold(), item.detail.casefold()): item for item in existing.evidence}
    for item in incoming.evidence:
        evidence[(item.kind, item.path.casefold(), item.detail.casefold())] = item
    return _Candidate(
        canonical_root=existing.canonical_root,
        source_root=existing.source_root,
        package_root=existing.package_root,
        config_path=existing.config_path or incoming.config_path,
        runtime_facts=runtime,
        binding_evidence=binding,
        evidence=sorted(evidence.values(), key=lambda item: (item.kind, item.path, item.detail)),
    )


def _collect_candidates(repository_root: Path) -> dict[str, _Candidate]:
    discovered: dict[str, _Candidate] = {}
    root_fallback: _Candidate | None = None
    for relative, path in _iter_repository_files(repository_root):
        candidate: _Candidate | None = None
        if path.parent.name == ".foundry" and path.suffix.casefold() in {".yaml", ".yml"} and path.name.casefold().startswith("agent-metadata"):
            candidate = _discover_metadata_candidate(relative, path, repository_root)
        elif path.name == "azure.yaml":
            continue
        elif path.name in _ENTRYPOINT_FILE_NAMES:
            candidate = _discover_runtime_candidate(relative, path, repository_root)
            if any(
                existing.source_root.casefold() == candidate.canonical_root.casefold()
                or existing.package_root.casefold() == candidate.canonical_root.casefold()
                for existing in discovered.values()
            ):
                continue
            blocked_by_parent = False
            for existing in discovered.values():
                if not existing.include_runtime_descendants:
                    existing_root = PurePosixPath(existing.canonical_root)
                    candidate_root = PurePosixPath(candidate.canonical_root)
                    if existing_root == candidate_root:
                        blocked_by_parent = True
                        break
            if blocked_by_parent:
                continue
            if candidate.canonical_root == ".":
                root_fallback = candidate if root_fallback is None else _merge_candidates(root_fallback, candidate)
                continue
        if candidate is None:
            continue
        key = candidate.canonical_root.casefold()
        if key in discovered and discovered[key].canonical_root != candidate.canonical_root:
            raise BootstrapConfigError(f"case-fold-colliding discovery roots: {discovered[key].canonical_root!r} and {candidate.canonical_root!r}")
        discovered[key] = _merge_candidates(discovered[key], candidate) if key in discovered else candidate
    if root_fallback is not None and ".".casefold() not in discovered:
        discovered["."] = root_fallback
    if not discovered:
        discovered["."] = _Candidate(canonical_root=".", source_root=".", package_root=".")
    return discovered


def _fingerprint_root(repository_root: Path, relative_root: str) -> str:
    root_path = repository_root if relative_root == "." else repository_root / PurePosixPath(relative_root)
    payload = [_hash_file(relative, path) for relative, path in _iter_repository_files(root_path)]
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _assess_binding(repo_agent_id: str, runtime_facts: RuntimeFacts, binding_evidence: BindingEvidence) -> tuple[BindingAssessment, tuple[DiscoveryBlocker, ...]]:
    if binding_evidence.project_endpoint or binding_evidence.agent_name or binding_evidence.expected_version:
        if not runtime_facts.entrypoints:
            blockers = (DiscoveryBlocker(code="missing-entrypoint", detail="binding evidence exists but no supported entrypoint file was found"),)
            return BindingAssessment(agent_id=repo_agent_id, classification="bound-diverged", detail=blockers[0].detail), blockers
        if not runtime_facts.ready:
            return BindingAssessment(agent_id=repo_agent_id, classification="bound-unknown", detail="binding evidence exists but runtime facts are incomplete"), ()
        if binding_evidence.source_fingerprint and binding_evidence.package_fingerprint and binding_evidence.expected_version:
            return BindingAssessment(agent_id=repo_agent_id, classification="bound-aligned", detail="runtime facts align with injected binding evidence"), ()
        return BindingAssessment(agent_id=repo_agent_id, classification="bound-unknown", detail="binding evidence is incomplete"), ()
    if runtime_facts.ready:
        return BindingAssessment(agent_id=repo_agent_id, classification="ready-unbound", detail="runtime readiness found without binding evidence"), ()
    blockers = []
    if not runtime_facts.entrypoints:
        blockers.append(DiscoveryBlocker(code="missing-entrypoint", detail="no supported entrypoint file was found"))
    if runtime_facts.entrypoints and not (runtime_facts.frameworks or runtime_facts.handlers):
        blockers.append(DiscoveryBlocker(code="missing-runtime-facts", detail="entrypoint exists but no framework import or handler was found"))
    return BindingAssessment(agent_id=repo_agent_id, classification="not-ready", detail=blockers[0].detail if blockers else "runtime is not ready"), tuple(blockers)


def _normalize_selection(selected_agents: Sequence[Mapping[str, str] | str] | None) -> tuple[dict[str, str], bool]:
    if selected_agents is None:
        return {}, False
    selection: dict[str, str] = {}
    seen_ids: dict[str, str] = {}
    for item in selected_agents:
        if isinstance(item, str):
            root = item if item == "." else validate_repository_relative_path(item, field="selected root")
            repo_agent_id = _normalize_repo_agent_id(PurePosixPath(root).name or root)
        else:
            raw_root = item.get("root")
            if not isinstance(raw_root, str):
                raise BootstrapConfigError("selected_agents entries must include root")
            root = raw_root if raw_root == "." else validate_repository_relative_path(raw_root, field="selected root")
            raw_id = item.get("repoAgentId") or item.get("agent_id") or item.get("name")
            if not isinstance(raw_id, str) or not raw_id.strip():
                raise BootstrapConfigError("selected_agents entries must include repoAgentId")
            repo_agent_id = _normalize_repo_agent_id(raw_id)
        root_key = root.casefold()
        id_key = repo_agent_id.casefold()
        if root_key in selection:
            raise BootstrapConfigError(f"duplicate selected root: {root!r}")
        if id_key in seen_ids:
            raise BootstrapConfigError(f"duplicate selected repoAgentId: {repo_agent_id!r}")
        selection[root_key] = repo_agent_id
        seen_ids[id_key] = root
    return selection, True


def discover_repository_agents(
    repository_root: str | Path,
    *,
    selected_agents: Sequence[Mapping[str, str] | str] | None = None,
    approved_shared_sources: Mapping[str, Sequence[str]] | None = None,
    binding_evidence_by_root: Mapping[str, Mapping[str, str | None]] | None = None,
) -> DiscoveryResult:
    root = Path(repository_root).resolve()
    if not root.exists() or not root.is_dir():
        raise BootstrapConfigError("repository_root must be an existing directory")
    discovered = _collect_candidates(root)
    workflow_evidence = _workflow_evidence(root)
    instruction_evidence = _instruction_evidence(root)
    selected_map, explicit_selection = _normalize_selection(selected_agents)
    approved_map = {("." if key == "." else validate_repository_relative_path(key, field="approved shared source root")).casefold(): tuple(sorted({("." if value == "." else validate_repository_relative_path(value, field="approved shared source root")) for value in values}, key=str.casefold)) for key, values in (approved_shared_sources or {}).items()}
    results: list[DiscoveredAgent] = []
    source_roots: list[tuple[str, PurePosixPath]] = []
    for candidate in sorted(discovered.values(), key=lambda item: (item.canonical_root.casefold(), item.config_path or "")):
        root_key = candidate.canonical_root.casefold()
        if explicit_selection:
            if root_key not in selected_map:
                continue
            repo_agent_id = selected_map[root_key]
        else:
            repo_agent_id = _normalize_repo_agent_id(PurePosixPath(candidate.canonical_root).name if candidate.canonical_root != "." else "root")
        injected = binding_evidence_by_root.get(candidate.canonical_root, {}) if binding_evidence_by_root else {}
        runtime_facts = _runtime_facts_for_root(root, candidate.source_root)
        source_fingerprint = _fingerprint_root(root, candidate.source_root)
        package_fingerprint = _fingerprint_root(root, candidate.package_root)
        binding = candidate.binding_evidence.model_copy(
            update={
                "project_endpoint": injected.get("project_endpoint") or candidate.binding_evidence.project_endpoint,
                "agent_name": injected.get("agent_name") or candidate.binding_evidence.agent_name,
                "expected_version": injected.get("expected_version") or candidate.binding_evidence.expected_version,
                "source_fingerprint": injected.get("source_fingerprint"),
                "package_fingerprint": injected.get("package_fingerprint"),
            }
        )
        assessment, blockers = _assess_binding(repo_agent_id, runtime_facts, binding)
        evidence = tuple(sorted({*candidate.evidence, *workflow_evidence, *instruction_evidence}, key=lambda item: (item.kind, item.path, item.detail)))
        confidence = round(min(1.0, sum(item.confidence for item in evidence) / max(len(evidence), 1)), 4)
        source_roots.append((candidate.canonical_root, PurePosixPath(candidate.source_root)))
        results.append(
            DiscoveredAgent(
                repoAgentId=repo_agent_id,
                root=candidate.canonical_root,
                configPath=candidate.config_path,
                sourceRoot=candidate.source_root,
                packageRoot=candidate.package_root,
                evidence=evidence,
                confidence=confidence,
                packageFingerprint=package_fingerprint,
                sourceFingerprint=source_fingerprint,
                bindingAssessment=assessment,
                blockers=blockers,
            )
        )
    if explicit_selection:
        unmatched = sorted(set(selected_map) - {agent.root.casefold() for agent in results})
        if unmatched:
            raise BootstrapConfigError(f"selected roots were not discovered: {unmatched}")
    final_results: list[DiscoveredAgent] = []
    for agent in sorted(results, key=lambda item: (item.repoAgentId.casefold(), item.root, item.configPath or "")):
        overlaps = []
        agent_root = PurePosixPath(agent.sourceRoot)
        for other_root_name, other_root in source_roots:
            if other_root_name == agent.root:
                continue
            if agent_root == other_root or agent_root in other_root.parents or other_root in agent_root.parents:
                overlaps.append(other_root_name)
        approved = set(approved_map.get(agent.root.casefold(), ()))
        unapproved = sorted(root_name for root_name in overlaps if root_name.casefold() not in {item.casefold() for item in approved})
        if unapproved:
            blockers = tuple(sorted(agent.blockers + (DiscoveryBlocker(code="unapproved-shared-source", detail=f"shared source overlap requires explicit approval: {', '.join(unapproved)}"),), key=lambda item: (item.code, item.detail)))
            final_results.append(agent.model_copy(update={"blockers": blockers, "approvedSharedSourceRepoAgentIds": tuple(), "bindingAssessment": agent.bindingAssessment.model_copy(update={"classification": "not-ready", "detail": blockers[0].detail})}))
            continue
        final_results.append(agent.model_copy(update={"approvedSharedSourceRepoAgentIds": tuple(sorted(set(overlaps) & approved, key=str.casefold))}))
    return DiscoveryResult(repositoryRoot=".", agents=tuple(final_results))


def discovery_result_json(result: DiscoveryResult) -> str:
    return json.dumps(result.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


__all__ = ["DiscoveryBlocker", "DiscoveredAgent", "DiscoveryEvidence", "DiscoveryResult", "discover_repository_agents", "discovery_result_json"]
