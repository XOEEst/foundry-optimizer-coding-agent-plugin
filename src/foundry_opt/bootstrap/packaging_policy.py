"""Exclusion policy for packaging a reviewed agent source tree as an owned draft.

The activation smoke run uploads the repository's own source, so anything that is secret,
generated, machine-local, or part of the tool's managed state must never enter the archive.
"""

from __future__ import annotations

PACKAGE_EXCLUDES: tuple[str, ...] = (
    ".git/**",
    ".git",
    ".github/**",
    ".foundry-opt/**",
    ".venv/**",
    "venv/**",
    "node_modules/**",
    "__pycache__/**",
    "**/__pycache__/**",
    "*.pyc",
    "**/*.pyc",
    ".mypy_cache/**",
    ".pytest_cache/**",
    ".ruff_cache/**",
    ".tox/**",
    "dist/**",
    "build/**",
    "*.egg-info/**",
    ".env",
    ".env.*",
    "**/.env",
    "**/.env.*",
    "*.pem",
    "*.pfx",
    "*.key",
    "**/*.pem",
    "**/*.pfx",
    "**/*.key",
    "secrets/**",
    "**/secrets/**",
    "credentials.json",
    "**/credentials.json",
    "secrets.json",
    "**/secrets.json",
    "datasets/**",
    "**/datasets/**",
    "traces/**",
    "**/traces/**",
    "prompts/**",
    "**/prompts/**",
    ".DS_Store",
    "**/.DS_Store",
)

__all__ = ["PACKAGE_EXCLUDES"]
