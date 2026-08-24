from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

from foundry_opt.repository_contracts import RepositoryRegistry


@dataclass(frozen=True, slots=True)
class RuntimeCheckout:
    repository: str
    commit: str
    package_root: Path
    uv_lock_sha256: str
    optimizer_skill_path: str


def verify_runtime_checkout(
    registry: RepositoryRegistry,
    checkout: Path,
) -> RuntimeCheckout:
    if not registry.has_exact_runtime_provenance:
        raise ValueError("registry v2 exact runtime provenance is required")
    distribution = registry.distribution
    assert distribution.pin is not None
    assert distribution.uv_lock_sha256 is not None
    root = Path(checkout).resolve(strict=True)
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
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise ValueError(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout.strip()
