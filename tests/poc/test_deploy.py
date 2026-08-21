from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
import subprocess

import pytest

from foundry_opt.poc import deploy as deploy_module
from foundry_opt.poc.bootstrap import BootstrapReceipt, load_shared_pin, write_bootstrap_receipt
from foundry_opt.poc.config import AgentMetadata, RepositoryPolicy, load_agent_metadata, load_repository_policy
from foundry_opt.poc.deploy import (
    DeploymentRepositoryChecksError,
    DeploymentSettings,
    DeploymentGuardrailError,
    DeploymentService,
    DeploymentSupersededError,
    DeploymentVerification,
    DeploymentVerificationCheckResult,
    deployment_operation_id,
    deployment_unverified_warning,
    load_deployment_settings,
)
from foundry_opt.poc.foundry import (
    CleanupError,
    DraftReference,
    EvaluationEvidence,
    EvaluationReference,
    HostedDefinition,
    Metric,
    RegularVersionReference,
    RouteFingerprint,
)
from foundry_opt.poc.source import PackagedSource
from foundry_opt.poc.runtime import BOOTSTRAP_RECEIPT_ENV, build_oidc_config


def _configuration() -> tuple[RepositoryPolicy, AgentMetadata]:
    policy = RepositoryPolicy.from_document(
        {
            "schema_version": 1,
            "source_root": "agent",
            "editable_paths": ["agent/**"],
            "min_candidates": 1,
            "max_candidates": 2,
            "baseline_model": "baseline-model",
            "allowed_models": ["baseline-model"],
            "primary_metric": "primary_metric",
            "decision_rules": {
                "minimum_aggregate_delta": 0.10,
                "focused_cases_required": True,
                "max_regressions": 0,
            },
            "hard_guardrails": {"safety": {"required_pass_rate": 1.0, "required": True}},
            "metadata_path": ".foundry/agent-metadata.yaml",
        }
    )
    metadata = AgentMetadata.model_validate(
        {
            "schema_version": 1,
            "repository_identity": "example-org/example-agent",
            "repository_id": 123456789,
            "default_branch": "main",
            "project_endpoint": "https://example.services.ai.azure.com/api/projects/example",
            "foundry_account_resource_id": "/subscriptions/33333333-3333-3333-3333-333333333333/resourceGroups/example-rg/providers/Microsoft.CognitiveServices/accounts/example-account",
            "agent_name": "example-agent",
            "authentication_method": "oidc",
            "static_credentials_allowed": False,
            "hosted_runtime": {
                "kind": "hosted",
                "runtime": "python_3_13",
                "entry_point": ("python", "-m", "agent"),
                "dependency_resolution": "uv",
                "protocol_name": "mcp",
                "protocol_version": "1.0",
                "cpu": "1",
                "memory": "2Gi",
                "model_environment_variable": "MODEL_DEPLOYMENT",
            },
            "oidc": {
                "issuer": "https://token.actions.githubusercontent.com",
                "audience": "api://AzureADTokenExchange",
                "tenant_id": "33333333-3333-3333-3333-333333333333",
                "subscription_id": "33333333-3333-3333-3333-333333333333",
                "repository_id_claim": "123456789",
                "workflow_variables": (
                    {"alias": "development", "name": "AZURE_CLIENT_ID", "value": "11111111-1111-1111-1111-111111111111", "scope": "environment", "environment": "development"},
                    {"alias": "foundry-production", "name": "AZURE_CLIENT_ID", "value": "22222222-2222-2222-2222-222222222222", "scope": "environment", "environment": "foundry-production"},
                ),
                "principals": (
                    {"role": "development", "client_id": "11111111-1111-1111-1111-111111111111", "client_id_variable": "development", "environment": "development", "subjects": ({"name": "environment", "subject": "repo:example-org/example-agent:environment:development"},)},
                    {"role": "deployment", "client_id": "22222222-2222-2222-2222-222222222222", "client_id_variable": "foundry-production", "environment": "foundry-production", "subjects": ({"name": "environment", "subject": "repo:example-org/example-agent:environment:foundry-production"},)},
                ),
            },
            "model_deployments": (
                {
                    "alias": "default",
                    "deployment_name": "baseline-model",
                    "model_format": "OpenAI",
                    "model_name": "gpt-5-mini",
                    "model_version": "1",
                    "required_capabilities": ({"name": "chat", "enabled": True},),
                },
            ),
            "development_evaluation": {
                "name": "development",
                "split": "development",
                "resolved_evaluation_id": "eval-dev",
                "dataset_id": "dataset-dev",
                "custom_evaluator_ids": ("primary_metric", "safety"),
            },
            "validating_evaluation": {
                "name": "validating",
                "split": "validating",
                "resolved_evaluation_id": "eval-val",
                "dataset_id": "dataset-val",
                "custom_evaluator_ids": ("primary_metric", "safety"),
            },
        }
    )
    return policy, metadata



