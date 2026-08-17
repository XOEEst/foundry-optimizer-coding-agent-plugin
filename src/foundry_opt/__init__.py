"""Public POC entrypoints for the Foundry optimization package."""

from __future__ import annotations

__version__ = "0.2.0"

from foundry_opt.cli import app, main
from foundry_opt.poc import runtime

__all__ = ["__version__", "app", "main", "runtime"]
