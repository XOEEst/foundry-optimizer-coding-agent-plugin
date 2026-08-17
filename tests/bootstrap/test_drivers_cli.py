from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import pytest

from foundry_opt.bootstrap.command_io import BootstrapCliError
from foundry_opt.bootstrap.contracts import BootstrapAction, BootstrapPlan, BootstrapReceipt
from foundry_opt.bootstrap.drivers import AzurePhaseDriver, EvaluationPhaseDriver, GitHubPhaseDriver, RepositoryPhaseDriver
from foundry_opt.bootstrap.providers.github import GitHubBootstrapProvider
from foundry_opt.bootstrap.providers.foundry import FoundryAdapter


def test_github_driver_resolves_token_from_gh(monkeypatch) -> None:
    monkeypatch.delenv("FOUNDRY_OPT_GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="secret\n", stderr=""))
    assert GitHubPhaseDriver()._resolve_token() == "secret"


def test_repository_driver_exports_state(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    driver = RepositoryPhaseDriver(repository_root=repo, payloads=())
    receipt = BootstrapReceipt.create(operation_id="op", runtime_repository="https://github.com/org/repo.git", runtime_commit="a" * 40, repository_identity="org/repo", plan_hash="a" * 64)
    exported = driver.export_provider_state(receipt)
    assert exported["receipt_hash"] == receipt.receipt_hash


def test_azure_driver_requires_authoritative_plan_input() -> None:
    driver = AzurePhaseDriver()
    with pytest.raises(BootstrapCliError, match="BootstrapPlanInput is required"):
        driver.plan({"operation_id": "op", "runtime_repository": "https://github.com/org/repo.git", "runtime_commit": "a" * 40, "repository_id": "org/repo"})


def test_evaluation_driver_requires_provider() -> None:
    driver = EvaluationPhaseDriver()
    try:
        driver.inventory()
    except Exception as exc:
        assert "requires configured project input" in str(exc)
