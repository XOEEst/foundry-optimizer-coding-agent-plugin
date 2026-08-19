from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any, Callable

import pytest

from foundry_opt.poc.bootstrap import BootstrapReceipt, write_bootstrap_receipt
from foundry_opt.poc.candidate import CandidateWorkspace
from foundry_opt.poc.controller import OptimizeJobController
from foundry_opt.poc.evidence import (
    RenderedComment,
    baseline_marker_id,
    candidate_marker_id,
    final_marker_id,
)
from foundry_opt.poc.foundry import (
    DraftReference,
    EvaluationEvidence,
    EvaluationReference,
    HostedDefinition,
    Metric,
    RouteDriftError,
    RouteFingerprint,
    ServiceError,
)
from foundry_opt.poc.github import (
    BrokerRemoteError,
    BrokerUnavailableError,
    CommentReceipt,
    FinalDecision,
    PullRequestReceipt,
)
from foundry_opt.poc.runtime import (
    BOOTSTRAP_RECEIPT_ENV,
    BROKER_SOCKET_ENV,
    DEADLINE_SECONDS_ENV,
    STATE_ROOT_ENV,
    BrokerClosure,
    BrokerIssueComments,
    ControllerFoundryOperations,
    RuntimeIntegrationError,
    RuntimeSidecarStore,
    build_hosted_definition,
    build_job_identity,
    build_runtime_controller,
    capture_route_fingerprint,
    load_runtime_paths,
    load_runtime_settings,
)
from foundry_opt.poc.state import BaselineState, CandidateState, JobIdentity, JobStateStore


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _route(sha256: str = "a" * 64) -> RouteFingerprint:
    return RouteFingerprint(
        agent_name="travel-agent",
        latest_version="7",
        selector={"version_selection_rules": ({"type": "static", "version": "7"},)},
        endpoint_configuration={
            "version_selector": {
                "version_selection_rules": ({"type": "static", "version": "7"},)
            }
        },
        sha256=sha256,
    )


def _evidence(
    *,
    evaluation_id: str,
    dataset_id: str,
    version: str,
    quality_passed: int,
    quality_failed: int,
    quality_score: float,
    safety_passed: int = 4,
    safety_failed: int = 0,
    safety_score: float = 1.0,
) -> EvaluationEvidence:
    return EvaluationEvidence(
        reference=EvaluationReference(
            evaluation_id=evaluation_id,
            run_id=f"run-{evaluation_id}-{version}",
            dataset_id=dataset_id,
            agent_name="travel-agent",
            agent_version=version,
            evaluator_ids=("quality", "safety"),
        ),
        metrics=(
            Metric(
                name="quality",
                score=quality_score,
                passed=quality_failed == 0 and quality_passed > 0,
                focused_cases=quality_passed + quality_failed,
                passed_cases=quality_passed,
                failed_cases=quality_failed,
            ),
            Metric(
                name="safety",
                score=safety_score,
                passed=safety_failed == 0 and safety_passed > 0,
                focused_cases=safety_passed + safety_failed,
                passed_cases=safety_passed,
                failed_cases=safety_failed,
            ),
        ),
        total_cases=quality_passed + quality_failed,
        passed_cases=quality_passed,
        failed_cases=quality_failed,
        report_url=f"https://example.invalid/{evaluation_id}/{version}",
    )


