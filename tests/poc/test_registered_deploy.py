from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from foundry_opt.repository_contracts import (
    ActivationBinding,
    BootstrapSidecar,
    DefaultEvaluatorBundle,
    EvaluationLineage,
    EvaluatorNormalization,
    EvaluatorReference,
    HardGuardrail,
    ImmutableDatasetReference,
    ImmutableDefinitionReference,
    ResolvedEvaluator,
    ResolvedWeightedObjective,
    VerificationBundle,
    VerificationSettings,
)
from foundry_opt.poc.deploy import (
    DeploymentGuardrail,
    DeploymentReceipt,
    DeploymentVerificationReceipt,
    PACKAGE_FINGERPRINT_METADATA_KEY,
    PROFILE_FINGERPRINT_METADATA_KEY,
    REGISTRY_FINGERPRINT_METADATA_KEY,
    REPO_AGENT_ID_METADATA_KEY,
    SOURCE_FINGERPRINT_METADATA_KEY,
    TARGET_FINGERPRINT_METADATA_KEY,
    load_registered_deployment_settings,
    load_registered_verification_settings,
    publish_registered_deployment,
    verify_registered_deployment,
)
from foundry_opt.poc.runtime import RuntimeIntegrationError
from foundry_opt.poc.source import package_git_source
from foundry_opt.verification import VerificationCheckSpec


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_ROOT = (
    REPOSITORY_ROOT / "src" / "foundry_opt" / "templates" / "customer-repo"
)
CLIENT_ID = "44444444-4444-4444-4444-444444444444"
TENANT_ID = "22222222-2222-2222-2222-222222222222"
SUBSCRIPTION_ID = "33333333-3333-3333-3333-333333333333"


def _evaluated_sidecar() -> BootstrapSidecar:
    profile = BootstrapSidecar.from_document(
        (TEMPLATE_ROOT / "agent" / ".foundry" / "foundry-opt.yaml").read_text(
            encoding="utf-8"
        )
    )
    objective = ResolvedWeightedObjective.create(
        (
            ResolvedEvaluator(
                reference=EvaluatorReference(
                    evaluator_id="azureai://built-in/evaluators/safety",
                    provenance="reused_existing",
                ),
                normalization=EvaluatorNormalization(kind="pass_fail"),
                weight=1.0,
            ),
        )
    )
    development = ImmutableDatasetReference(
        dataset_id="azureai://accounts/example/projects/example/data/development/versions/1"
    )
    validating = ImmutableDatasetReference(
        dataset_id="azureai://accounts/example/projects/example/data/validating/versions/1"
    )
    development_definition = ImmutableDefinitionReference(
        definition_id="eval_development"
    )
    validating_definition = ImmutableDefinitionReference(
        definition_id="eval_validating"
    )
    bundle = VerificationBundle(
        development_dataset=development,
        validating_dataset=validating,
        development_definition=development_definition,
        validating_definition=validating_definition,
        default_evaluator_bundle=DefaultEvaluatorBundle(
            objective=objective,
            datasets=(development, validating),
            definitions=(development_definition, validating_definition),
        ),
    )
    lineage = EvaluationLineage(
        split_algorithm_version="v1",
        split_hash="a" * 64,
        split_lineage_hash="b" * 64,
        development_case_count=20,
        validating_case_count=10,
        dataset_strategy="synthetic_only",
        generation_context_fingerprint="c" * 64,
        evaluator_provenance="reused_existing",
        bundle_objective_hash=objective.objective_hash,
        activation_binding=ActivationBinding(
            operation_id="test-activation",
            plan_hash="d" * 64,
            approval_hash="e" * 64,
            receipt_hash="f" * 64,
            runtime_commit="1" * 40,
            finalization_hash="2" * 64,
        ),
    )
    return profile.model_copy(
        update={
            "verification": VerificationSettings(
                mode="required",
                evaluation_gate_policy="require_foundry_evaluation",
                bundle=bundle,
                lineage=lineage,
            )
        }
    )


