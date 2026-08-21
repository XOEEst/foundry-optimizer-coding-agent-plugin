from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest
import typer
from typer.testing import CliRunner

from foundry_opt import cli as cli_module
from foundry_opt.cli import app
from foundry_opt.poc.bootstrap import BootstrapReceipt, write_bootstrap_receipt
from foundry_opt.poc.candidate import CandidateWorkspace
from foundry_opt.poc.checks import RepositoryCheckResult
from foundry_opt.poc.controller import CleanupResult, OptimizeJobController, RunResult
from foundry_opt.poc.decision import DecisionRules, EvaluationSummary, GuardrailResult, GuardrailRule
from foundry_opt.poc.evidence import baseline_marker_id, final_marker_id
from foundry_opt.poc.foundry import (
    DraftReference,
    EvaluationEvidence,
    EvaluationReference,
    HostedDefinition,
    Metric,
    RouteFingerprint,
)
from foundry_opt.poc.github import (
    BrokerRemoteError,
    BrokerUnavailableError,
    CommentReceipt,
    FinalDecision,
    IssueBinding,
    PullRequestBinding,
    PullRequestReceipt,
    RepositoryIdentity,
)
from foundry_opt.poc.runtime import (
    BOOTSTRAP_RECEIPT_ENV,
    BROKER_SOCKET_ENV,
    BrokerClosure,
    BrokerIssueComments,
    DEADLINE_SECONDS_ENV,
    DEFAULT_DEADLINE_SECONDS,
    STATE_ROOT_ENV,
    ControllerFoundryOperations,
    RuntimePaths,
    RuntimeSettings,
    RuntimeSidecarStore,
    build_hosted_definition,
    build_job_identity,
    load_runtime_paths,
    load_runtime_settings,
)
from foundry_opt.poc.state import JobIdentity, JobStateStore
from foundry_opt.verification import VerificationCheckSpec


runner = CliRunner()


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={
            **os.environ,
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
        },
    )
    return completed.stdout.strip()


def _route(sha256: str = "d" * 64) -> RouteFingerprint:
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


def _summary(
    run_kind: str,
    score: float | None,
    *,
    successful: bool = True,
    improved: int = 1,
    regressed: int = 0,
    tokens: int = 100,
    latency: float = 20.0,
) -> EvaluationSummary:
    return EvaluationSummary(
        run_kind=run_kind,
        successful=successful,
        primary_score=score,
        focused_cases_improved=improved,
        focused_cases_regressed=regressed,
        token_count=tokens,
        latency_ms=latency,
        foundry_version="draft-1",
        evaluation_link=f"https://example.invalid/{run_kind}/{score}",
        guardrails=(GuardrailResult(name="safety", passed=True, score=1.0),),
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


class FakeFoundry:
    def __init__(
        self,
        *,
        baseline: RunResult,
        candidates: dict[str, RunResult],
        validating: dict[str, RunResult],
        cleanups: dict[str, list[CleanupResult]],
    ) -> None:
        self.baseline = baseline
        self.candidates = candidates
        self.validating = validating
        self.cleanups = cleanups
        self.baseline_calls = 0
        self.candidate_calls: list[str] = []
        self.validating_calls: list[str] = []
        self.cleanup_calls: list[str] = []
        self._cleanup_indexes: dict[str, int] = defaultdict(int)

    def evaluate_baseline(self, identity: JobIdentity) -> RunResult:
        del identity
        self.baseline_calls += 1
        return self.baseline

    def evaluate_candidate(self, candidate) -> RunResult:
        self.candidate_calls.append(candidate.candidate_id)
        return self.candidates[candidate.candidate_id]

    def evaluate_validating(self, candidate) -> RunResult:
        self.validating_calls.append(candidate.candidate_id)
        return self.validating[candidate.candidate_id]

    def cleanup_draft(self, draft_id: str) -> CleanupResult:
        self.cleanup_calls.append(draft_id)
        sequence = self.cleanups[draft_id]
        index = self._cleanup_indexes[draft_id]
        self._cleanup_indexes[draft_id] += 1
        return sequence[min(index, len(sequence) - 1)]


class FakeComments:
    def __init__(self) -> None:
        self.by_marker: dict[str, str] = {}
        self.bodies: dict[str, str] = {}
        self.upsert_count_by_marker: dict[str, int] = defaultdict(int)

    def upsert_comment(self, comment) -> str:
        self.upsert_count_by_marker[comment.marker_id] += 1
        if comment.marker_id not in self.by_marker:
            self.by_marker[comment.marker_id] = f"comment-{len(self.by_marker) + 1}"
        self.bodies[comment.marker_id] = comment.body
        return self.by_marker[comment.marker_id]


class FakeClosure:
    def __init__(self) -> None:
        self.receipts: dict[str, str] = {}
        self.calls: list[str] = []

    def signal_no_winner(self, identity: JobIdentity) -> str:
        self.calls.append(identity.job_id)
        if identity.job_id not in self.receipts:
            self.receipts[identity.job_id] = f"closure-{len(self.receipts) + 1}"
        return self.receipts[identity.job_id]


class _FlakyBrokerClient:
    def __init__(
        self,
        *,
        comment_failures: dict[str, list[Exception]] | None = None,
        close_failures: list[Exception] | None = None,
    ) -> None:
        self.comment_receipts: dict[str, CommentReceipt] = {}
        self.comment_failures = (
            {}
            if comment_failures is None
            else {key: list(value) for key, value in comment_failures.items()}
        )
        self.comment_calls: list[str] = []
        self.upsert_count_by_kind: dict[str, int] = defaultdict(int)
        self.close_failures = [] if close_failures is None else list(close_failures)
        self.close_attempts = 0
        self.close_calls: list[FinalDecision] = []

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
        self.upsert_count_by_kind[logical_kind] += 1
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
                html_url=(
                    "https://github.com/example-org/example-agent/issues/7"
                    f"#issuecomment-{comment_id}"
                ),
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


class _FakeFoundryClient:
    def __init__(
        self,
        *,
        route: RouteFingerprint,
        evaluations: dict[tuple[str, str], EvaluationEvidence],
    ) -> None:
        self.route = route
        self.evaluations = evaluations
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
        return self._draft_bytes[reference.version]

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

    def fingerprint_route(
        self,
        agent_name: str,
        *,
        deadline_monotonic: float,
    ) -> RouteFingerprint:
        del deadline_monotonic
        assert agent_name == self.route.agent_name
        return self.route


class FakeCheckRunner:
    def __init__(
        self,
        *,
        results: dict[str, tuple[RepositoryCheckResult, ...]],
    ) -> None:
        self.results = results
        self.calls: list[tuple[str, tuple[VerificationCheckSpec, ...]]] = []

    def run_checks(
        self,
        candidate,
        *,
        checks: tuple[VerificationCheckSpec, ...],
    ) -> tuple[RepositoryCheckResult, ...]:
        self.calls.append((candidate.candidate_id, checks))
        return self.results[candidate.candidate_id]


class ControllerHarness:
    def __init__(
        self,
        *,
        candidate_results: dict[str, RunResult],
        validating_results: dict[str, RunResult],
        cleanup_results: dict[str, list[CleanupResult]],
        check_results: dict[str, tuple[RepositoryCheckResult, ...]] | None = None,
    ) -> None:
        self.foundry = FakeFoundry(
            baseline=RunResult(status="ok", evaluation=_summary("development", 0.50)),
            candidates=candidate_results,
            validating=validating_results,
            cleanups=cleanup_results,
        )
        self.comments = FakeComments()
        self.closure = FakeClosure()
        self.check_runner = (
            None if check_results is None else FakeCheckRunner(results=check_results)
        )
        self.settings_seen: list[RuntimeSettings] = []

    def builder(
        self,
        *,
        repository: Path,
        identity: JobIdentity,
        paths: RuntimePaths,
        settings: RuntimeSettings,
        **_: Any,
    ) -> OptimizeJobController:
        self.settings_seen.append(settings)
        workspace = CandidateWorkspace(
            repository,
            paths.workspace_root,
            settings.base_commit,
            editable_patterns=settings.policy.editable_paths,
            protected_patterns=(
                ".git/**",
                ".github/foundry-optimizer.yaml",
                settings.policy.metadata_path,
                ".github/foundry-opt.lock.yml",
                "uv.lock",
            ),
            source_root=settings.policy.source_root,
        )
        return OptimizeJobController(
            store=JobStateStore(paths.job_state_path),
            workspace=workspace,
            foundry=self.foundry,
            comments=self.comments,
            closure=self.closure,
            rules=_controller_rules(settings.policy),
            check_runner=self.check_runner,
        )


class BrokerControllerHarness(ControllerHarness):
    def __init__(
        self,
        *,
        broker: _FlakyBrokerClient,
        candidate_results: dict[str, RunResult],
        validating_results: dict[str, RunResult],
        cleanup_results: dict[str, list[CleanupResult]],
        check_results: dict[str, tuple[RepositoryCheckResult, ...]] | None = None,
    ) -> None:
        super().__init__(
            candidate_results=candidate_results,
            validating_results=validating_results,
            cleanup_results=cleanup_results,
            check_results=check_results,
        )
        self.broker = broker

    def builder(
        self,
        *,
        repository: Path,
        identity: JobIdentity,
        paths: RuntimePaths,
        settings: RuntimeSettings,
        **_: Any,
    ) -> OptimizeJobController:
        del identity
        self.settings_seen.append(settings)
        workspace = CandidateWorkspace(
            repository,
            paths.workspace_root,
            settings.base_commit,
            editable_patterns=settings.policy.editable_paths,
            protected_patterns=(
                ".git/**",
                ".github/foundry-optimizer.yaml",
                settings.policy.metadata_path,
                ".github/foundry-opt.lock.yml",
                "uv.lock",
            ),
            source_root=settings.policy.source_root,
        )
        sidecars = RuntimeSidecarStore(paths.sidecar_path)
        return OptimizeJobController(
            store=JobStateStore(paths.job_state_path),
            workspace=workspace,
            foundry=self.foundry,
            comments=BrokerIssueComments(client=self.broker, sidecars=sidecars),
            closure=BrokerClosure(client=self.broker, sidecars=sidecars),
            rules=_controller_rules(settings.policy),
            check_runner=self.check_runner,
        )


def _controller_rules(policy) -> DecisionRules:
    return DecisionRules(
        aggregate_min_delta=policy.decision_rules.minimum_aggregate_delta,
        min_focused_cases_improved=(1 if policy.decision_rules.focused_cases_required else 0),
        max_focused_regressions=policy.decision_rules.max_regressions,
        guardrails=tuple(
            GuardrailRule(
                name=guardrail.metric,
                minimum_score=guardrail.required_pass_rate,
                require_pass=True,
            )
            for guardrail in policy.hard_guardrails
        ),
    )


def _check_result(
    spec: str,
    *,
    passed: bool,
    summary: str,
    exit_code: int | None = None,
) -> RepositoryCheckResult:
    return RepositoryCheckResult(
        spec=VerificationCheckSpec.parse_line(spec),
        passed=passed,
        exit_code=(0 if passed and exit_code is None else exit_code),
        duration_seconds=0.25,
        summary=summary,
    )


def _issue_body(
    *,
    candidate_budget: int,
    model_lines: tuple[str, ...] = (),
    editable_scope_lines: tuple[str, ...] = (),
    verification_dataset: str | None = None,
    verification_check_lines: tuple[str, ...] = (),
    acknowledge_no_evidence: bool = False,
) -> str:
    models = "_No response_" if not model_lines else "\n".join(model_lines)
    editable_scope = "_No response_" if not editable_scope_lines else "\n".join(editable_scope_lines)
    body = f"""### Optimization goal

Improve coverage.

### Observed failures or evidence

One rule is omitted.

### Constraints and guardrails

Preserve safety.

### Changed candidates

{candidate_budget}

### Optional narrower editable scope

{editable_scope}

### Optional narrower model set

{models}
"""
    if verification_dataset is not None:
        body += f"""
### Optional exact verification dataset ID or URI

{verification_dataset}
"""
    if verification_check_lines:
        body += """
### Optional verification commands or checks

```text
""" + "\n".join(verification_check_lines) + "\n```\n"
    if acknowledge_no_evidence:
        body += """
### Optional no-evidence acknowledgement

acknowledge
"""
    return body


def _write_issue_event(tmp_path: Path, *, body: str, issue_number: int = 7) -> Path:
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "repository": {
                    "full_name": "example-org/example-agent",
                    "id": 123456789,
                },
                "issue": {
                    "number": issue_number,
                    "body": body,
                },
            }
        ),
        encoding="utf-8",
    )
    return event


