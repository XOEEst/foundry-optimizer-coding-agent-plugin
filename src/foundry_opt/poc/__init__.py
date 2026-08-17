"""Public POC entrypoints for the Foundry optimization package."""

from __future__ import annotations

from importlib import import_module
from typing import Any

from foundry_opt import __version__

__all__ = ["__version__", "app", "main", "runtime"]


def main() -> None:
    from foundry_opt import main as package_main

    package_main()


def __getattr__(name: str) -> Any:
    if name == "app":
        from foundry_opt import app

        return app
    if name == "runtime":
        return import_module("foundry_opt.poc.runtime")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
