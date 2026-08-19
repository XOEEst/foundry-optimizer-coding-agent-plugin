from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from foundry_opt.poc.deploy import (
    DeploymentGuardrail,
    DeploymentReceipt,
    load_registered_deployment_settings,
    publish_registered_deployment,
)
from foundry_opt.poc.runtime import RuntimeIntegrationError


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_ROOT = (
    REPOSITORY_ROOT / "src" / "foundry_opt" / "templates" / "customer-repo"
)
CLIENT_ID = "44444444-4444-4444-4444-444444444444"
TENANT_ID = "22222222-2222-2222-2222-222222222222"
SUBSCRIPTION_ID = "33333333-3333-3333-3333-333333333333"


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _registered_repository(tmp_path: Path) -> tuple[Path, str, dict[str, str]]:
    repository = tmp_path / "repo"
    (repository / ".foundry-opt").mkdir(parents=True)
    (repository / "agent" / ".foundry").mkdir(parents=True)
    (repository / "agent" / "main.py").write_text("print('ready')\n", encoding="utf-8")
    (repository / ".gitattributes").write_text(
        ".foundry-opt/** text eol=lf\nagent/.foundry/** text eol=lf\n",
        encoding="utf-8",
    )

    registry = yaml.safe_load(
        (TEMPLATE_ROOT / ".foundry-opt" / "registry.yaml").read_text(
            encoding="utf-8"
        )
    )
    registry["identity"] = {
        "schema_version": 1,
        "kind": "user_assigned_managed_identity",
        "resource_id": (
            f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/example-rg/"
            "providers/Microsoft.ManagedIdentity/userAssignedIdentities/example"
        ),
        "client_id": CLIENT_ID,
    }
    (repository / ".foundry-opt" / "registry.yaml").write_text(
        yaml.safe_dump(registry, sort_keys=False),
        encoding="utf-8",
    )
    (repository / "agent" / ".foundry" / "foundry-opt.yaml").write_text(
        (TEMPLATE_ROOT / "agent" / ".foundry" / "foundry-opt.yaml").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    registry_bytes = (repository / ".foundry-opt" / "registry.yaml").read_bytes()
    sidecar_bytes = (
        repository / "agent" / ".foundry" / "foundry-opt.yaml"
    ).read_bytes()
    (repository / ".foundry-opt" / "bootstrap.lock.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "engine": "repository-engine",
                "runtime_repository": registry["distribution"]["repository"],
                "channel": "repository",
                "runtime_commit": registry["distribution"]["pin"],
                "managed_files": [
                    {
                        "schema_version": 1,
                        "path": ".foundry-opt/registry.yaml",
                        "ownership_mode": "owned",
                        "owner_scope": "repository",
                        "template_id": "registry",
                        "template_base_sha256": hashlib.sha256(
                            registry_bytes
                        ).hexdigest(),
                        "applied_sha256": hashlib.sha256(
                            registry_bytes
                        ).hexdigest(),
                        "semantic_patch_id": None,
                    },
                    {
                        "schema_version": 1,
                        "path": "agent/.foundry/foundry-opt.yaml",
                        "ownership_mode": "owned",
                        "owner_scope": "agent",
                        "template_id": "sidecar",
                        "template_base_sha256": hashlib.sha256(
                            sidecar_bytes
                        ).hexdigest(),
                        "applied_sha256": hashlib.sha256(
                            sidecar_bytes
                        ).hexdigest(),
                        "semantic_patch_id": None,
                    },
                ],
                "github_environments": [],
                "cloud_resources": [],
                "sidecar_paths": ["agent/.foundry/foundry-opt.yaml"],
                "last_activation": {
                    "schema_version": 1,
                    "outcome": "succeeded",
                    "detail": None,
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    _git(repository, "init")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "user.name", "Test")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "fixture")
    commit = _git(repository, "rev-parse", "HEAD")
    environment = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": "example-org/example-repo",
        "GITHUB_REPOSITORY_ID": "123456789",
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_REF_NAME": "main",
        "GITHUB_SHA": commit,
        "FOUNDRY_OPT_DEPLOYMENT_CLIENT_ID": CLIENT_ID,
        "AZURE_TENANT_ID": TENANT_ID,
        "RUNNER_TEMP": str(tmp_path / "runner"),
        "GH_TOKEN": "test-token",
    }
    return repository, commit, environment


