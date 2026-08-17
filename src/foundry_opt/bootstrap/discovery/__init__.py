from __future__ import annotations

import hashlib
import json
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml
from pydantic import BaseModel, ConfigDict, field_validator

from foundry_opt.bootstrap.canonical import canonical_json_bytes
from foundry_opt.bootstrap.contracts import BindingAssessment
from foundry_opt.bootstrap.errors import BootstrapConfigError
from foundry_opt.poc.config import validate_repository_relative_path

_TEXT_SUFFIXES = frozenset({".cs", ".js", ".json", ".md", ".mjs", ".py", ".toml", ".ts", ".tsx", ".txt", ".yaml", ".yml"})
_PACKAGE_FILE_NAMES = frozenset({"package.json", "pyproject.toml", "requirements.txt", "Dockerfile", "Dockerfile.app"})
_ENTRYPOINT_FILE_NAMES = frozenset({"main.py", "app.py", "server.py", "index.js", "index.ts", "program.cs"})
_INSTRUCTION_FILE_NAMES = frozenset({"AGENTS.md", "CLAUDE.md", "README.md"})
_ALLOWED_TOP_LEVEL_DIRS = frozenset({".foundry", ".github", "agents", "app", "apps", "services", "service", "src", "skills", "tests", "packages"})
_BLOCKED_EXACT_NAMES = frozenset({".env", ".env.local", ".env.development", ".env.production", "credentials.json", "secrets.json", "trace.json", "trace.ndjson", "trace.jsonl", "dataset.csv", "dataset.json", "dataset.jsonl", "dataset.parquet", "prompt.txt", "prompts.txt"})
_BLOCKED_SEGMENTS = frozenset({".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".venv", "venv", "node_modules", "datasets", "traces", "prompts", "secrets"})
_FRAMEWORK_MARKERS: tuple[tuple[str, str], ...] = (("fastapi", "fastapi"), ("flask", "flask"), ("@azure/functions", "azure-functions"), ("azure.functions", "azure-functions"), ("express", "express"), ("microsoft.semantickernel", "semantic-kernel"))
_HANDLER_MARKERS: tuple[str, ...] = ("responses.create", "invoke(", "handler(", "app.route", "MapPost(", "Function(")
_MAX_TEXT_BYTES = 64 * 1024
_MAX_HASH_BYTES = 2 * 1024 * 1024
_MAX_METADATA_BYTES = 16 * 1024
_MAX_DEPTH = 8
_MAX_FILES = 5000
_MAX_AGGREGATE_BYTES = 16 * 1024 * 1024
_SHA256_LEN = 64


def _repo_rel(path: Path, repository_root: Path) -> str:
    try:
        relative = path.relative_to(repository_root)
    except ValueError as exc:
        raise BootstrapConfigError(f"path is outside repository root: {path}") from exc
    if relative == Path("."):
        return "."
    return validate_repository_relative_path(relative.as_posix(), field="repository path")


def _normalize_root(root: str, *, field: str) -> str:
    if root == ".":
        return root
    return validate_repository_relative_path(root, field=field)


def _normalize_repo_agent_id(value: str) -> str:
    normalized = "-".join(part for part in "".join(ch.lower() if ch.isalnum() or ch in "._-" else "-" for ch in value).strip("-._").split("-") if part)
    if not normalized:
        raise BootstrapConfigError("repoAgentId cannot be empty after normalization")
    return normalized


def _derived_repo_agent_id(root: str) -> str:
    if root == ".":
        return "root"
    posix = PurePosixPath(root)
    name = posix.name or root.replace("/", "-")
    return _normalize_repo_agent_id(f"{name}-{root.replace('/', '-')}")


def _safe_lstat(path: Path) -> stat.result:
    try:
        return path.lstat()
    except OSError as exc:
        raise BootstrapConfigError(f"unable to stat repository path: {path}") from exc


def _validate_root_path(repository_root: Path, relative_root: str) -> Path:
    if relative_root == ".":
        return repository_root
    target = (repository_root / PurePosixPath(relative_root)).resolve()
    try:
        target.relative_to(repository_root)
    except ValueError as exc:
        raise BootstrapConfigError(f"repository root resolves outside repository root: {relative_root!r}") from exc
    if not target.exists():
        raise BootstrapConfigError(f"repository root does not exist: {relative_root!r}")
    lst = _safe_lstat(target)
    if stat.S_ISLNK(lst.st_mode):
        raise BootstrapConfigError(f"repository root must not be a symlink: {relative_root!r}")
    if not stat.S_ISDIR(lst.st_mode):
        raise BootstrapConfigError(f"repository root must be a directory: {relative_root!r}")
    return target


def _resolve_repository_relative(repository_root: Path, value: str, *, field: str, allow_dot: bool = False) -> str:
    if allow_dot and value == ".":
        return "."
    try:
        relative = validate_repository_relative_path(value, field=field)
    except Exception as exc:
        raise BootstrapConfigError(f"{field} resolves outside repository root: {value!r}") from exc
    _validate_root_path(repository_root, relative)
    return relative


def _is_allowed_relative(relative: PurePosixPath, *, allow_all_top_level: bool) -> bool:
    parts = relative.parts
    if not parts:
        return False
    if any(part.casefold() in _BLOCKED_SEGMENTS for part in parts):
        return False
    name = relative.name.casefold()
    if name in _BLOCKED_EXACT_NAMES or name.startswith(".env"):
        return False
    top = parts[0]
    return allow_all_top_level or top in _ALLOWED_TOP_LEVEL_DIRS or top.startswith(".")


@dataclass
class _WalkBudget:
    files_seen: int = 0
    bytes_seen: int = 0


def _iter_repository_files(repository_root: Path, *, root_relative: str = ".", allow_all_top_level: bool = False) -> Iterable[tuple[PurePosixPath, Path]]:
    base = _validate_root_path(repository_root, root_relative)
    budget = _WalkBudget()
    pending: list[tuple[Path, int]] = [(base, 0)]
    while pending:
        current, depth = pending.pop()
        if depth > _MAX_DEPTH:
            raise BootstrapConfigError(f"repository scan exceeded max depth under {root_relative!r}")
        for child in sorted(current.iterdir(), key=lambda item: item.name.casefold(), reverse=True):
            lst = _safe_lstat(child)
            relative = PurePosixPath(_repo_rel(child, repository_root))
            if stat.S_ISLNK(lst.st_mode):
                raise BootstrapConfigError(f"repository contains symlinked path: {relative.as_posix()}")
            if not _is_allowed_relative(relative, allow_all_top_level=allow_all_top_level):
                continue
            if stat.S_ISDIR(lst.st_mode):
                pending.append((child, depth + 1))
                continue
            if not stat.S_ISREG(lst.st_mode):
                raise BootstrapConfigError(f"repository contains unsupported special file: {relative.as_posix()}")
            budget.files_seen += 1
            budget.bytes_seen += lst.st_size
            if budget.files_seen > _MAX_FILES:
                raise BootstrapConfigError("repository scan exceeded max file count")
            if budget.bytes_seen > _MAX_AGGREGATE_BYTES:
                raise BootstrapConfigError("repository scan exceeded aggregate byte budget")
            yield relative, child


def _read_text_file(path: Path, *, max_bytes: int = _MAX_TEXT_BYTES) -> str:
    size = path.stat().st_size
    if size > max_bytes:
        return ""
    if path.suffix.casefold() not in _TEXT_SUFFIXES and path.name not in _PACKAGE_FILE_NAMES:
        return ""
    return path.read_text(encoding="utf-8", errors="ignore")


def _hash_file(relative: PurePosixPath, path: Path) -> dict[str, str]:
    size = path.stat().st_size
    if size > _MAX_HASH_BYTES:
        raise BootstrapConfigError(f"fingerprint input exceeds size limit: {relative.as_posix()}")
    return {"path": relative.as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _validate_sha256(value: str | None, *, field: str) -> str | None:
    if value is None:
        return None
    if len(value) != _SHA256_LEN or any(ch not in "0123456789abcdef" for ch in value):
        raise BootstrapConfigError(f"{field} must be a lowercase sha256 hex digest")
    return value


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

    expected_project_endpoint: str | None = None
    expected_agent_name: str | None = None
    expected_version: str | None = None
    observed_project_endpoint: str | None = None
    observed_agent_name: str | None = None
    observed_version: str | None = None
    observed_source_fingerprint: str | None = None
    observed_package_fingerprint: str | None = None


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
        return _normalize_root(value, field="repository path")

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
    evidence: list[DiscoveryEvidence] = field(default_factory=list)


def _merge_evidence(existing: Sequence[DiscoveryEvidence], incoming: Sequence[DiscoveryEvidence]) -> list[DiscoveryEvidence]:
    merged: dict[tuple[str, str, str], DiscoveryEvidence] = {}
    seen_paths: dict[str, str] = {}
    for item in [*existing, *incoming]:
        path_key = item.path.casefold()
        previous_path = seen_paths.get(path_key)
        if previous_path is not None and previous_path != item.path:
            raise BootstrapConfigError(f"case-fold-colliding evidence paths: {previous_path!r} and {item.path!r}")
        seen_paths[path_key] = item.path
        merged[(item.kind, item.path.casefold(), item.detail.casefold())] = item
    return sorted(merged.values(), key=lambda item: (item.kind, item.path, item.detail))


def _check_casefold_path_collision(paths: Iterable[str]) -> None:
    seen: dict[str, str] = {}
    for path in paths:
        key = path.casefold()
        previous = seen.get(key)
        if previous is not None and previous != path:
            raise BootstrapConfigError(f"case-fold-colliding evidence paths: {previous!r} and {path!r}")
        seen[key] = path


def _discover_metadata_candidate(relative: PurePosixPath, path: Path, repository_root: Path) -> _Candidate:
    payload = yaml.safe_load(_read_text_file(path, max_bytes=_MAX_METADATA_BYTES))
    if payload is None:
        payload = {}
    if not isinstance(payload, Mapping):
        raise BootstrapConfigError(f"metadata file must contain a mapping: {relative.as_posix()}")
    root_dir = path.parent.parent if path.parent.name == ".foundry" else path.parent
    canonical_root = _repo_rel(root_dir, repository_root)
    declared_source = str(payload.get("source_root", canonical_root))
    declared_package = str(payload.get("package_root", declared_source))
    return _Candidate(
        canonical_root=canonical_root,
        source_root=_resolve_repository_relative(repository_root, declared_source, field="source_root", allow_dot=True),
        package_root=_resolve_repository_relative(repository_root, declared_package, field="package_root", allow_dot=True),
        config_path=relative.as_posix(),
        evidence=[DiscoveryEvidence(kind="agent-metadata", path=relative.as_posix(), detail=path.name, confidence=0.95)],
    )


def _discover_runtime_candidate(relative: PurePosixPath, path: Path, repository_root: Path) -> _Candidate:
    root_path = path.parent
    for probe in path.parents:
        if probe == repository_root:
            break
        if (probe / ".foundry").exists():
            root_path = probe
            break
    else:
        parent = path.parent
        if parent.parent == repository_root and path.parent.name in {"app", "src", "service", "services", "agents", "apps", "packages"}:
            root_path = repository_root
    canonical_root = _repo_rel(root_path, repository_root)
    text = _read_text_file(path).casefold()
    frameworks = tuple(sorted({label for needle, label in _FRAMEWORK_MARKERS if needle in text}, key=str.casefold))
    handlers = tuple(sorted({marker for marker in _HANDLER_MARKERS if marker.casefold() in text}, key=str.casefold))
    evidence = [DiscoveryEvidence(kind="entrypoint", path=relative.as_posix(), detail=path.name, confidence=0.6)]
    evidence.extend(DiscoveryEvidence(kind="framework-import", path=relative.as_posix(), detail=item, confidence=0.4) for item in frameworks)
    evidence.extend(DiscoveryEvidence(kind="handler", path=relative.as_posix(), detail=item, confidence=0.4) for item in handlers)
    return _Candidate(canonical_root=canonical_root, source_root=canonical_root, package_root=canonical_root, evidence=evidence)


def _merge_candidates(existing: _Candidate, incoming: _Candidate) -> _Candidate:
    if existing.source_root.casefold() != incoming.source_root.casefold() or existing.package_root.casefold() != incoming.package_root.casefold():
        raise BootstrapConfigError(f"conflicting discovery roots for {existing.canonical_root!r}")
    if existing.config_path and incoming.config_path and existing.config_path.casefold() != incoming.config_path.casefold():
        raise BootstrapConfigError(f"conflicting config paths for {existing.canonical_root!r}")
    return _Candidate(
        canonical_root=existing.canonical_root,
        source_root=existing.source_root,
        package_root=existing.package_root,
        config_path=existing.config_path or incoming.config_path,
        evidence=_merge_evidence(existing.evidence, incoming.evidence),
    )


def _collect_candidates(repository_root: Path) -> dict[str, _Candidate]:
    discovered: dict[str, _Candidate] = {}
    scanned_paths: list[str] = []
    for relative, path in _iter_repository_files(repository_root, allow_all_top_level=False):
        scanned_paths.append(relative.as_posix())
        candidate: _Candidate | None = None
        if path.parent.name == ".foundry" and path.suffix.casefold() in {".yaml", ".yml"} and path.name.casefold().startswith("agent-metadata"):
            candidate = _discover_metadata_candidate(relative, path, repository_root)
        elif path.name == "azure.yaml":
            continue
        elif path.name in _ENTRYPOINT_FILE_NAMES and relative.parts[0] in _ALLOWED_TOP_LEVEL_DIRS:
            candidate = _discover_runtime_candidate(relative, path, repository_root)
            if any(existing.source_root.casefold() == candidate.source_root.casefold() for existing in discovered.values()):
                continue
            if candidate.canonical_root == "." and any(existing.canonical_root == "." for existing in discovered.values()):
                continue
        if candidate is None:
            continue
        key = candidate.canonical_root.casefold()
        if key in discovered and discovered[key].canonical_root != candidate.canonical_root:
            raise BootstrapConfigError(f"case-fold-colliding discovery roots: {discovered[key].canonical_root!r} and {candidate.canonical_root!r}")
        discovered[key] = _merge_candidates(discovered[key], candidate) if key in discovered else candidate
    if not discovered:
        return {}
    _check_casefold_path_collision(scanned_paths)
    return dict(sorted(discovered.items(), key=lambda item: (item[1].canonical_root.casefold(), item[1].config_path or "")))


def _runtime_facts_for_root(repository_root: Path, relative_root: str) -> RuntimeFacts:
    entrypoints: set[str] = set()
    frameworks: set[str] = set()
    handlers: set[str] = set()
    for relative, path in _iter_repository_files(repository_root, root_relative=relative_root, allow_all_top_level=True):
        if path.name in _ENTRYPOINT_FILE_NAMES:
            entrypoints.add(relative.as_posix())
        text = _read_text_file(path).casefold()
        frameworks.update(label for needle, label in _FRAMEWORK_MARKERS if needle in text)
        handlers.update(marker for marker in _HANDLER_MARKERS if marker.casefold() in text)
    return RuntimeFacts(
        entrypoints=tuple(sorted(entrypoints, key=str.casefold)),
        frameworks=tuple(sorted(frameworks, key=str.casefold)),
        handlers=tuple(sorted(handlers, key=str.casefold)),
    )


def _merge_runtime_facts(base: RuntimeFacts, extra: RuntimeFacts) -> RuntimeFacts:
    return RuntimeFacts(
        entrypoints=tuple(sorted(set(base.entrypoints) | set(extra.entrypoints), key=str.casefold)),
        frameworks=tuple(sorted(set(base.frameworks) | set(extra.frameworks), key=str.casefold)),
        handlers=tuple(sorted(set(base.handlers) | set(extra.handlers), key=str.casefold)),
    )


def _fingerprint_root(repository_root: Path, relative_root: str) -> str:
    payload = [_hash_file(relative, path) for relative, path in _iter_repository_files(repository_root, root_relative=relative_root, allow_all_top_level=True)]
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _workflow_evidence(repository_root: Path) -> list[DiscoveryEvidence]:
    workflows_root = repository_root / ".github" / "workflows"
    if not workflows_root.exists():
        return []
    evidence: list[DiscoveryEvidence] = []
    for relative, path in _iter_repository_files(repository_root, root_relative=".github/workflows", allow_all_top_level=True):
        text = _read_text_file(path)
        if "foundry" in text.casefold() or "deploy" in text.casefold():
            evidence.append(DiscoveryEvidence(kind="workflow", path=relative.as_posix(), detail="foundry/deploy workflow", confidence=0.2))
    return evidence


def _instruction_evidence(repository_root: Path) -> list[DiscoveryEvidence]:
    evidence: list[DiscoveryEvidence] = []
    for name in sorted(_INSTRUCTION_FILE_NAMES):
        path = repository_root / name
        if path.exists():
            evidence.append(DiscoveryEvidence(kind="instructions", path=_repo_rel(path, repository_root), detail=name, confidence=0.15))
    return evidence


def _normalize_selection(selected_agents: Sequence[Mapping[str, str] | str] | None) -> tuple[dict[str, str], bool]:
    if selected_agents is None:
        return {}, False
    selection: dict[str, str] = {}
    seen_ids: dict[str, str] = {}
    for item in selected_agents:
        if isinstance(item, str):
            if item == ".":
                raise BootstrapConfigError("string selection for '.' requires explicit repoAgentId")
            root = validate_repository_relative_path(item, field="selected root")
            repo_agent_id = _derived_repo_agent_id(root)
        else:
            raw_root = item.get("root")
            if not isinstance(raw_root, str):
                raise BootstrapConfigError("selected_agents entries must include root")
            root = _normalize_root(raw_root, field="selected root")
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


def _normalize_binding_evidence(payload: Mapping[str, str | None] | None, *, root: str) -> BindingEvidence:
    if payload is None:
        return BindingEvidence()
    return BindingEvidence(
        observed_project_endpoint=payload.get("project_endpoint"),
        observed_agent_name=payload.get("agent_name"),
        observed_version=payload.get("expected_version") or payload.get("version"),
        observed_source_fingerprint=_validate_sha256(payload.get("source_fingerprint"), field=f"binding_evidence_by_root[{root!r}].source_fingerprint"),
        observed_package_fingerprint=_validate_sha256(payload.get("package_fingerprint"), field=f"binding_evidence_by_root[{root!r}].package_fingerprint"),
    )


def _assess_binding(
    repo_agent_id: str,
    runtime_facts: RuntimeFacts,
    expected: BindingEvidence,
    observed: BindingEvidence,
    local_source_fingerprint: str,
    local_package_fingerprint: str,
) -> tuple[BindingAssessment, tuple[DiscoveryBlocker, ...]]:
    if expected.expected_project_endpoint or expected.expected_agent_name or expected.expected_version:
        if not runtime_facts.entrypoints:
            blockers = (DiscoveryBlocker(code="missing-entrypoint", detail="binding evidence exists but no supported entrypoint file was found"),)
            return BindingAssessment(agent_id=repo_agent_id, classification="bound-diverged", detail=blockers[0].detail), blockers
        mismatches: list[str] = []
        if observed.observed_project_endpoint is None or observed.observed_project_endpoint != expected.expected_project_endpoint:
            mismatches.append("project-endpoint")
        if observed.observed_agent_name is None or observed.observed_agent_name != expected.expected_agent_name:
            mismatches.append("agent-name")
        if observed.observed_version is None or observed.observed_version != expected.expected_version:
            mismatches.append("version")
        if observed.observed_source_fingerprint is None or observed.observed_source_fingerprint != local_source_fingerprint:
            mismatches.append("source-fingerprint")
        if observed.observed_package_fingerprint is None or observed.observed_package_fingerprint != local_package_fingerprint:
            mismatches.append("package-fingerprint")
        if not mismatches:
            return BindingAssessment(agent_id=repo_agent_id, classification="bound-aligned", detail="expected and observed binding evidence exactly match local fingerprints"), ()
        if observed.observed_project_endpoint or observed.observed_agent_name or observed.observed_version or observed.observed_source_fingerprint or observed.observed_package_fingerprint:
            return BindingAssessment(agent_id=repo_agent_id, classification="bound-diverged", detail=f"binding mismatch: {', '.join(mismatches)}"), ()
        return BindingAssessment(agent_id=repo_agent_id, classification="bound-unknown", detail="expected binding exists without observed evidence"), ()
    if runtime_facts.ready:
        return BindingAssessment(agent_id=repo_agent_id, classification="ready-unbound", detail="runtime readiness found without binding evidence"), ()
    blockers = []
    if not runtime_facts.entrypoints:
        blockers.append(DiscoveryBlocker(code="missing-entrypoint", detail="no supported entrypoint file was found"))
    if runtime_facts.entrypoints and not (runtime_facts.frameworks or runtime_facts.handlers):
        blockers.append(DiscoveryBlocker(code="missing-runtime-facts", detail="entrypoint exists but no framework import or handler was found"))
    return BindingAssessment(agent_id=repo_agent_id, classification="not-ready", detail=blockers[0].detail if blockers else "runtime is not ready"), tuple(blockers)


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
    approved_map = {
        _normalize_root(key, field="approved shared source root").casefold(): tuple(sorted({_normalize_root(value, field="approved shared source root") for value in values}, key=str.casefold))
        for key, values in (approved_shared_sources or {}).items()
    }
    agent_ids_by_root: dict[str, str] = {}
    for candidate in discovered.values():
        root_key = candidate.canonical_root.casefold()
        agent_ids_by_root[root_key] = selected_map[root_key] if explicit_selection and root_key in selected_map else _derived_repo_agent_id(candidate.canonical_root)
    reverse_ids: dict[str, str] = {}
    for root_key, agent_id in agent_ids_by_root.items():
        previous = reverse_ids.get(agent_id.casefold())
        if previous is not None and previous != root_key:
            raise BootstrapConfigError(f"derived repoAgentId collision requires explicit IDs: {agent_id!r}")
        reverse_ids[agent_id.casefold()] = root_key
    results: list[DiscoveredAgent] = []
    source_roots: list[tuple[str, str, PurePosixPath]] = []
    for candidate in sorted(discovered.values(), key=lambda item: (item.canonical_root.casefold(), item.config_path or "")):
        root_key = candidate.canonical_root.casefold()
        if explicit_selection and root_key not in selected_map:
            continue
        repo_agent_id = agent_ids_by_root[root_key]
        runtime_facts = _merge_runtime_facts(_runtime_facts_for_root(root, candidate.source_root), _runtime_facts_for_root(root, candidate.canonical_root))
        source_fingerprint = _fingerprint_root(root, candidate.source_root)
        package_fingerprint = _fingerprint_root(root, candidate.package_root)
        metadata_text = _read_text_file(root / PurePosixPath(candidate.config_path), max_bytes=_MAX_METADATA_BYTES) if candidate.config_path else ""
        metadata = yaml.safe_load(metadata_text) if metadata_text else {}
        expected = BindingEvidence(
            expected_project_endpoint=str(metadata.get("project_endpoint")) if isinstance(metadata, Mapping) and metadata.get("project_endpoint") is not None else None,
            expected_agent_name=str(metadata.get("agent_name")) if isinstance(metadata, Mapping) and metadata.get("agent_name") is not None else None,
            expected_version=str(metadata.get("expected_version")) if isinstance(metadata, Mapping) and metadata.get("expected_version") is not None else None,
        )
        observed = _normalize_binding_evidence(binding_evidence_by_root.get(candidate.canonical_root) if binding_evidence_by_root else None, root=candidate.canonical_root)
        assessment, blockers = _assess_binding(repo_agent_id, runtime_facts, expected, observed, source_fingerprint, package_fingerprint)
        evidence = tuple(_merge_evidence(candidate.evidence, [*workflow_evidence, *instruction_evidence]))
        confidence = round(min(1.0, sum(item.confidence for item in evidence) / max(len(evidence), 1)), 4)
        source_roots.append((candidate.canonical_root, repo_agent_id, PurePosixPath(candidate.source_root)))
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
        overlaps: list[tuple[str, str]] = []
        agent_root = PurePosixPath(agent.sourceRoot)
        for other_root, other_id, other_source_root in source_roots:
            if other_root == agent.root:
                continue
            if agent_root == other_source_root or agent_root in other_source_root.parents or other_source_root in agent_root.parents:
                overlaps.append((other_root, other_id))
        approved_roots = {_normalize_root(item, field="approved shared source root").casefold() for item in approved_map.get(agent.root.casefold(), ())}
        unapproved = sorted(other_id for other_root, other_id in overlaps if other_root.casefold() not in approved_roots)
        if unapproved:
            blockers = tuple(sorted(agent.blockers + (DiscoveryBlocker(code="unapproved-shared-source", detail=f"shared source overlap requires explicit approval: {', '.join(unapproved)}"),), key=lambda item: (item.code, item.detail)))
            final_results.append(agent.model_copy(update={"blockers": blockers, "approvedSharedSourceRepoAgentIds": tuple(), "bindingAssessment": agent.bindingAssessment.model_copy(update={"classification": "not-ready", "detail": blockers[0].detail})}))
            continue
        approved_ids = tuple(sorted({other_id for other_root, other_id in overlaps if other_root.casefold() in approved_roots}, key=str.casefold))
        final_results.append(agent.model_copy(update={"approvedSharedSourceRepoAgentIds": approved_ids}))
    return DiscoveryResult(repositoryRoot=".", agents=tuple(final_results))


def discovery_result_json(result: DiscoveryResult) -> str:
    return json.dumps(result.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


__all__ = ["DiscoveryBlocker", "DiscoveredAgent", "DiscoveryEvidence", "DiscoveryResult", "discover_repository_agents", "discovery_result_json"]