def _split_evaluated_sidecar() -> BootstrapSidecar:
    profile = _evaluated_sidecar()
    development_evaluator_ids = (
        "advisory_safety_7124618c-5a0d-49b0-a9dc-ad55e4c32030",
        "policy_coverage_9d3e2d8b-81e6-436b-96a3-b46a46ef6dce",
        "azureml://registries/azureml/evaluators/builtin.task_completion/versions/19",
    )
    validating_evaluator_ids = (
        "advisory_safety_4cef6e56-2b2e-4150-9331-da56485dac56",
        "policy_coverage_030f008b-0351-4ae3-8d6b-bb112ffee5c4",
        "azureml://registries/azureml/evaluators/builtin.task_completion/versions/19",
    )
    objective = ResolvedWeightedObjective.create(
        tuple(
            ResolvedEvaluator(
                reference=EvaluatorReference(
                    evaluator_id=evaluator_id,
                    provenance="reused_existing",
                ),
                normalization=EvaluatorNormalization(kind="pass_fail"),
                weight=1.0,
            )
            for evaluator_id in development_evaluator_ids
        )
    )
    current_bundle = profile.require_verification_bundle(
        detail="fixture requires a verification bundle"
    )
    bundle = current_bundle.model_copy(
        update={
            "default_evaluator_bundle": current_bundle.default_evaluator_bundle.model_copy(
                update={"objective": objective}
            ),
            "development_evaluator_ids": development_evaluator_ids,
            "validating_evaluator_ids": validating_evaluator_ids,
        }
    )
    current_lineage = profile.verification.lineage
    assert current_lineage is not None
    document = profile.model_dump(mode="json")
    document["hard_guardrails"] = [
        HardGuardrail(
            evaluator_name="advisory_safety",
            required_pass_rate=1.0,
        ).model_dump(mode="json")
    ]
    document["verification"] = VerificationSettings(
        mode="required",
        evaluation_gate_policy="require_foundry_evaluation",
        bundle=bundle,
        lineage=current_lineage.model_copy(
            update={"bundle_objective_hash": objective.objective_hash}
        ),
    ).model_dump(mode="json")
    return BootstrapSidecar.from_document(document)