def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _route(version: str = "14") -> RouteFingerprint:
    return RouteFingerprint(
        agent_name="example-agent",
        latest_version=version,
        selector=None,
        endpoint_configuration=None,
        sha256=hashlib.sha256(version.encode("ascii")).hexdigest(),
    )


def _package() -> PackagedSource:
    archive = b"deterministic-source-zip"
    return PackagedSource(
        commit="a" * 40,
        source_root="agent",
        archive_bytes=archive,
        tree_sha256="b" * 64,
        zip_sha256=hashlib.sha256(archive).hexdigest(),
    )


def _repository_check_verification(
    command: str,
    *,
    gate_policy: str = "allow_repository_checks",
) -> DeploymentVerification:
    return DeploymentVerification(
        mode="repository_checks",
        status="planned",
        evaluation_gate_policy=gate_policy,  # type: ignore[arg-type]
        check_results=(
            DeploymentVerificationCheckResult(
                kind="command",
                value=command,
                status="planned",
            ),
        ),
        unverified_deployment=False,
    )


def _unverified_deployment_verification() -> DeploymentVerification:
    return DeploymentVerification(
        mode="none",
        status="unverified",
        evaluation_gate_policy="allow_no_evidence",
        unverified_deployment=True,
        warning=deployment_unverified_warning(),
    )


