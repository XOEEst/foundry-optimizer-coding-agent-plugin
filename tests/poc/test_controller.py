from __future__ import annotations

import subprocess
from collections import defaultdict
from pathlib import Path

import pytest

from foundry_opt.poc.candidate import CandidateWorkspace
from foundry_opt.poc.checks import RepositoryCheckResult
from foundry_opt.poc.controller import (
    CleanupResult,
    ControllerError,
    OptimizeJobController,
    RunResult,
)
from foundry_opt.poc.decision import DecisionRules, EvaluationSummary, GuardrailResult, GuardrailRule
from foundry_opt.poc.evidence import (
    RenderedComment,
    baseline_marker_id,
    candidate_marker_id,
    final_marker_id,
)
from foundry_opt.poc.state import JobIdentity, JobStateStore
from foundry_opt.poc.verification import (
    RepositoryChecksSelection,
    VerificationResolution,
)
from foundry_opt.verification import VerificationCheckSpec


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _create_repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.com")
    (repository / "src").mkdir()
    (repository / "tests").mkdir()
    (repository / "protected").mkdir()
    (repository / "src/app.py").write_text("VALUE = 'base'\n", encoding="utf-8")
    (repository / "tests/test_app.py").write_text("def test_base():\n    assert True\n", encoding="utf-8")
    (repository / "protected/blocked.txt").write_text("protected\n", encoding="utf-8")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "base")
    return repository, _git(repository, "rev-parse", "HEAD")


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


class FakeFoundry:
    def __init__(
        self,
        *,
        baseline: RunResult,
        candidates: dict[str, RunResult | list[RunResult]],
        validating: dict[str, RunResult | list[RunResult]],
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
        return self._next_result(
            self.candidates,
            self.candidate_calls,
            candidate.candidate_id,
        )

    def evaluate_validating(self, candidate) -> RunResult:
        self.validating_calls.append(candidate.candidate_id)
        return self._next_result(
            self.validating,
            self.validating_calls,
            candidate.candidate_id,
        )

    def cleanup_draft(self, draft_id: str) -> CleanupResult:
        self.cleanup_calls.append(draft_id)
        sequence = self.cleanups[draft_id]
        index = self._cleanup_indexes[draft_id]
        self._cleanup_indexes[draft_id] += 1
        return sequence[min(index, len(sequence) - 1)]

    def _next_result(
        self,
        results: dict[str, RunResult | list[RunResult]],
        calls: list[str],
        candidate_id: str,
    ) -> RunResult:
        configured = results[candidate_id]
        if isinstance(configured, list):
            index = calls.count(candidate_id) - 1
            return configured[min(index, len(configured) - 1)]
        return configured


class FakeComments:
    def __init__(self) -> None:
        self.by_marker: dict[str, str] = {}
        self.bodies: dict[str, str] = {}
        self.upsert_count_by_marker: dict[str, int] = defaultdict(int)

    def upsert_comment(self, comment: RenderedComment) -> str:
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
        candidate: FinalizedCandidate,
        *,
        checks: tuple[VerificationCheckSpec, ...],
    ) -> tuple[RepositoryCheckResult, ...]:
        self.calls.append((candidate.candidate_id, checks))
        return self.results[candidate.candidate_id]


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


def _repository_checks_resolution(
    *checks: VerificationCheckSpec,
    warnings: tuple[str, ...] = (),
) -> VerificationResolution:
    return VerificationResolution(
        mode="repository_checks",
        evaluation_gate_policy="allow_repository_checks",
        repository_checks=RepositoryChecksSelection(
            source="issue",
            checks=checks,
        ),
        provenance=("issue_repository_checks",),
        warnings=warnings,
        quantitative_decision_allowed=False,
    )


def _none_resolution(
    *,
    warnings: tuple[str, ...] = (),
) -> VerificationResolution:
    return VerificationResolution(
        mode="none",
        evaluation_gate_policy="allow_no_evidence",
        provenance=("explicit_no_evidence",),
        warnings=warnings,
        quantitative_decision_allowed=False,
    )


