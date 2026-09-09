"""Public entrypoints for the Foundry optimization package."""

from __future__ import annotations

from typing import Any

__version__ = "0.2.0"


def main() -> Any:
    from foundry_opt.cli import main as cli_main

    return cli_main()


def __getattr__(name: str) -> Any:
    if name == "app":
        from foundry_opt.cli import app

        return app
    if name == "runtime":
        from foundry_opt.poc import runtime

        return runtime
    raise AttributeError(name)


__all__ = ["__version__", "app", "main", "runtime"]