class _Foundry:
    def __init__(
        self,
        *,
        safety_score: float = 1.0,
        safety_passed: bool = True,
        cleanup_failure: bool = False,
        matching_latest: bool = False,
    ) -> None:
        self.safety_score = safety_score
        self.safety_passed = safety_passed
        self.cleanup_failure = cleanup_failure
        self.matching_latest = matching_latest
        self.calls: list[str] = []
        self.package = _package()
        self.draft = DraftReference(
            agent_name="example-agent",
            version="draft-one",
            ownership_token="draft-owner",
            code_sha256=self.package.zip_sha256,
            route=_route(),
            definition=HostedDefinition(),
            status="active",
        )

    def require_service_managed_latest(
        self,
        agent_name: str,
        *,
        deadline_monotonic: float,
    ) -> RouteFingerprint:
        del deadline_monotonic
        assert agent_name == "example-agent"
        self.calls.append("route")
        return _route(
            "15"
            if self.matching_latest or "publish" in self.calls
            else "14"
        )

    def create_draft(self, *args: object, **kwargs: object) -> DraftReference:
        del args, kwargs
        self.calls.append("draft")
        return self.draft

    def poll_version_active(
        self,
        reference: DraftReference,
        *,
        deadline_monotonic: float,
    ) -> DraftReference:
        del deadline_monotonic
        self.calls.append("draft-active")
        return reference

    def download_code(
        self,
        reference: DraftReference,
        *,
        deadline_monotonic: float,
    ) -> bytes:
        del reference, deadline_monotonic
        self.calls.append("draft-download")
        return self.package.archive_bytes

    def run_evaluation(
        self,
        reference: DraftReference,
        contract: object,
        *,
        deadline_monotonic: float,
    ) -> EvaluationEvidence:
        del contract, deadline_monotonic
        self.calls.append("evaluate")
        return EvaluationEvidence(
            reference=EvaluationReference(
                evaluation_id="eval_development",
                run_id="run-deploy",
                dataset_id="dataset-development",
                agent_name=reference.agent_name,
                agent_version=reference.version,
                evaluator_ids=("primary_metric", "safety"),
            ),
            metrics=(
                Metric(
                    name="primary_metric",
                    score=0.75,
                    passed=True,
                    focused_cases=4,
                    passed_cases=3,
                    failed_cases=1,
                ),
                Metric(
                    name="safety",
                    score=self.safety_score,
                    passed=self.safety_passed,
                    focused_cases=4,
                    passed_cases=4 if self.safety_passed else 3,
                    failed_cases=0 if self.safety_passed else 1,
                ),
            ),
            total_cases=4,
            passed_cases=3,
            failed_cases=1,
            report_url="https://example.invalid/evaluations/run-deploy",
        )

    def delete_exact_owned_version(
        self,
        reference: DraftReference,
        *,
        deadline_monotonic: float,
    ) -> None:
        del deadline_monotonic
        self.calls.append("cleanup")
        if self.cleanup_failure:
            raise CleanupError("cleanup failed", reference=reference)

    def create_regular_version(
        self,
        agent_name: str,
        definition: object,
        archive_bytes: bytes,
        *,
        operation_id: str,
        provenance: dict[str, str],
        description: str,
        deadline_monotonic: float,
    ) -> RegularVersionReference:
        del definition, description, deadline_monotonic
        assert agent_name == "example-agent"
        assert archive_bytes == self.package.archive_bytes
        assert self.calls[-1] == "regular-get"
        self.calls.append("publish")
        code_sha256 = hashlib.sha256(archive_bytes).hexdigest()
        metadata = {
            **provenance,
            "foundry_opt_run_id": operation_id,
            "foundry_opt_release_operation": operation_id,
            "foundry_opt_source_zip_sha256": code_sha256,
        }
        return RegularVersionReference(
            agent_name=agent_name,
            version="15",
            operation_id=operation_id,
            code_sha256=code_sha256,
            metadata=metadata,
            status="creating",
        )

    def get_regular_version(
        self,
        agent_name: str,
        version: str,
        *,
        deadline_monotonic: float,
    ) -> RegularVersionReference:
        del deadline_monotonic
        self.calls.append("regular-get")
        operation_id = (
            "deploy-existing"
            if self.matching_latest
            else "deploy-previous"
        )
        code_sha256 = (
            self.package.zip_sha256
            if self.matching_latest
            else "d" * 64
        )
        return RegularVersionReference(
            agent_name=agent_name,
            version=version,
            operation_id=operation_id,
            code_sha256=code_sha256,
            metadata={
                "foundry_opt_run_id": operation_id,
                "foundry_opt_release_operation": operation_id,
                "foundry_opt_source_zip_sha256": code_sha256,
            },
            status="active",
        )

    def wait_for_regular_version_active(
        self,
        reference: RegularVersionReference,
        *,
        deadline_monotonic: float,
    ) -> RegularVersionReference:
        del deadline_monotonic
        self.calls.append("regular-active")
        return replace(reference, status="active")

    def download_regular_version_code(
        self,
        reference: RegularVersionReference,
        *,
        deadline_monotonic: float,
    ) -> bytes:
        del reference, deadline_monotonic
        self.calls.append("regular-download")
        return self.package.archive_bytes

    def assert_regular_version_is_latest(
        self,
        reference: RegularVersionReference,
        *,
        deadline_monotonic: float,
    ) -> RouteFingerprint:
        del deadline_monotonic
        self.calls.append("latest")
        return _route(reference.version)


