from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from foundry_opt.repository_contracts import DistributionSettings, RepositoryRegistry


_REGISTRY_PATH = ".foundry-opt/registry.yaml"
_COMMIT = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")


@dataclass(frozen=True, slots=True)
class RuntimeCheckout:
    repository: str
    commit: str
    package_root: Path
    uv_lock_sha256: str
    optimizer_skill_path: str


def uses_copilot_main_runtime(
    registry: RepositoryRegistry, *, environment: Mapping[str, str] | None = None,
) -> bool:
    env = os.environ if environment is None else environment
    return (
        registry.github.copilot_runtime == "main"
        and env.get("GITHUB_ACTIONS") == "true"
        and env.get("GITHUB_EVENT_NAME") == "dynamic"
    )


def resolve_optimizer_distribution(
    registry: RepositoryRegistry,
    *,
    environment: Mapping[str, str] | None = None,
    repository_root: Path | None = None,
) -> DistributionSettings:
    """Resolve session provenance locally; setup alone fetches the main ref."""
    env = os.environ if environment is None else environment
    if not uses_copilot_main_runtime(registry, environment=env):
        return registry.distribution
    if not registry.has_exact_runtime_provenance:
        raise ValueError("main runtime requires recorded exact fallback provenance")
    try:
        return _resolve_main_distribution(registry, env, repository_root)
    except OSError as error:
        raise ValueError(f"main runtime provenance is unavailable: {error}") from error