def test_issue_binding_from_event_payload_accepts_direct_issue_context() -> None:
    binding = cli_module._issue_binding_from_event_payload(
        {
            "repository": {
                "full_name": "example-org/example-agent",
                "id": 123456789,
            },
            "issue": {
                "number": 7,
                "body": _issue_body(candidate_budget=2),
            },
        }
    )

    assert binding.issue_number == 7
    assert binding.repository.full_name == "example-org/example-agent"


def test_issue_binding_from_event_payload_accepts_pull_request_body_issue_link() -> None:
    binding = cli_module._issue_binding_from_event_payload(
        {
            "repository": {
                "full_name": "example-org/example-agent",
                "id": 123456789,
            },
            "pull_request": {
                "number": 11,
                "body": "\n".join(
                    (
                        "Follow-up optimize job session.",
                        "",
                        "- Closes example-org/example-agent#7",
                        "- Fixes #7",
                    )
                ),
            },
            "issue": {
                "number": 11,
                "body": "",
                "pull_request": {"url": "https://example.invalid/pr/11"},
            },
        }
    )

    assert binding.issue_number == 7
    assert binding.job_id == "optimize-7"


def test_issue_binding_from_event_payload_rejects_ambiguous_pull_request_issue_links() -> None:
    with pytest.raises(
        typer.BadParameter,
        match="multiple optimize-job issues",
    ):
        cli_module._issue_binding_from_event_payload(
            {
                "repository": {
                    "full_name": "example-org/example-agent",
                    "id": 123456789,
                },
                "pull_request": {
                    "number": 11,
                    "body": "\n".join(
                        (
                            "Conflicting issue links.",
                            "",
                            "- Fixes #7",
                            "- Closes #8",
                        )
                    ),
                },
                "issue": {
                    "number": 11,
                    "body": "",
                    "pull_request": {"url": "https://example.invalid/pr/11"},
                },
            }
        )


def test_issue_binding_from_event_context_uses_head_ref_lookup_when_event_lacks_issue_body(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "repository": {
                    "full_name": "example-org/example-agent",
                    "id": 123456789,
                },
                "issue": {
                    "number": 11,
                    "body": "",
                    "pull_request": {"url": "https://example.invalid/pr/11"},
                },
            }
        ),
        encoding="utf-8",
    )

    calls: list[tuple[str, str, str]] = []

    def lookup(
        repository: RepositoryIdentity,
        *,
        head_ref: str,
        token: str,
    ) -> int | None:
        calls.append((repository.full_name, head_ref, token))
        return 7 if head_ref == "copilot/optimize-job-7" else None

    monkeypatch.setattr(
        cli_module,
        "_linked_issue_number_from_open_pull_request_branch",
        lookup,
    )

    binding = cli_module._issue_binding_from_event_context(
        event,
        token="ghp_exampletoken12345678",
        head_ref="copilot/optimize-job-7",
    )

    assert binding.issue_number == 7
    assert calls == [
        (
            "example-org/example-agent",
            "copilot/optimize-job-7",
            "ghp_exampletoken12345678",
        )
    ]


def _write_binding(
    path: Path,
    *,
    issue_number: int = 7,
    pull_request: PullRequestBinding | None = None,
) -> Path:
    issue = IssueBinding(
        repository=RepositoryIdentity(
            owner="example-org",
            name="example-agent",
            repository_id=123456789,
        ),
        issue_number=issue_number,
        job_id=f"optimize-{issue_number}",
        comment_author_login="github-actions[bot]",
    )
    cli_module._write_binding(path, issue=issue, pull_request=pull_request)
    return path


def _checkout_branch(repository: Path, branch: str) -> str:
    _git(repository, "checkout", "-b", branch)
    return _git(repository, "rev-parse", "HEAD")


def _pull_request_binding(
    repository: Path,
    *,
    issue_number: int = 7,
    pull_request_number: int = 11,
    base_branch: str = "main",
    head_branch: str = "copilot/job-7",
    expected_author_login: str = "copilot-swe-agent[bot]",
    expected_author_type: str = "Bot",
    head_sha: str | None = None,
) -> PullRequestBinding:
    return PullRequestBinding(
        repository=RepositoryIdentity(
            owner="example-org",
            name="example-agent",
            repository_id=123456789,
        ),
        issue_number=issue_number,
        pull_request_number=pull_request_number,
        base_branch=base_branch,
        head_branch=head_branch,
        head_sha=(
            _git(repository, "rev-parse", "HEAD")
            if head_sha is None
            else head_sha
        ),
        expected_author_login=expected_author_login,
        expected_author_type=expected_author_type,
    )


def _create_runtime_repository(tmp_path: Path) -> tuple[Path, str, dict[str, str]]:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.com")
    (repository / "src").mkdir()
    (repository / "tests").mkdir()
    (repository / "src" / "main.py").write_text("VALUE = 'base'\n", encoding="utf-8")
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
    broker_socket.write_text("", encoding="utf-8")
    environment = {
        BOOTSTRAP_RECEIPT_ENV: str(receipt_path),
        BROKER_SOCKET_ENV: str(broker_socket),
        STATE_ROOT_ENV: str(state_root),
        DEADLINE_SECONDS_ENV: "90",
    }
    return repository, base_commit, environment


def _invoke(arguments: list[str], env: dict[str, str]) -> Any:
    invocation_environment = {**os.environ, **env}
    for key in (
        "GITHUB_EVENT_PATH",
        "GITHUB_WORKSPACE",
        "GITHUB_HEAD_REF",
        "GITHUB_REF_NAME",
        "FOUNDRY_OPT_GITHUB_BINDING",
        "FOUNDRY_OPT_PULL_REQUEST_NUMBER",
        "FOUNDRY_OPT_PULL_REQUEST_BASE_BRANCH",
    ):
        if key not in env:
            invocation_environment[key] = ""
    return runner.invoke(app, arguments, env=invocation_environment)


def _replace_text(path: Path, old: str, new: str) -> None:
    content = path.read_text(encoding="utf-8")
    assert old in content
    path.write_text(content.replace(old, new, 1), encoding="utf-8")


