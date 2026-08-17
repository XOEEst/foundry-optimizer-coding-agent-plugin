from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
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
_ALLOWED_TOP_LEVEL_DIRS = frozenset({".foundry", ".github", "agents", "app", "apps", "services", "service", "src", "skills", "tests", "packages", "shared"})
_BLOCKED_EXACT_NAMES = frozenset({".env", ".env.local", ".env.development", ".env.production", "credentials.json", "secrets.json", "trace.json", "trace.ndjson", "trace.jsonl", "dataset.csv", "dataset.json", "dataset.jsonl", "dataset.parquet", "prompt.txt", "prompts.txt"})
_BLOCKED_SEGMENTS = frozenset({".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".venv", "venv", "node_modules", "datasets", "traces", "prompts", "secrets"})
_FRAMEWORK_MARKERS: tuple[tuple[str, str], ...] = (("fastapi", "fastapi"), ("flask", "flask"), ("@azure/functions", "azure-functions"), ("azure.functions", "azure-functions"), ("express", "express"), ("microsoft.semantickernel", "semantic-kernel"))
_HANDLER_MARKERS: tuple[str, ...] = ("responses.create", "invoke(", "handler(", "app.route", "MapPost(", "Function(")
_MAX_TEXT_BYTES = 64 * 1024
_MAX_HASH_BYTES = 2 * 1024 * 1024
_MAX_METADATA_BYTES = 16 * 1024
_MAX_DEPTH = 8
_MAX_ENTRIES = 5000
_MAX_FILES = 2000
_MAX_AGGREGATE_BYTES = 16 * 1024 * 1024
_SHA256_LEN = 64


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
    return _normalize_repo_agent_id(f"{posix.name or 'root'}-{root.replace('/', '-')}")


def _repo_rel(path: Path, repository_root: Path) -> str:
    try:
        relative = path.relative_to(repository_root)
    except ValueError as exc:
        raise BootstrapConfigError(f"path is outside repository root: {path}") from exc
    if relative == Path("."):
        return "."
    return validate_repository_relative_path(relative.as_posix(), field="repository path")


def _safe_lstat(path: Path) -> os.stat_result:
    try:
        return path.lstat()
    except OSError as exc:
        raise BootstrapConfigError(f"unable to stat repository path: {path}") from exc


def _assert_no_links_in_path(repository_root: Path, relative_root: str) -> Path:
    current = repository_root
    if relative_root == ".":
        return current
    for segment in PurePosixPath(relative_root).parts:
        current = current / segment
        lst = _safe_lstat(current)
        if stat.S_ISLNK(lst.st_mode) or current.is_junction():
            raise BootstrapConfigError(f"repository path component must not be a symlink or junction: {relative_root!r}")
    return current


def _normalize_real_path(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _resolve_repository_relative(repository_root: Path, value: str, *, field: str, allow_dot: bool = False) -> str:
    if allow_dot and value == ".":
        return "."
    try:
        relative = validate_repository_relative_path(value, field=field)
    except Exception as exc:
        raise BootstrapConfigError(f"{field} resolves outside repository root: {value!r}") from exc
    target = _assert_no_links_in_path(repository_root, relative)
    resolved = target.resolve()
    root_resolved = repository_root.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise BootstrapConfigError(f"{field} resolves outside repository root: {value!r}") from exc
    if not target.exists():
        raise BootstrapConfigError(f"{field} does not exist: {value!r}")
    if not target.is_dir():
        raise BootstrapConfigError(f"{field} must resolve to a directory: {value!r}")
    if not _is_allowed_relative(PurePosixPath(relative)):
        raise BootstrapConfigError(f"{field} scan root is blocked: {value!r}")
    return relative


def _is_allowed_relative(relative: PurePosixPath) -> bool:
    parts = relative.parts
    if not parts:
        return False
    if any(part.casefold() in _BLOCKED_SEGMENTS for part in parts):
        return False
    if relative.name.casefold() in _BLOCKED_EXACT_NAMES or relative.name.casefold().startswith(".env"):
        return False
    return parts[0] in _ALLOWED_TOP_LEVEL_DIRS


@dataclass
class _Budget:
    entries: int = 0
    files: int = 0
    bytes: int = 0


@dataclass(frozen=True)
class _ScannedFile:
    relative: str
    size: int
    sha256: str | None
    text: str | None


class _ScanCache:
    def __init__(self, repository_root: Path) -> None:
        self.repository_root = repository_root
        self.files_by_root: dict[str, tuple[_ScannedFile, ...]] = {}
        self.fingerprint_by_root: dict[str, str] = {}
        self.runtime_by_root: dict[str, RuntimeFacts] = {}
        self.budget = _Budget()
        self.casefold_paths: dict[tuple[str, ...], str] = {}

    def scan_root(self, root: str) -> tuple[_ScannedFile, ...]:
        normalized = _normalize_root(root, field="scan root")
        cached = self.files_by_root.get(normalized)
        if cached is not None:
            return cached
        if normalized != "." and "." in self.files_by_root:
            prefix = f"{normalized}/"
            derived = tuple(
                item
                for item in self.files_by_root["."]
                if item.relative.startswith(prefix)
            )
            self.files_by_root[normalized] = derived
            return derived
        base = self.repository_root if normalized == "." else _assert_no_links_in_path(self.repository_root, normalized)
        out: list[_ScannedFile] = []
        pending: list[tuple[Path, int]] = [(base, 0)]
        while pending:
            current, depth = pending.pop()
            if depth > _MAX_DEPTH:
                raise BootstrapConfigError(f"repository scan exceeded max depth under {normalized!r}")
            for child in sorted(current.iterdir(), key=lambda item: (item.name.casefold(), item.name), reverse=True):
                self.budget.entries += 1
                if self.budget.entries > _MAX_ENTRIES:
                    raise BootstrapConfigError("repository scan exceeded max entry count")
                relative = PurePosixPath(_repo_rel(child, self.repository_root))
                if not _is_allowed_relative(relative):
                    continue
                collision_key = tuple(part.casefold() for part in relative.parts)
                previous = self.casefold_paths.get(collision_key)
                if previous is not None and previous != relative.as_posix():
                    raise BootstrapConfigError(
                        "case-fold-colliding repository paths: "
                        f"{previous!r} and {relative.as_posix()!r}"
                    )
                self.casefold_paths[collision_key] = relative.as_posix()
                lst = _safe_lstat(child)
                if stat.S_ISLNK(lst.st_mode) or child.is_junction():
                    raise BootstrapConfigError(f"repository contains symlinked path: {relative.as_posix()}")
                if stat.S_ISDIR(lst.st_mode):
                    pending.append((child, depth + 1))
                    continue
                if not stat.S_ISREG(lst.st_mode):
                    raise BootstrapConfigError(f"repository contains unsupported special file: {relative.as_posix()}")
                self.budget.files += 1
                self.budget.bytes += lst.st_size
                if self.budget.files > _MAX_FILES:
                    raise BootstrapConfigError("repository scan exceeded max file count")
                if self.budget.bytes > _MAX_AGGREGATE_BYTES:
                    raise BootstrapConfigError("repository scan exceeded aggregate byte budget")
                sha256 = None
                if lst.st_size <= _MAX_HASH_BYTES:
                    sha256 = hashlib.sha256(child.read_bytes()).hexdigest()
                text = None
                if lst.st_size <= _MAX_TEXT_BYTES and (child.suffix.casefold() in _TEXT_SUFFIXES or child.name in _PACKAGE_FILE_NAMES):
                    text = child.read_text(encoding="utf-8", errors="ignore")
                out.append(_ScannedFile(relative=relative.as_posix(), size=lst.st_size, sha256=sha256, text=text))
        result = tuple(sorted(out, key=lambda item: (PurePosixPath(item.relative).parts, item.relative.casefold(), item.relative)))
        self.files_by_root[normalized] = result
        return result


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


@dataclass(frozen=True)
class _Candidate:
    canonical_root: str
    source_root: str
    package_root: str
    config_path: str | None
    evidence: tuple[DiscoveryEvidence, ...]


def _merge_evidence(existing: Sequence[DiscoveryEvidence], incoming: Sequence[DiscoveryEvidence]) -> tuple[DiscoveryEvidence, ...]:
    merged: dict[tuple[str, str, str], DiscoveryEvidence] = {}
    seen: dict[tuple[str, ...], str] = {}
    for item in [*existing, *incoming]:
        segments = tuple(segment.casefold() for segment in PurePosixPath(item.path).parts)
        previous = seen.get(segments)
        if previous is not None and previous != item.path:
            raise BootstrapConfigError(f"case-fold-colliding evidence paths: {previous!r} and {item.path!r}")
        seen[segments] = item.path
        merged[(item.kind, item.path.casefold(), item.detail.casefold())] = item
    return tuple(sorted(merged.values(), key=lambda item: (tuple(part.casefold() for part in PurePosixPath(item.path).parts), item.kind, item.detail.casefold(), item.path, item.detail)))


def _validate_sha256(value: str | None, *, field: str) -> str | None:
    if value is None:
        return None
    if len(value) != _SHA256_LEN or any(ch not in "0123456789abcdef" for ch in value):
        raise BootstrapConfigError(f"{field} must be a lowercase sha256 hex digest")
    return value


def _runtime_facts_for_root(cache: _ScanCache, root: str) -> RuntimeFacts:
    cached = cache.runtime_by_root.get(root)
    if cached is not None:
        return cached
    entrypoints: set[str] = set()
    frameworks: set[str] = set()
    handlers: set[str] = set()
    for file in cache.scan_root(root):
        name = PurePosixPath(file.relative).name
        if name in _ENTRYPOINT_FILE_NAMES:
            entrypoints.add(file.relative)
        text = (file.text or "").casefold()
        frameworks.update(label for needle, label in _FRAMEWORK_MARKERS if needle in text)
        handlers.update(marker for marker in _HANDLER_MARKERS if marker.casefold() in text)
    facts = RuntimeFacts(
        entrypoints=tuple(sorted(entrypoints, key=lambda item: (tuple(part.casefold() for part in PurePosixPath(item).parts), item.casefold(), item))),
        frameworks=tuple(sorted(frameworks, key=str.casefold)),
        handlers=tuple(sorted(handlers, key=str.casefold)),
    )
    cache.runtime_by_root[root] = facts
    return facts


def _fingerprint_root(cache: _ScanCache, root: str) -> str:
    cached = cache.fingerprint_by_root.get(root)
    if cached is not None:
        return cached
    payload = []
    for file in cache.scan_root(root):
        if file.sha256 is None:
            raise BootstrapConfigError(f"fingerprint input exceeds size limit: {file.relative}")
        payload.append({"path": file.relative, "sha256": file.sha256})
    fingerprint = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    cache.fingerprint_by_root[root] = fingerprint
    return fingerprint


def _discover_runtime_candidate(relative: str, text: str, repository_root: Path) -> _Candidate:
    rel = PurePosixPath(relative)
    root_path = repository_root / rel.parent
    found_metadata_ancestor = False
    for probe in root_path.parents:
        if probe == repository_root:
            break
        if (probe / ".foundry").exists():
            root_path = probe
            found_metadata_ancestor = True
            break
    if not found_metadata_ancestor and rel.parent.parent == PurePosixPath(".") and rel.parent.name in {"app", "src", "service", "services", "agents", "apps", "packages"}:
        root_path = repository_root
    canonical_root = _repo_rel(root_path, repository_root)
    lowered = text.casefold()
    frameworks = tuple(sorted({label for needle, label in _FRAMEWORK_MARKERS if needle in lowered}, key=str.casefold))
    handlers = tuple(sorted({marker for marker in _HANDLER_MARKERS if marker.casefold() in lowered}, key=str.casefold))
    evidence = [DiscoveryEvidence(kind="entrypoint", path=relative, detail=rel.name, confidence=0.6)]
    evidence.extend(DiscoveryEvidence(kind="framework-import", path=relative, detail=item, confidence=0.4) for item in frameworks)
    evidence.extend(DiscoveryEvidence(kind="handler", path=relative, detail=item, confidence=0.4) for item in handlers)
    return _Candidate(canonical_root=canonical_root, source_root=canonical_root, package_root=canonical_root, config_path=None, evidence=_merge_evidence((), evidence))


def _discover_metadata_candidate(relative: str, text: str, repository_root: Path) -> _Candidate:
    if len(text.encode("utf-8")) > _MAX_METADATA_BYTES:
        raise BootstrapConfigError(f"metadata file exceeds byte budget: {relative}")
    payload = yaml.safe_load(text)
    if payload is None:
        payload = {}
    if not isinstance(payload, Mapping):
        raise BootstrapConfigError(f"metadata file must contain a mapping: {relative}")
    path = PurePosixPath(relative)
    canonical_root = str(path.parent.parent) if path.parent.name == ".foundry" else str(path.parent)
    if canonical_root == "":
        canonical_root = "."
    return _Candidate(
        canonical_root=canonical_root,
        source_root=_resolve_repository_relative(repository_root, str(payload.get("source_root", canonical_root)), field="source_root", allow_dot=True),
        package_root=_resolve_repository_relative(repository_root, str(payload.get("package_root", payload.get("source_root", canonical_root))), field="package_root", allow_dot=True),
        config_path=relative,
        evidence=(DiscoveryEvidence(kind="agent-metadata", path=relative, detail=path.name, confidence=0.95),),
    )


def _collect_candidates(cache: _ScanCache) -> dict[str, _Candidate]:
    discovered: dict[str, _Candidate] = {}
    for file in cache.scan_root("."):
        path = PurePosixPath(file.relative)
        candidate: _Candidate | None = None
        if path.parent.name == ".foundry" and path.suffix.casefold() in {".yaml", ".yml"} and path.name.casefold().startswith("agent-metadata"):
            candidate = _discover_metadata_candidate(file.relative, file.text or "", cache.repository_root)
        elif path.name == "azure.yaml":
            continue
        elif path.name in _ENTRYPOINT_FILE_NAMES and path.parts[0] in _ALLOWED_TOP_LEVEL_DIRS:
            candidate = _discover_runtime_candidate(file.relative, file.text or "", cache.repository_root)
            if any(existing.source_root.casefold() == candidate.canonical_root.casefold() for existing in discovered.values()):
                continue
        if candidate is None:
            continue
        key = candidate.canonical_root.casefold()
        previous = discovered.get(key)
        if previous is None:
            discovered[key] = candidate
            continue
        if previous.canonical_root != candidate.canonical_root:
            raise BootstrapConfigError(f"case-fold-colliding discovery roots: {previous.canonical_root!r} and {candidate.canonical_root!r}")
        if candidate.config_path is None and previous.config_path is not None:
            discovered[key] = _Candidate(
                canonical_root=previous.canonical_root,
                source_root=previous.source_root,
                package_root=previous.package_root,
                config_path=previous.config_path,
                evidence=_merge_evidence(previous.evidence, candidate.evidence),
            )
            continue
        if previous.source_root.casefold() != candidate.source_root.casefold() or previous.package_root.casefold() != candidate.package_root.casefold():
            raise BootstrapConfigError(f"conflicting discovery roots for {candidate.canonical_root!r}")
        discovered[key] = _Candidate(
            canonical_root=previous.canonical_root,
            source_root=previous.source_root,
            package_root=previous.package_root,
            config_path=previous.config_path or candidate.config_path,
            evidence=_merge_evidence(previous.evidence, candidate.evidence),
        )
    return dict(sorted(discovered.items(), key=lambda item: (tuple(part.casefold() for part in PurePosixPath(item[1].canonical_root).parts), item[1].canonical_root.casefold(), item[1].canonical_root, item[1].config_path or "")))


def _workflow_evidence(cache: _ScanCache) -> tuple[DiscoveryEvidence, ...]:
    if ".github" not in _ALLOWED_TOP_LEVEL_DIRS:
        return ()
    try:
        files = cache.scan_root(".github/workflows")
    except BootstrapConfigError:
        return ()
    out = []
    for file in files:
        text = file.text or ""
        if "foundry" in text.casefold() or "deploy" in text.casefold():
            out.append(DiscoveryEvidence(kind="workflow", path=file.relative, detail="foundry/deploy workflow", confidence=0.2))
    return _merge_evidence((), out)


def _instruction_evidence(cache: _ScanCache) -> tuple[DiscoveryEvidence, ...]:
    out = []
    for file in cache.scan_root("."):
        if PurePosixPath(file.relative).name in _INSTRUCTION_FILE_NAMES and len(PurePosixPath(file.relative).parts) == 1:
            out.append(DiscoveryEvidence(kind="instructions", path=file.relative, detail=PurePosixPath(file.relative).name, confidence=0.15))
    return _merge_evidence((), out)


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
        if root.casefold() in selection:
            raise BootstrapConfigError(f"duplicate selected root: {root!r}")
        if repo_agent_id.casefold() in seen_ids:
            raise BootstrapConfigError(f"duplicate selected repoAgentId: {repo_agent_id!r}")
        selection[root.casefold()] = repo_agent_id
        seen_ids[repo_agent_id.casefold()] = root
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


def _assess_binding(repo_agent_id: str, runtime_facts: RuntimeFacts, expected: BindingEvidence, observed: BindingEvidence, local_source_fingerprint: str, local_package_fingerprint: str) -> tuple[BindingAssessment, tuple[DiscoveryBlocker, ...]]:
    if expected.expected_project_endpoint or expected.expected_agent_name or expected.expected_version:
        if not runtime_facts.entrypoints:
            blockers = (DiscoveryBlocker(code="missing-entrypoint", detail="binding evidence exists but no supported entrypoint file was found"),)
            return BindingAssessment(agent_id=repo_agent_id, classification="bound-diverged", detail=blockers[0].detail), blockers
        mismatches: list[str] = []
        if expected.expected_project_endpoint is not None and observed.observed_project_endpoint != expected.expected_project_endpoint:
            mismatches.append("project-endpoint")
        if expected.expected_agent_name is not None and observed.observed_agent_name != expected.expected_agent_name:
            mismatches.append("agent-name")
        if expected.expected_version is not None and observed.observed_version != expected.expected_version:
            mismatches.append("version")
        if observed.observed_source_fingerprint != local_source_fingerprint:
            mismatches.append("source-fingerprint")
        if observed.observed_package_fingerprint != local_package_fingerprint:
            mismatches.append("package-fingerprint")
        if not mismatches:
            return BindingAssessment(agent_id=repo_agent_id, classification="bound-aligned", detail="expected and observed binding evidence exactly match local fingerprints"), ()
        if any((observed.observed_project_endpoint, observed.observed_agent_name, observed.observed_version, observed.observed_source_fingerprint, observed.observed_package_fingerprint)):
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
    cache = _ScanCache(root)
    discovered = _collect_candidates(cache)
    workflow_evidence = _workflow_evidence(cache)
    instruction_evidence = _instruction_evidence(cache)
    selected_map, explicit_selection = _normalize_selection(selected_agents)
    approved_map = {
        _normalize_root(key, field="approved shared source root").casefold(): tuple(sorted({_normalize_root(value, field="approved shared source root") for value in values}, key=lambda item: (tuple(part.casefold() for part in PurePosixPath(item).parts), item.casefold(), item)))
        for key, values in (approved_shared_sources or {}).items()
    }
    agent_ids_by_root: dict[str, str] = {}
    for candidate in discovered.values():
        root_key = candidate.canonical_root.casefold()
        agent_ids_by_root[root_key] = selected_map[root_key] if explicit_selection and root_key in selected_map else _derived_repo_agent_id(candidate.canonical_root)
    seen_ids: dict[str, str] = {}
    for root_key, agent_id in agent_ids_by_root.items():
        previous = seen_ids.get(agent_id.casefold())
        if previous is not None and previous != root_key:
            raise BootstrapConfigError(f"derived repoAgentId collision requires explicit IDs: {agent_id!r}")
        seen_ids[agent_id.casefold()] = root_key
    results: list[DiscoveredAgent] = []
    overlap_roots: list[tuple[str, str, str]] = []
    for candidate in discovered.values():
        root_key = candidate.canonical_root.casefold()
        if explicit_selection and root_key not in selected_map:
            continue
        repo_agent_id = agent_ids_by_root[root_key]
        runtime_facts = _runtime_facts_for_root(cache, candidate.source_root)
        source_fingerprint = _fingerprint_root(cache, candidate.source_root)
        package_fingerprint = _fingerprint_root(cache, candidate.package_root)
        metadata_text = next((file.text for file in cache.scan_root(".") if file.relative == candidate.config_path), "") if candidate.config_path else ""
        metadata = yaml.safe_load(metadata_text) if metadata_text else {}
        expected = BindingEvidence(
            expected_project_endpoint=str(metadata.get("project_endpoint")) if isinstance(metadata, Mapping) and metadata.get("project_endpoint") is not None else None,
            expected_agent_name=str(metadata.get("agent_name")) if isinstance(metadata, Mapping) and metadata.get("agent_name") is not None else None,
            expected_version=str(metadata.get("expected_version")) if isinstance(metadata, Mapping) and metadata.get("expected_version") is not None else None,
        )
        observed = _normalize_binding_evidence(binding_evidence_by_root.get(candidate.canonical_root) if binding_evidence_by_root else None, root=candidate.canonical_root)
        assessment, blockers = _assess_binding(repo_agent_id, runtime_facts, expected, observed, source_fingerprint, package_fingerprint)
        evidence = _merge_evidence(candidate.evidence, [*workflow_evidence, *instruction_evidence])
        confidence = round(min(1.0, sum(item.confidence for item in evidence) / max(len(evidence), 1)), 4)
        overlap_roots.append((candidate.canonical_root, repo_agent_id, _normalize_real_path(_assert_no_links_in_path(root, candidate.source_root if candidate.source_root != "." else "."))))
        results.append(DiscoveredAgent(repoAgentId=repo_agent_id, root=candidate.canonical_root, configPath=candidate.config_path, sourceRoot=candidate.source_root, packageRoot=candidate.package_root, evidence=evidence, confidence=confidence, packageFingerprint=package_fingerprint, sourceFingerprint=source_fingerprint, bindingAssessment=assessment, blockers=blockers))
    if explicit_selection:
        unmatched = sorted(set(selected_map) - {agent.root.casefold() for agent in results})
        if unmatched:
            raise BootstrapConfigError(f"selected roots were not discovered: {unmatched}")
    final_results: list[DiscoveredAgent] = []
    for agent in sorted(results, key=lambda item: (tuple(part.casefold() for part in PurePosixPath(item.root).parts), item.repoAgentId.casefold(), item.root, item.configPath or "")):
        agent_alias = _normalize_real_path(_assert_no_links_in_path(root, agent.sourceRoot if agent.sourceRoot != "." else "."))
        overlaps: list[tuple[str, str]] = []
        for other_root, other_id, other_alias in overlap_roots:
            if other_root == agent.root:
                continue
            if agent_alias == other_alias or agent_alias.startswith(other_alias + os.sep) or other_alias.startswith(agent_alias + os.sep):
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