def _repository_checks_sidecar(
    *,
    evaluation_gate_policy: str = "allow_repository_checks",
) -> BootstrapSidecar:
    profile = BootstrapSidecar.from_document(
        (TEMPLATE_ROOT / "agent" / ".foundry" / "foundry-opt.yaml").read_text(
            encoding="utf-8"
        )
    )
    return profile.model_copy(
        update={
            "verification": VerificationSettings(
                mode="optional",
                repository_checks=(
                    VerificationCheckSpec(
                        kind="command",
                        value="python -c \"print('deployment-check')\"",
                    ),
                ),
                evaluation_gate_policy=evaluation_gate_policy,  # type: ignore[arg-type]
            )
        }
    )


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _registered_repository(
    tmp_path: Path,
    *,
    immutable_subject: bool = False,
    sidecar: BootstrapSidecar | None = None,
) -> tuple[Path, str, dict[str, str]]:
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
    registry["schema_version"] = 2
    registry["distribution"].update(
        {
            "package_path": ".",
            "uv_lock_sha256": "b" * 64,
            "optimizer_skill_path": "plugins/foundry-agent-optimizer",
        }
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
    if immutable_subject:
        registry["github"]["oidc_subject_prefix"] = (
            "repo:example-org@987654321/example-repo@123456789"
        )
    (repository / ".foundry-opt" / "registry.yaml").write_text(
        yaml.safe_dump(registry, sort_keys=False),
        encoding="utf-8",
    )
    selected_sidecar = _evaluated_sidecar() if sidecar is None else sidecar
    (repository / "agent" / ".foundry" / "foundry-opt.yaml").write_text(
        yaml.safe_dump(
            selected_sidecar.model_dump(mode="json"),
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    _git(repository, "init")
    _git(repository, "config", "user.email", "test@example.invalid")
    _git(repository, "config", "user.name", "Test")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "fixture")
    commit = _git(repository, "rev-parse", "HEAD")
    event_path = tmp_path / "github-event.json"
    event_path.write_text(
        json.dumps(
            {
                "repository": {
                    "default_branch": "main",
                    "full_name": "example-org/example-repo",
                    "id": 123456789,
                }
            }
        ),
        encoding="utf-8",
    )
    environment = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "push",
        "GITHUB_EVENT_PATH": str(event_path),
        "GITHUB_REPOSITORY": "example-org/example-repo",
        "GITHUB_REPOSITORY_ID": "123456789",
        "GITHUB_REPOSITORY_OWNER_ID": "987654321",
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
    assert settings.verification.mode == "foundry_evaluation"
    assert settings.oidc_config.expected_subject == (
        "repo:example-org/example-repo:environment:foundry-production"
    )
    assert "violence" in settings.metadata.development_evaluation.custom_evaluator_ids
    assert settings.selection.sidecar.default_evaluator_bundle.objective.objective_hash


def test_registered_settings_preserve_split_specific_evaluator_ids(
    tmp_path: Path,
) -> None:
    sidecar = _split_evaluated_sidecar()
    repository, commit, environment = _registered_repository(
        tmp_path,
        sidecar=sidecar,
    )

    settings = load_registered_deployment_settings(
        repository,
        repo_agent_id="example-agent",
        exact_source=commit,
        environment=environment,
    )

    assert (
        settings.metadata.development_evaluation.custom_evaluator_ids
        == sidecar.verification.bundle.resolved_development_evaluator_ids
    )
    assert (
        settings.metadata.validating_evaluation.custom_evaluator_ids
        == sidecar.verification.bundle.resolved_validating_evaluator_ids
    )


def test_registered_settings_support_repository_checks_without_bundle(
    tmp_path: Path,
) -> None:
    repository, commit, environment = _registered_repository(
        tmp_path,
        sidecar=_repository_checks_sidecar(),
    )

    settings = load_registered_deployment_settings(
        repository,
        repo_agent_id="example-agent",
        exact_source=commit,
        environment=environment,
    )

    assert settings.verification.mode == "repository_checks"
    assert settings.verification.evaluator_ids == ()
    assert settings.verification.check_results[0].status == "planned"
    assert settings.metadata.development_evaluation.custom_evaluator_ids == ()
    assert settings.reconciliation_metadata[REPO_AGENT_ID_METADATA_KEY] == "example-agent"
    for key in (
        SOURCE_FINGERPRINT_METADATA_KEY,
        PACKAGE_FINGERPRINT_METADATA_KEY,
        PROFILE_FINGERPRINT_METADATA_KEY,
        REGISTRY_FINGERPRINT_METADATA_KEY,
        TARGET_FINGERPRINT_METADATA_KEY,
    ):
        assert len(settings.reconciliation_metadata[key]) == 64


def test_registered_settings_support_repository_checks_when_allow_no_evidence(
    tmp_path: Path,
) -> None:
    repository, commit, environment = _registered_repository(
        tmp_path,
        sidecar=_repository_checks_sidecar(
            evaluation_gate_policy="allow_no_evidence"
        ),
    )

    settings = load_registered_deployment_settings(
        repository,
        repo_agent_id="example-agent",
        exact_source=commit,
        environment=environment,
    )

    assert settings.verification.mode == "repository_checks"
    assert settings.verification.evaluation_gate_policy == "allow_no_evidence"
    assert settings.verification.check_results[0].status == "planned"


def test_registered_settings_allow_unverified_publication_without_bundle(
    tmp_path: Path,
) -> None:
    repository, commit, environment = _registered_repository(
        tmp_path,
        sidecar=BootstrapSidecar.from_document(
            (TEMPLATE_ROOT / "agent" / ".foundry" / "foundry-opt.yaml").read_text(
                encoding="utf-8"
            )
        ),
    )

    settings = load_registered_deployment_settings(
        repository,
        repo_agent_id="example-agent",
        exact_source=commit,
        environment=environment,
    )

    assert settings.verification.mode == "none"
    assert settings.verification.unverified_deployment is True
    assert settings.verification.warning is not None
    assert settings.verification.warning.code == "deployment-unverified"
    assert settings.metadata.development_evaluation.custom_evaluator_ids == ()


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


def test_registered_settings_accept_immutable_github_subjects(
    tmp_path: Path,
) -> None:
    repository, commit, environment = _registered_repository(
        tmp_path,
        immutable_subject=True,
    )

    settings = load_registered_deployment_settings(
        repository,
        repo_agent_id="example-agent",
        exact_source=commit,
        environment=environment,
    )

    expected = (
        "repo:example-org@987654321/example-repo@123456789:"
        "environment:foundry-production"
    )
    assert settings.oidc_config.expected_subject == expected
    principal = settings.metadata.oidc.principals[0]
    assert principal.subjects[0].subject == expected


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


def test_registered_verification_settings_accept_pull_request_context(
    tmp_path: Path,
) -> None:
    repository, commit, environment = _registered_repository(tmp_path)
    environment.update(
        {
            "GITHUB_REF": "refs/pull/17/merge",
            "GITHUB_REF_NAME": "17/merge",
            "GITHUB_BASE_REF": "release",
            "GITHUB_EVENT_NAME": "pull_request",
        }
    )

    settings = load_registered_verification_settings(
        repository,
        repo_agent_id="example-agent",
        exact_source=commit,
        environment=environment,
    )

    assert settings.release_commit == commit
    assert settings.metadata.default_branch == "main"


def test_registered_publish_settings_reject_feature_branch_dispatch(
    tmp_path: Path,
) -> None:
    repository, commit, environment = _registered_repository(tmp_path)
    environment.update(
        {
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/feature/provenance",
            "GITHUB_REF_NAME": "feature/provenance",
        }
    )

    with pytest.raises(RuntimeIntegrationError, match="default branch"):
        load_registered_deployment_settings(
            repository,
            repo_agent_id="example-agent",
            exact_source=commit,
            environment=environment,
        )


def test_registered_publish_settings_reject_pull_request_context(
    tmp_path: Path,
) -> None:
    repository, commit, environment = _registered_repository(tmp_path)
    environment.update(
        {
            "GITHUB_REF": "refs/pull/17/merge",
            "GITHUB_REF_NAME": "17/merge",
            "GITHUB_BASE_REF": "main",
        }
    )

    with pytest.raises(RuntimeIntegrationError, match="exact branch push ref"):
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


def test_registered_reconciliation_uses_exact_commit_with_dirty_worktree(
    tmp_path: Path,
) -> None:
    repository, commit, environment = _registered_repository(tmp_path)
    clean = load_registered_deployment_settings(
        repository,
        repo_agent_id="example-agent",
        exact_source=commit,
        environment=environment,
    )
    (repository / "agent" / "main.py").write_text(
        "print('dirty worktree must not deploy')\n",
        encoding="utf-8",
    )

    dirty = load_registered_deployment_settings(
        repository,
        repo_agent_id="example-agent",
        exact_source=commit,
        environment=environment,
    )

    assert dirty.reconciliation_metadata == clean.reconciliation_metadata


def test_registered_merge_sha_with_identical_package_remains_a_noop(
    tmp_path: Path,
) -> None:
    repository, original_commit, environment = _registered_repository(tmp_path)
    original = load_registered_deployment_settings(
        repository,
        repo_agent_id="example-agent",
        exact_source=original_commit,
        environment=environment,
    )
    original_package = package_git_source(
        repository,
        commit=original_commit,
        source_root=original.policy.source_root,
        work_root=tmp_path / "original-package",
    )
    main_branch = _git(repository, "branch", "--show-current")
    _git(repository, "checkout", "-b", "feature/noop")
    _git(repository, "commit", "--allow-empty", "-m", "feature")
    _git(repository, "checkout", main_branch)
    _git(repository, "commit", "--allow-empty", "-m", "main")
    _git(repository, "merge", "--no-ff", "feature/noop", "-m", "merge feature")
    merge_commit = _git(repository, "rev-parse", "HEAD")
    environment["GITHUB_SHA"] = merge_commit
    for relative in (
        ".foundry-opt/registry.yaml",
        "agent/.foundry/foundry-opt.yaml",
    ):
        path = repository / relative
        content = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        path.write_bytes(content.replace(b"\n", b"\r\n"))

    merged = load_registered_deployment_settings(
        repository,
        repo_agent_id="example-agent",
        exact_source=merge_commit,
        environment=environment,
    )
    merged_package = package_git_source(
        repository,
        commit=merge_commit,
        source_root=merged.policy.source_root,
        work_root=tmp_path / "merged-package",
    )

    assert merged_package.archive_bytes == original_package.archive_bytes
    assert merged.reconciliation_metadata == original.reconciliation_metadata


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
            repository_root: object,
            verification: object,
            reconciliation_metadata: object,
        ) -> DeploymentReceipt:
            assert repository == "example-org/example-repo"
            assert release_commit == commit
            assert getattr(packaged, "commit") == commit
            assert getattr(packaged, "source_root") == "agent"
            assert repository_root == settings.repository_root
            assert getattr(verification, "mode") == "foundry_evaluation"
            assert reconciliation_metadata == settings.reconciliation_metadata
            completed_verification = settings.verification.model_copy(
                update={
                    "status": "passed",
                    "evaluation_link": "https://example.invalid/evaluations/deploy",
                    "guardrails": (
                        DeploymentGuardrail(
                            name="violence",
                            score=1.0,
                            required_pass_rate=1.0,
                            passed=True,
                        ),
                    ),
                }
            )
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
                reconciliation_metadata=settings.reconciliation_metadata,
                evaluation_link="https://example.invalid/evaluations/deploy",
                guardrails=completed_verification.guardrails,
                verification=completed_verification,
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


def test_registered_verify_packages_exact_source_and_closes_clients(
    tmp_path: Path,
) -> None:
    repository, commit, environment = _registered_repository(tmp_path)
    environment.update(
        {
            "GITHUB_REF": "refs/pull/17/merge",
            "GITHUB_REF_NAME": "17/merge",
            "GITHUB_BASE_REF": "main",
        }
    )
    settings = load_registered_verification_settings(
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
            assert kwargs["freshness_check"] is None

        def verify(
            self,
            *,
            repository: str,
            release_commit: str,
            packaged: object,
            repository_root: object,
            verification: object,
        ) -> DeploymentVerificationReceipt:
            assert repository == "example-org/example-repo"
            assert release_commit == commit
            assert getattr(packaged, "commit") == commit
            assert getattr(packaged, "source_root") == "agent"
            assert repository_root == settings.repository_root
            assert getattr(verification, "mode") == "foundry_evaluation"
            completed_verification = settings.verification.model_copy(
                update={
                    "status": "passed",
                    "evaluation_link": "https://example.invalid/evaluations/deploy",
                    "guardrails": (
                        DeploymentGuardrail(
                            name="violence",
                            score=1.0,
                            required_pass_rate=1.0,
                            passed=True,
                        ),
                    ),
                }
            )
            return DeploymentVerificationReceipt(
                repository=repository,
                release_commit=release_commit,
                project_endpoint=settings.metadata.project_endpoint,
                agent_name=settings.metadata.agent_name,
                operation_id="deploy-verified",
                source_root="agent",
                source_tree_sha256=getattr(packaged, "tree_sha256"),
                source_zip_sha256=getattr(packaged, "zip_sha256"),
                verification=completed_verification,
            )

    receipt = verify_registered_deployment(
        settings,
        environment=environment,
        credential_builder=lambda *_args, **_kwargs: Credential(),
        evaluation_backend_factory=lambda **_kwargs: object(),
        foundry_client_factory=lambda *_args, **_kwargs: Client(),
        service_factory=Service,
    )

    assert receipt.published is False
    assert receipt.verification.status == "passed"
    assert receipt.repo_agent_id == "example-agent"
    assert receipt.config_path == "agent/.foundry/foundry-opt.yaml"
    assert closed == ["client", "credential"]