def _resolve_main_distribution(
    registry: RepositoryRegistry,
    environment: Mapping[str, str],
    repository_root: Path | None,
) -> DistributionSettings:
    customer = (
        _environment_path(environment, "GITHUB_WORKSPACE")
        if repository_root is None
        else Path(repository_root).resolve(strict=True)
    )
    if Path(_git_text(customer, "rev-parse", "--show-toplevel")).resolve() != customer:
        raise ValueError("customer repository must be the Git worktree root")
    committed = RepositoryRegistry.from_document(
        _git_bytes(customer, "show", f"HEAD:{_REGISTRY_PATH}")
    )
    if (
        committed.github.copilot_runtime != "main"
        or committed.distribution != registry.distribution
    ):
        raise ValueError("main runtime policy does not match the committed registry")
    sha = environment.get("FOUNDRY_OPT_RUNTIME_SHA", "")
    if not _COMMIT.fullmatch(sha):
        raise ValueError("FOUNDRY_OPT_RUNTIME_SHA must record an exact commit")
    root = _environment_path(environment, "FOUNDRY_OPT_SHARED_ROOT")
    if root.is_relative_to(customer) or customer.is_relative_to(root):
        raise ValueError("main runtime checkout must be outside the customer worktree")
    if Path(_git_text(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise ValueError("FOUNDRY_OPT_SHARED_ROOT must be the runtime Git worktree root")
    distribution = registry.distribution
    origins = _git_text(root, "config", "--local", "--get-all", "remote.origin.url").splitlines()
    if len(origins) != 1 or _normalized_origin(origins[0]) != _normalized_origin(distribution.repository):
        raise ValueError("main runtime origin does not match the committed distribution repository")
    if _git_text(root, "rev-parse", "HEAD") != sha:
        raise ValueError("runtime HEAD does not match FOUNDRY_OPT_RUNTIME_SHA")
    if _git_text(root, "rev-parse", "--verify", "refs/remotes/origin/main") != sha:
        raise ValueError("runtime origin/main does not match FOUNDRY_OPT_RUNTIME_SHA")
    if _git_text(root, "rev-parse", "--abbrev-ref", "HEAD") != "HEAD":
        raise ValueError("main runtime checkout must have a detached HEAD")

    package = _approved_path(root, distribution.package_path)
    skill = _approved_path(root, distribution.optimizer_skill_path)
    if _environment_path(environment, "FOUNDRY_OPT_PACKAGE_ROOT") != package:
        raise ValueError("FOUNDRY_OPT_PACKAGE_ROOT does not match the approved package_path")
    if _environment_path(environment, "FOUNDRY_OPT_SKILL_SOURCE") != skill:
        raise ValueError("FOUNDRY_OPT_SKILL_SOURCE does not match the approved optimizer_skill_path")
    if not (skill / "SKILL.md").is_file():
        raise ValueError("optimizer SKILL.md is missing from the main runtime checkout")
    loaded = _loaded_runtime_source().resolve(strict=True)
    source_roots = (package / "src" / "foundry_opt", package / "foundry_opt")
    if not any(loaded.is_relative_to(source_root) for source_root in source_roots):
        raise ValueError("loaded runtime code is not from the selected runtime package")

    tracked = _verify_main_tree(root, sha, package, skill)
    for required in (package / "uv.lock", skill / "SKILL.md", loaded):
        if required.relative_to(root).as_posix() not in tracked:
            raise ValueError(f"runtime source is not tracked in the selected commit: {required.name}")
    # Editable installs may create bytecode, but must not introduce runtime or skill sources.
    for source_root in (*source_roots, skill):
        if not source_root.is_dir():
            continue
        for path in source_root.rglob("*"):
            if path.is_dir() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            if path.relative_to(root).as_posix() not in tracked:
                raise ValueError(f"untracked file in selected runtime sources: {path.name}")
    actual_lock = hashlib.sha256((package / "uv.lock").read_bytes()).hexdigest()
    return distribution.model_copy(update={"pin": sha, "uv_lock_sha256": actual_lock})


def _verify_main_tree(root: Path, sha: str, package: Path, skill: Path) -> set[str]:
    tracked: set[str] = set()
    algorithm = "sha1" if len(sha) == 40 else "sha256"
    for entry in _git_bytes(root, "ls-tree", "-rz", "--full-tree", sha).split(b"\0"):
        if not entry:
            continue
        header, encoded_path = entry.split(b"\t", 1)
        mode, kind, object_id = header.decode("ascii").split()
        relative = encoded_path.decode("utf-8")
        path = root / relative
        if not (path.is_relative_to(package) or path.is_relative_to(skill)):
            continue
        if kind != "blob" or mode not in {"100644", "100755"}:
            raise ValueError("runtime package and skill must contain regular tracked files")
        resolved = path.resolve(strict=True)
        if resolved != path or not resolved.is_relative_to(root):
            raise ValueError("runtime tracked files must not traverse symbolic links")
        content = path.read_bytes()
        actual = hashlib.new(algorithm, f"blob {len(content)}\0".encode() + content).hexdigest()
        if actual != object_id:
            raise ValueError(f"runtime working contents do not match the selected commit: {relative}")
        tracked.add(relative)
    return tracked


def _loaded_runtime_source() -> Path:
    return Path(__file__)


def _environment_path(environment: Mapping[str, str], name: str) -> Path:
    value = environment.get(name)
    if not value or not Path(value).is_absolute():
        raise ValueError(f"{name} must identify an absolute existing session path")
    return Path(value).resolve(strict=True)


def _approved_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_dir():
        raise ValueError("runtime package and skill paths must stay inside the selected checkout")
    return path


def _normalized_origin(value: str) -> str:
    return value.removesuffix(".git").replace("git@github.com:", "https://github.com/")


def verify_runtime_checkout(
    registry: RepositoryRegistry,
    checkout: Path,
    *,
    environment: Mapping[str, str] | None = None,
    repository_root: Path | None = None,
) -> RuntimeCheckout:
    if not registry.has_exact_runtime_provenance:
        raise ValueError("registry v2 exact runtime provenance is required")
    distribution = resolve_optimizer_distribution(
        registry, environment=environment, repository_root=repository_root,
    )
    assert distribution.pin is not None
    assert distribution.uv_lock_sha256 is not None
    root = Path(checkout).resolve(strict=True)
    if uses_copilot_main_runtime(registry, environment=environment):
        env = os.environ if environment is None else environment
        if root != _environment_path(env, "FOUNDRY_OPT_SHARED_ROOT"):
            raise ValueError("shared checkout does not match the selected main runtime checkout")
    commit = _git_text(root, "rev-parse", "HEAD")
    if commit != distribution.pin:
        raise ValueError("runtime checkout commit does not match registry")
    package_root = (
        root
        if distribution.package_path == "."
        else root / distribution.package_path
    )
    lock_path = package_root / "uv.lock"
    actual_lock = hashlib.sha256(lock_path.read_bytes()).hexdigest()
    if actual_lock != distribution.uv_lock_sha256:
        raise ValueError("runtime uv.lock does not match registry")
    skill_path = root / distribution.optimizer_skill_path
    if not skill_path.is_dir():
        raise ValueError("optimizer skill path is missing from runtime checkout")
    return RuntimeCheckout(
        repository=distribution.repository,
        commit=commit,
        package_root=package_root,
        uv_lock_sha256=actual_lock,
        optimizer_skill_path=distribution.optimizer_skill_path,
    )


def _git_text(repository: Path, *arguments: str) -> str:
    return _git_bytes(repository, *arguments).decode("utf-8", errors="replace").strip()


def _git_bytes(repository: Path, *arguments: str) -> bytes:
    environment = {
        key: value for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
    })
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        capture_output=True,
        env=environment,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).decode("utf-8", errors="replace").strip()
        raise ValueError(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout
