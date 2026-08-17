"""Typer command registration for bootstrap operations."""

from __future__ import annotations

import json
from pathlib import Path

import typer

from foundry_opt.distribution import (
    load_shared_pin,
    verify_shared_checkout,
    write_bootstrap_receipt,
)


def _echo_json(value: object) -> None:
    typer.echo(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        )
    )


def register_bootstrap_commands(app: typer.Typer) -> None:
    @app.command("verify")
    def verify(
        pin_path: Path = typer.Option(..., "--pin"),
        checkout: Path = typer.Option(..., "--checkout"),
        receipt: Path = typer.Option(..., "--receipt"),
    ) -> None:
        """Verify an exact shared checkout and persist its bootstrap receipt."""

        pin = load_shared_pin(pin_path)
        verified = verify_shared_checkout(pin, checkout)
        write_bootstrap_receipt(receipt, verified)
        _echo_json(
            {
                "commit": verified.commit,
                "receipt": str(receipt.resolve()),
                "receipt_sha256": verified.receipt_sha256,
                "repository": verified.repository,
                "status": "verified",
            }
        )


__all__ = ["register_bootstrap_commands"]