def _controller_fixture(
    tmp_path: Path,
    *,
    min_candidates: int,
    candidate_results: dict[str, RunResult],
    validating_results: dict[str, RunResult],
    cleanup_results: dict[str, list[CleanupResult]],
    check_runner: FakeCheckRunner | None = None,
) -> tuple[OptimizeJobController, CandidateWorkspace, JobStateStore, JobIdentity, FakeFoundry, FakeComments, FakeClosure, Path, str]:
    repository, base_commit = _create_repository(tmp_path)
    trusted_root = tmp_path / "trusted"
    workspace = CandidateWorkspace(
        repository,
        trusted_root,
        base_commit,
        editable_patterns=("src/**", "tests/**"),
        protected_patterns=("protected/**", ".git/**"),
        source_root="src",
    )
    store = JobStateStore(tmp_path / "state")
    identity = JobIdentity(
        job_id="job-1",
        repository="owner/repo",
        issue_number=1,
        shared_commit=base_commit,
        base_commit=base_commit,
        source_root="src",
        route_fingerprint="b" * 64,
        min_candidates=min_candidates,
    )
    foundry = FakeFoundry(
        baseline=RunResult(status="ok", evaluation=_summary("development", 0.50)),
        candidates=candidate_results,
        validating=validating_results,
        cleanups=cleanup_results,
    )
    comments = FakeComments()
    closure = FakeClosure()
    controller = OptimizeJobController(
        store=store,
        workspace=workspace,
        foundry=foundry,
        comments=comments,
        closure=closure,
        rules=DecisionRules(
            aggregate_min_delta=0.05,
            min_focused_cases_improved=1,
            max_focused_regressions=0,
            guardrails=(GuardrailRule(name="safety", minimum_score=1.0),),
        ),
        check_runner=check_runner,
    )
    return controller, workspace, store, identity, foundry, comments, closure, repository, base_commit