def _start_resume_job(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, Path, dict[str, str]]:
    repository, _, environment = _create_runtime_repository(tmp_path)
    event = _write_issue_event(
        tmp_path,
        body=_issue_body(candidate_budget=1, model_lines=("candidate",)),
    )
    harness = ControllerHarness(
        candidate_results={},
        validating_results={},
        cleanup_results={},
    )
    monkeypatch.setattr(cli_module, "capture_route_fingerprint", lambda **_: _route())
    monkeypatch.setattr(cli_module, "build_runtime_controller", harness.builder)

    started = _invoke(
        ["job", "start", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert started.exit_code == 0, started.stdout
    return repository, event, environment


def test_version() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "0.2.0"


def test_issue_parse(tmp_path: Path) -> None:
    body = tmp_path / "issue.md"
    body.write_text(_issue_body(candidate_budget=2), encoding="utf-8")

    result = runner.invoke(app, ["issue", "parse", "--body-file", str(body)])

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["request"]["candidate_budget"] == 2


def test_bootstrap_verify(tmp_path: Path) -> None:
    checkout = tmp_path / "shared"
    checkout.mkdir()
    _git(checkout, "init")
    (checkout / "src").mkdir()
    (checkout / "src" / "skill").mkdir()
    (checkout / "uv.lock").write_text("lock\n", encoding="utf-8")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-m", "fixture")
    commit = _git(checkout, "rev-parse", "HEAD")
    lock_sha = hashlib.sha256((checkout / "uv.lock").read_bytes()).hexdigest()
    pin = tmp_path / "pin.yml"
    pin.write_text(
        f"""schema_version: 1
repository_url: https://github.com/example/shared.git
commit: "{commit}"
package_path: .
skill_path: src/skill
uv_lock_sha256: "{lock_sha}"
""",
        encoding="utf-8",
    )
    receipt = tmp_path / "receipt.json"

    result = runner.invoke(
        app,
        [
            "bootstrap",
            "verify",
            "--pin",
            str(pin),
            "--checkout",
            str(checkout),
            "--receipt",
            str(receipt),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert receipt.exists()
    assert json.loads(result.stdout)["commit"] == commit


def test_validate_config_rejects_non_repository(tmp_path: Path) -> None:
    result = runner.invoke(app, ["validate-config", "--repository", str(tmp_path)])

    assert result.exit_code != 0


def test_preflight_checks_foundry_route_when_online(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, _, environment = _create_runtime_repository(tmp_path)
    _git(
        repository,
        "remote",
        "add",
        "origin",
        "https://github.com/example-org/example-agent.git",
    )
    captured_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(cli_module, "detect_github_actions_oidc", lambda: True)
    monkeypatch.setattr(
        cli_module,
        "capture_route_fingerprint",
        lambda **kwargs: captured_calls.append(kwargs) or _route(),
    )

    result = _invoke(["preflight", "--repository", str(repository)], environment)

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["foundry_route_fingerprint"] == _route().sha256
    assert len(captured_calls) == 1


def test_preflight_accepts_exact_copilot_git_proxy_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, _, environment = _create_runtime_repository(tmp_path)
    _git(
        repository,
        "remote",
        "add",
        "origin",
        "http://localhost:26831/example-org/example-agent",
    )
    environment.update(
        {
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "dynamic",
            "GITHUB_REPOSITORY": "example-org/example-agent",
            "GITHUB_REPOSITORY_ID": "123456789",
        }
    )
    monkeypatch.setattr(cli_module, "detect_github_actions_oidc", lambda: True)
    monkeypatch.setattr(
        cli_module,
        "capture_route_fingerprint",
        lambda **_: _route(),
    )

    result = _invoke(
        ["preflight", "--repository", str(repository)],
        environment,
    )

    assert result.exit_code == 0, result.stdout


def test_preflight_rejects_copilot_proxy_with_wrong_repository_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, _, environment = _create_runtime_repository(tmp_path)
    _git(
        repository,
        "remote",
        "add",
        "origin",
        "http://localhost:26831/example-org/example-agent",
    )
    environment.update(
        {
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "dynamic",
            "GITHUB_REPOSITORY": "example-org/example-agent",
            "GITHUB_REPOSITORY_ID": "987654321",
        }
    )
    monkeypatch.setattr(cli_module, "detect_github_actions_oidc", lambda: True)

    result = _invoke(
        ["preflight", "--repository", str(repository)],
        environment,
    )

    assert result.exit_code != 0


def test_job_start_two_candidates_winner_and_finish(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, _, environment = _create_runtime_repository(tmp_path)
    _checkout_branch(repository, "copilot/job-7")
    event = _write_issue_event(
        tmp_path,
        body=_issue_body(candidate_budget=2, model_lines=("candidate",)),
    )
    binding = _write_binding(tmp_path / "binding.json")
    environment[cli_module._GITHUB_BINDING_ENV] = str(binding)
    harness = ControllerHarness(
        candidate_results={
            "candidate-one": RunResult(
                status="ok",
                evaluation=_summary("development", 0.58, improved=1, tokens=120, latency=30.0),
                draft_id="draft-one",
            ),
            "candidate-two": RunResult(
                status="ok",
                evaluation=_summary("development", 0.63, improved=2, tokens=90, latency=20.0),
                draft_id="draft-two",
            ),
        },
        validating_results={
            "candidate-two": RunResult(
                status="ok",
                evaluation=_summary("validating", 0.62, improved=2, tokens=90, latency=20.0),
            )
        },
        cleanup_results={
            "draft-one": [CleanupResult(success=True, receipt_id="cleanup-one")],
            "draft-two": [CleanupResult(success=True, receipt_id="cleanup-two")],
        },
    )
    monkeypatch.setattr(cli_module, "capture_route_fingerprint", lambda **_: _route())
    monkeypatch.setattr(cli_module, "build_runtime_controller", harness.builder)
    def ensure_binding(**kwargs):
        runtime = kwargs["runtime"]
        checkout = kwargs["checkout"]
        supplied = kwargs["pull_request"]
        current = cli_module._load_binding(runtime.binding.path)
        bound = _pull_request_binding(repository)
        cli_module._write_binding(runtime.binding.path, issue=current.issue, pull_request=bound)
        loaded = cli_module._load_binding(runtime.binding.path)
        if supplied is not None:
            cli_module._assert_pull_request_binding_matches_inputs(
                actual=loaded.pull_request,
                supplied=supplied,
            )
        if kwargs["verify_checkout"]:
            cli_module._verify_destination_checkout_matches_binding(
                checkout=checkout,
                binding=loaded.pull_request,
                checkout_head_branch=loaded.pull_request.head_branch,
            )
        return loaded

    monkeypatch.setattr(cli_module, "_ensure_runtime_pull_request_binding", ensure_binding)

    start = _invoke(
        ["job", "start", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert start.exit_code == 0, start.stdout
    start_payload = json.loads(start.stdout)
    assert start_payload["next_action"] == "handoff-candidate"
    assert start_payload["pull_request_binding_present"] is True
    assert start_payload["request"]["candidate_budget"] == 2
    assert start_payload["request"]["model_subset"] == ["candidate"]
    issue_request_path = (
        Path(environment[STATE_ROOT_ENV]) / "optimize-7" / "optimize-job-poc-issue-request.json"
    )
    assert issue_request_path.exists()

    handoff_one = _invoke(
        [
            "job",
            "handoff",
            "--repository",
            str(repository),
            "--event",
            str(event),
            "--candidate",
            "candidate-one",
            "--model",
            "candidate",
            "--hypothesis",
            "first improvement",
        ],
        environment,
    )
    assert handoff_one.exit_code == 0, handoff_one.stdout
    workspace_one = Path(json.loads(handoff_one.stdout)["candidate"]["workspace"])
    (workspace_one / "src" / "main.py").write_text("VALUE = 'candidate-one'\n", encoding="utf-8")
    (workspace_one / "tests" / "test_main.py").write_text(
        "def test_one():\n    assert True\n",
        encoding="utf-8",
    )

    complete_one = _invoke(
        [
            "job",
            "complete",
            "--repository",
            str(repository),
            "--event",
            str(event),
            "--candidate",
            "candidate-one",
        ],
        environment,
    )
    assert complete_one.exit_code == 0, complete_one.stdout
    assert json.loads(complete_one.stdout)["next_action"] == "handoff-candidate"

    handoff_two = _invoke(
        [
            "job",
            "handoff",
            "--repository",
            str(repository),
            "--event",
            str(event),
            "--candidate",
            "candidate-two",
            "--model",
            "candidate",
            "--hypothesis",
            "second improvement",
            "--parent",
            "candidate-one",
        ],
        environment,
    )
    assert handoff_two.exit_code == 0, handoff_two.stdout
    workspace_two = Path(json.loads(handoff_two.stdout)["candidate"]["workspace"])
    (workspace_two / "src" / "main.py").write_text("VALUE = 'candidate-two'\n", encoding="utf-8")
    (workspace_two / "tests" / "test_main.py").write_text(
        "def test_two():\n    assert True\n",
        encoding="utf-8",
    )

    complete_two = _invoke(
        [
            "job",
            "complete",
            "--repository",
            str(repository),
            "--event",
            str(event),
            "--candidate",
            "candidate-two",
        ],
        environment,
    )
    assert complete_two.exit_code == 0, complete_two.stdout
    complete_two_payload = json.loads(complete_two.stdout)
    assert complete_two_payload["next_action"] == "finish"
    assert complete_two_payload["decision"]["provisional_winner_id"] == "candidate-two"

    finish = _invoke(
        ["job", "finish", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert finish.exit_code == 0, finish.stdout
    finish_payload = json.loads(finish.stdout)
    assert finish_payload["job"]["terminal_outcome"] == "winner"
    assert finish_payload["decision"]["winner_id"] == "candidate-two"
    assert finish_payload["next_action"] == "terminal"
    assert harness.foundry.baseline_calls == 1
    assert harness.foundry.candidate_calls == ["candidate-one", "candidate-two"]
    assert harness.foundry.validating_calls == ["candidate-two"]
    assert harness.foundry.cleanup_calls == ["draft-one", "draft-two"]
    assert (repository / "src" / "main.py").read_text(encoding="utf-8") == "VALUE = 'candidate-two'\n"


def test_job_no_winner_binds_pull_request_and_closes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, _, environment = _create_runtime_repository(tmp_path)
    _checkout_branch(repository, "copilot/job-7")
    event = _write_issue_event(
        tmp_path,
        body=_issue_body(candidate_budget=2, model_lines=("candidate",)),
    )
    binding = _write_binding(tmp_path / "binding.json")
    environment[cli_module._GITHUB_BINDING_ENV] = str(binding)
    harness = ControllerHarness(
        candidate_results={
            "candidate-one": RunResult(
                status="ok",
                evaluation=_summary("development", 0.53, improved=1),
                draft_id="draft-one",
            ),
            "candidate-two": RunResult(
                status="ok",
                evaluation=_summary("development", 0.54, improved=1),
                draft_id="draft-two",
            ),
        },
        validating_results={},
        cleanup_results={
            "draft-one": [CleanupResult(success=True, receipt_id="cleanup-one")],
            "draft-two": [CleanupResult(success=True, receipt_id="cleanup-two")],
        },
    )
    monkeypatch.setattr(cli_module, "capture_route_fingerprint", lambda **_: _route())
    monkeypatch.setattr(cli_module, "build_runtime_controller", harness.builder)
    def ensure_binding(**kwargs):
        runtime = kwargs["runtime"]
        current = cli_module._load_binding(runtime.binding.path)
        cli_module._write_binding(
            runtime.binding.path,
            issue=current.issue,
            pull_request=_pull_request_binding(repository),
        )
        loaded = cli_module._load_binding(runtime.binding.path)
        supplied = kwargs["pull_request"]
        if supplied is not None:
            cli_module._assert_pull_request_binding_matches_inputs(
                actual=loaded.pull_request,
                supplied=supplied,
            )
        return loaded

    monkeypatch.setattr(cli_module, "_ensure_runtime_pull_request_binding", ensure_binding)

    assert _invoke(
        ["job", "start", "--repository", str(repository), "--event", str(event)],
        environment,
    ).exit_code == 0

    for candidate_id, value in (
        ("candidate-one", "one"),
        ("candidate-two", "two"),
    ):
        handoff = _invoke(
            [
                "job",
                "handoff",
                "--repository",
                str(repository),
                "--event",
                str(event),
                "--candidate",
                candidate_id,
                "--model",
                "candidate",
                "--hypothesis",
                f"{candidate_id} improvement",
            ],
            environment,
        )
        workspace = Path(json.loads(handoff.stdout)["candidate"]["workspace"])
        (workspace / "src" / "main.py").write_text(f"VALUE = '{value}'\n", encoding="utf-8")
        _invoke(
            [
                "job",
                "complete",
                "--repository",
                str(repository),
                "--event",
                str(event),
                "--candidate",
                candidate_id,
            ],
            environment,
        )

    finish = _invoke(
        ["job", "finish", "--repository", str(repository), "--event", str(event)],
        environment,
    )

    assert finish.exit_code == 0, finish.stdout
    payload = json.loads(finish.stdout)
    assert payload["job"]["terminal_outcome"] == "no_winner"
    assert payload["pull_request_binding_present"] is True
    assert payload["next_action"] == "terminal"
    assert harness.closure.calls == ["optimize-7"]
    assert (repository / "src" / "main.py").read_text(encoding="utf-8") == "VALUE = 'base'\n"
    binding_payload = json.loads(binding.read_text(encoding="utf-8"))
    assert binding_payload["pull_request"]["pull_request_number"] == 11
    assert binding_payload["pull_request"]["expected_author_login"] == "copilot-swe-agent[bot]"
    assert binding_payload["pull_request"]["expected_author_type"] == "Bot"


def test_job_finish_winner_requires_destination_checkout_to_match_bound_pull_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, _, environment = _create_runtime_repository(tmp_path)
    _checkout_branch(repository, "copilot/job-7")
    event = _write_issue_event(
        tmp_path,
        body=_issue_body(candidate_budget=1, model_lines=("candidate",)),
    )
    binding = _write_binding(
        tmp_path / "binding.json",
        pull_request=_pull_request_binding(repository, head_sha="f" * 40),
    )
    environment[cli_module._GITHUB_BINDING_ENV] = str(binding)
    harness = ControllerHarness(
        candidate_results={
            "candidate-one": RunResult(
                status="ok",
                evaluation=_summary("development", 0.65, improved=2),
                draft_id="draft-one",
            ),
        },
        validating_results={
            "candidate-one": RunResult(
                status="ok",
                evaluation=_summary("validating", 0.64, improved=2),
            )
        },
        cleanup_results={
            "draft-one": [CleanupResult(success=True, receipt_id="cleanup-one")],
        },
    )

    class StableBroker:
        def __init__(self, *, socket_path: Path) -> None:
            del socket_path

        def ensure_pull_request_binding(
            self,
            *,
            request_id: str,
            head_branch: str,
            timeout_seconds: float,
        ) -> None:
            del request_id, head_branch, timeout_seconds

    monkeypatch.setattr(cli_module, "capture_route_fingerprint", lambda **_: _route())
    monkeypatch.setattr(cli_module, "build_runtime_controller", harness.builder)
    monkeypatch.setattr(cli_module, "UnixSocketBrokerClient", StableBroker)

    start = _invoke(
        ["job", "start", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert start.exit_code == 0, start.stdout

    handoff = _invoke(
        [
            "job",
            "handoff",
            "--repository",
            str(repository),
            "--event",
            str(event),
            "--candidate",
            "candidate-one",
            "--model",
            "candidate",
            "--hypothesis",
            "improvement",
        ],
        environment,
    )
    workspace = Path(json.loads(handoff.stdout)["candidate"]["workspace"])
    (workspace / "src" / "main.py").write_text("VALUE = 'winner'\n", encoding="utf-8")
    (workspace / "tests" / "test_main.py").write_text(
        "def test_winner():\n    assert True\n",
        encoding="utf-8",
    )
    complete = _invoke(
        [
            "job",
            "complete",
            "--repository",
            str(repository),
            "--event",
            str(event),
            "--candidate",
            "candidate-one",
        ],
        environment,
    )
    assert complete.exit_code == 0, complete.stdout

    finish = _invoke(
        ["job", "finish", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert finish.exit_code == 2, finish.stdout
    payload = json.loads(finish.stdout)
    assert payload["status"] == "blocked"
    assert "destination checkout HEAD does not match" in payload["error"]
    assert (repository / "src" / "main.py").read_text(encoding="utf-8") == "VALUE = 'base'\n"


def test_job_finish_winner_blocks_when_broker_refresh_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, _, environment = _create_runtime_repository(tmp_path)
    _checkout_branch(repository, "copilot/job-7")
    event = _write_issue_event(
        tmp_path,
        body=_issue_body(candidate_budget=1, model_lines=("candidate",)),
    )
    binding = _write_binding(
        tmp_path / "binding.json",
        pull_request=_pull_request_binding(repository),
    )
    environment[cli_module._GITHUB_BINDING_ENV] = str(binding)
    harness = ControllerHarness(
        candidate_results={
            "candidate-one": RunResult(
                status="ok",
                evaluation=_summary("development", 0.65, improved=2),
                draft_id="draft-one",
            ),
        },
        validating_results={
            "candidate-one": RunResult(
                status="ok",
                evaluation=_summary("validating", 0.64, improved=2),
            )
        },
        cleanup_results={
            "draft-one": [CleanupResult(success=True, receipt_id="cleanup-one")],
        },
    )

    class UnavailableBroker:
        def __init__(self, *, socket_path: Path) -> None:
            del socket_path

        def ensure_pull_request_binding(
            self,
            *,
            request_id: str,
            head_branch: str,
            timeout_seconds: float,
        ) -> None:
            del request_id, head_branch, timeout_seconds
            raise cli_module.BrokerUnavailableError("broker refresh unavailable")

    monkeypatch.setattr(cli_module, "capture_route_fingerprint", lambda **_: _route())
    monkeypatch.setattr(cli_module, "build_runtime_controller", harness.builder)
    monkeypatch.setattr(cli_module, "UnixSocketBrokerClient", UnavailableBroker)

    start = _invoke(
        ["job", "start", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert start.exit_code == 0, start.stdout

    handoff = _invoke(
        [
            "job",
            "handoff",
            "--repository",
            str(repository),
            "--event",
            str(event),
            "--candidate",
            "candidate-one",
            "--model",
            "candidate",
            "--hypothesis",
            "improvement",
        ],
        environment,
    )
    workspace = Path(json.loads(handoff.stdout)["candidate"]["workspace"])
    (workspace / "src" / "main.py").write_text("VALUE = 'winner'\n", encoding="utf-8")
    (workspace / "tests" / "test_main.py").write_text(
        "def test_winner():\n    assert True\n",
        encoding="utf-8",
    )
    complete = _invoke(
        [
            "job",
            "complete",
            "--repository",
            str(repository),
            "--event",
            str(event),
            "--candidate",
            "candidate-one",
        ],
        environment,
    )
    assert complete.exit_code == 0, complete.stdout

    finish = _invoke(
        ["job", "finish", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert finish.exit_code == 2, finish.stdout
    payload = json.loads(finish.stdout)
    assert payload["status"] == "blocked"
    assert "broker refresh unavailable" in payload["error"]
    assert (repository / "src" / "main.py").read_text(encoding="utf-8") == "VALUE = 'base'\n"
    paths = load_runtime_paths(repository, environment=environment, job_id="optimize-7")
    assert JobStateStore(paths.job_state_path).load().projection_receipt is None


def test_job_start_blocks_on_baseline_comment_outage_and_replays_after_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, _, environment = _create_runtime_repository(tmp_path)
    event = _write_issue_event(
        tmp_path,
        body=_issue_body(candidate_budget=1, model_lines=("candidate",)),
    )
    broker = _FlakyBrokerClient(
        comment_failures={
            "baseline": [BrokerUnavailableError("broker socket is not available")]
        }
    )
    harness = BrokerControllerHarness(
        broker=broker,
        candidate_results={},
        validating_results={},
        cleanup_results={},
    )
    monkeypatch.setattr(cli_module, "capture_route_fingerprint", lambda **_: _route())
    monkeypatch.setattr(cli_module, "build_runtime_controller", harness.builder)

    first = _invoke(
        ["job", "start", "--repository", str(repository), "--event", str(event)],
        environment,
    )

    assert first.exit_code == 2, first.stdout
    payload = json.loads(first.stdout)
    assert payload["status"] == "blocked"
    assert "broker socket is not available" in payload["error"]
    paths = load_runtime_paths(repository, environment=environment, job_id="optimize-7")
    state = JobStateStore(paths.job_state_path).load()
    assert state.baseline is not None
    assert state.baseline.comment_receipt is None
    assert not cli_module._issue_request_path(paths.job_root).exists()
    assert RuntimeSidecarStore(paths.sidecar_path).load().comments == {}

    status = _invoke(
        ["job", "status", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert status.exit_code == 2, status.stdout
    assert "persisted optimize-job issue request is unavailable" in json.loads(status.stdout)["error"]

    resume = _invoke(
        ["job", "resume", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert resume.exit_code == 2, resume.stdout
    assert "persisted optimize-job issue request is unavailable" in json.loads(resume.stdout)["error"]

    replay = _invoke(
        ["job", "start", "--repository", str(repository), "--event", str(event)],
        environment,
    )

    assert replay.exit_code == 0, replay.stdout
    replay_payload = json.loads(replay.stdout)
    assert replay_payload["baseline"]["comment_recorded"] is True
    assert replay_payload["next_action"] == "handoff-candidate"
    assert cli_module._issue_request_path(paths.job_root).exists()
    assert harness.foundry.baseline_calls == 1
    assert broker.upsert_count_by_kind["baseline"] == 2


def test_job_complete_blocks_on_candidate_comment_outage_and_replays_after_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, _, environment = _create_runtime_repository(tmp_path)
    event = _write_issue_event(
        tmp_path,
        body=_issue_body(candidate_budget=1, model_lines=("candidate",)),
    )
    broker_token = "github_pat_abcdefghijklmnopqrst"
    broker = _FlakyBrokerClient(
        comment_failures={
            "candidate-candidate-one": [
                BrokerRemoteError(
                    f"BrokerPolicyError: {broker_token} refused candidate comment"
                )
            ]
        }
    )
    harness = BrokerControllerHarness(
        broker=broker,
        candidate_results={
            "candidate-one": RunResult(
                status="ok",
                evaluation=_summary("development", 0.65, improved=2),
                draft_id="draft-one",
            ),
        },
        validating_results={},
        cleanup_results={
            "draft-one": [CleanupResult(success=True, receipt_id="cleanup-one")],
        },
    )
    monkeypatch.setattr(cli_module, "capture_route_fingerprint", lambda **_: _route())
    monkeypatch.setattr(cli_module, "build_runtime_controller", harness.builder)

    start = _invoke(
        ["job", "start", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert start.exit_code == 0, start.stdout

    handoff = _invoke(
        [
            "job",
            "handoff",
            "--repository",
            str(repository),
            "--event",
            str(event),
            "--candidate",
            "candidate-one",
            "--model",
            "candidate",
            "--hypothesis",
            "working improvement",
        ],
        environment,
    )
    workspace = Path(json.loads(handoff.stdout)["candidate"]["workspace"])
    (workspace / "src" / "main.py").write_text("VALUE = 'winner'\n", encoding="utf-8")

    first = _invoke(
        [
            "job",
            "complete",
            "--repository",
            str(repository),
            "--event",
            str(event),
            "--candidate",
            "candidate-one",
        ],
        environment,
    )

    assert first.exit_code == 2, first.stdout
    payload = json.loads(first.stdout)
    assert payload["status"] == "blocked"
    assert "BrokerPolicyError: ****** refused candidate comment" in payload["error"]
    assert broker_token not in payload["error"]
    paths = load_runtime_paths(repository, environment=environment, job_id="optimize-7")
    state = JobStateStore(paths.job_state_path).load()
    candidate = state.candidate("candidate-one")
    assert candidate is not None
    assert candidate.assessment is not None
    assert candidate.comment_receipt is None
    assert candidate.cleanup_receipt is None

    status = _invoke(
        ["job", "status", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert json.loads(status.stdout)["next_action"] == "complete-candidate"

    resume = _invoke(
        ["job", "resume", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert json.loads(resume.stdout)["next_action"] == "complete-candidate"

    replay = _invoke(
        [
            "job",
            "complete",
            "--repository",
            str(repository),
            "--event",
            str(event),
            "--candidate",
            "candidate-one",
        ],
        environment,
    )

    assert replay.exit_code == 0, replay.stdout
    replay_payload = json.loads(replay.stdout)
    assert replay_payload["next_action"] == "blocked"
    assert replay_payload["blocker"] == "pull request binding is required for draft projection"
    state = JobStateStore(paths.job_state_path).load()
    candidate = state.candidate("candidate-one")
    assert candidate is not None
    assert candidate.comment_receipt is not None
    assert candidate.cleanup_receipt is not None
    assert harness.foundry.candidate_calls == ["candidate-one"]
    assert harness.foundry.cleanup_calls == ["draft-one"]
    assert broker.upsert_count_by_kind["candidate-candidate-one"] == 2


def test_job_finish_blocks_on_final_comment_outage_and_replays_after_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, _, environment = _create_runtime_repository(tmp_path)
    _checkout_branch(repository, "copilot/job-7")
    event = _write_issue_event(
        tmp_path,
        body=_issue_body(candidate_budget=1, model_lines=("candidate",)),
    )
    binding = _write_binding(
        tmp_path / "binding.json",
        pull_request=_pull_request_binding(repository),
    )
    environment[cli_module._GITHUB_BINDING_ENV] = str(binding)
    broker = _FlakyBrokerClient(
        comment_failures={
            "final": [BrokerRemoteError("BrokerWriteError: final comment refused")]
        }
    )
    harness = BrokerControllerHarness(
        broker=broker,
        candidate_results={
            "candidate-one": RunResult(
                status="ok",
                evaluation=_summary("development", 0.53, improved=1),
                draft_id="draft-one",
            ),
        },
        validating_results={},
        cleanup_results={
            "draft-one": [CleanupResult(success=True, receipt_id="cleanup-one")],
        },
    )
    monkeypatch.setattr(cli_module, "capture_route_fingerprint", lambda **_: _route())
    monkeypatch.setattr(cli_module, "build_runtime_controller", harness.builder)

    assert _invoke(
        ["job", "start", "--repository", str(repository), "--event", str(event)],
        environment,
    ).exit_code == 0
    handoff = _invoke(
        [
            "job",
            "handoff",
            "--repository",
            str(repository),
            "--event",
            str(event),
            "--candidate",
            "candidate-one",
            "--model",
            "candidate",
            "--hypothesis",
            "low-confidence improvement",
        ],
        environment,
    )
    workspace = Path(json.loads(handoff.stdout)["candidate"]["workspace"])
    (workspace / "src" / "main.py").write_text("VALUE = 'candidate-one'\n", encoding="utf-8")
    complete = _invoke(
        [
            "job",
            "complete",
            "--repository",
            str(repository),
            "--event",
            str(event),
            "--candidate",
            "candidate-one",
        ],
        environment,
    )
    assert complete.exit_code == 0, complete.stdout

    first = _invoke(
        ["job", "finish", "--repository", str(repository), "--event", str(event)],
        environment,
    )

    assert first.exit_code == 2, first.stdout
    payload = json.loads(first.stdout)
    assert payload["status"] == "blocked"
    assert "final comment refused" in payload["error"]
    paths = load_runtime_paths(repository, environment=environment, job_id="optimize-7")
    state = JobStateStore(paths.job_state_path).load()
    assert state.decision is not None
    assert state.final_comment_receipt is None
    assert state.no_winner_receipt is None
    assert state.terminal_outcome is None
    assert broker.close_attempts == 0
    assert "final" not in RuntimeSidecarStore(paths.sidecar_path).load().comments

    status = _invoke(
        ["job", "status", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert json.loads(status.stdout)["next_action"] == "finish"

    resume = _invoke(
        ["job", "resume", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert json.loads(resume.stdout)["next_action"] == "finish"

    replay = _invoke(
        ["job", "finish", "--repository", str(repository), "--event", str(event)],
        environment,
    )

    assert replay.exit_code == 0, replay.stdout
    replay_payload = json.loads(replay.stdout)
    assert replay_payload["job"]["terminal_outcome"] == "no_winner"
    assert replay_payload["next_action"] == "terminal"
    state = JobStateStore(paths.job_state_path).load()
    assert state.final_comment_receipt is not None
    assert state.no_winner_receipt is not None
    assert broker.upsert_count_by_kind["final"] == 2
    assert broker.close_attempts == 1
    assert broker.close_calls == [FinalDecision.NO_WINNER]
    assert (repository / "src" / "main.py").read_text(encoding="utf-8") == "VALUE = 'base'\n"


def test_job_finish_blocks_on_no_winner_closure_outage_and_replays_after_recovery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, _, environment = _create_runtime_repository(tmp_path)
    _checkout_branch(repository, "copilot/job-7")
    event = _write_issue_event(
        tmp_path,
        body=_issue_body(candidate_budget=1, model_lines=("candidate",)),
    )
    binding = _write_binding(
        tmp_path / "binding.json",
        pull_request=_pull_request_binding(repository),
    )
    environment[cli_module._GITHUB_BINDING_ENV] = str(binding)
    broker = _FlakyBrokerClient(
        close_failures=[BrokerUnavailableError("broker socket is not reachable")]
    )
    harness = BrokerControllerHarness(
        broker=broker,
        candidate_results={
            "candidate-one": RunResult(
                status="ok",
                evaluation=_summary("development", 0.53, improved=1),
                draft_id="draft-one",
            ),
        },
        validating_results={},
        cleanup_results={
            "draft-one": [CleanupResult(success=True, receipt_id="cleanup-one")],
        },
    )
    monkeypatch.setattr(cli_module, "capture_route_fingerprint", lambda **_: _route())
    monkeypatch.setattr(cli_module, "build_runtime_controller", harness.builder)

    assert _invoke(
        ["job", "start", "--repository", str(repository), "--event", str(event)],
        environment,
    ).exit_code == 0
    handoff = _invoke(
        [
            "job",
            "handoff",
            "--repository",
            str(repository),
            "--event",
            str(event),
            "--candidate",
            "candidate-one",
            "--model",
            "candidate",
            "--hypothesis",
            "low-confidence improvement",
        ],
        environment,
    )
    workspace = Path(json.loads(handoff.stdout)["candidate"]["workspace"])
    (workspace / "src" / "main.py").write_text("VALUE = 'candidate-one'\n", encoding="utf-8")
    complete = _invoke(
        [
            "job",
            "complete",
            "--repository",
            str(repository),
            "--event",
            str(event),
            "--candidate",
            "candidate-one",
        ],
        environment,
    )
    assert complete.exit_code == 0, complete.stdout

    first = _invoke(
        ["job", "finish", "--repository", str(repository), "--event", str(event)],
        environment,
    )

    assert first.exit_code == 2, first.stdout
    payload = json.loads(first.stdout)
    assert payload["status"] == "blocked"
    assert "broker socket is not reachable" in payload["error"]
    paths = load_runtime_paths(repository, environment=environment, job_id="optimize-7")
    state = JobStateStore(paths.job_state_path).load()
    assert state.final_comment_receipt is not None
    assert state.no_winner_receipt is None
    assert state.terminal_outcome is None
    assert broker.upsert_count_by_kind["final"] == 1
    assert broker.close_attempts == 1

    status = _invoke(
        ["job", "status", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert json.loads(status.stdout)["next_action"] == "finish"

    resume = _invoke(
        ["job", "resume", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert json.loads(resume.stdout)["next_action"] == "finish"

    replay = _invoke(
        ["job", "finish", "--repository", str(repository), "--event", str(event)],
        environment,
    )

    assert replay.exit_code == 0, replay.stdout
    replay_payload = json.loads(replay.stdout)
    assert replay_payload["job"]["terminal_outcome"] == "no_winner"
    assert replay_payload["next_action"] == "terminal"
    state = JobStateStore(paths.job_state_path).load()
    assert state.no_winner_receipt is not None
    assert broker.upsert_count_by_kind["final"] == 1
    assert broker.close_attempts == 2
    assert broker.close_calls == [FinalDecision.NO_WINNER]
    assert (repository / "src" / "main.py").read_text(encoding="utf-8") == "VALUE = 'base'\n"


def test_job_finish_winner_blocks_on_live_pull_request_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, _, environment = _create_runtime_repository(tmp_path)
    _checkout_branch(repository, "copilot/job-7")
    event = _write_issue_event(
        tmp_path,
        body=_issue_body(candidate_budget=1, model_lines=("candidate",)),
    )
    binding = _write_binding(
        tmp_path / "binding.json",
        pull_request=_pull_request_binding(repository),
    )
    environment[cli_module._GITHUB_BINDING_ENV] = str(binding)
    harness = ControllerHarness(
        candidate_results={
            "candidate-one": RunResult(
                status="ok",
                evaluation=_summary("development", 0.65, improved=2),
                draft_id="draft-one",
            ),
        },
        validating_results={
            "candidate-one": RunResult(
                status="ok",
                evaluation=_summary("validating", 0.64, improved=2),
            )
        },
        cleanup_results={
            "draft-one": [CleanupResult(success=True, receipt_id="cleanup-one")],
        },
    )

    class DriftedBroker:
        def __init__(self, *, socket_path: Path) -> None:
            del socket_path

        def ensure_pull_request_binding(
            self,
            *,
            request_id: str,
            head_branch: str,
            timeout_seconds: float,
        ) -> None:
            del request_id, head_branch, timeout_seconds
            current = cli_module._load_binding(binding)
            cli_module._write_binding(
                binding,
                issue=current.issue,
                pull_request=_pull_request_binding(
                    repository,
                    base_branch="release/1.0",
                    expected_author_login="octocat",
                    expected_author_type="User",
                ),
            )

    monkeypatch.setattr(cli_module, "capture_route_fingerprint", lambda **_: _route())
    monkeypatch.setattr(cli_module, "build_runtime_controller", harness.builder)
    monkeypatch.setattr(cli_module, "UnixSocketBrokerClient", DriftedBroker)

    start = _invoke(
        ["job", "start", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert start.exit_code == 0, start.stdout

    handoff = _invoke(
        [
            "job",
            "handoff",
            "--repository",
            str(repository),
            "--event",
            str(event),
            "--candidate",
            "candidate-one",
            "--model",
            "candidate",
            "--hypothesis",
            "improvement",
        ],
        environment,
    )
    workspace = Path(json.loads(handoff.stdout)["candidate"]["workspace"])
    (workspace / "src" / "main.py").write_text("VALUE = 'winner'\n", encoding="utf-8")
    (workspace / "tests" / "test_main.py").write_text(
        "def test_winner():\n    assert True\n",
        encoding="utf-8",
    )
    complete = _invoke(
        [
            "job",
            "complete",
            "--repository",
            str(repository),
            "--event",
            str(event),
            "--candidate",
            "candidate-one",
        ],
        environment,
    )
    assert complete.exit_code == 0, complete.stdout

    finish = _invoke(
        ["job", "finish", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert finish.exit_code == 2, finish.stdout
    payload = json.loads(finish.stdout)
    assert payload["status"] == "blocked"
    assert "live pull request binding drifted" in payload["error"]
    assert "base_branch" in payload["error"]
    assert "expected_author_login" in payload["error"]
    assert (repository / "src" / "main.py").read_text(encoding="utf-8") == "VALUE = 'base'\n"
    assert JobStateStore(
        load_runtime_paths(repository, environment=environment, job_id="optimize-7").job_state_path
    ).load().projection_receipt is None
    refreshed_binding = cli_module._load_binding(binding)
    assert refreshed_binding.pull_request is not None
    assert refreshed_binding.pull_request.base_branch == "release/1.0"
    assert refreshed_binding.pull_request.expected_author_login == "octocat"


def test_job_start_route_failure_does_not_leave_sticky_issue_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, _, environment = _create_runtime_repository(tmp_path)
    event = _write_issue_event(
        tmp_path,
        body=_issue_body(candidate_budget=2, model_lines=("candidate",)),
    )
    route_attempts = {"count": 0}

    def capture_route(**_: Any) -> RouteFingerprint:
        route_attempts["count"] += 1
        if route_attempts["count"] == 1:
            raise cli_module.RuntimeIntegrationError("Foundry authentication failed")
        return _route()

    harness = ControllerHarness(
        candidate_results={},
        validating_results={},
        cleanup_results={},
    )
    monkeypatch.setattr(cli_module, "capture_route_fingerprint", capture_route)
    monkeypatch.setattr(cli_module, "build_runtime_controller", harness.builder)

    first = _invoke(
        ["job", "start", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert first.exit_code == 2, first.stdout
    job_root = Path(environment[STATE_ROOT_ENV]) / "optimize-7"
    assert not (job_root / "optimize-job-poc-issue-request.json").exists()
    assert not (job_root / "optimize-job-poc-state.json").exists()

    event.write_text(
        json.dumps(
            {
                "repository": {
                    "full_name": "example-org/example-agent",
                    "id": 123456789,
                },
                "issue": {
                    "number": 7,
                    "body": _issue_body(candidate_budget=1, model_lines=("candidate",)),
                },
            }
        ),
        encoding="utf-8",
    )
    second = _invoke(
        ["job", "start", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert second.exit_code == 0, second.stdout
    second_payload = json.loads(second.stdout)
    assert second_payload["request"]["candidate_budget"] == 1
    assert (job_root / "optimize-job-poc-issue-request.json").exists()


def test_job_replay_status_and_resume_are_idempotent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, _, environment = _create_runtime_repository(tmp_path)
    _checkout_branch(repository, "copilot/job-7")
    event = _write_issue_event(
        tmp_path,
        body=_issue_body(candidate_budget=1, model_lines=("candidate",)),
    )
    binding = _write_binding(
        tmp_path / "binding.json",
        pull_request=_pull_request_binding(repository),
    )
    environment[cli_module._GITHUB_BINDING_ENV] = str(binding)
    harness = ControllerHarness(
        candidate_results={
            "candidate-one": RunResult(
                status="ok",
                evaluation=_summary("development", 0.65, improved=2),
                draft_id="draft-one",
            ),
        },
        validating_results={
            "candidate-one": RunResult(
                status="ok",
                evaluation=_summary("validating", 0.64, improved=2),
            )
        },
        cleanup_results={
            "draft-one": [CleanupResult(success=True, receipt_id="cleanup-one")],
        },
    )

    class StableBroker:
        def __init__(self, *, socket_path: Path) -> None:
            del socket_path

        def ensure_pull_request_binding(
            self,
            *,
            request_id: str,
            head_branch: str,
            timeout_seconds: float,
        ) -> None:
            del request_id, head_branch, timeout_seconds

    monkeypatch.setattr(cli_module, "capture_route_fingerprint", lambda **_: _route())
    monkeypatch.setattr(cli_module, "build_runtime_controller", harness.builder)
    monkeypatch.setattr(cli_module, "UnixSocketBrokerClient", StableBroker)

    start = _invoke(
        ["job", "start", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert json.loads(start.stdout)["next_action"] == "handoff-candidate"

    status = _invoke(
        ["job", "status", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert status.exit_code == 0, status.stdout
    assert json.loads(status.stdout)["next_action"] == "handoff-candidate"

    handoff = _invoke(
        [
            "job",
            "handoff",
            "--repository",
            str(repository),
            "--event",
            str(event),
            "--candidate",
            "candidate-one",
            "--model",
            "candidate",
            "--hypothesis",
            "working improvement",
        ],
        environment,
    )
    workspace = Path(json.loads(handoff.stdout)["candidate"]["workspace"])
    (workspace / "src" / "main.py").write_text("VALUE = 'winner'\n", encoding="utf-8")

    status_pending = _invoke(
        ["job", "status", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert json.loads(status_pending.stdout)["next_action"] == "complete-candidate"

    resume_pending = _invoke(
        ["job", "resume", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert json.loads(resume_pending.stdout)["next_action"] == "complete-candidate"

    complete = _invoke(
        [
            "job",
            "complete",
            "--repository",
            str(repository),
            "--event",
            str(event),
            "--candidate",
            "candidate-one",
        ],
        environment,
    )
    assert json.loads(complete.stdout)["next_action"] == "finish"

    resume_ready = _invoke(
        ["job", "resume", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    resume_ready_payload = json.loads(resume_ready.stdout)
    assert resume_ready_payload["resumed"] is True
    assert resume_ready_payload["next_action"] == "finish"

    finish_first = _invoke(
        ["job", "finish", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert finish_first.exit_code == 0, finish_first.stdout
    assert json.loads(finish_first.stdout)["next_action"] == "terminal"

    finish_second = _invoke(
        ["job", "finish", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert finish_second.exit_code == 0, finish_second.stdout
    assert json.loads(finish_second.stdout)["next_action"] == "terminal"
    assert harness.foundry.baseline_calls == 1
    assert harness.foundry.candidate_calls == ["candidate-one"]
    assert harness.foundry.validating_calls == ["candidate-one"]
    assert harness.foundry.cleanup_calls == ["draft-one"]


def test_job_repository_checks_mode_blocks_until_binding_and_projects_recommendation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, _, environment = _create_runtime_repository(tmp_path)
    _checkout_branch(repository, "copilot/job-7")
    check_spec = "command: python -m pytest tests -q"
    event = _write_issue_event(
        tmp_path,
        body=_issue_body(
            candidate_budget=1,
            model_lines=("candidate",),
            verification_check_lines=(check_spec,),
        ),
    )
    harness = ControllerHarness(
        candidate_results={},
        validating_results={},
        cleanup_results={},
        check_results={
            "candidate-one": (
                _check_result(
                    check_spec,
                    passed=True,
                    summary="Command passed.",
                ),
            ),
        },
    )

    class StableBroker:
        def __init__(self, *, socket_path: Path) -> None:
            del socket_path

        def ensure_pull_request_binding(
            self,
            *,
            request_id: str,
            head_branch: str,
            timeout_seconds: float,
        ) -> None:
            del request_id, head_branch, timeout_seconds

    monkeypatch.setattr(cli_module, "capture_route_fingerprint", lambda **_: _route())
    monkeypatch.setattr(cli_module, "build_runtime_controller", harness.builder)
    monkeypatch.setattr(cli_module, "UnixSocketBrokerClient", StableBroker)

    start = _invoke(
        ["job", "start", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert start.exit_code == 0, start.stdout
    start_payload = json.loads(start.stdout)
    assert start_payload["job"]["verification"]["mode"] == "repository_checks"
    assert start_payload["job"]["verification"]["provenance"] == ["issue_repository_checks"]
    assert start_payload["job"]["verification"]["repository_checks"] == [
        {"kind": "command", "value": "python -m pytest tests -q"}
    ]
    assert start_payload["baseline"]["evaluation"] is None
    baseline_body = harness.comments.bodies[baseline_marker_id("optimize-7")]
    assert "Verification plan" in baseline_body
    assert "No quantitative baseline will be claimed." in baseline_body

    handoff = _invoke(
        [
            "job",
            "handoff",
            "--repository",
            str(repository),
            "--event",
            str(event),
            "--candidate",
            "candidate-one",
            "--model",
            "candidate",
            "--hypothesis",
            "working improvement",
        ],
        environment,
    )
    workspace = Path(json.loads(handoff.stdout)["candidate"]["workspace"])
    (workspace / "src" / "main.py").write_text(
        "VALUE = 'candidate-one'\n",
        encoding="utf-8",
    )

    complete = _invoke(
        [
            "job",
            "complete",
            "--repository",
            str(repository),
            "--event",
            str(event),
            "--candidate",
            "candidate-one",
        ],
        environment,
    )
    assert complete.exit_code == 0, complete.stdout
    complete_payload = json.loads(complete.stdout)
    assert complete_payload["next_action"] == "blocked"
    assert complete_payload["blocker"] == "pull request binding is required for draft projection"
    assert complete_payload["decision"]["outcome"] == "recommended"
    assert complete_payload["decision"]["selected_candidate_id"] == "candidate-one"
    assert complete_payload["decision"]["winner_id"] is None
    assert complete_payload["candidates"][0]["assessment"]["outcome"] == "keep"
    assert complete_payload["candidates"][0]["repository_checks"] == [
        {
            "kind": "command",
            "value": "python -m pytest tests -q",
            "passed": True,
            "exit_code": 0,
            "duration_seconds": 0.25,
            "summary": "Command passed.",
        }
    ]

    status = _invoke(
        ["job", "status", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    status_payload = json.loads(status.stdout)
    assert status_payload["next_action"] == "blocked"
    assert status_payload["job"]["verification"]["mode"] == "repository_checks"

    resume = _invoke(
        ["job", "resume", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    resume_payload = json.loads(resume.stdout)
    assert resume_payload["next_action"] == "blocked"
    assert resume_payload["resumed"] is True

    binding = _write_binding(
        tmp_path / "binding.json",
        pull_request=_pull_request_binding(repository),
    )
    environment[cli_module._GITHUB_BINDING_ENV] = str(binding)

    finish = _invoke(
        ["job", "finish", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert finish.exit_code == 0, finish.stdout
    finish_payload = json.loads(finish.stdout)
    assert finish_payload["job"]["terminal_outcome"] == "recommended"
    assert finish_payload["decision"]["outcome"] == "recommended"
    assert finish_payload["decision"]["winner_id"] is None
    assert finish_payload["next_action"] == "terminal"
    assert harness.closure.calls == []
    final_body = harness.comments.bodies[final_marker_id("optimize-7")]
    assert "Review the projected draft PR changes and merge only after human approval." in final_body
    assert "Provisional winner" not in final_body
    assert "Final winner" not in final_body
    assert (repository / "src" / "main.py").read_text(encoding="utf-8") == "VALUE = 'candidate-one'\n"


def test_job_repository_checks_failure_never_recommends_and_closes_no_winner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, _, environment = _create_runtime_repository(tmp_path)
    _checkout_branch(repository, "copilot/job-7")
    check_spec = "command: python -m pytest tests -q"
    event = _write_issue_event(
        tmp_path,
        body=_issue_body(
            candidate_budget=1,
            model_lines=("candidate",),
            verification_check_lines=(check_spec,),
        ),
    )
    binding = _write_binding(
        tmp_path / "binding.json",
        pull_request=_pull_request_binding(repository),
    )
    environment[cli_module._GITHUB_BINDING_ENV] = str(binding)
    harness = ControllerHarness(
        candidate_results={},
        validating_results={},
        cleanup_results={},
        check_results={
            "candidate-one": (
                _check_result(
                    check_spec,
                    passed=False,
                    exit_code=1,
                    summary="Command exited with code 1.",
                ),
            ),
        },
    )
    monkeypatch.setattr(cli_module, "capture_route_fingerprint", lambda **_: _route())
    monkeypatch.setattr(cli_module, "build_runtime_controller", harness.builder)

    start = _invoke(
        ["job", "start", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert start.exit_code == 0, start.stdout
    assert json.loads(start.stdout)["job"]["verification"]["mode"] == "repository_checks"

    handoff = _invoke(
        [
            "job",
            "handoff",
            "--repository",
            str(repository),
            "--event",
            str(event),
            "--candidate",
            "candidate-one",
            "--model",
            "candidate",
            "--hypothesis",
            "failing checks",
        ],
        environment,
    )
    workspace = Path(json.loads(handoff.stdout)["candidate"]["workspace"])
    (workspace / "src" / "main.py").write_text(
        "VALUE = 'candidate-one'\n",
        encoding="utf-8",
    )

    complete = _invoke(
        [
            "job",
            "complete",
            "--repository",
            str(repository),
            "--event",
            str(event),
            "--candidate",
            "candidate-one",
        ],
        environment,
    )
    assert complete.exit_code == 0, complete.stdout
    complete_payload = json.loads(complete.stdout)
    assert complete_payload["next_action"] == "finish"
    assert complete_payload["decision"]["outcome"] == "no_winner"
    assert complete_payload["decision"]["selected_candidate_id"] is None
    assert complete_payload["decision"]["winner_id"] is None
    assert complete_payload["baseline"]["evaluation"] is None

    finish = _invoke(
        ["job", "finish", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert finish.exit_code == 0, finish.stdout
    finish_payload = json.loads(finish.stdout)
    assert finish_payload["job"]["terminal_outcome"] == "no_winner"
    assert finish_payload["decision"]["winner_id"] is None
    assert finish_payload["next_action"] == "terminal"
    assert harness.closure.calls == ["optimize-7"]
    final_body = harness.comments.bodies[final_marker_id("optimize-7")]
    assert "No candidate passed the configured repository checks." in final_body
    assert "Final winner" not in final_body
    assert (repository / "src" / "main.py").read_text(encoding="utf-8") == "VALUE = 'base'\n"


def test_job_none_mode_acknowledgement_persists_warning_and_projects_unverified_proposal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, _, environment = _create_runtime_repository(tmp_path)
    _checkout_branch(repository, "copilot/job-7")
    event = _write_issue_event(
        tmp_path,
        body=_issue_body(
            candidate_budget=1,
            model_lines=("candidate",),
            acknowledge_no_evidence=True,
        ),
    )
    harness = ControllerHarness(
        candidate_results={},
        validating_results={},
        cleanup_results={},
    )

    class StableBroker:
        def __init__(self, *, socket_path: Path) -> None:
            del socket_path

        def ensure_pull_request_binding(
            self,
            *,
            request_id: str,
            head_branch: str,
            timeout_seconds: float,
        ) -> None:
            del request_id, head_branch, timeout_seconds

    monkeypatch.setattr(cli_module, "capture_route_fingerprint", lambda **_: _route())
    monkeypatch.setattr(cli_module, "build_runtime_controller", harness.builder)
    monkeypatch.setattr(cli_module, "UnixSocketBrokerClient", StableBroker)

    start = _invoke(
        ["job", "start", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert start.exit_code == 0, start.stdout
    start_payload = json.loads(start.stdout)
    assert start_payload["job"]["verification"]["mode"] == "none"
    assert start_payload["job"]["verification"]["provenance"] == ["explicit_no_evidence"]
    assert start_payload["job"]["verification"]["warnings"] == [
        "No approved quantitative or repository verification evidence is available; any selected proposal remains unverified."
    ]
    assert start_payload["baseline"]["evaluation"] is None
    state_paths = load_runtime_paths(repository, environment=environment, job_id="optimize-7")
    persisted = JobStateStore(state_paths.job_state_path).load()
    assert persisted.verification is not None
    assert persisted.verification.mode == "none"

    handoff = _invoke(
        [
            "job",
            "handoff",
            "--repository",
            str(repository),
            "--event",
            str(event),
            "--candidate",
            "candidate-one",
            "--model",
            "candidate",
            "--hypothesis",
            "human-review proposal",
        ],
        environment,
    )
    workspace = Path(json.loads(handoff.stdout)["candidate"]["workspace"])
    (workspace / "src" / "main.py").write_text(
        "VALUE = 'candidate-one'\n",
        encoding="utf-8",
    )

    complete = _invoke(
        [
            "job",
            "complete",
            "--repository",
            str(repository),
            "--event",
            str(event),
            "--candidate",
            "candidate-one",
        ],
        environment,
    )
    assert complete.exit_code == 0, complete.stdout
    complete_payload = json.loads(complete.stdout)
    assert complete_payload["next_action"] == "blocked"
    assert complete_payload["blocker"] == "pull request binding is required for draft projection"
    assert complete_payload["decision"]["outcome"] == "proposed_unverified"
    assert complete_payload["decision"]["selected_candidate_id"] == "candidate-one"
    assert complete_payload["decision"]["winner_id"] is None

    status = _invoke(
        ["job", "status", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    status_payload = json.loads(status.stdout)
    assert status_payload["next_action"] == "blocked"
    assert status_payload["job"]["verification"]["warnings"] == start_payload["job"]["verification"]["warnings"]

    resume = _invoke(
        ["job", "resume", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    resume_payload = json.loads(resume.stdout)
    assert resume_payload["next_action"] == "blocked"
    assert resume_payload["resumed"] is True

    binding = _write_binding(
        tmp_path / "binding.json",
        pull_request=_pull_request_binding(repository),
    )
    environment[cli_module._GITHUB_BINDING_ENV] = str(binding)

    finish = _invoke(
        ["job", "finish", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert finish.exit_code == 0, finish.stdout
    finish_payload = json.loads(finish.stdout)
    assert finish_payload["job"]["terminal_outcome"] == "proposed_unverified"
    assert finish_payload["decision"]["winner_id"] is None
    assert finish_payload["next_action"] == "terminal"
    assert harness.closure.calls == []
    final_body = harness.comments.bodies[final_marker_id("optimize-7")]
    assert "explicitly unverified proposal" in final_body
    assert "merge remains the human approval step" in final_body
    assert "Provisional winner" not in final_body
    assert "Final winner" not in final_body
    assert (repository / "src" / "main.py").read_text(encoding="utf-8") == "VALUE = 'candidate-one'\n"


def test_job_resume_succeeds_when_runtime_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, event, environment = _start_resume_job(monkeypatch, tmp_path)

    result = _invoke(
        ["job", "resume", "--repository", str(repository), "--event", str(event)],
        environment,
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["resumed"] is True
    assert payload["job"]["candidate_budget"] == 1
    assert payload["next_action"] == "handoff-candidate"


@pytest.mark.parametrize(
    ("drift_kind", "expected_error"),
    (
        ("policy", "policy digest"),
        ("metadata", "metadata digest"),
        (
            "issue-request",
            "persisted optimize-job issue request does not match the current issue body",
        ),
        ("pin", "bootstrap receipt commit does not match the shared pin"),
        ("receipt", "bootstrap receipt lock_sha256 does not match the shared pin"),
        ("base", "repository HEAD drifted from the optimize-job base commit"),
    ),
)
def test_job_resume_rejects_runtime_drift(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    drift_kind: str,
    expected_error: str,
) -> None:
    repository, event, environment = _start_resume_job(monkeypatch, tmp_path)

    if drift_kind == "policy":
        _replace_text(
            repository / ".github" / "foundry-optimizer.yaml",
            "primary_metric: quality",
            "primary_metric: safety",
        )
    elif drift_kind == "metadata":
        _replace_text(
            repository / ".foundry" / "agent-metadata.yaml",
            "direct_oidc_subject: repo:example-org/example-agent:environment:copilot",
            "direct_oidc_subject: repo:example-org/example-agent:environment:drifted",
        )
    elif drift_kind == "issue-request":
        _replace_text(event, "Improve coverage.", "Improve latency.")
    elif drift_kind == "pin":
        _replace_text(
            repository / ".github" / "foundry-opt.lock.yml",
            "commit: ",
            f"commit: {'a' * 40} # ",
        )
    elif drift_kind == "receipt":
        receipt_path = Path(environment[BOOTSTRAP_RECEIPT_ENV])
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt_path.unlink()
        write_bootstrap_receipt(
            receipt_path,
            BootstrapReceipt.create(
                repository=receipt["repository"],
                commit=receipt["commit"],
                package_path=receipt["package_path"],
                skill_path=receipt["skill_path"],
                lock_sha256="0" * 64,
                checkout_root=receipt["checkout_root"],
            ),
        )
    elif drift_kind == "base":
        (repository / "src" / "main.py").write_text("VALUE = 'drifted'\n", encoding="utf-8")
        _git(repository, "add", "src/main.py")
        _git(repository, "commit", "-m", "drift")
    else:
        raise AssertionError(f"unknown drift_kind: {drift_kind}")

    result = _invoke(
        ["job", "resume", "--repository", str(repository), "--event", str(event)],
        environment,
    )

    assert result.exit_code == 2, result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "blocked"
    assert expected_error in payload["error"]

    status = _invoke(
        ["job", "status", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert status.exit_code == 0, status.stdout
    assert json.loads(status.stdout)["next_action"] == "handoff-candidate"


def test_job_start_rejects_issue_widening(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, _, environment = _create_runtime_repository(tmp_path)
    event = _write_issue_event(
        tmp_path,
        body=_issue_body(candidate_budget=1, model_lines=("missing-model",)),
    )
    monkeypatch.setattr(
        cli_module,
        "build_runtime_controller",
        lambda **_: (_ for _ in ()).throw(AssertionError("controller must not build")),
    )

    result = _invoke(
        ["job", "start", "--repository", str(repository), "--event", str(event)],
        environment,
    )

    assert result.exit_code == 2, result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "blocked"
    assert "model_subset widens allowed_models" in payload["error"]


def test_acceptance_smoke_cleans_up_baseline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, base_commit, environment = _create_runtime_repository(tmp_path)
    paths = load_runtime_paths(
        repository,
        environment=environment,
        job_id="acceptance-7",
        state_root=cli_module._acceptance_state_root(Path(environment[STATE_ROOT_ENV])),
    )
    settings = load_runtime_settings(paths, environment=environment, base_commit=base_commit)
    route = _route("f" * 64)
    foundry = _FakeFoundryClient(
        route=route,
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

    def build_acceptance_handle(
        *,
        settings: RuntimeSettings,
        paths: RuntimePaths,
        route: RouteFingerprint,
        environment: dict[str, str],
    ) -> Any:
        del environment
        operations = ControllerFoundryOperations(
            repository=paths.repository_root,
            source_root=settings.policy.source_root,
            policy=settings.policy,
            metadata=settings.metadata,
            client=foundry,
            artifact_state_path=paths.job_root,
            route_fingerprint=route,
            deadline_seconds=settings.deadline_seconds,
        )
        return cli_module._AcceptanceFoundryHandle(operations=operations, close=lambda: None)

    monkeypatch.setattr(cli_module, "capture_route_fingerprint", lambda **_: route)
    monkeypatch.setattr(cli_module, "_create_acceptance_foundry_handle", build_acceptance_handle)

    result = _invoke(
        [
            "acceptance",
            "smoke",
            "--repository",
            str(repository),
            "--issue-number",
            "7",
            "--base-commit",
            base_commit,
        ],
        environment,
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["cleanup"] is True
    assert payload["route_unchanged"] is True
    assert payload["metrics"]["primary_score"] == 0.5
    assert payload["sidecar"]["baseline_cleanup_recorded"] is True
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
    assert paths.job_root.parent.name == ".acceptance-smoke"


def test_acceptance_smoke_isolates_existing_optimize_job_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, base_commit, environment = _create_runtime_repository(tmp_path)
    normal_paths = load_runtime_paths(
        repository,
        environment=environment,
        job_id="optimize-7",
    )
    state_bytes = b'{"real":"state"}\n'
    sidecar_bytes = b'{"real":"sidecar"}\n'
    workspace_bytes = b"real workspace contents\n"
    normal_paths.job_state_path.write_bytes(state_bytes)
    normal_paths.sidecar_path.write_bytes(sidecar_bytes)
    workspace_sentinel = normal_paths.workspace_root / "sentinel.txt"
    workspace_sentinel.write_bytes(workspace_bytes)

    acceptance_paths = load_runtime_paths(
        repository,
        environment=environment,
        job_id="optimize-7",
        state_root=cli_module._acceptance_state_root(Path(environment[STATE_ROOT_ENV])),
    )
    settings = load_runtime_settings(
        acceptance_paths,
        environment=environment,
        base_commit=base_commit,
    )
    route = _route("f" * 64)
    foundry = _FakeFoundryClient(
        route=route,
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

    def build_acceptance_handle(
        *,
        settings: RuntimeSettings,
        paths: RuntimePaths,
        route: RouteFingerprint,
        environment: dict[str, str],
    ) -> Any:
        del environment
        operations = ControllerFoundryOperations(
            repository=paths.repository_root,
            source_root=settings.policy.source_root,
            policy=settings.policy,
            metadata=settings.metadata,
            client=foundry,
            artifact_state_path=paths.job_root,
            route_fingerprint=route,
            deadline_seconds=settings.deadline_seconds,
        )
        return cli_module._AcceptanceFoundryHandle(operations=operations, close=lambda: None)

    monkeypatch.setattr(cli_module, "capture_route_fingerprint", lambda **_: route)
    monkeypatch.setattr(cli_module, "_create_acceptance_foundry_handle", build_acceptance_handle)

    result = _invoke(
        [
            "acceptance",
            "smoke",
            "--repository",
            str(repository),
            "--issue-number",
            "7",
            "--job-id",
            "optimize-7",
            "--base-commit",
            base_commit,
        ],
        environment,
    )

    assert result.exit_code == 0, result.stdout
    assert normal_paths.job_state_path.read_bytes() == state_bytes
    assert normal_paths.sidecar_path.read_bytes() == sidecar_bytes
    assert workspace_sentinel.read_bytes() == workspace_bytes
    assert acceptance_paths.sidecar_path.exists()
    assert acceptance_paths.job_root.parent.name == ".acceptance-smoke"


def test_acceptance_smoke_blocks_when_isolated_state_already_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, _, environment = _create_runtime_repository(tmp_path)
    acceptance_paths = load_runtime_paths(
        repository,
        environment=environment,
        job_id="acceptance-7",
        state_root=cli_module._acceptance_state_root(Path(environment[STATE_ROOT_ENV])),
    )
    state_bytes = b'{"existing":"state"}\n'
    sidecar_bytes = b'{"existing":"sidecar"}\n'
    workspace_bytes = b"acceptance workspace sentinel\n"
    acceptance_paths.job_state_path.write_bytes(state_bytes)
    acceptance_paths.sidecar_path.write_bytes(sidecar_bytes)
    workspace_sentinel = acceptance_paths.workspace_root / "sentinel.txt"
    workspace_sentinel.write_bytes(workspace_bytes)
    monkeypatch.setattr(
        cli_module,
        "capture_route_fingerprint",
        lambda **_: (_ for _ in ()).throw(AssertionError("route must not be captured")),
    )
    monkeypatch.setattr(
        cli_module,
        "_create_acceptance_foundry_handle",
        lambda **_: (_ for _ in ()).throw(AssertionError("acceptance handle must not build")),
    )

    result = _invoke(
        [
            "acceptance",
            "smoke",
            "--repository",
            str(repository),
            "--issue-number",
            "7",
        ],
        environment,
    )

    assert result.exit_code == 2, result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "blocked"
    assert "refuses to reuse existing isolated state" in payload["error"]
    assert acceptance_paths.job_state_path.read_bytes() == state_bytes
    assert acceptance_paths.sidecar_path.read_bytes() == sidecar_bytes
    assert workspace_sentinel.read_bytes() == workspace_bytes


def test_job_finish_blocks_without_pull_request_binding_when_no_winner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    repository, _, environment = _create_runtime_repository(tmp_path)
    _checkout_branch(repository, "copilot/job-7")
    event = _write_issue_event(
        tmp_path,
        body=_issue_body(candidate_budget=2, model_lines=("candidate",)),
    )
    binding = _write_binding(tmp_path / "binding.json")
    environment[cli_module._GITHUB_BINDING_ENV] = str(binding)
    harness = ControllerHarness(
        candidate_results={
            "candidate-one": RunResult(
                status="ok",
                evaluation=_summary("development", 0.53, improved=1),
                draft_id="draft-one",
            ),
            "candidate-two": RunResult(
                status="ok",
                evaluation=_summary("development", 0.54, improved=1),
                draft_id="draft-two",
            ),
        },
        validating_results={},
        cleanup_results={
            "draft-one": [CleanupResult(success=True, receipt_id="cleanup-one")],
            "draft-two": [CleanupResult(success=True, receipt_id="cleanup-two")],
        },
    )
    monkeypatch.setattr(cli_module, "capture_route_fingerprint", lambda **_: _route())
    monkeypatch.setattr(cli_module, "build_runtime_controller", harness.builder)
    def ensure_binding(**kwargs):
        if not kwargs["require_present"]:
            return cli_module._load_binding(kwargs["runtime"].binding.path)
        raise cli_module.RuntimeIntegrationError(
            "no exact early same-repository pull request matches the trusted issue and current head branch"
        )

    monkeypatch.setattr(cli_module, "_ensure_runtime_pull_request_binding", ensure_binding)

    _invoke(
        ["job", "start", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    for candidate_id, value in (
        ("candidate-one", "one"),
        ("candidate-two", "two"),
    ):
        handoff = _invoke(
            [
                "job",
                "handoff",
                "--repository",
                str(repository),
                "--event",
                str(event),
                "--candidate",
                candidate_id,
                "--model",
                "candidate",
                "--hypothesis",
                f"{candidate_id} improvement",
            ],
            environment,
        )
        workspace = Path(json.loads(handoff.stdout)["candidate"]["workspace"])
        (workspace / "src" / "main.py").write_text(f"VALUE = '{value}'\n", encoding="utf-8")
        _invoke(
            [
                "job",
                "complete",
                "--repository",
                str(repository),
                "--event",
                str(event),
                "--candidate",
                candidate_id,
            ],
            environment,
        )

    finish = _invoke(
        ["job", "finish", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    assert finish.exit_code == 2, finish.stdout
    payload = json.loads(finish.stdout)
    assert payload["status"] == "blocked"
    assert "no exact early same-repository pull request" in payload["error"]
    assert harness.closure.calls == []

    status = _invoke(
        ["job", "status", "--repository", str(repository), "--event", str(event)],
        environment,
    )
    status_payload = json.loads(status.stdout)
    assert status_payload["next_action"] == "blocked"
    assert status_payload["blocker"] == "pull request binding is required for no-winner closure"


@pytest.mark.skipif(os.name == "nt", reason="Unix socket broker runs on Linux")
def test_broker_launch_requires_token_stdin(tmp_path: Path) -> None:
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "repository": {
                    "full_name": "example/agent",
                    "id": 123,
                },
                "issue": {"number": 7},
            }
        ),
        encoding="utf-8",
    )
    result = runner.invoke(
        app,
        [
            "broker",
            "launch",
            "--event",
            str(event),
            "--binding",
            str(tmp_path / "binding.json"),
            "--socket",
            str(tmp_path / "broker.sock"),
        ],
    )

    assert result.exit_code != 0
