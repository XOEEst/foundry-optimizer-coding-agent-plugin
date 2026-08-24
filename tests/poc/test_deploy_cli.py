from __future__ import annotations

import json

from typer.testing import CliRunner

from foundry_opt import cli as cli_module
from foundry_opt.cli import app
from foundry_opt.poc.deploy import (
    DeploymentGuardrail,
    DeploymentReceipt,
    DeploymentRepositoryChecksError,
    DeploymentVerification,
    DeploymentVerificationCheckResult,
    DeploymentVerificationReceipt,
    deployment_unverified_warning,
)


runner = CliRunner()


def _foundry_verification() -> DeploymentVerification:
    return DeploymentVerification(
        mode="foundry_evaluation",
        status="passed",
        evaluation_id="eval_development",
        dataset_id="dataset-development",
        evaluator_ids=("primary_metric", "safety"),
        evaluation_link="https://example.invalid/evaluations/run",
        guardrails=(
            DeploymentGuardrail(
                name="safety",
                score=1.0,
                required_pass_rate=1.0,
                passed=True,
            ),
        ),
        unverified_deployment=False,
    )


def _unverified_verification() -> DeploymentVerification:
    return DeploymentVerification(
        mode="none",
        status="unverified",
        evaluation_gate_policy="allow_no_evidence",
        unverified_deployment=True,
        warning=deployment_unverified_warning(),
    )


def _failed_repository_check_verification() -> DeploymentVerification:
    return DeploymentVerification(
        mode="repository_checks",
        status="failed",
        evaluation_gate_policy="allow_repository_checks",
        check_results=(
            DeploymentVerificationCheckResult(
                kind="command",
                value="python -m pytest -q",
                status="failed",
                detail="Command exited with code 1.",
            ),
        ),
        unverified_deployment=False,
    )


def test_deploy_publish_registered_writes_receipt(monkeypatch, tmp_path) -> None:
    settings = object()
    monkeypatch.setattr(
        cli_module,
        "load_registered_deployment_settings",
        lambda *args, **kwargs: settings,
    )
    monkeypatch.setattr(
        cli_module,
        "publish_registered_deployment",
        lambda loaded, **kwargs: DeploymentReceipt(
            repository="example-org/example-agent",
            release_commit="a" * 40,
            project_endpoint="https://example.services.ai.azure.com/api/projects/example",
            agent_name="example-agent",
            previous_version="14",
            published_version="15",
            operation_id="deploy-registered",
            reconciled=False,
            source_root="agent",
            source_tree_sha256="b" * 64,
            source_zip_sha256="c" * 64,
            evaluation_link="https://example.invalid/evaluations/run",
            guardrails=_foundry_verification().guardrails,
            verification=_foundry_verification(),
        ),
    )
    receipt = tmp_path / "registered-deployment-receipt.json"

    result = runner.invoke(
        app,
        [
            "deploy",
            "publish-registered",
            "--repo-agent-id",
            "example-agent",
            "--exact-source",
            "a" * 40,
            "--receipt",
            str(receipt),
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["published_version"] == "15"
    assert json.loads(receipt.read_text(encoding="utf-8")) == payload


def test_deploy_verify_registered_writes_receipt(monkeypatch, tmp_path) -> None:
    settings = object()
    monkeypatch.setattr(
        cli_module,
        "load_registered_verification_settings",
        lambda *args, **kwargs: settings,
    )
    monkeypatch.setattr(
        cli_module,
        "verify_registered_deployment",
        lambda loaded, **kwargs: DeploymentVerificationReceipt(
            repository="example-org/example-agent",
            release_commit="a" * 40,
            project_endpoint="https://example.services.ai.azure.com/api/projects/example",
            agent_name="example-agent",
            operation_id="deploy-verified",
            source_root="agent",
            source_tree_sha256="b" * 64,
            source_zip_sha256="c" * 64,
            verification=_foundry_verification(),
            repo_agent_id="example-agent",
            config_path="agent/.foundry/foundry-opt.yaml",
        ),
    )
    receipt = tmp_path / "registered-verification-receipt.json"

    result = runner.invoke(
        app,
        [
            "deploy",
            "verify-registered",
            "--repo-agent-id",
            "example-agent",
            "--exact-source",
            "a" * 40,
            "--receipt",
            str(receipt),
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "verified"
    assert payload["published"] is False
    assert payload["repo_agent_id"] == "example-agent"
    assert json.loads(receipt.read_text(encoding="utf-8")) == payload


def test_deploy_verify_registered_persists_blocked_payload(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        cli_module,
        "load_registered_verification_settings",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        cli_module,
        "verify_registered_deployment",
        lambda loaded, **kwargs: (_ for _ in ()).throw(
            DeploymentRepositoryChecksError(
                "deployment repository checks did not all pass: python -m pytest -q",
                verification=_failed_repository_check_verification(),
            )
        ),
    )
    receipt = tmp_path / "registered-verification-receipt.json"

    result = runner.invoke(
        app,
        [
            "deploy",
            "verify-registered",
            "--repo-agent-id",
            "example-agent",
            "--exact-source",
            "a" * 40,
            "--receipt",
            str(receipt),
        ],
    )

    assert result.exit_code == 2, result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "blocked"
    assert payload["verification"]["status"] == "failed"
    assert json.loads(receipt.read_text(encoding="utf-8")) == payload


def test_deploy_publish_registered_emits_unverified_warning(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        cli_module,
        "load_registered_deployment_settings",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        cli_module,
        "publish_registered_deployment",
        lambda loaded, **kwargs: DeploymentReceipt(
            repository="example-org/example-agent",
            release_commit="a" * 40,
            project_endpoint="https://example.services.ai.azure.com/api/projects/example",
            agent_name="example-agent",
            previous_version="14",
            published_version="15",
            operation_id="deploy-registered",
            reconciled=False,
            source_root="agent",
            source_tree_sha256="b" * 64,
            source_zip_sha256="c" * 64,
            verification=_unverified_verification(),
        ),
    )
    receipt = tmp_path / "registered-deployment-receipt.json"

    result = runner.invoke(
        app,
        [
            "deploy",
            "publish-registered",
            "--repo-agent-id",
            "example-agent",
            "--exact-source",
            "a" * 40,
            "--receipt",
            str(receipt),
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["verification"]["status"] == "unverified"
    assert payload["verification"]["unverified_deployment"] is True
    assert payload["verification"]["warning"]["code"] == "deployment-unverified"