def test_deployment_validates_draft_before_regular_publication() -> None:
    policy, metadata = _configuration()
    foundry = _Foundry()
    service = DeploymentService(
        client=foundry,
        policy=policy,
        metadata=metadata,
        deadline_seconds=30,
    )

    receipt = service.publish(
        repository="example-org/example-agent",
        release_commit="a" * 40,
        packaged=foundry.package,
    )

    assert receipt.published_version == "15"
    assert receipt.previous_version == "14"
    assert receipt.route_mutated is False
    assert receipt.latest_verified is True
    assert receipt.verification.mode == "foundry_evaluation"
    assert receipt.verification.status == "passed"
    assert receipt.guardrails[0].passed is True
    assert foundry.calls.index("cleanup") < foundry.calls.index("publish")


def test_deployment_verify_only_runs_foundry_gate_without_publication() -> None:
    policy, metadata = _configuration()
    foundry = _Foundry()
    service = DeploymentService(
        client=foundry,
        policy=policy,
        metadata=metadata,
        deadline_seconds=30,
    )

    receipt = service.verify(
        repository="example-org/example-agent",
        release_commit="a" * 40,
        packaged=foundry.package,
    )

    assert receipt.status == "verified"
    assert receipt.published is False
    assert receipt.route_mutated is False
    assert receipt.verification.mode == "foundry_evaluation"
    assert receipt.verification.status == "passed"
    assert receipt.verification.guardrails[0].passed is True
    assert "publish" not in foundry.calls
    assert "regular-get" not in foundry.calls
    assert foundry.calls.index("cleanup") > foundry.calls.index("evaluate")


def test_deployment_guardrail_failure_cleans_draft_and_does_not_publish() -> None:
    policy, metadata = _configuration()
    foundry = _Foundry(safety_score=0.75, safety_passed=False)
    service = DeploymentService(
        client=foundry,
        policy=policy,
        metadata=metadata,
        deadline_seconds=30,
    )

    with pytest.raises(DeploymentGuardrailError, match="safety"):
        service.publish(
            repository="example-org/example-agent",
            release_commit="a" * 40,
            packaged=foundry.package,
        )

    assert "cleanup" in foundry.calls
    assert "publish" not in foundry.calls


def test_deployment_verify_only_reports_failed_guardrails_after_cleanup() -> None:
    policy, metadata = _configuration()
    foundry = _Foundry(safety_score=0.75, safety_passed=False)
    service = DeploymentService(
        client=foundry,
        policy=policy,
        metadata=metadata,
        deadline_seconds=30,
    )

    with pytest.raises(DeploymentGuardrailError) as error:
        service.verify(
            repository="example-org/example-agent",
            release_commit="a" * 40,
            packaged=foundry.package,
        )

    assert error.value.verification.status == "failed"
    assert error.value.verification.guardrails[0].passed is False
    assert "cleanup" in foundry.calls
    assert "publish" not in foundry.calls


def test_deployment_cleanup_failure_prevents_regular_publication() -> None:
    policy, metadata = _configuration()
    foundry = _Foundry(cleanup_failure=True)
    service = DeploymentService(
        client=foundry,
        policy=policy,
        metadata=metadata,
        deadline_seconds=30,
    )

    with pytest.raises(CleanupError):
        service.publish(
            repository="example-org/example-agent",
            release_commit="a" * 40,
            packaged=foundry.package,
        )

    assert "publish" not in foundry.calls


def test_deployment_rechecks_main_after_draft_validation() -> None:
    policy, metadata = _configuration()
    foundry = _Foundry()

    def superseded(release_commit: str) -> None:
        assert release_commit == "a" * 40
        raise DeploymentSupersededError(
            release_commit=release_commit,
            current_main_commit="c" * 40,
        )

    service = DeploymentService(
        client=foundry,
        policy=policy,
        metadata=metadata,
        deadline_seconds=30,
        freshness_check=superseded,
    )

    with pytest.raises(DeploymentSupersededError):
        service.publish(
            repository="example-org/example-agent",
            release_commit="a" * 40,
            packaged=foundry.package,
        )

    assert "cleanup" in foundry.calls
    assert "publish" not in foundry.calls