def test_registered_settings_project_the_sidecar_contract(tmp_path: Path) -> None:
    repository, commit, environment = _registered_repository(tmp_path)

    settings = load_registered_deployment_settings(
        repository,
        repo_agent_id="example-agent",
        exact_source=commit,
        environment=environment,
    )

    assert settings.release_commit == commit
    assert settings.policy.source_root == "agent"
    assert settings.metadata.repository_identity == "example-org/example-repo"
    assert settings.metadata.repository_id == 123456789
    assert settings.metadata.oidc.tenant_id == TENANT_ID
    assert settings.oidc_config.expected_subject == (
        "repo:example-org/example-repo:environment:foundry-production"
    )
    assert "violence" in settings.metadata.development_evaluation.custom_evaluator_ids
    assert settings.selection.sidecar.default_evaluator_bundle.objective.objective_hash


def test_registered_settings_reject_client_id_drift(tmp_path: Path) -> None:
    repository, commit, environment = _registered_repository(tmp_path)
    environment["FOUNDRY_OPT_DEPLOYMENT_CLIENT_ID"] = (
        "55555555-5555-5555-5555-555555555555"
    )

    with pytest.raises(RuntimeIntegrationError, match="client id"):
        load_registered_deployment_settings(
            repository,
            repo_agent_id="example-agent",
            exact_source=commit,
            environment=environment,
        )


def test_registered_settings_require_github_actions_oidc(tmp_path: Path) -> None:
    repository, commit, environment = _registered_repository(tmp_path)
    environment["GITHUB_ACTIONS"] = "false"

    with pytest.raises(RuntimeIntegrationError, match="GitHub Actions OIDC"):
        load_registered_deployment_settings(
            repository,
            repo_agent_id="example-agent",
            exact_source=commit,
            environment=environment,
        )


def test_registered_settings_reject_uncommitted_sidecar_drift(tmp_path: Path) -> None:
    repository, commit, environment = _registered_repository(tmp_path)
    sidecar = repository / "agent" / ".foundry" / "foundry-opt.yaml"
    sidecar.write_text(
        sidecar.read_text(encoding="utf-8") + "\n# uncommitted drift\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeIntegrationError, match="exact source commit"):
        load_registered_deployment_settings(
            repository,
            repo_agent_id="example-agent",
            exact_source=commit,
            environment=environment,
        )


def test_registered_publish_packages_exact_source_and_closes_clients(
    tmp_path: Path,
) -> None:
    repository, commit, environment = _registered_repository(tmp_path)
    settings = load_registered_deployment_settings(
        repository,
        repo_agent_id="example-agent",
        exact_source=commit,
        environment=environment,
    )
    closed: list[str] = []

    class Credential:
        def close(self) -> None:
            closed.append("credential")

    class Client:
        def close(self) -> None:
            closed.append("client")

    class Service:
        def __init__(self, **kwargs: object) -> None:
            assert kwargs["policy"] == settings.policy
            assert kwargs["metadata"] == settings.metadata
            assert kwargs["freshness_check"] is not None

        def publish(
            self,
            *,
            repository: str,
            release_commit: str,
            packaged: object,
        ) -> DeploymentReceipt:
            assert repository == "example-org/example-repo"
            assert release_commit == commit
            assert getattr(packaged, "commit") == commit
            assert getattr(packaged, "source_root") == "agent"
            return DeploymentReceipt(
                repository=repository,
                release_commit=release_commit,
                project_endpoint=settings.metadata.project_endpoint,
                agent_name=settings.metadata.agent_name,
                previous_version="4",
                published_version="5",
                operation_id="deploy-registered",
                reconciled=False,
                source_root="agent",
                source_tree_sha256=getattr(packaged, "tree_sha256"),
                source_zip_sha256=getattr(packaged, "zip_sha256"),
                evaluation_link="https://example.invalid/evaluations/deploy",
                guardrails=(
                    DeploymentGuardrail(
                        name="violence",
                        score=1.0,
                        required_pass_rate=1.0,
                        passed=True,
                    ),
                ),
            )

    receipt = publish_registered_deployment(
        settings,
        environment=environment,
        credential_builder=lambda *_args, **_kwargs: Credential(),
        evaluation_backend_factory=lambda **_kwargs: object(),
        foundry_client_factory=lambda *_args, **_kwargs: Client(),
        service_factory=Service,
    )

    assert receipt.published_version == "5"
    assert closed == ["client", "credential"]
