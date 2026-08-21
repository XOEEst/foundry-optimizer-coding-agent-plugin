from __future__ import annotations

import json

from typer.testing import CliRunner

from foundry_opt import cli as cli_module
from foundry_opt.cli import app
from foundry_opt.poc.deploy import (
    DeploymentGuardrail,
    DeploymentPreflight,
    DeploymentReceipt,
    DeploymentSupersededError,
    DeploymentVerification,
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


def test_deploy_preflight_emits_machine_readable_contract(monkeypatch) -> None:
    settings = object()
    monkeypatch.setattr(
        cli_module,
        "load_deployment_settings",
        lambda *args, **kwargs: settings,
    )
    monkeypatch.setattr(
        cli_module,
        "run_deployment_preflight",
        lambda loaded, **kwargs: DeploymentPreflight(
            repository="example-org/example-agent",
            release_commit="a" * 40,
            project_endpoint="https://example.services.ai.azure.com/api/projects/example",
            agent_name="example-agent",
            previous_version="14",
            deployment_environment="foundry-production",
            deployment_client_id="22222222-2222-2222-2222-222222222222",
            source_root="agent",
        ),
    )

    result = runner.invoke(app, ["deploy", "preflight"])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "ready"
    assert payload["previous_version"] == "14"
    assert payload["route_mode"] == "service-managed-latest"


def test_deploy_publish_writes_receipt(monkeypatch, tmp_path) -> None:
    settings = object()
    monkeypatch.setattr(
        cli_module,
        "load_deployment_settings",
        lambda *args, **kwargs: settings,
    )
    monkeypatch.setattr(
        cli_module,
        "publish_deployment",
        lambda loaded, **kwargs: DeploymentReceipt(
            repository="example-org/example-agent",
            release_commit="a" * 40,
            project_endpoint="https://example.services.ai.azure.com/api/projects/example",
            agent_name="example-agent",
            previous_version="14",
            published_version="15",
            operation_id="deploy-abc",
            reconciled=False,
            source_root="agent",
            source_tree_sha256="b" * 64,
            source_zip_sha256="c" * 64,
            evaluation_link="https://example.invalid/evaluations/run",
            guardrails=_foundry_verification().guardrails,
            verification=_foundry_verification(),
        ),
    )
    receipt = tmp_path / "deployment-receipt.json"

    result = runner.invoke(
        app,
        ["deploy", "publish", "--receipt", str(receipt)],
    )

    assert result.exit_code == 0, result.stdout
    stdout_payload = json.loads(result.stdout)
    file_payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert stdout_payload == file_payload
    assert file_payload["published_version"] == "15"
    assert file_payload["route_mutated"] is False
    assert file_payload["verification"]["mode"] == "foundry_evaluation"


def test_deploy_publish_reports_superseded_as_success(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        cli_module,
        "load_deployment_settings",
        lambda *args, **kwargs: object(),
    )
    monkeypatch.setattr(
        cli_module,
        "publish_deployment",
        lambda loaded, **kwargs: (_ for _ in ()).throw(
            DeploymentSupersededError(
                release_commit="a" * 40,
                current_main_commit="b" * 40,
            )
        ),
    )
    receipt = tmp_path / "deployment-receipt.json"

    result = runner.invoke(
        app,
        ["deploy", "publish", "--receipt", str(receipt)],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "superseded"
    assert payload["published"] is False
    assert json.loads(receipt.read_text(encoding="utf-8")) == payload


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
