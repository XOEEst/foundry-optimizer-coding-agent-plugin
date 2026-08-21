from __future__ import annotations

import os
import subprocess
import time
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from foundry_opt.poc.candidate import FinalizedCandidate
from foundry_opt.verification import VerificationCheckSpec


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RepositoryCheckResult(_FrozenModel):
    spec: VerificationCheckSpec
    passed: bool
    exit_code: int | None = None
    duration_seconds: float = Field(ge=0)
    summary: str = Field(min_length=1, max_length=256)


@runtime_checkable
class RepositoryCheckRunnerProtocol(Protocol):
    def run_checks(
        self,
        candidate: FinalizedCandidate,
        *,
        checks: tuple[VerificationCheckSpec, ...],
    ) -> tuple[RepositoryCheckResult, ...]: ...


class LocalRepositoryCheckRunner:
    """Execute trusted repository verification commands in the candidate workspace."""

    def __init__(self, *, timeout_seconds: float = 300.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._timeout_seconds = float(timeout_seconds)

    def run_checks(
        self,
        candidate: FinalizedCandidate,
        *,
        checks: tuple[VerificationCheckSpec, ...],
    ) -> tuple[RepositoryCheckResult, ...]:
        return tuple(
            self._run_check(candidate, check=check)
            for check in checks
        )

    def _run_check(
        self,
        candidate: FinalizedCandidate,
        *,
        check: VerificationCheckSpec,
    ) -> RepositoryCheckResult:
        if check.kind == "check":
            return RepositoryCheckResult(
                spec=check,
                passed=False,
                exit_code=None,
                duration_seconds=0.0,
                summary=(
                    "Named repository checks are not supported by the local optimize-job runtime."
                ),
            )
        started = time.monotonic()
        try:
            completed = subprocess.run(
                check.value,
                cwd=candidate.workspace_path,
                check=False,
                shell=True,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
                env=_check_environment(),
            )
        except subprocess.TimeoutExpired:
            return RepositoryCheckResult(
                spec=check,
                passed=False,
                exit_code=None,
                duration_seconds=time.monotonic() - started,
                summary=f"Command timed out after {self._timeout_seconds:.0f} seconds.",
            )
        except OSError:
            return RepositoryCheckResult(
                spec=check,
                passed=False,
                exit_code=None,
                duration_seconds=time.monotonic() - started,
                summary="Command could not be executed.",
            )
        return RepositoryCheckResult(
            spec=check,
            passed=completed.returncode == 0,
            exit_code=None if completed.returncode < 0 else completed.returncode,
            duration_seconds=time.monotonic() - started,
            summary=(
                "Command passed."
                if completed.returncode == 0
                else f"Command exited with code {completed.returncode}."
            ),
        )


def _check_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "PYTHONHOME",
        "PYTHONPATH",
    ):
        environment.pop(key, None)
    return environment


__all__ = [
    "LocalRepositoryCheckRunner",
    "RepositoryCheckResult",
    "RepositoryCheckRunnerProtocol",
]
