"""Compatibility marker for workflow-owned static skill packaging.

The foundry-bootstrap archive and checksum are produced with standard shell
tools in ``.github/workflows/release-foundry-bootstrap-skill.yml``. This module
intentionally exposes no Python release builder.
"""

from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
RELEASE_WORKFLOW = (
    REPOSITORY_ROOT
    / ".github"
    / "workflows"
    / "release-foundry-bootstrap-skill.yml"
)


__all__ = ["RELEASE_WORKFLOW"]