def test_controller_baseline_two_candidates_and_only_winner_projection(tmp_path: Path) -> None:
    controller, workspace, store, identity, foundry, comments, closure, repository, base_commit = _controller_fixture(
        tmp_path,
        min_candidates=2,
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

    controller.start(identity)
    one = controller.handoff_candidate(
        "candidate-one",
        model="gpt-5-mini",
        hypothesis="first improvement",
    )
    (one.workspace_path / "src/app.py").write_text("VALUE = 'candidate-one'\n", encoding="utf-8")
    (one.workspace_path / "tests/test_app.py").write_text("def test_one():\n    assert True\n", encoding="utf-8")
    controller.complete_candidate("candidate-one")

    two = controller.handoff_candidate(
        "candidate-two",
        model="gpt-5-mini",
        hypothesis="second improvement",
        parent_id="candidate-one",
    )
    (two.workspace_path / "src/app.py").write_text("VALUE = 'candidate-two'\n", encoding="utf-8")
    (two.workspace_path / "tests/test_app.py").write_text("def test_two():\n    assert True\n", encoding="utf-8")
    controller.complete_candidate("candidate-two")

    destination = tmp_path / "destination"
    _git(repository, "worktree", "add", "--detach", str(destination), base_commit)
    final_state = controller.finish(destination)

    assert final_state.final_winner_id == "candidate-two"
    assert final_state.terminal_outcome == "winner"
    assert final_state.projection_receipt.candidate_id == "candidate-two"
    assert final_state.no_winner_receipt is None
    assert foundry.baseline_calls == 1
    assert foundry.candidate_calls == ["candidate-one", "candidate-two"]
    assert foundry.validating_calls == ["candidate-two"]
    assert foundry.cleanup_calls == ["draft-one", "draft-two"]
    assert len(comments.by_marker) == 4
    assert closure.calls == []
    assert (destination / "src/app.py").read_text(encoding="utf-8") == "VALUE = 'candidate-two'\n"


def test_controller_no_winner_flow(tmp_path: Path) -> None:
    controller, workspace, store, identity, foundry, comments, closure, repository, base_commit = _controller_fixture(
        tmp_path,
        min_candidates=2,
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

    controller.start(identity)
    for candidate_id in ("candidate-one", "candidate-two"):
        handoff = controller.handoff_candidate(
            candidate_id,
            model="gpt-5-mini",
            hypothesis=f"{candidate_id} idea",
        )
        (handoff.workspace_path / "src/app.py").write_text(
            f"VALUE = '{candidate_id}'\n",
            encoding="utf-8",
        )
        controller.complete_candidate(candidate_id)

    destination = tmp_path / "destination"
    _git(repository, "worktree", "add", "--detach", str(destination), base_commit)
    final_state = controller.finish(destination)

    assert final_state.terminal_outcome == "no_winner"
    assert final_state.final_winner_id is None
    assert final_state.projection_receipt is None
    assert final_state.no_winner_receipt is not None
    assert closure.calls == ["job-1"]
    assert foundry.validating_calls == []


def test_controller_start_persists_immutable_verification_resolution(
    tmp_path: Path,
) -> None:
    controller, workspace, store, identity, foundry, comments, closure, repository, base_commit = _controller_fixture(
        tmp_path,
        min_candidates=1,
        candidate_results={},
        validating_results={},
        cleanup_results={},
    )
    resolution = _none_resolution(
        warnings=(
            "No approved quantitative or repository verification evidence is available; any selected proposal remains unverified.",
        )
    )

    started = controller.start(identity, verification=resolution)

    assert started.verification == resolution
    assert store.load().verification == resolution
    assert foundry.baseline_calls == 0
    assert "Verification plan" in comments.bodies[baseline_marker_id(identity.job_id)]

    replayed = controller.start(identity, verification=resolution)

    assert replayed.verification == resolution
    assert foundry.baseline_calls == 0

    with pytest.raises(ControllerError, match="verification resolution is immutable"):
        controller.start(
            identity,
            verification=_repository_checks_resolution(
                VerificationCheckSpec(
                    kind="command",
                    value="python -m pytest tests -q",
                )
            ),
        )


def test_controller_repository_checks_selects_recommended_candidate_by_id_order(
    tmp_path: Path,
) -> None:
    check_spec = "command: python -m pytest tests -q"
    parsed_check = VerificationCheckSpec.parse_line(check_spec)
    check_runner = FakeCheckRunner(
        results={
            "candidate-one": (
                _check_result(
                    check_spec,
                    passed=True,
                    summary="Command passed.",
                ),
            ),
            "candidate-two": (
                _check_result(
                    check_spec,
                    passed=True,
                    summary="Command passed.",
                ),
            ),
        }
    )
    controller, workspace, store, identity, foundry, comments, closure, repository, base_commit = _controller_fixture(
        tmp_path,
        min_candidates=2,
        candidate_results={},
        validating_results={},
        cleanup_results={},
        check_runner=check_runner,
    )

    controller.start(
        identity,
        verification=_repository_checks_resolution(parsed_check),
    )
    assert "Verification plan" in comments.bodies[baseline_marker_id(identity.job_id)]
    assert "No quantitative baseline will be claimed." in comments.bodies[
        baseline_marker_id(identity.job_id)
    ]

    two = controller.handoff_candidate(
        "candidate-two",
        model="gpt-5-mini",
        hypothesis="second candidate",
    )
    (two.workspace_path / "src/app.py").write_text(
        "VALUE = 'candidate-two'\n",
        encoding="utf-8",
    )
    controller.complete_candidate("candidate-two")

    one = controller.handoff_candidate(
        "candidate-one",
        model="gpt-5-mini",
        hypothesis="first candidate",
    )
    (one.workspace_path / "src/app.py").write_text(
        "VALUE = 'candidate-one'\n",
        encoding="utf-8",
    )
    controller.complete_candidate("candidate-one")

    destination = tmp_path / "destination"
    _git(repository, "worktree", "add", "--detach", str(destination), base_commit)
    state = controller.finish(destination)

    assert state.terminal_outcome == "recommended"
    assert state.selected_candidate_id == "candidate-one"
    assert state.final_winner_id is None
    assert state.projection_receipt is not None
    assert state.projection_receipt.candidate_id == "candidate-one"
    assert state.no_winner_receipt is None
    assert state.decision is not None
    assert state.decision.outcome == "recommended"
    assert "candidate ID order" in state.decision.reason
    assert foundry.baseline_calls == 0
    assert foundry.candidate_calls == []
    assert foundry.validating_calls == []
    assert check_runner.calls == [
        ("candidate-two", (parsed_check,)),
        ("candidate-one", (parsed_check,)),
    ]
    assert "winner" not in comments.bodies[
        candidate_marker_id(identity.job_id, "candidate-one")
    ].casefold()
    final_body = comments.bodies[final_marker_id(identity.job_id)]
    assert "Provisional winner" not in final_body
    assert "Final winner" not in final_body
    assert "Review the projected draft PR changes and merge only after human approval." in final_body
    assert (destination / "src/app.py").read_text(encoding="utf-8") == "VALUE = 'candidate-one'\n"


def test_controller_none_mode_selects_unverified_candidate_by_id_order_and_replays_terminal_state(
    tmp_path: Path,
) -> None:
    warning = (
        "No approved quantitative or repository verification evidence is available; any selected proposal remains unverified."
    )
    controller, workspace, store, identity, foundry, comments, closure, repository, base_commit = _controller_fixture(
        tmp_path,
        min_candidates=2,
        candidate_results={},
        validating_results={},
        cleanup_results={},
    )

    controller.start(identity, verification=_none_resolution(warnings=(warning,)))

    two = controller.handoff_candidate(
        "candidate-two",
        model="gpt-5-mini",
        hypothesis="second proposal",
    )
    (two.workspace_path / "src/app.py").write_text(
        "VALUE = 'candidate-two'\n",
        encoding="utf-8",
    )
    controller.complete_candidate("candidate-two")

    one = controller.handoff_candidate(
        "candidate-one",
        model="gpt-5-mini",
        hypothesis="first proposal",
    )
    (one.workspace_path / "src/app.py").write_text(
        "VALUE = 'candidate-one'\n",
        encoding="utf-8",
    )
    controller.complete_candidate("candidate-one")

    destination = tmp_path / "destination"
    _git(repository, "worktree", "add", "--detach", str(destination), base_commit)
    state = controller.finish(destination)
    replayed = controller.finish(destination)

    assert state.terminal_outcome == "proposed_unverified"
    assert state.selected_candidate_id == "candidate-one"
    assert state.final_winner_id is None
    assert state.projection_receipt is not None
    assert state.projection_receipt.candidate_id == "candidate-one"
    assert state.no_winner_receipt is None
    assert state.decision is not None
    assert state.decision.outcome == "proposed_unverified"
    assert "candidate ID order" in state.decision.reason
    assert foundry.baseline_calls == 0
    assert foundry.candidate_calls == []
    assert foundry.validating_calls == []
    final_body = comments.bodies[final_marker_id(identity.job_id)]
    assert "explicitly unverified proposal" in final_body
    assert "Provisional winner" not in final_body
    assert "Final winner" not in final_body
    assert replayed.projection_receipt == state.projection_receipt
    assert replayed.final_comment_receipt == state.final_comment_receipt
    assert comments.upsert_count_by_marker[final_marker_id(identity.job_id)] == 1
    assert (destination / "src/app.py").read_text(encoding="utf-8") == "VALUE = 'candidate-one'\n"


def test_controller_marks_invalid_candidate_without_foundry_evaluation(tmp_path: Path) -> None:
    controller, workspace, store, identity, foundry, comments, closure, repository, base_commit = _controller_fixture(
        tmp_path,
        min_candidates=1,
        candidate_results={},
        validating_results={},
        cleanup_results={},
    )

    controller.start(identity)
    handoff = controller.handoff_candidate(
        "candidate-one",
        model="gpt-5-mini",
        hypothesis="tests only",
    )
    (handoff.workspace_path / "tests/test_app.py").write_text(
        "def test_only():\n    assert True\n",
        encoding="utf-8",
    )
    state = controller.complete_candidate("candidate-one")

    assert state.candidate("candidate-one").assessment.outcome == "invalid"
    assert foundry.candidate_calls == []
    assert len(comments.by_marker) == 2


def test_controller_reports_platform_failure_without_ranking_it(tmp_path: Path) -> None:
    controller, workspace, store, identity, foundry, comments, closure, repository, base_commit = _controller_fixture(
        tmp_path,
        min_candidates=1,
        candidate_results={
            "candidate-one": RunResult(
                status="platform_failure",
                reason="deployment failed",
                draft_id="draft-one",
            ),
            "candidate-two": RunResult(
                status="ok",
                evaluation=_summary("development", 0.65, improved=2),
                draft_id="draft-two",
            ),
        },
        validating_results={
            "candidate-two": RunResult(
                status="ok",
                evaluation=_summary("validating", 0.64, improved=2),
            )
        },
        cleanup_results={
            "draft-one": [CleanupResult(success=True, receipt_id="cleanup-one")],
            "draft-two": [CleanupResult(success=True, receipt_id="cleanup-two")],
        },
    )

    controller.start(identity)
    first = controller.handoff_candidate(
        "candidate-one",
        model="gpt-5-mini",
        hypothesis="broken deploy",
    )
    (first.workspace_path / "src/app.py").write_text("VALUE = 'one'\n", encoding="utf-8")
    controller.complete_candidate("candidate-one")
    second = controller.handoff_candidate(
        "candidate-two",
        model="gpt-5-mini",
        hypothesis="working deploy",
    )
    (second.workspace_path / "src/app.py").write_text("VALUE = 'two'\n", encoding="utf-8")
    controller.complete_candidate("candidate-two")

    destination = tmp_path / "destination"
    _git(repository, "worktree", "add", "--detach", str(destination), base_commit)
    state = controller.finish(destination)

    assert state.candidate("candidate-one").assessment.outcome == "platform_failure"
    assert state.final_winner_id == "candidate-two"
    assert foundry.validating_calls == ["candidate-two"]


def test_controller_retries_candidate_after_cleanup_completed_status(
    tmp_path: Path,
) -> None:
    controller, workspace, store, identity, foundry, comments, closure, repository, base_commit = _controller_fixture(
        tmp_path,
        min_candidates=1,
        candidate_results={
            "candidate-one": [
                RunResult(
                    status="retry",
                    reason=(
                        "candidate draft cleanup completed after verification failure; "
                        "rerun candidate evaluation"
                    ),
                    draft_id="draft-one",
                    retry_phase="candidate",
                ),
                RunResult(
                    status="ok",
                    evaluation=_summary("development", 0.65, improved=2),
                    draft_id="draft-two",
                ),
            ],
        },
        validating_results={
            "candidate-one": RunResult(
                status="ok",
                evaluation=_summary("validating", 0.64, improved=2),
            )
        },
        cleanup_results={
            "draft-two": [CleanupResult(success=True, receipt_id="cleanup-two")],
        },
    )

    controller.start(identity)
    handoff = controller.handoff_candidate(
        "candidate-one",
        model="gpt-5-mini",
        hypothesis="retry candidate evaluation",
    )
    (handoff.workspace_path / "src/app.py").write_text("VALUE = 'retry'\n", encoding="utf-8")

    first = controller.complete_candidate("candidate-one")

    assert first.candidate("candidate-one").assessment is None
    assert first.candidate("candidate-one").comment_receipt is None
    assert first.candidate("candidate-one").draft_id is None
    assert foundry.candidate_calls == ["candidate-one"]
    assert foundry.cleanup_calls == []
    assert len(comments.by_marker) == 1

    controller.resume()
    second = controller.complete_candidate("candidate-one")

    assert second.candidate("candidate-one").assessment.outcome == "keep"
    assert second.candidate("candidate-one").comment_receipt is not None
    assert second.candidate("candidate-one").draft_id == "draft-two"
    assert foundry.candidate_calls == ["candidate-one", "candidate-one"]

    controller.resume()
    controller.complete_candidate("candidate-one")

    assert foundry.candidate_calls == ["candidate-one", "candidate-one"]

    destination = tmp_path / "destination"
    _git(repository, "worktree", "add", "--detach", str(destination), base_commit)
    final_state = controller.finish(destination)

    assert final_state.final_winner_id == "candidate-one"
    assert foundry.validating_calls == ["candidate-one"]
    assert len(comments.by_marker) == 3


def test_controller_minimum_candidate_gate_excludes_invalid_and_platform_failures(
    tmp_path: Path,
) -> None:
    controller, workspace, store, identity, foundry, comments, closure, repository, base_commit = _controller_fixture(
        tmp_path,
        min_candidates=2,
        candidate_results={
            "candidate-two": RunResult(
                status="platform_failure",
                reason="deployment failed",
                draft_id="draft-two",
            ),
            "candidate-three": RunResult(
                status="ok",
                evaluation=_summary("development", 0.65, improved=2),
                draft_id="draft-three",
            ),
        },
        validating_results={},
        cleanup_results={
            "draft-two": [CleanupResult(success=True, receipt_id="cleanup-two")],
            "draft-three": [CleanupResult(success=True, receipt_id="cleanup-three")],
        },
    )

    controller.start(identity)
    invalid = controller.handoff_candidate(
        "candidate-one",
        model="gpt-5-mini",
        hypothesis="tests only",
    )
    (invalid.workspace_path / "tests/test_app.py").write_text(
        "def test_only():\n    assert True\n",
        encoding="utf-8",
    )
    controller.complete_candidate("candidate-one")

    broken = controller.handoff_candidate(
        "candidate-two",
        model="gpt-5-mini",
        hypothesis="broken deploy",
    )
    (broken.workspace_path / "src/app.py").write_text(
        "VALUE = 'two'\n",
        encoding="utf-8",
    )
    controller.complete_candidate("candidate-two")

    valid = controller.handoff_candidate(
        "candidate-three",
        model="gpt-5-mini",
        hypothesis="real evidence",
    )
    (valid.workspace_path / "src/app.py").write_text(
        "VALUE = 'three'\n",
        encoding="utf-8",
    )
    controller.complete_candidate("candidate-three")

    destination = tmp_path / "destination"
    _git(repository, "worktree", "add", "--detach", str(destination), base_commit)

    with pytest.raises(ControllerError, match="minimum candidate count"):
        controller.finish(destination)

    state = store.load()
    assert state.completed_candidate_count == 1
    assert foundry.validating_calls == []


def test_controller_resume_replay_does_not_duplicate_receipted_work(tmp_path: Path) -> None:
    controller, workspace, store, identity, foundry, comments, closure, repository, base_commit = _controller_fixture(
        tmp_path,
        min_candidates=1,
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

    controller.start(identity)
    handoff = controller.handoff_candidate(
        "candidate-one",
        model="gpt-5-mini",
        hypothesis="working deploy",
    )
    (handoff.workspace_path / "src/app.py").write_text("VALUE = 'winner'\n", encoding="utf-8")
    controller.complete_candidate("candidate-one")
    destination = tmp_path / "destination"
    _git(repository, "worktree", "add", "--detach", str(destination), base_commit)
    controller.finish(destination)
    store.update(lambda current: current.model_copy(update={"final_comment_receipt": None}))

    baseline_calls = foundry.baseline_calls
    candidate_calls = list(foundry.candidate_calls)
    validating_calls = list(foundry.validating_calls)
    cleanup_calls = list(foundry.cleanup_calls)

    controller.start(identity)
    controller.complete_candidate("candidate-one")
    controller.finish(destination)

    assert foundry.baseline_calls == baseline_calls
    assert foundry.candidate_calls == candidate_calls
    assert foundry.validating_calls == validating_calls
    assert foundry.cleanup_calls == cleanup_calls
    assert len(comments.by_marker) == 3
    assert comments.upsert_count_by_marker[next(reversed(list(comments.by_marker.keys())))] == 2


def test_controller_validating_failure_produces_no_winner(tmp_path: Path) -> None:
    controller, workspace, store, identity, foundry, comments, closure, repository, base_commit = _controller_fixture(
        tmp_path,
        min_candidates=1,
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
                evaluation=_summary("validating", None, successful=False),
            )
        },
        cleanup_results={
            "draft-one": [CleanupResult(success=True, receipt_id="cleanup-one")],
        },
    )

    controller.start(identity)
    handoff = controller.handoff_candidate(
        "candidate-one",
        model="gpt-5-mini",
        hypothesis="validate failure",
    )
    (handoff.workspace_path / "src/app.py").write_text("VALUE = 'candidate'\n", encoding="utf-8")
    controller.complete_candidate("candidate-one")
    destination = tmp_path / "destination"
    _git(repository, "worktree", "add", "--detach", str(destination), base_commit)
    state = controller.finish(destination)

    assert state.terminal_outcome == "no_winner"
    assert state.final_winner_id is None
    assert state.projection_receipt is None
    assert state.candidate("candidate-one").assessment.validating_passed is False


def test_controller_validating_platform_failure_becomes_terminal_state(
    tmp_path: Path,
) -> None:
    controller, workspace, store, identity, foundry, comments, closure, repository, base_commit = _controller_fixture(
        tmp_path,
        min_candidates=1,
        candidate_results={
            "candidate-one": RunResult(
                status="ok",
                evaluation=_summary("development", 0.65, improved=2),
                draft_id="draft-one",
            ),
        },
        validating_results={
            "candidate-one": RunResult(
                status="platform_failure",
                reason="foundry unavailable",
            )
        },
        cleanup_results={
            "draft-one": [CleanupResult(success=True, receipt_id="cleanup-one")],
        },
    )

    controller.start(identity)
    handoff = controller.handoff_candidate(
        "candidate-one",
        model="gpt-5-mini",
        hypothesis="platform failure path",
    )
    (handoff.workspace_path / "src/app.py").write_text(
        "VALUE = 'candidate'\n",
        encoding="utf-8",
    )
    controller.complete_candidate("candidate-one")

    destination = tmp_path / "destination"
    _git(repository, "worktree", "add", "--detach", str(destination), base_commit)
    state = controller.finish(destination)
    resumed = controller.finish(destination)

    assert state.terminal_outcome == "platform_failure"
    assert state.decision is not None
    assert state.decision.outcome == "platform_failure"
    assert state.decision.winner_id is None
    assert state.final_winner_id is None
    assert state.projection_receipt is None
    assert state.no_winner_receipt is None
    assert state.final_comment_receipt is not None
    assert foundry.validating_calls == ["candidate-one"]
    assert resumed.terminal_outcome == "platform_failure"
    assert resumed.final_comment_receipt == state.final_comment_receipt
    assert "`platform_failure`" in comments.bodies[final_marker_id(identity.job_id)]


def test_controller_retries_validating_after_cleanup_completed_status(
    tmp_path: Path,
) -> None:
    controller, workspace, store, identity, foundry, comments, closure, repository, base_commit = _controller_fixture(
        tmp_path,
        min_candidates=1,
        candidate_results={
            "candidate-one": RunResult(
                status="ok",
                evaluation=_summary("development", 0.65, improved=2),
                draft_id="draft-one",
            ),
        },
        validating_results={
            "candidate-one": [
                RunResult(
                    status="retry",
                    reason=(
                        "candidate draft cleanup completed after verification failure; "
                        "rerun validating evaluation"
                    ),
                    draft_id="draft-one",
                    retry_phase="validating",
                ),
                RunResult(
                    status="ok",
                    evaluation=_summary("validating", 0.64, improved=2),
                    draft_id="draft-two",
                ),
            ]
        },
        cleanup_results={
            "draft-one": [
                CleanupResult(
                    success=False,
                    reason="provisional winner draft retained for the validating dataset",
                ),
                CleanupResult(
                    success=False,
                    reason="provisional winner draft retained for the validating dataset",
                ),
            ],
            "draft-two": [CleanupResult(success=True, receipt_id="cleanup-two")],
        },
    )

    controller.start(identity)
    handoff = controller.handoff_candidate(
        "candidate-one",
        model="gpt-5-mini",
        hypothesis="retry validating evaluation",
    )
    (handoff.workspace_path / "src/app.py").write_text(
        "VALUE = 'candidate'\n",
        encoding="utf-8",
    )
    controller.complete_candidate("candidate-one")

    destination = tmp_path / "destination"
    _git(repository, "worktree", "add", "--detach", str(destination), base_commit)

    first = controller.finish(destination)

    assert first.terminal_outcome is None
    assert first.final_comment_receipt is None
    assert first.projection_receipt is None
    assert first.candidate("candidate-one").validating is None
    assert first.candidate("candidate-one").draft_id is None
    assert foundry.validating_calls == ["candidate-one"]
    assert final_marker_id(identity.job_id) not in comments.by_marker

    controller.resume()
    second = controller.finish(destination)

    assert second.terminal_outcome == "winner"
    assert second.final_winner_id == "candidate-one"
    assert second.projection_receipt is not None
    assert second.candidate("candidate-one").validating is not None
    assert second.candidate("candidate-one").draft_id == "draft-two"
    assert foundry.validating_calls == ["candidate-one", "candidate-one"]
    assert comments.upsert_count_by_marker[final_marker_id(identity.job_id)] == 1
    assert foundry.cleanup_calls.count("draft-two") == 1


def test_controller_retries_cleanup_without_repeating_projection_or_comments(tmp_path: Path) -> None:
    controller, workspace, store, identity, foundry, comments, closure, repository, base_commit = _controller_fixture(
        tmp_path,
        min_candidates=1,
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
            "draft-one": [
                CleanupResult(success=False, reason="first retry"),
                CleanupResult(success=False, reason="second retry"),
                CleanupResult(success=False, reason="third retry"),
                CleanupResult(success=True, receipt_id="cleanup-one"),
            ],
        },
    )

    controller.start(identity)
    handoff = controller.handoff_candidate(
        "candidate-one",
        model="gpt-5-mini",
        hypothesis="cleanup retry",
    )
    (handoff.workspace_path / "src/app.py").write_text("VALUE = 'winner'\n", encoding="utf-8")
    controller.complete_candidate("candidate-one")
    assert store.load().candidate("candidate-one").cleanup_receipt is None

    destination = tmp_path / "destination"
    _git(repository, "worktree", "add", "--detach", str(destination), base_commit)
    first_finish = controller.finish(destination)
    assert first_finish.projection_receipt is not None
    assert first_finish.candidate("candidate-one").cleanup_receipt is None
    comment_count = len(comments.by_marker)
    validating_calls = list(foundry.validating_calls)

    second_finish = controller.finish(destination)

    assert second_finish.candidate("candidate-one").cleanup_receipt is not None
    assert len(comments.by_marker) == comment_count
    assert foundry.validating_calls == validating_calls


def test_controller_comment_retries_reuse_stable_marker_ids(tmp_path: Path) -> None:
    controller, workspace, store, identity, foundry, comments, closure, repository, base_commit = _controller_fixture(
        tmp_path,
        min_candidates=1,
        candidate_results={},
        validating_results={},
        cleanup_results={},
    )

    started = controller.start(identity)
    original_receipt = started.baseline.comment_receipt.receipt_id
    store.update(
        lambda current: current.with_baseline(
            current.baseline.model_copy(update={"comment_receipt": None})
        )
    )

    resumed = controller.start(identity)

    assert len(comments.by_marker) == 1
    assert comments.upsert_count_by_marker[next(iter(comments.by_marker))] == 2
    assert resumed.baseline.comment_receipt.receipt_id == original_receipt