def _create_runtime_repository(tmp_path: Path) -> tuple[Path, str, dict[str, str]]:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.com")
    (repository / "src").mkdir()
    (repository / "tests").mkdir()
    (repository / "src" / "main.py").write_text(
        "VALUE = 'base'\n",
        encoding="utf-8",
    )
    (repository / "tests" / "test_main.py").write_text(
        "def test_base():\n    assert True\n",
        encoding="utf-8",
    )
    (repository / "uv.lock").write_text("lockfile = 1\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "base")
    shared_commit = _git(repository, "rev-parse", "HEAD")

    lock_sha256 = hashlib.sha256((repository / "uv.lock").read_bytes()).hexdigest()
    (repository / ".github").mkdir()
    (repository / ".foundry").mkdir()
    (repository / ".github" / "foundry-opt.lock.yml").write_text(
        "\n".join(
            (
                "schema_version: 1",
                "repository_url: https://github.com/example-org/shared-skill",
                f"commit: {shared_commit}",
                "package_path: .",
                "skill_path: skills/foundry-agent-optimizer",
                f"uv_lock_sha256: {lock_sha256}",
                "",
            )
        ),
        encoding="ascii",
    )
    (repository / ".github" / "foundry-optimizer.yaml").write_text(
        "\n".join(
            (
                "schema_version: 1",
                "source_root: src",
                "editable_paths:",
                "  - src/**",
                "  - tests/**",
                "min_candidates: 1",
                "max_candidates: 2",
                "baseline_model: baseline",
                "allowed_models:",
                "  - baseline",
                "  - candidate",
                "primary_metric: quality",
                "decision_rules:",
                "  minimum_aggregate_delta: 0.05",
                "  focused_cases_required: true",
                "  max_regressions: 0",
                "hard_guardrails:",
                "  safety:",
                "    required_pass_rate: 1.0",
                "metadata_path: .foundry/agent-metadata.yaml",
                "",
            )
        ),
        encoding="ascii",
    )
    (repository / ".foundry" / "agent-metadata.yaml").write_text(
        "\n".join(
            (
                "schema_version: 1",
                "repository_identity: example-org/example-agent",
                "repository_id: 123456789",
                "default_branch: main",
                "project_endpoint: https://example.services.ai.azure.com/api/projects/example",
                "foundry_account_resource_id: /subscriptions/00000000-0000-0000-0000-000000000001/resourceGroups/example/providers/Microsoft.CognitiveServices/accounts/example",
                "agent_name: travel-agent",
                "authentication_method: oidc",
                "static_credentials_allowed: false",
                "hosted_runtime:",
                "  kind: hosted",
                "  runtime: python_3_13",
                "  entry_point:",
                "    - python",
                "    - main.py",
                "  dependency_resolution: remote_build",
                "  protocol_name: responses",
                "  protocol_version: \"2.0.0\"",
                "  cpu: \"0.5\"",
                "  memory: 1Gi",
                "  model_environment_variable: AZURE_AI_MODEL_DEPLOYMENT_NAME",
                "oidc:",
                "  issuer: https://token.actions.githubusercontent.com",
                "  audience: api://AzureADTokenExchange",
                "  tenant_id: 00000000-0000-0000-0000-000000000001",
                "  subscription_id: 00000000-0000-0000-0000-000000000001",
                "  repository_id_claim: \"123456789\"",
                "  workflow_variables:",
                "    - alias: optimizer_client_id",
                "      name: AZURE_OPTIMIZER_CLIENT_ID",
                "      value: 11111111-1111-1111-1111-111111111111",
                "      scope: repository",
                "  principals:",
                "    - role: optimizer",
                "      client_id: 11111111-1111-1111-1111-111111111111",
                "      client_id_variable: optimizer_client_id",
                "      environment: copilot",
                "      subject: repo:example-org/example-agent:environment:copilot",
                "      direct_oidc_subject: repo:example-org/example-agent:environment:copilot",
                "model_deployments:",
                "  - alias: baseline",
                "    deployment_name: dep-baseline",
                "    model_format: OpenAI",
                "    model_name: gpt-5-mini",
                "    model_version: \"1\"",
                "    required_capabilities:",
                "      - name: responses",
                "        enabled: true",
                "  - alias: candidate",
                "    deployment_name: dep-candidate",
                "    model_format: OpenAI",
                "    model_name: gpt-5.6-sol",
                "    model_version: \"1\"",
                "    required_capabilities:",
                "      - name: responses",
                "        enabled: true",
                "development_evaluation:",
                "  name: development",
                "  split: development",
                "  resolved_evaluation_id: eval-development",
                "  dataset_id: dataset-development",
                "  custom_evaluator_ids:",
                "    - quality",
                "    - safety",
                "validating_evaluation:",
                "  name: validating",
                "  split: validating",
                "  resolved_evaluation_id: eval-validating",
                "  dataset_id: dataset-validating",
                "  custom_evaluator_ids:",
                "    - quality",
                "    - safety",
                "",
            )
        ),
        encoding="ascii",
    )
    _git(repository, "add", ".github", ".foundry")
    _git(repository, "commit", "-m", "config")
    base_commit = _git(repository, "rev-parse", "HEAD")

    shared_checkout = tmp_path / "shared-checkout"
    shared_checkout.mkdir()
    receipt_path = tmp_path / "bootstrap-receipt.json"
    write_bootstrap_receipt(
        receipt_path,
        BootstrapReceipt.create(
            repository="https://github.com/example-org/shared-skill",
            commit=shared_commit,
            package_path=".",
            skill_path="skills/foundry-agent-optimizer",
            lock_sha256=lock_sha256,
            checkout_root=str(shared_checkout.resolve()),
        ),
    )
    state_root = tmp_path / "state-root"
    state_root.mkdir()
    broker_socket = tmp_path / "broker.sock"
    environment = {
        BOOTSTRAP_RECEIPT_ENV: str(receipt_path),
        BROKER_SOCKET_ENV: str(broker_socket),
        STATE_ROOT_ENV: str(state_root),
        DEADLINE_SECONDS_ENV: "90",
    }
    return repository, base_commit, environment


class _FakeFoundryClient:
    def __init__(
        self,
        *,
        route: RouteFingerprint,
        evaluations: dict[tuple[str, str], EvaluationEvidence],
        route_after_delete: RouteFingerprint | None = None,
        download_overrides: dict[str, bytes] | None = None,
        delete_failures: dict[str, list[Exception]] | None = None,
        on_poll: Callable[[DraftReference], None] | None = None,
    ) -> None:
        self.route = route
        self.route_after_delete = route_after_delete or route
        self.evaluations = evaluations
        self.download_overrides = {} if download_overrides is None else dict(download_overrides)
        self.delete_failures = (
            {}
            if delete_failures is None
            else {key: list(value) for key, value in delete_failures.items()}
        )
        self.on_poll = on_poll
        self.created: list[dict[str, Any]] = []
        self.polled: list[str] = []
        self.downloaded: list[str] = []
        self.evaluated: list[tuple[str, str]] = []
        self.deleted: list[str] = []
        self._draft_bytes: dict[str, bytes] = {}
        self._counter = 0

    def create_draft(
        self,
        agent_name: str,
        hosted_definition: HostedDefinition,
        code_zip: bytes,
        *,
        deadline_monotonic: float,
    ) -> DraftReference:
        del deadline_monotonic
        self._counter += 1
        version = f"draft-{self._counter}"
        reference = DraftReference(
            agent_name=agent_name,
            version=version,
            ownership_token=f"owned-{self._counter}",
            code_sha256=hashlib.sha256(code_zip).hexdigest(),
            route=self.route,
            definition=HostedDefinition.coerce(hosted_definition),
            service_id=f"{agent_name}:{version}",
            status="creating",
        )
        self.created.append(
            {
                "agent_name": agent_name,
                "definition": reference.definition.as_payload(),
                "code_sha256": reference.code_sha256,
            }
        )
        self._draft_bytes[version] = code_zip
        return reference

    def poll_version_active(
        self,
        reference: DraftReference,
        *,
        deadline_monotonic: float,
        poll_interval_seconds: float = 5.0,
    ) -> DraftReference:
        del deadline_monotonic, poll_interval_seconds
        self.polled.append(reference.version)
        if self.on_poll is not None:
            self.on_poll(reference)
        return DraftReference(
            agent_name=reference.agent_name,
            version=reference.version,
            ownership_token=reference.ownership_token,
            code_sha256=reference.code_sha256,
            route=reference.route,
            definition=reference.definition,
            service_id=reference.service_id,
            status="active",
        )

    def download_code(
        self,
        reference: DraftReference,
        *,
        deadline_monotonic: float,
    ) -> bytes:
        del deadline_monotonic
        self.downloaded.append(reference.version)
        return self.download_overrides.get(reference.version, self._draft_bytes[reference.version])

    def run_evaluation(
        self,
        reference: DraftReference,
        contract,
        *,
        deadline_monotonic: float,
    ) -> EvaluationEvidence:
        del deadline_monotonic
        key = (reference.version, contract.evaluation_id)
        self.evaluated.append(key)
        return self.evaluations[key]

    def delete_exact_owned_version(
        self,
        reference: DraftReference,
        *,
        deadline_monotonic: float,
    ) -> None:
        del deadline_monotonic
        self.deleted.append(reference.version)
        failures = self.delete_failures.get(reference.version)
        if failures:
            failure = failures.pop(0)
            if not failures:
                self.delete_failures.pop(reference.version, None)
            raise failure

    def fingerprint_route(
        self,
        agent_name: str,
        *,
        deadline_monotonic: float,
    ) -> RouteFingerprint:
        del deadline_monotonic
        assert agent_name == self.route.agent_name
        return self.route_after_delete if self.deleted else self.route


class _FakeBrokerClient:
    def __init__(
        self,
        *,
        comment_failures: dict[str, list[Exception]] | None = None,
        close_failures: list[Exception] | None = None,
    ) -> None:
        self.comment_receipts: dict[str, CommentReceipt] = {}
        self.comment_calls: list[str] = []
        self.comment_failures = (
            {}
            if comment_failures is None
            else {key: list(value) for key, value in comment_failures.items()}
        )
        self.close_calls: list[FinalDecision] = []
        self.close_attempts = 0
        self.close_error: Exception | None = None
        self.close_failures = [] if close_failures is None else list(close_failures)

    def upsert_comment(
        self,
        *,
        request_id: str,
        logical_kind: str,
        markdown: str,
        timeout_seconds: float,
    ) -> CommentReceipt:
        del request_id, markdown, timeout_seconds
        self.comment_calls.append(logical_kind)
        failures = self.comment_failures.get(logical_kind)
        if failures:
            failure = failures.pop(0)
            if not failures:
                self.comment_failures.pop(logical_kind, None)
            raise failure
        receipt = self.comment_receipts.get(logical_kind)
        if receipt is None:
            comment_id = len(self.comment_receipts) + 1
            receipt = CommentReceipt(
                repository_id=123456789,
                issue_number=7,
                logical_kind=logical_kind,
                marker=f"<!-- marker:{logical_kind} -->",
                comment_id=comment_id,
                api_url=f"https://api.example.invalid/comments/{comment_id}",
                html_url=f"https://github.com/example-org/example-agent/issues/7#issuecomment-{comment_id}",
                body_sha256=hashlib.sha256(logical_kind.encode("ascii")).hexdigest(),
                action="created",
            )
            self.comment_receipts[logical_kind] = receipt
        return receipt

    def close_no_winner(
        self,
        *,
        request_id: str,
        final_decision_receipt,
        timeout_seconds: float,
    ) -> PullRequestReceipt:
        del request_id, timeout_seconds
        self.close_attempts += 1
        if self.close_failures:
            raise self.close_failures.pop(0)
        if self.close_error is not None:
            raise self.close_error
        self.close_calls.append(final_decision_receipt.decision)
        return PullRequestReceipt(
            repository_id=123456789,
            issue_number=7,
            pull_request_number=11,
            api_url="https://api.example.invalid/pulls/11",
            html_url="https://github.com/example-org/example-agent/pull/11",
            state="closed",
            merged=False,
            action="closed",
        )


def test_build_hosted_definition_emits_exact_payload(tmp_path: Path) -> None:
    repository, _, environment = _create_runtime_repository(tmp_path)
    paths = load_runtime_paths(repository, environment=environment, job_id="job-7")
    settings = load_runtime_settings(paths, environment=environment)

    definition = build_hosted_definition(settings.metadata, "candidate")

    assert definition.as_payload() == {
        "kind": "hosted",
        "cpu": "0.5",
        "memory": "1Gi",
        "environment_variables": {
            "AZURE_AI_MODEL_DEPLOYMENT_NAME": "dep-candidate",
            "AZURE_AI_PROJECT_ENDPOINT": settings.metadata.project_endpoint,
        },
        "protocol_versions": (
            {"protocol": "responses", "version": "2.0.0"},
        ),
        "container_protocol_versions": (),
        "code_configuration": {
            "runtime": "python_3_13",
            "entry_point": ("python", "main.py"),
            "dependency_resolution": "remote_build",
        },
    }
    with pytest.raises(RuntimeIntegrationError, match="selected model"):
        build_hosted_definition(settings.metadata, "missing-model")


def test_controller_foundry_operations_baseline_create_eval_and_cleanup(
    tmp_path: Path,
) -> None:
    repository, base_commit, environment = _create_runtime_repository(tmp_path)
    paths = load_runtime_paths(repository, environment=environment, job_id="job-7")
    settings = load_runtime_settings(paths, environment=environment, base_commit=base_commit)
    identity = build_job_identity(
        settings=settings,
        issue_number=7,
        job_id="job-7",
        route_fingerprint=_route(),
    )

    def assert_persisted_before_poll(reference: DraftReference) -> None:
        baseline = RuntimeSidecarStore(paths.sidecar_path).load().baseline
        assert baseline is not None
        assert baseline.pending_reference is not None
        assert baseline.pending_reference.version == reference.version
        assert baseline.reference_verified is False

    foundry = _FakeFoundryClient(
        route=_route(),
        evaluations={
            ("draft-1", "eval-development"): _evidence(
                evaluation_id="eval-development",
                dataset_id="dataset-development",
                version="draft-1",
                quality_passed=2,
                quality_failed=2,
                quality_score=0.50,
            )
        },
        on_poll=assert_persisted_before_poll,
    )
    operations = ControllerFoundryOperations(
        repository=repository,
        source_root=settings.policy.source_root,
        policy=settings.policy,
        metadata=settings.metadata,
        client=foundry,
        artifact_state_path=paths.job_root,
        route_fingerprint=_route(),
    )

    result = operations.evaluate_baseline(identity)

    assert result.status == "ok"
    assert result.evaluation is not None
    assert result.evaluation.primary_score == 0.50
    assert result.evaluation.focused_cases_improved == 0
    assert result.evaluation.focused_cases_regressed == 0
    assert len(result.evaluation.guardrails) == 1
    assert result.evaluation.guardrails[0].name == "safety"
    assert result.evaluation.guardrails[0].passed is True
    assert result.evaluation.guardrails[0].score == 1.0
    assert foundry.created[0]["definition"] == build_hosted_definition(
        settings.metadata,
        settings.policy.baseline_model,
    ).as_payload()
    assert foundry.polled == ["draft-1"]
    assert foundry.downloaded == ["draft-1"]
    assert foundry.evaluated == [("draft-1", "eval-development")]
    assert foundry.deleted == ["draft-1"]

    baseline = RuntimeSidecarStore(paths.sidecar_path).load().baseline
    assert baseline is not None
    assert baseline.cleanup_receipt_id == "cleanup:draft-1"
    assert baseline.pending_reference is None
    assert baseline.metric("quality").passed_cases == 2
    assert baseline.metric("quality").failed_cases == 2

    cached = operations.evaluate_baseline(identity)
    assert cached == result
    assert len(foundry.created) == 1


def test_controller_foundry_operations_candidate_resume_validating_and_cleanup(
    tmp_path: Path,
) -> None:
    repository, base_commit, environment = _create_runtime_repository(tmp_path)
    paths = load_runtime_paths(repository, environment=environment, job_id="job-7")
    settings = load_runtime_settings(paths, environment=environment, base_commit=base_commit)
    identity = build_job_identity(
        settings=settings,
        issue_number=7,
        job_id="job-7",
        route_fingerprint=_route(),
    )
    foundry = _FakeFoundryClient(
        route=_route(),
        evaluations={
            ("draft-1", "eval-development"): _evidence(
                evaluation_id="eval-development",
                dataset_id="dataset-development",
                version="draft-1",
                quality_passed=2,
                quality_failed=2,
                quality_score=0.50,
            ),
            ("draft-2", "eval-development"): _evidence(
                evaluation_id="eval-development",
                dataset_id="dataset-development",
                version="draft-2",
                quality_passed=4,
                quality_failed=0,
                quality_score=1.00,
            ),
            ("draft-2", "eval-validating"): _evidence(
                evaluation_id="eval-validating",
                dataset_id="dataset-validating",
                version="draft-2",
                quality_passed=3,
                quality_failed=1,
                quality_score=0.75,
            ),
        },
    )
    operations = ControllerFoundryOperations(
        repository=repository,
        source_root=settings.policy.source_root,
        policy=settings.policy,
        metadata=settings.metadata,
        client=foundry,
        artifact_state_path=paths.job_root,
        route_fingerprint=_route(),
    )
    baseline = operations.evaluate_baseline(identity)
    workspace = CandidateWorkspace(
        repository,
        tmp_path / "candidate-root",
        base_commit,
        editable_patterns=("src/**", "tests/**"),
        source_root="src",
    )
    prepared = workspace.prepare(
        "candidate-one",
        model="candidate",
        hypothesis="Improve the prompt.",
    )
    (prepared.workspace_path / "src" / "main.py").write_text(
        "VALUE = 'candidate'\n",
        encoding="utf-8",
    )
    finalized = workspace.finalize("candidate-one")

    candidate = operations.evaluate_candidate(finalized)

    assert candidate.status == "ok"
    assert candidate.draft_id == "draft-2"
    assert candidate.evaluation is not None
    assert candidate.evaluation.primary_score == 1.00
    assert candidate.evaluation.focused_cases_improved == 2
    assert candidate.evaluation.focused_cases_regressed == 0
    assert foundry.created[1]["definition"]["environment_variables"] == {
        "AZURE_AI_MODEL_DEPLOYMENT_NAME": "dep-candidate",
        "AZURE_AI_PROJECT_ENDPOINT": settings.metadata.project_endpoint,
    }

    cached = operations.evaluate_candidate(finalized)
    assert cached == candidate
    assert foundry.evaluated.count(("draft-2", "eval-development")) == 1

    drifted = ControllerFoundryOperations(
        repository=repository,
        source_root=settings.policy.source_root,
        policy=settings.policy,
        metadata=settings.metadata.model_copy(
            update={
                "project_endpoint": (
                    "https://example.services.ai.azure.com/api/projects/drifted"
                )
            }
        ),
        client=foundry,
        artifact_state_path=paths.job_root,
        route_fingerprint=_route(),
    )
    with pytest.raises(RuntimeIntegrationError, match="metadata digest"):
        drifted.evaluate_validating(finalized)
    assert foundry.evaluated.count(("draft-2", "eval-validating")) == 0

    resumed = ControllerFoundryOperations(
        repository=repository,
        source_root=settings.policy.source_root,
        policy=settings.policy,
        metadata=settings.metadata,
        client=foundry,
        artifact_state_path=paths.job_root,
        route_fingerprint=_route(),
    )
    validating = resumed.evaluate_validating(finalized)
    assert validating.status == "ok"
    assert validating.evaluation is not None
    assert validating.evaluation.primary_score == 0.75
    assert validating.evaluation.focused_cases_improved == 1
    assert validating.evaluation.focused_cases_regressed == 0
    assert foundry.evaluated[-1] == ("draft-2", "eval-validating")

    store = JobStateStore(paths.job_state_path)
    state = store.initialize(identity)
    state = state.with_baseline(BaselineState(evaluation=baseline.evaluation))
    state = state.with_candidate(
        CandidateState(
            handoff=prepared,
            finalized=finalized,
            development=candidate.evaluation,
            validating=validating.evaluation,
            draft_id=candidate.draft_id,
        )
    )
    state = state.model_copy(update={"provisional_winner_id": finalized.candidate_id})
    state = store.save(state, expected_generation=state.generation)

    deferred = resumed.cleanup_draft(candidate.draft_id)
    assert deferred.success is False
    assert "validating dataset" in deferred.reason
    assert foundry.deleted == ["draft-1"]

    terminal = state.model_copy(update={"terminal_outcome": "no_winner"})
    store.save(terminal, expected_generation=terminal.generation)

    cleaned = resumed.cleanup_draft(candidate.draft_id)
    assert cleaned.success is True
    assert cleaned.receipt_id == "cleanup:draft-2"
    assert foundry.deleted == ["draft-1", "draft-2"]

    idempotent = resumed.cleanup_draft(candidate.draft_id)
    assert idempotent.success is True
    assert idempotent.receipt_id == "cleanup:draft-2"
    assert foundry.deleted.count("draft-2") == 1


def test_controller_foundry_operations_candidate_verification_failure_blocks_new_drafts(
    tmp_path: Path,
) -> None:
    repository, base_commit, environment = _create_runtime_repository(tmp_path)
    paths = load_runtime_paths(repository, environment=environment, job_id="job-7")
    settings = load_runtime_settings(paths, environment=environment, base_commit=base_commit)
    identity = build_job_identity(
        settings=settings,
        issue_number=7,
        job_id="job-7",
        route_fingerprint=_route(),
    )
    foundry = _FakeFoundryClient(
        route=_route(),
        evaluations={
            ("draft-1", "eval-development"): _evidence(
                evaluation_id="eval-development",
                dataset_id="dataset-development",
                version="draft-1",
                quality_passed=2,
                quality_failed=2,
                quality_score=0.50,
            ),
            ("draft-3", "eval-development"): _evidence(
                evaluation_id="eval-development",
                dataset_id="dataset-development",
                version="draft-3",
                quality_passed=4,
                quality_failed=0,
                quality_score=1.00,
            ),
        },
        download_overrides={"draft-2": b"mismatch"},
        delete_failures={"draft-2": [ServiceError("delete failed")]},
    )
    operations = ControllerFoundryOperations(
        repository=repository,
        source_root=settings.policy.source_root,
        policy=settings.policy,
        metadata=settings.metadata,
        client=foundry,
        artifact_state_path=paths.job_root,
        route_fingerprint=_route(),
    )
    baseline = operations.evaluate_baseline(identity)
    assert baseline.status == "ok"

    workspace = CandidateWorkspace(
        repository,
        tmp_path / "candidate-root",
        base_commit,
        editable_patterns=("src/**", "tests/**"),
        source_root="src",
    )
    prepared = workspace.prepare(
        "candidate-one",
        model="candidate",
        hypothesis="Improve the prompt.",
    )
    (prepared.workspace_path / "src" / "main.py").write_text(
        "VALUE = 'candidate'\n",
        encoding="utf-8",
    )
    finalized = workspace.finalize("candidate-one")

    first = operations.evaluate_candidate(finalized)
    assert first.status == "platform_failure"
    assert first.draft_id == "draft-2"
    assert "cleanup failed" in (first.reason or "")
    candidate_sidecar = RuntimeSidecarStore(paths.sidecar_path).load().candidates["candidate-one"]
    assert candidate_sidecar.reference is not None
    assert candidate_sidecar.cleanup_required is True
    assert candidate_sidecar.cleanup_receipt_id is None
    assert len(foundry.created) == 2

    second = operations.evaluate_candidate(finalized)
    assert second.status == "retry"
    assert second.draft_id == "draft-2"
    assert second.retry_phase == "candidate"
    assert "rerun candidate evaluation" in (second.reason or "")
    candidate_sidecar = RuntimeSidecarStore(paths.sidecar_path).load().candidates["candidate-one"]
    assert candidate_sidecar.reference is None
    assert candidate_sidecar.cleanup_required is False
    assert candidate_sidecar.cleanup_receipt_id == "cleanup:draft-2"
    assert candidate_sidecar.retry_phase == "candidate"
    assert len(foundry.created) == 2

    third = operations.evaluate_candidate(finalized)
    assert third.status == "ok"
    assert third.draft_id == "draft-3"
    assert third.evaluation is not None
    assert third.evaluation.primary_score == 1.00
    assert len(foundry.created) == 3
    assert foundry.evaluated.count(("draft-3", "eval-development")) == 1


def test_controller_foundry_operations_validating_verification_failure_retries_clean_draft(
    tmp_path: Path,
) -> None:
    repository, base_commit, environment = _create_runtime_repository(tmp_path)
    paths = load_runtime_paths(repository, environment=environment, job_id="job-7")
    settings = load_runtime_settings(paths, environment=environment, base_commit=base_commit)
    identity = build_job_identity(
        settings=settings,
        issue_number=7,
        job_id="job-7",
        route_fingerprint=_route(),
    )
    foundry = _FakeFoundryClient(
        route=_route(),
        evaluations={
            ("draft-1", "eval-development"): _evidence(
                evaluation_id="eval-development",
                dataset_id="dataset-development",
                version="draft-1",
                quality_passed=2,
                quality_failed=2,
                quality_score=0.50,
            ),
            ("draft-2", "eval-development"): _evidence(
                evaluation_id="eval-development",
                dataset_id="dataset-development",
                version="draft-2",
                quality_passed=4,
                quality_failed=0,
                quality_score=1.00,
            ),
            ("draft-3", "eval-validating"): _evidence(
                evaluation_id="eval-validating",
                dataset_id="dataset-validating",
                version="draft-3",
                quality_passed=3,
                quality_failed=1,
                quality_score=0.75,
            ),
        },
    )
    operations = ControllerFoundryOperations(
        repository=repository,
        source_root=settings.policy.source_root,
        policy=settings.policy,
        metadata=settings.metadata,
        client=foundry,
        artifact_state_path=paths.job_root,
        route_fingerprint=_route(),
    )
    baseline = operations.evaluate_baseline(identity)
    assert baseline.status == "ok"

    workspace = CandidateWorkspace(
        repository,
        tmp_path / "candidate-root",
        base_commit,
        editable_patterns=("src/**", "tests/**"),
        source_root="src",
    )
    prepared = workspace.prepare(
        "candidate-one",
        model="candidate",
        hypothesis="Improve the prompt.",
    )
    (prepared.workspace_path / "src" / "main.py").write_text(
        "VALUE = 'candidate'\n",
        encoding="utf-8",
    )
    finalized = workspace.finalize("candidate-one")

    candidate = operations.evaluate_candidate(finalized)
    assert candidate.status == "ok"
    assert candidate.draft_id == "draft-2"

    foundry.download_overrides["draft-2"] = b"mismatch"
    first = operations.evaluate_validating(finalized)
    assert first.status == "retry"
    assert first.draft_id == "draft-2"
    assert first.retry_phase == "validating"
    assert "rerun validating evaluation" in (first.reason or "")
    validating_sidecar = RuntimeSidecarStore(paths.sidecar_path).load().candidates["candidate-one"]
    assert validating_sidecar.reference is None
    assert validating_sidecar.cleanup_required is False
    assert validating_sidecar.cleanup_receipt_id == "cleanup:draft-2"
    assert validating_sidecar.retry_phase == "validating"
    assert foundry.evaluated.count(("draft-2", "eval-validating")) == 0

    second = operations.evaluate_validating(finalized)
    assert second.status == "ok"
    assert second.draft_id == "draft-3"
    assert second.evaluation is not None
    assert second.evaluation.primary_score == 0.75
    assert foundry.evaluated.count(("draft-3", "eval-validating")) == 1

    cached = operations.evaluate_validating(finalized)
    assert cached == second
    assert foundry.evaluated.count(("draft-3", "eval-validating")) == 1


def test_controller_foundry_operations_propagates_route_drift(
    tmp_path: Path,
) -> None:
    repository, base_commit, environment = _create_runtime_repository(tmp_path)
    paths = load_runtime_paths(repository, environment=environment, job_id="job-7")
    settings = load_runtime_settings(paths, environment=environment, base_commit=base_commit)
    identity = build_job_identity(
        settings=settings,
        issue_number=7,
        job_id="job-7",
        route_fingerprint=_route("a" * 64),
    )
    foundry = _FakeFoundryClient(
        route=_route("a" * 64),
        route_after_delete=_route("b" * 64),
        evaluations={
            ("draft-1", "eval-development"): _evidence(
                evaluation_id="eval-development",
                dataset_id="dataset-development",
                version="draft-1",
                quality_passed=2,
                quality_failed=2,
                quality_score=0.50,
            )
        },
    )
    operations = ControllerFoundryOperations(
        repository=repository,
        source_root=settings.policy.source_root,
        policy=settings.policy,
        metadata=settings.metadata,
        client=foundry,
        artifact_state_path=paths.job_root,
        route_fingerprint=_route("a" * 64),
    )

    with pytest.raises(RouteDriftError):
        operations.evaluate_baseline(identity)

    baseline = RuntimeSidecarStore(paths.sidecar_path).load().baseline
    assert baseline is not None
    assert baseline.pending_reference is not None
    assert baseline.cleanup_receipt_id is None


def test_controller_foundry_operations_rejects_identity_digest_drift_before_create(
    tmp_path: Path,
) -> None:
    repository, base_commit, environment = _create_runtime_repository(tmp_path)
    paths = load_runtime_paths(repository, environment=environment, job_id="job-7")
    settings = load_runtime_settings(paths, environment=environment, base_commit=base_commit)
    identity = build_job_identity(
        settings=settings,
        issue_number=7,
        job_id="job-7",
        route_fingerprint=_route(),
    )
    drifted_policy = settings.policy.model_copy(update={"primary_metric": "safety"})
    foundry = _FakeFoundryClient(route=_route(), evaluations={})
    operations = ControllerFoundryOperations(
        repository=repository,
        source_root=drifted_policy.source_root,
        policy=drifted_policy,
        metadata=settings.metadata,
        client=foundry,
        artifact_state_path=paths.job_root,
        route_fingerprint=_route(),
    )

    with pytest.raises(RuntimeIntegrationError, match="policy digest"):
        operations.evaluate_baseline(identity)

    assert foundry.created == []


def test_broker_issue_comments_map_markers_and_persist_final_receipt(
    tmp_path: Path,
) -> None:
    sidecars = RuntimeSidecarStore(tmp_path / "runtime-sidecars.json")
    broker = _FakeBrokerClient()
    comments = BrokerIssueComments(client=broker, sidecars=sidecars)
    closure = BrokerClosure(client=broker, sidecars=sidecars)
    identity = JobIdentity(
        job_id="job-7",
        repository="example-org/example-agent",
        issue_number=7,
        shared_commit="a" * 40,
        base_commit="b" * 40,
        source_root="src",
        route_fingerprint="c" * 64,
        min_candidates=1,
    )

    baseline_id = comments.upsert_comment(
        RenderedComment(
            marker_id=baseline_marker_id(identity.job_id),
            title="Baseline",
            body="baseline body",
        )
    )
    candidate_id = comments.upsert_comment(
        RenderedComment(
            marker_id=candidate_marker_id(identity.job_id, "candidate-one"),
            title="Candidate",
            body="candidate body",
        )
    )
    final_id = comments.upsert_comment(
        RenderedComment(
            marker_id=final_marker_id(identity.job_id),
            title="Final",
            body="final body",
        )
    )

    assert broker.comment_calls == ["baseline", "candidate-candidate-one", "final"]
    assert baseline_id == "comment:1"
    assert candidate_id == "comment:2"
    assert final_id == "comment:3"
    assert set(sidecars.load().comments) == {
        "baseline",
        "candidate-candidate-one",
        "final",
    }

    receipt_id = closure.signal_no_winner(identity)
    assert receipt_id == "pull-request:11"
    assert broker.close_calls == [FinalDecision.NO_WINNER]


@pytest.mark.parametrize(
    ("marker_id", "logical_kind", "error_text", "expected_message", "error_factory"),
    (
        (
            baseline_marker_id("job-7"),
            "baseline",
            "broker socket is not available",
            "broker socket is not available",
            BrokerUnavailableError,
        ),
        (
            candidate_marker_id("job-7", "candidate-one"),
            "candidate-candidate-one",
            "BrokerPolicyError: github_pat_abcdefghijklmnopqrst refused candidate comment",
            "BrokerPolicyError: ****** refused candidate comment",
            BrokerRemoteError,
        ),
        (
            final_marker_id("job-7"),
            "final",
            "BrokerWriteError: final comment refused",
            "BrokerWriteError: final comment refused",
            BrokerRemoteError,
        ),
    ),
)
def test_broker_issue_comments_wrap_broker_failures_without_persisting_receipts(
    tmp_path: Path,
    marker_id: str,
    logical_kind: str,
    error_text: str,
    expected_message: str,
    error_factory,
) -> None:
    sidecars = RuntimeSidecarStore(tmp_path / "runtime-sidecars.json")
    broker = _FakeBrokerClient(
        comment_failures={logical_kind: [error_factory(error_text)]}
    )
    comments = BrokerIssueComments(client=broker, sidecars=sidecars)
    comment = RenderedComment(
        marker_id=marker_id,
        title="Comment",
        body="comment body",
    )

    with pytest.raises(RuntimeIntegrationError) as excinfo:
        comments.upsert_comment(comment)

    assert expected_message in str(excinfo.value)
    if expected_message != error_text:
        assert error_text not in str(excinfo.value)
    assert broker.comment_calls == [logical_kind]
    assert logical_kind not in sidecars.load().comments

    receipt_id = comments.upsert_comment(comment)

    assert receipt_id == "comment:1"
    assert sidecars.load().comments[logical_kind].logical_kind == logical_kind


def test_broker_closure_wraps_broker_outages_and_replays_after_recovery(
    tmp_path: Path,
) -> None:
    sidecars = RuntimeSidecarStore(tmp_path / "runtime-sidecars.json")
    broker = _FakeBrokerClient(
        close_failures=[BrokerUnavailableError("broker socket is not reachable")]
    )
    comments = BrokerIssueComments(client=broker, sidecars=sidecars)
    closure = BrokerClosure(client=broker, sidecars=sidecars)
    identity = JobIdentity(
        job_id="job-7",
        repository="example-org/example-agent",
        issue_number=7,
        shared_commit="a" * 40,
        base_commit="b" * 40,
        source_root="src",
        route_fingerprint="c" * 64,
        min_candidates=1,
    )
    comments.upsert_comment(
        RenderedComment(
            marker_id=final_marker_id(identity.job_id),
            title="Final",
            body="final body",
        )
    )

    with pytest.raises(RuntimeIntegrationError, match="broker socket is not reachable"):
        closure.signal_no_winner(identity)

    assert broker.close_attempts == 1
    assert broker.close_calls == []
    assert sidecars.load().comments["final"].logical_kind == "final"

    receipt_id = closure.signal_no_winner(identity)

    assert receipt_id == "pull-request:11"
    assert broker.close_attempts == 2
    assert broker.close_calls == [FinalDecision.NO_WINNER]


def test_broker_closure_reports_missing_pull_binding(tmp_path: Path) -> None:
    sidecars = RuntimeSidecarStore(tmp_path / "runtime-sidecars.json")
    broker = _FakeBrokerClient()
    comments = BrokerIssueComments(client=broker, sidecars=sidecars)
    closure = BrokerClosure(client=broker, sidecars=sidecars)
    identity = JobIdentity(
        job_id="job-7",
        repository="example-org/example-agent",
        issue_number=7,
        shared_commit="a" * 40,
        base_commit="b" * 40,
        source_root="src",
        route_fingerprint="c" * 64,
        min_candidates=1,
    )
    comments.upsert_comment(
        RenderedComment(
            marker_id=final_marker_id(identity.job_id),
            title="Final",
            body="final body",
        )
    )
    broker.close_error = BrokerRemoteError(
        "GitHubPolicyError: trusted binding file does not yet contain a pull request binding"
    )

    with pytest.raises(RuntimeIntegrationError, match="pull request binding"):
        closure.signal_no_winner(identity)


def test_build_runtime_controller_uses_injected_dependencies(tmp_path: Path) -> None:
    repository, base_commit, environment = _create_runtime_repository(tmp_path)
    paths = load_runtime_paths(repository, environment=environment, job_id="job-7")
    settings = load_runtime_settings(paths, environment=environment, base_commit=base_commit)

    credential_configs: list[object] = []
    credential_instances: list[object] = []
    evaluation_backend_calls: list[tuple[str, object]] = []
    foundry_instances: list[object] = []
    broker_socket_paths: list[Path] = []

    class _Credential:
        def __init__(self, config: object) -> None:
            self.config = config
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class _CaptureClient:
        def __init__(
            self,
            project_endpoint: str,
            token_provider: object,
            *,
            evaluation_backend: object | None = None,
        ) -> None:
            self.project_endpoint = project_endpoint
            self.token_provider = token_provider
            self.evaluation_backend = evaluation_backend
            self.closed = False
            foundry_instances.append(self)

        def fingerprint_route(
            self,
            agent_name: str,
            *,
            deadline_monotonic: float,
        ) -> RouteFingerprint:
            del deadline_monotonic
            assert agent_name == "travel-agent"
            return _route("d" * 64)

        def close(self) -> None:
            self.closed = True

    def credential_builder(config: object, *, environment: Any = None) -> _Credential:
        del environment
        credential_configs.append(config)
        credential = _Credential(config)
        credential_instances.append(credential)
        return credential

    def evaluation_backend_factory(*, project_endpoint: str, credential: object) -> object:
        evaluation_backend_calls.append((project_endpoint, credential))
        return {"backend": "evaluation"}

    def broker_client_factory(*, socket_path: Path) -> _FakeBrokerClient:
        broker_socket_paths.append(socket_path)
        return _FakeBrokerClient()

    captured = capture_route_fingerprint(
        repository=repository,
        environment=environment,
        paths=paths,
        settings=settings,
        credential_builder=credential_builder,
        foundry_client_factory=_CaptureClient,
    )
    identity = build_job_identity(
        settings=settings,
        issue_number=7,
        job_id="job-7",
        route_fingerprint=captured,
    )

    controller = build_runtime_controller(
        repository=repository,
        identity=identity,
        environment=environment,
        paths=paths,
        settings=settings,
        captured_route=captured,
        credential_builder=credential_builder,
        evaluation_backend_factory=evaluation_backend_factory,
        foundry_client_factory=_CaptureClient,
        broker_client_factory=broker_client_factory,
    )

    assert isinstance(controller, OptimizeJobController)
    assert controller._store.path == paths.job_state_path
    assert controller._workspace.base_commit == base_commit
    assert controller._rules.aggregate_min_delta == 0.05
    assert controller._rules.min_focused_cases_improved == 1
    assert len(controller._rules.guardrails) == 1
    assert controller._rules.guardrails[0].name == "safety"
    assert controller._rules.guardrails[0].minimum_score == 1.0
    assert controller._rules.guardrails[0].require_pass is True
    assert captured.sha256 == "d" * 64
    assert foundry_instances[0].closed is True
    assert foundry_instances[1].evaluation_backend == {"backend": "evaluation"}
    assert evaluation_backend_calls == [
        (settings.metadata.project_endpoint, credential_instances[1])
    ]
    assert broker_socket_paths == [paths.broker_socket_path]
    assert credential_configs[0].expected_subject == (
        "repo:example-org/example-agent:environment:copilot"
    )
