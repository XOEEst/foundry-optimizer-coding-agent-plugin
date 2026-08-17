from __future__ import annotations

import enum
import json
from pathlib import Path
from typing import Any

import typer


class BootstrapExitCode(enum.IntEnum):
    OK = 0
    CONFIG = 20
    AUTH = 21
    MISSING = 22
    CONFLICT = 23
    STALE = 24
    APPLY = 25
    RUNTIME = 26


class BootstrapCliError(Exception):
    def __init__(self, code: str, message: str, *, exit_code: BootstrapExitCode, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.details = details or {}


def emit_json(payload: dict[str, Any]) -> None:
    typer.echo(json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")))


def load_json_file(path: Path, *, subject: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BootstrapCliError("missing-file", f"{subject} file does not exist", exit_code=BootstrapExitCode.MISSING, details={"path": str(path)}) from exc
    except json.JSONDecodeError as exc:
        raise BootstrapCliError("invalid-json", f"{subject} file is not valid JSON", exit_code=BootstrapExitCode.CONFIG, details={"path": str(path)}) from exc
    if not isinstance(value, dict):
        raise BootstrapCliError("invalid-json", f"{subject} file must contain a JSON object", exit_code=BootstrapExitCode.CONFIG, details={"path": str(path)})
    return value


def write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, sort_keys=True, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