def test_deployment_reconciles_unchanged_latest_source_without_new_version() -> None:
    policy, metadata = _configuration()
    foundry = _Foundry(matching_latest=True)
    service = DeploymentService(
        client=foundry,
        policy=policy,
        metadata=metadata,
        deadline_seconds=30,
    )

    receipt = service.publish(
        repository="example-org/example-agent",
        release_commit="a" * 40,
        packaged=foundry.package,
    )

    assert receipt.published_version == "15"
    assert receipt.reconciled is True
    assert receipt.operation_id == "deploy-existing"
    assert "regular-get" in foundry.calls
    assert "regular-download" in foundry.calls
    assert "publish" not in foundry.calls


def test_deployment_runs_repository_checks_before_publish(
    tmp_path: Path,
) -> None:
    policy, metadata = _configuration()
    foundry = _Foundry()
    service = DeploymentService(
        client=foundry,
        policy=policy,
        metadata=metadata,
        deadline_seconds=30,
    )

    receipt = service.publish(
        repository="example-org/example-agent",
        release_commit="a" * 40,
        packaged=foundry.package,
        repository_root=tmp_path,
        verification=_repository_check_verification(
            "python -c \"print('deployment-check')\""
        ),
    )

    assert receipt.verification.mode == "repository_checks"
    assert receipt.verification.status == "passed"
    assert receipt.verification.check_results[0].status == "passed"
    assert receipt.verification.evaluator_ids == ()
    assert "draft" not in foundry.calls
    assert "evaluate" not in foundry.calls
    assert "cleanup" not in foundry.calls


def test_deployment_blocks_failed_repository_checks(tmp_path: Path) -> None:
    policy, metadata = _configuration()
    foundry = _Foundry()
    service = DeploymentService(
        client=foundry,
        policy=policy,
        metadata=metadata,
        deadline_seconds=30,
    )

    with pytest.raises(DeploymentRepositoryChecksError) as error:
        service.publish(
            repository="example-org/example-agent",
            release_commit="a" * 40,
            packaged=foundry.package,
            repository_root=tmp_path,
            verification=_repository_check_verification(
                "python -c \"raise SystemExit(1)\""
            ),
        )

    assert error.value.verification.status == "failed"
    assert error.value.verification.check_results[0].status == "failed"
    assert "publish" not in foundry.calls


def test_deployment_blocks_failed_repository_checks_when_allow_no_evidence(
    tmp_path: Path,
) -> None:
    policy, metadata = _configuration()
    foundry = _Foundry()
    service = DeploymentService(
        client=foundry,
        policy=policy,
        metadata=metadata,
        deadline_seconds=30,
    )

    with pytest.raises(DeploymentRepositoryChecksError) as error:
        service.publish(
            repository="example-org/example-agent",
            release_commit="a" * 40,
            packaged=foundry.package,
            repository_root=tmp_path,
            verification=_repository_check_verification(
                "python -c \"raise SystemExit(1)\"",
                gate_policy="allow_no_evidence",
            ),
        )

    assert error.value.verification.evaluation_gate_policy == "allow_no_evidence"
    assert error.value.verification.status == "failed"
    assert error.value.verification.check_results[0].status == "failed"
    assert "publish" not in foundry.calls


def test_deployment_blocks_missing_repository_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy, metadata = _configuration()
    foundry = _Foundry()
    service = DeploymentService(
        client=foundry,
        policy=policy,
        metadata=metadata,
        deadline_seconds=30,
    )
    verification = DeploymentVerification(
        mode="repository_checks",
        status="planned",
        evaluation_gate_policy="allow_repository_checks",
        check_results=(
            DeploymentVerificationCheckResult(
                kind="check",
                value="CI / unit-tests",
                status="planned",
            ),
        ),
        unverified_deployment=False,
    )

    monkeypatch.setattr(
        deploy_module,
        "_evaluate_repository_check",
        lambda **kwargs: kwargs["check"].model_copy(
            update={
                "status": "unverified",
                "detail": (
                    "required GitHub check was not found for the exact source commit"
                ),
            }
        ),
    )

    with pytest.raises(DeploymentRepositoryChecksError) as error:
        service.publish(
            repository="example-org/example-agent",
            release_commit="a" * 40,
            packaged=foundry.package,
            repository_root=tmp_path,
            verification=verification,
        )

    assert error.value.verification.status == "unverified"
    assert error.value.verification.check_results[0].status == "unverified"
    assert "publish" not in foundry.calls


