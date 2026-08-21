from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Sequence

import pytest


_PROBE_MARKER = "__FOUNDRY_OPT_PROBE__"
_IMPORT_SURFACE_SCRIPT = """
import json
import sys

import foundry_opt
import foundry_opt.poc

payload = {
    "version": foundry_opt.__version__,
    "callable_main": callable(foundry_opt.main),
    "callable_poc_main": callable(foundry_opt.poc.main),
    "runtime_name": foundry_opt.runtime.__name__,
    "shared_runtime": foundry_opt.runtime is foundry_opt.poc.runtime,
    "legacy": sorted(
        name
        for name in sys.modules
        if name in {
            "foundry_opt.candidates",
            "foundry_opt.config",
            "foundry_opt.evidence",
            "foundry_opt.fakes",
            "foundry_opt.foundry",
            "foundry_opt.github",
            "foundry_opt.interfaces",
            "foundry_opt.interactive",
            "foundry_opt.onboarding",
            "foundry_opt.optimize_job.checkpoint",
            "foundry_opt.optimize_job.deadline",
            "foundry_opt.optimize_job.intake",
            "foundry_opt.optimize_job.progress",
            "foundry_opt.optimize_job.runner",
            "foundry_opt.optimize_job.store",
            "foundry_opt.release",
            "foundry_opt.scaffold",
            "foundry_opt.winner_pr",
        }
        or name.startswith(
            (
                "foundry_opt.candidates.",
                "foundry_opt.config.",
                "foundry_opt.evidence.",
                "foundry_opt.foundry.",
                "foundry_opt.github.",
                "foundry_opt.interactive.",
                "foundry_opt.onboarding.",
                "foundry_opt.release.",
                "foundry_opt.winner_pr.",
            )
        )
    ),
}
print(json.dumps(payload, sort_keys=True))
"""
_CLI_PROBE_SCRIPT = """
import json
import runpy
import sys

sys.argv = ["foundry-opt", *sys.argv[1:]]
exit_code = 0
try:
    runpy.run_module("foundry_opt", run_name="__main__")
except SystemExit as exc:
    code = exc.code
    exit_code = code if isinstance(code, int) else 0

payload = {
    "exit_code": exit_code,
    "legacy": sorted(
        name
        for name in sys.modules
        if name in {
            "foundry_opt.candidates",
            "foundry_opt.config",
            "foundry_opt.evidence",
            "foundry_opt.fakes",
            "foundry_opt.foundry",
            "foundry_opt.github",
            "foundry_opt.interfaces",
            "foundry_opt.interactive",
            "foundry_opt.onboarding",
            "foundry_opt.optimize_job.checkpoint",
            "foundry_opt.optimize_job.deadline",
            "foundry_opt.optimize_job.intake",
            "foundry_opt.optimize_job.progress",
            "foundry_opt.optimize_job.runner",
            "foundry_opt.optimize_job.store",
            "foundry_opt.release",
            "foundry_opt.scaffold",
            "foundry_opt.winner_pr",
        }
        or name.startswith(
            (
                "foundry_opt.candidates.",
                "foundry_opt.config.",
                "foundry_opt.evidence.",
                "foundry_opt.foundry.",
                "foundry_opt.github.",
                "foundry_opt.interactive.",
                "foundry_opt.onboarding.",
                "foundry_opt.release.",
                "foundry_opt.winner_pr.",
            )
        )
    ),
}
sys.stderr.write("\\n__FOUNDRY_OPT_PROBE__" + json.dumps(payload, sort_keys=True))
raise SystemExit(exit_code)
"""


def _subprocess_environment() -> dict[str, str]:
    return {
        **os.environ,
        "NO_COLOR": "1",
        "PYTHONIOENCODING": "utf-8",
    }


def _legacy_modules(payload: dict[str, object]) -> list[str]:
    value = payload["legacy"]
    assert isinstance(value, list)
    modules = [item for item in value if isinstance(item, str)]
    assert len(modules) == len(value)
    return modules


def _run_inline(script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", script, *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_subprocess_environment(),
    )


def _run_cli_probe(
    arguments: Sequence[str],
) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    completed = _run_inline(_CLI_PROBE_SCRIPT, *arguments)
    marker_index = completed.stderr.rfind(_PROBE_MARKER)
    assert marker_index != -1, completed.stderr
    payload = json.loads(completed.stderr[marker_index + len(_PROBE_MARKER) :])
    assert isinstance(payload, dict)
    return completed, payload


def test_public_package_exports_only_poc_entrypoints() -> None:
    completed = _run_inline(_IMPORT_SURFACE_SCRIPT)

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["version"] == "0.2.0"
    assert payload["callable_main"] is True
    assert payload["callable_poc_main"] is True
    assert payload["runtime_name"] == "foundry_opt.poc.runtime"
    assert payload["shared_runtime"] is True
    assert _legacy_modules(payload) == []


@pytest.mark.parametrize(
    ("arguments", "expected_fragment"),
    [
        (["--help"], "validate-config"),
        (["version"], "0.2.0"),
        (
            ["validate-config", "--help"],
            "Validate the target repository contract",
        ),
        (
            ["preflight", "--help"],
            "Verify bootstrap, policy, metadata, OIDC, and broker prerequisites",
        ),
        (["deploy", "--help"], "verify-registered"),
        (["bootstrap", "--help"], "verify"),
        (["issue", "--help"], "parse"),
        (["broker", "--help"], "launch"),
        (["job", "--help"], "start"),
        (["acceptance", "--help"], "smoke"),
    ],
)
def test_module_entrypoint_help_stays_on_poc_command_tree(
    arguments: list[str],
    expected_fragment: str,
) -> None:
    completed, payload = _run_cli_probe(arguments)

    assert completed.returncode == 0, completed.stderr
    assert expected_fragment in completed.stdout
    assert payload["exit_code"] == 0
    assert _legacy_modules(payload) == []
