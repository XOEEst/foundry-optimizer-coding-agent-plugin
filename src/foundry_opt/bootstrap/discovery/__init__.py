from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml
from pydantic import BaseModel, field_validator

from foundry_opt.bootstrap.canonical import canonical_json_bytes
from foundry_opt.bootstrap.contracts import BindingAssessment
from foundry_opt.bootstrap.errors import BootstrapConfigError
from foundry_opt.poc.config import validate_repository_relative_path

_INSTRUCTION_NAMES = frozenset({"AGENTS.md", "CLAUDE.md", "README.md"})
_PACKAGE_FILES = frozenset({"package.json", "pyproject.toml", "requirements.txt", "Dockerfile", "Dockerfile.app"})
_METADATA_RE = re.compile(r"^agent-metadata(?:\.[^.]+)?\.ya?ml$", re.IGNORECASE)
_ENTRYPOINT_FILE_RE = re.compile(r"^(main|app|server|index|program)\.(py|js|ts|tsx|mjs|cjs|cs)$", re.IGNORECASE)
_BLOCKED_FILE_NAMES = frozenset({".env", ".env.local", "secrets.json", "credentials.json", "trace.json", "trace.ndjson", "dataset.csv", "dataset.jsonl", "dataset.parquet"})
_BLOCKED_SEGMENTS = frozenset({"node_modules", ".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache"})
_FRAMEWORK_HINTS: tuple[tuple[str, str], ...] = (
    ("fastapi", "fastapi"),
    ("flask", "flask"),
    ("azure.functions", "azure-functions"),
    ("express", "express"),
    ("next/server", "nextjs"),
    ("@azure/functions", "azure-functions"),
    ("microsoft.semantickernel", "semantic-kernel"),
)
_HANDLER_HINTS: tuple[str, ...] = ("responses.create", "invoke(", "handler(", "app.route", "MapPost(", "Function(")
_PROMPT_DIR_NAMES = frozenset({"prompts", "skills", "tests"})


def _repo_rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _safe_rel(path: Path, root: Path) -> str:
    return validate_repository_relative_path(_repo_rel(path, root))


def _stable_repo_agent_id(value: str) -> str:
    slug = re.sub(r"[^a-z0-9._-]+", "-", value.casefold()).strip("-._")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        raise BootstrapConfigError("repoAgentId cannot be empty after normalization")
    return slug


def _within(parent: PurePosixPath, child: PurePosixPath) -> bool:
    return parent == child or parent in child.parents


class DiscoveryEvidence(BaseModel):
    kind: str
    path: str
    detail: str
    confidence: float


class DiscoveryBlocker(BaseModel):
    code: str
    detail: str


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

    @field_validator("root", "sourceRoot")
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


@dataclass(frozen=True)
class _Candidate:
    root: Path
    config_path: Path | None
    source_root: Path
    evidence: list[DiscoveryEvidence]


def _read_yaml(path: Path) -> Mapping[str, object]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, Mapping) else {}


def _iter_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*"), key=lambda p: p.as_posix()):
        if path.is_file() and not any(segment in _BLOCKED_SEGMENTS for segment in path.parts):
            yield path