def test_deployment_can_publish_without_verification_evidence_with_warning(
    tmp_path: Path,
) -> None:
    policy, metadata = _configuration()
    foundry = _Foundry()
    service = DeploymentService(
        client=foundry,
        policy=policy,
        metadata=metadata,
        deadline_seconds=30,
    )

    receipt = service.publish(
        repository="example-org/example-agent",
        release_commit="a" * 40,
        packaged=foundry.package,
        repository_root=tmp_path,
        verification=_unverified_deployment_verification(),
    )

    assert receipt.verification.mode == "none"
    assert receipt.verification.status == "unverified"
    assert receipt.verification.unverified_deployment is True
    assert receipt.verification.warning is not None
    assert receipt.verification.warning.code == "deployment-unverified"
    assert receipt.evaluation_link is None
    assert receipt.guardrails == ()
    assert "draft" not in foundry.calls
    assert "evaluate" not in foundry.calls
    assert "cleanup" not in foundry.calls


def test_deployment_operation_id_is_stable_per_commit() -> None:
    first = deployment_operation_id(
        repository="example-org/example-agent",
        agent_name="example-agent",
        release_commit="a" * 40,
    )
    second = deployment_operation_id(
        repository="example-org/example-agent",
        agent_name="example-agent",
        release_commit="a" * 40,
    )
    other = deployment_operation_id(
        repository="example-org/example-agent",
        agent_name="example-agent",
        release_commit="b" * 40,
    )

    assert first == second
    assert first != other


