"""Shared bootstrap runtime, repository, and state lookup helpers."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from pathlib import Path

from foundry_opt.bootstrap.errors import BootstrapApplyError, BootstrapConfigError

_RUNTIME_COMMIT_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
_GITHUB_REMOTE_PATTERNS = (
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$",
    r"^git@github\.com:(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?$",
    r"^ssh://git@github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$",
)


def default_state_root() -> Path:
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "foundry-opt" / "bootstrap"
    return Path.home() / ".foundry-opt" / "bootstrap"


def resolve_state_root(state_root: Path | None = None) -> Path:
    return (Path(state_root) if state_root is not None else default_state_root()).resolve()


def scoped_state_root(scope: str, *, state_root: Path | None = None) -> Path:
    return resolve_state_root(state_root) / scope


def require_safe_operation_id(
    value: str,
    *,
    message: str,
    error_factory: Callable[[str], Exception],
) -> str:
    if not value or any(sep in value for sep in ("/", "\\", "..")):
        raise error_factory(message)
    return value


def resolve_state_child_directory(
    root: Path,
    *segments: str,
    escape_message: str,
) -> Path:
    target = root
    for segment in segments:
        target = target / segment
    target = target.resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise BootstrapApplyError(escape_message) from exc
    return target


def runtime_repository_from_environment() -> str:
    value = os.environ.get("FOUNDRY_OPT_RUNTIME_REPOSITORY")
    if not value:
        raise BootstrapConfigError("runtime repository must come from the verified environment")
    return value


def runtime_commit_from_environment() -> str:
    value = os.environ.get("FOUNDRY_OPT_RUNTIME_COMMIT")
    if value and _RUNTIME_COMMIT_PATTERN.fullmatch(value):
        return value
    raise BootstrapConfigError("runtime commit must come from the verified environment")


def github_remote_identity(value: str) -> tuple[str, str]:
    for pattern in _GITHUB_REMOTE_PATTERNS:
        match = re.fullmatch(pattern, value)
        if match is not None:
            return match.group("owner"), match.group("repo")
    raise BootstrapConfigError("repository remote must target github.com/owner/repo")