def _text_excerpt(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def _fingerprint(paths: Sequence[Path], root: Path) -> str:
    payload: list[dict[str, str]] = []
    for path in sorted(paths, key=lambda p: p.as_posix()):
        name = path.name.casefold()
        if name in _BLOCKED_FILE_NAMES or any(part.casefold().startswith(".env") for part in path.parts):
            continue
        rel = _repo_rel(path, root)
        payload.append({"path": rel, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _candidate_from_metadata(path: Path, root: Path) -> _Candidate:
    payload = _read_yaml(path)
    candidate_root = path.parent.parent if path.parent.name == ".foundry" else path.parent
    source_root = candidate_root
    declared_root = payload.get("source_root")
    if isinstance(declared_root, str) and declared_root not in {"", ".", ".."}:
        maybe = (candidate_root / PurePosixPath(declared_root)).resolve()
        if maybe.is_relative_to(root.resolve()) and maybe.exists():
            source_root = maybe
    evidence = [DiscoveryEvidence(kind="agent-metadata", path=_safe_rel(path, root), detail=f"metadata file {path.name}", confidence=0.99)]
    return _Candidate(root=candidate_root, config_path=path, source_root=source_root, evidence=evidence)


def _candidate_from_azure_yaml(path: Path, root: Path) -> list[_Candidate]:
    payload = _read_yaml(path)
    services = payload.get("services")
    if not isinstance(services, Mapping):
        return []
    candidates: list[_Candidate] = []
    for service_name, service in sorted(services.items(), key=lambda item: str(item[0]).casefold()):
        if not isinstance(service, Mapping) or not isinstance(service.get("project"), str):
            continue
        source_root = (path.parent / PurePosixPath(service["project"])).resolve()
        if source_root.exists() and source_root.is_dir() and source_root.is_relative_to(root.resolve()):
            candidates.append(
                _Candidate(
                    root=source_root,
                    config_path=None,
                    source_root=source_root,
                    evidence=[DiscoveryEvidence(kind="azure-service", path=_safe_rel(path, root), detail=f"service {service_name} project={service['project']}", confidence=0.75)],
                )
            )
    return candidates


def _augment_candidate(candidate: _Candidate, repo_root: Path) -> _Candidate:
    evidence = list(candidate.evidence)
    for repo_file in sorted(repo_root.glob("*"), key=lambda p: p.as_posix()):
        if repo_file.is_file() and repo_file.name in _INSTRUCTION_NAMES:
            rel = _safe_rel(repo_file, repo_root)
            evidence.append(DiscoveryEvidence(kind="instructions", path=rel, detail=repo_file.name, confidence=0.25))
    for workflow in sorted((repo_root / ".github" / "workflows").glob("*"), key=lambda p: p.as_posix()) if (repo_root / ".github" / "workflows").exists() else ():
        if workflow.is_file() and "foundry" in _text_excerpt(workflow).casefold():
            evidence.append(DiscoveryEvidence(kind="workflow", path=_safe_rel(workflow, repo_root), detail="foundry/deploy workflow", confidence=0.3))
    for child in sorted(candidate.root.rglob("*"), key=lambda p: p.as_posix()):
        if not child.is_file():
            continue
        rel = _safe_rel(child, repo_root)
        name = child.name
        lower = name.casefold()
        if name in _INSTRUCTION_NAMES:
            evidence.append(DiscoveryEvidence(kind="instructions", path=rel, detail=name, confidence=0.45))
        if name in _PACKAGE_FILES:
            evidence.append(DiscoveryEvidence(kind="package-manifest", path=rel, detail=name, confidence=0.65))
        if ".github/workflows/" in rel and "foundry" in _text_excerpt(child).casefold():
            evidence.append(DiscoveryEvidence(kind="workflow", path=rel, detail="foundry/deploy workflow", confidence=0.55))
        if _ENTRYPOINT_FILE_RE.match(name):
            evidence.append(DiscoveryEvidence(kind="entrypoint", path=rel, detail=name, confidence=0.55))
        if any(segment.casefold() in _PROMPT_DIR_NAMES for segment in child.parts):
            evidence.append(DiscoveryEvidence(kind="prompt-skill-test", path=rel, detail=str(PurePosixPath(rel).parent), confidence=0.4))
        lower_text = _text_excerpt(child).casefold()
        for needle, label in _FRAMEWORK_HINTS:
            if needle in lower_text:
                evidence.append(DiscoveryEvidence(kind="framework-import", path=rel, detail=label, confidence=0.35))
                break
        for needle in _HANDLER_HINTS:
            if needle.casefold() in lower_text:
                evidence.append(DiscoveryEvidence(kind="handler", path=rel, detail=needle, confidence=0.35))
                break
    dedup = {(item.kind, item.path.casefold(), item.detail.casefold()): item for item in evidence}
    return _Candidate(root=candidate.root, config_path=candidate.config_path, source_root=candidate.source_root, evidence=sorted(dedup.values(), key=lambda e: (e.kind, e.path, e.detail)))


def _assess_binding(repo_agent_id: str, evidence: Sequence[DiscoveryEvidence]) -> tuple[BindingAssessment, tuple[DiscoveryBlocker, ...]]:
    kinds = {item.kind for item in evidence}
    has_runtime = any(kind in kinds for kind in {"entrypoint", "framework-import", "handler"})
    has_binding = "agent-metadata" in kinds
    has_workflow = "workflow" in kinds
    if has_binding and has_runtime and has_workflow:
        return BindingAssessment(agent_id=repo_agent_id, classification="bound-aligned", detail="metadata, runtime, and workflow evidence align"), ()
    if has_binding and not has_runtime:
        blockers = (DiscoveryBlocker(code="missing-runtime-entrypoint", detail="binding metadata exists but no supported runtime evidence was found"),)
        return BindingAssessment(agent_id=repo_agent_id, classification="bound-diverged", detail=blockers[0].detail), blockers
    if has_binding:
        return BindingAssessment(agent_id=repo_agent_id, classification="bound-unknown", detail="binding metadata found but workflow alignment is incomplete"), ()
    if has_runtime and has_workflow:
        return BindingAssessment(agent_id=repo_agent_id, classification="ready-unbound", detail="runtime readiness found without remote binding"), ()
    blockers = []
    if not has_runtime:
        blockers.append(DiscoveryBlocker(code="missing-runtime-readiness", detail="no supported runtime or handler evidence was found"))
    if not has_workflow:
        blockers.append(DiscoveryBlocker(code="missing-foundry-workflow", detail="runtime evidence exists but no existing Foundry/deploy workflow was found" if has_runtime else "no existing Foundry/deploy workflow was found"))
    return BindingAssessment(agent_id=repo_agent_id, classification="not-ready", detail=blockers[0].detail), tuple(blockers)


def discover_repository_agents(repository_root: str | Path, *, selected_agents: Sequence[Mapping[str, str] | str] | None = None, approved_shared_sources: Mapping[str, Sequence[str]] | None = None) -> DiscoveryResult:
    root = Path(repository_root).resolve()
    if not root.exists() or not root.is_dir():
        raise BootstrapConfigError("repository_root must be an existing directory")
    candidates: list[_Candidate] = []
    for path in _iter_files(root):
        if path.parent.name == ".foundry" and _METADATA_RE.match(path.name):
            candidates.append(_candidate_from_metadata(path, root))
        elif path.name == "azure.yaml":
            candidates.extend(_candidate_from_azure_yaml(path, root))
    if not candidates:
        candidates.append(_Candidate(root=root, config_path=None, source_root=root, evidence=[]))
    explicit_roots = {candidate.root.resolve() for candidate in candidates}
    by_root = {}
    for candidate in map(lambda c: _augment_candidate(c, root), candidates):
        normalized = "." if candidate.root == root else _safe_rel(candidate.root, root)
        by_root[normalized.casefold()] = candidate
    for path in _iter_files(root):
        if not _ENTRYPOINT_FILE_RE.match(path.name):
            continue
        parent = path.parent.resolve()
        if any(parent == existing or parent.is_relative_to(existing) or existing.is_relative_to(parent) for existing in explicit_roots):
            continue
        candidate = _augment_candidate(_Candidate(root=parent, config_path=None, source_root=parent, evidence=[DiscoveryEvidence(kind="entrypoint", path=_safe_rel(path, root), detail=path.name, confidence=0.55)]), root)
        normalized = "." if candidate.root == root else _safe_rel(candidate.root, root)
        by_root.setdefault(normalized.casefold(), candidate)
    selected_map: dict[str, tuple[str, str | None]] = {}
    for item in selected_agents or ():
        if isinstance(item, str):
            normalized = _stable_repo_agent_id(item)
            selected_map[normalized.casefold()] = (normalized, None)
        elif isinstance(item, Mapping):
            normalized = _stable_repo_agent_id(str(item.get("repoAgentId") or item.get("agent_id") or item.get("name") or ""))
            selected_map[normalized.casefold()] = (normalized, str(item.get("root")) if item.get("root") else None)
    agents: list[DiscoveredAgent] = []
    overlap_roots: list[tuple[str, PurePosixPath]] = []
    for _, candidate in sorted(by_root.items()):
        normalized_root = "." if candidate.root == root else _safe_rel(candidate.root, root)
        repo_agent_id = _stable_repo_agent_id(candidate.root.name if normalized_root != "." else root.name)
        if selected_map:
            matched = selected_map.get(repo_agent_id.casefold()) or selected_map.get(_stable_repo_agent_id(normalized_root if normalized_root != "." else root.name).casefold())
            if matched is None:
                continue
            repo_agent_id = matched[0]
        source_root = "." if candidate.source_root == root else _safe_rel(candidate.source_root, root)
        config_path = _safe_rel(candidate.config_path, root) if candidate.config_path is not None else None
        binding, blockers = _assess_binding(repo_agent_id, candidate.evidence)
        repo_paths = list(_iter_files(candidate.root))
        package_files = [path for path in repo_paths if path.name in _PACKAGE_FILES or _ENTRYPOINT_FILE_RE.match(path.name)]
        if candidate.config_path is not None:
            package_files.append(candidate.config_path)
        overlap_roots.append((repo_agent_id, PurePosixPath(source_root)))
        agents.append(
            DiscoveredAgent(
                repoAgentId=repo_agent_id,
                root=normalized_root,
                configPath=config_path,
                sourceRoot=source_root,
                packageRoot=source_root,
                evidence=tuple(sorted(candidate.evidence, key=lambda e: (e.kind, e.path, e.detail))),
                confidence=round(min(1.0, sum(item.confidence for item in candidate.evidence) / max(len(candidate.evidence), 1)), 4),
                packageFingerprint=_fingerprint(package_files, root),
                sourceFingerprint=_fingerprint(repo_paths, root),
                bindingAssessment=binding,
                blockers=blockers,
            )
        )
    approved_map = {_stable_repo_agent_id(k).casefold(): tuple(sorted({_stable_repo_agent_id(v) for v in values}, key=str.casefold)) for k, values in (approved_shared_sources or {}).items()}
    final_agents = []
    for agent in sorted(agents, key=lambda a: (a.repoAgentId.casefold(), a.root, a.configPath or "")):
        shared = []
        agent_root = PurePosixPath(agent.sourceRoot)
        for other_id, other_root in overlap_roots:
            if other_id != agent.repoAgentId and (_within(agent_root, other_root) or _within(other_root, agent_root)) and other_id in approved_map.get(agent.repoAgentId.casefold(), ()):
                shared.append(other_id)
        final_agents.append(agent.model_copy(update={"approvedSharedSourceRepoAgentIds": tuple(sorted(set(shared), key=str.casefold))}))
    return DiscoveryResult(repositoryRoot=str(root), agents=tuple(final_agents))


def discovery_result_json(result: DiscoveryResult) -> str:
    return json.dumps(result.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


__all__ = ["DiscoveryBlocker", "DiscoveredAgent", "DiscoveryEvidence", "DiscoveryResult", "discover_repository_agents", "discovery_result_json"]