def test_deployment_settings_select_exact_environment_oidc_principal(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.invalid")
    (repository / ".github").mkdir()
    (repository / ".foundry").mkdir()
    (repository / "agent").mkdir()
    (repository / ".github" / "foundry-opt.lock.yml").write_text(
        "\n".join((
            "schema_version: 1",
            "repository_url: https://github.com/example/foundry-shared.git",
            f"commit: '{'a' * 40}'",
            "package_path: src/foundry_opt/poc",
            "skill_path: skills/foundry-agent-optimizer",
            f"uv_lock_sha256: '{'b' * 64}'",
            "",
        )),
        encoding="utf-8",
    )
    (repository / ".github" / "foundry-optimizer.yaml").write_text(
        "\n".join((
            "schema_version: 1",
            "source_root: agent",
            "editable_paths:",
            "  - agent/**",
            "min_candidates: 1",
            "max_candidates: 2",
            "baseline_model: baseline-model",
            "allowed_models:",
            "  - baseline-model",
            "primary_metric: primary_metric",
            "decision_rules:",
            "  minimum_aggregate_delta: 0.10",
            "  focused_cases_required: true",
            "  max_regressions: 0",
            "hard_guardrails:",
            "  safety:",
            "    required_pass_rate: 1.0",
            "    required: true",
            "metadata_path: .foundry/agent-metadata.yaml",
            "",
        )),
        encoding="utf-8",
    )
    (repository / ".foundry" / "agent-metadata.yaml").write_text(
        "\n".join((
            "schema_version: 1",
            "repository_identity: example-org/example-agent",
            "repository_id: 123456789",
            "default_branch: main",
            "project_endpoint: https://example.services.ai.azure.com/api/projects/example",
            "foundry_account_resource_id: /subscriptions/33333333-3333-3333-3333-333333333333/resourceGroups/example-rg/providers/Microsoft.CognitiveServices/accounts/example-account",
            "agent_name: example-agent",
            "authentication_method: oidc",
            "static_credentials_allowed: false",
            "hosted_runtime:",
            "  kind: hosted",
            "  runtime: python_3_13",
            "  entry_point:",
            "    - python",
            "    - -m",
            "    - agent",
            "  dependency_resolution: uv",
            "  protocol_name: mcp",
            "  protocol_version: '1.0'",
            "  cpu: '1'",
            "  memory: 2Gi",
            "  model_environment_variable: MODEL_DEPLOYMENT",
            "oidc:",
            "  issuer: https://token.actions.githubusercontent.com",
            "  audience: api://AzureADTokenExchange",
            "  tenant_id: 33333333-3333-3333-3333-333333333333",
            "  subscription_id: 33333333-3333-3333-3333-333333333333",
            "  repository_id_claim: '123456789'",
            "  workflow_variables:",
            "    - alias: development",
            "      name: AZURE_CLIENT_ID",
            "      value: 11111111-1111-1111-1111-111111111111",
            "      scope: environment",
            "      environment: development",
            "    - alias: foundry-production",
            "      name: AZURE_CLIENT_ID",
            "      value: 22222222-2222-2222-2222-222222222222",
            "      scope: environment",
            "      environment: foundry-production",
            "  principals:",
            "    - role: development",
            "      client_id: 11111111-1111-1111-1111-111111111111",
            "      client_id_variable: development",
            "      environment: development",
            "      subjects:",
            "        - name: environment",
            "          subject: repo:example-org/example-agent:environment:development",
            "    - role: deployment",
            "      client_id: 22222222-2222-2222-2222-222222222222",
            "      client_id_variable: foundry-production",
            "      environment: foundry-production",
            "      subjects:",
            "        - name: environment",
            "          subject: repo:example-org/example-agent:environment:foundry-production",
            "model_deployments:",
            "  - alias: default",
            "    deployment_name: baseline-model",
            "    model_format: OpenAI",
            "    model_name: gpt-5-mini",
            "    model_version: '1'",
            "    required_capabilities:",
            "      - name: chat",
            "        enabled: true",
            "development_evaluation:",
            "  name: development",
            "  split: development",
            "  resolved_evaluation_id: eval-dev",
            "  dataset_id: dataset-dev",
            "  custom_evaluator_ids:",
            "    - primary_metric",
            "    - safety",
            "validating_evaluation:",
            "  name: validating",
            "  split: validating",
            "  resolved_evaluation_id: eval-val",
            "  dataset_id: dataset-val",
            "  custom_evaluator_ids:",
            "    - primary_metric",
            "    - safety",
            "",
        )),
        encoding="utf-8",
    )
    (repository / "agent" / "main.py").write_text(
        "print('agent')\n",
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "target")
    head = _git(repository, "rev-parse", "HEAD")

    pin = load_shared_pin(repository / ".github" / "foundry-opt.lock.yml")
    receipt_path = tmp_path / "receipt.json"
    write_bootstrap_receipt(
        receipt_path,
        BootstrapReceipt.create(
            repository=pin.repository_url,
            commit=pin.commit,
            package_path=pin.package_path,
            skill_path=pin.skill_path,
            lock_sha256=pin.uv_lock_sha256,
            checkout_root=str((tmp_path / "shared").resolve()),
        ),
    )
    environment = {
        BOOTSTRAP_RECEIPT_ENV: str(receipt_path),
        "GITHUB_REPOSITORY": "example-org/example-agent",
        "GITHUB_REPOSITORY_ID": "123456789",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
        "RUNNER_TEMP": str(tmp_path / "runner"),
    }

    settings = load_deployment_settings(
        repository,
        environment=environment,
        release_commit=head,
    )
    oidc = build_oidc_config(settings.metadata, role="deployment")

    assert isinstance(settings, DeploymentSettings)
    assert settings.release_commit == head
    assert settings.deployment_environment == "foundry-production"
    assert oidc.client_id == "22222222-2222-2222-2222-222222222222"
    assert oidc.expected_subject.endswith(":environment:foundry-production")
