from __future__ import annotations

from pathlib import Path

from foundry_opt.bootstrap.contracts import (
    ActivationBinding,
    BootstrapSidecar,
    DefaultEvaluatorBundle,
    EvaluationLineage,
    EvaluatorNormalization,
    EvaluatorReference,
    ImmutableDatasetReference,
    ImmutableDefinitionReference,
    ResolvedEvaluator,
    ResolvedWeightedObjective,
    VerificationBundle,
    VerificationSettings,
)
from foundry_opt.poc.config import IssueEvaluatorEntry, OptimizeIssueRequest
from foundry_opt.poc.verification import resolve_verification
from foundry_opt.verification import VerificationCheckSpec, VerificationDatasetInput


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_SIDECAR = (
    REPOSITORY_ROOT
    / "src"
    / "foundry_opt"
    / "templates"
    / "customer-repo"
    / "agent"
    / ".foundry"
    / "foundry-opt.yaml"
)


def _bundle() -> VerificationBundle:
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
    return VerificationBundle(
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


def _profile(
    *,
    bundle: VerificationBundle | None = None,
    repository_checks: tuple[VerificationCheckSpec, ...] = (),
    mode: str = "optional",
) -> BootstrapSidecar:
    profile = BootstrapSidecar.from_document(TEMPLATE_SIDECAR.read_text(encoding="utf-8"))
    verification = VerificationSettings(
        mode=mode,  # type: ignore[arg-type]
        bundle=bundle,
        repository_checks=repository_checks,
        evaluation_gate_policy="allow_repository_checks",
        lineage=(
            EvaluationLineage(
                split_algorithm_version="v1",
                split_hash="a" * 64,
                split_lineage_hash="b" * 64,
                development_case_count=20,
                validating_case_count=10,
                dataset_strategy="synthetic_only",
                generation_context_fingerprint="c" * 64,
                evaluator_provenance="reused_existing",
                bundle_objective_hash=bundle.default_evaluator_bundle.objective.objective_hash,
                activation_binding=ActivationBinding(
                    operation_id="test-activation",
                    plan_hash="d" * 64,
                    approval_hash="e" * 64,
                    receipt_hash="f" * 64,
                    runtime_commit="1" * 40,
                    finalization_hash="2" * 64,
                ),
            )
            if bundle is not None
            else None
        ),
    )
    return profile.model_copy(update={"verification": verification})


def test_resolver_prefers_authorized_issue_foundry_inputs() -> None:
    profile = _profile(bundle=_bundle())
    issue = OptimizeIssueRequest(
        repo_agent_id="example-agent",
        goal="Improve quality.",
        observed_failures=("Failing case.",),
        candidate_budget=2,
        issue_evaluators=(
            IssueEvaluatorEntry(
                evaluator_id="azureai://built-in/evaluators/safety",
                weight=2.0,
            ),
        ),
        verification_dataset=VerificationDatasetInput(
            dataset_id_or_uri="azureai://accounts/a/projects/p/data/dev/versions/9"
        ),
    )

    resolution = resolve_verification(profile=profile, issue=issue)

    assert resolution.mode == "foundry_evaluation"
    assert resolution.foundry_evaluation is not None
    assert resolution.foundry_evaluation.source == "issue"
    assert resolution.provenance == ("issue_dataset", "issue_evaluators")
    assert resolution.quantitative_decision_allowed is True


def test_resolver_uses_repository_default_bundle_when_no_issue_override_exists() -> None:
    profile = _profile(bundle=_bundle())

    resolution = resolve_verification(profile=profile)

    assert resolution.mode == "foundry_evaluation"
    assert resolution.foundry_evaluation is not None
    assert resolution.foundry_evaluation.source == "repository_default_bundle"
    assert resolution.provenance == ("repository_default_bundle",)


def test_resolver_allows_issue_checks_to_override_foundry_defaults() -> None:
    profile = _profile(bundle=_bundle())
    issue = OptimizeIssueRequest(
        repo_agent_id="example-agent",
        goal="Improve quality.",
        observed_failures=("Failing case.",),
        candidate_budget=2,
        verification_checks=(
            VerificationCheckSpec(
                kind="command",
                value="python -m pytest tests/agent -q",
            ),
        ),
    )

    resolution = resolve_verification(profile=profile, issue=issue)

    assert resolution.mode == "repository_checks"
    assert resolution.repository_checks is not None
    assert resolution.repository_checks.source == "issue"
    assert resolution.provenance == ("issue_repository_checks",)


def test_resolver_allows_explicit_no_evidence_to_override_foundry_defaults() -> None:
    profile = _profile(bundle=_bundle())
    issue = OptimizeIssueRequest(
        repo_agent_id="example-agent",
        goal="Improve quality.",
        observed_failures=("Failing case.",),
        candidate_budget=2,
        acknowledge_no_evidence=True,
    )

    resolution = resolve_verification(profile=profile, issue=issue)

    assert resolution.mode == "none"
    assert resolution.provenance == ("explicit_no_evidence",)
    assert resolution.warnings == (
        "No approved quantitative or repository verification evidence is available; any selected proposal remains unverified.",
    )


def test_resolver_falls_through_to_issue_checks_when_issue_foundry_inputs_are_partial() -> None:
    profile = _profile(bundle=_bundle())
    issue = OptimizeIssueRequest(
        repo_agent_id="example-agent",
        goal="Improve quality.",
        observed_failures=("Failing case.",),
        candidate_budget=2,
        issue_evaluators=(
            IssueEvaluatorEntry(
                evaluator_id="azureai://built-in/evaluators/safety",
                weight=1.0,
            ),
        ),
        verification_checks=(
            VerificationCheckSpec(
                kind="command",
                value="python -m pytest tests/agent -q",
            ),
        ),
    )

    resolution = resolve_verification(profile=profile, issue=issue)

    assert resolution.mode == "repository_checks"
    assert resolution.repository_checks is not None
    assert resolution.repository_checks.source == "issue"
    assert resolution.provenance == ("issue_repository_checks",)
    assert resolution.warnings == (
        "issue-supplied evaluators were ignored because no exact verification dataset was provided",
    )
    assert resolution.quantitative_decision_allowed is False


def test_resolver_uses_repository_checks_when_no_foundry_evidence_exists() -> None:
    profile = _profile(
        repository_checks=(
            VerificationCheckSpec(kind="check", value="CI / unit-tests"),
        )
    )

    resolution = resolve_verification(profile=profile)

    assert resolution.mode == "repository_checks"
    assert resolution.repository_checks is not None
    assert resolution.repository_checks.source == "repository"
    assert resolution.provenance == ("repository_default_checks",)


def test_resolver_returns_none_for_explicit_no_evidence_mode() -> None:
    profile = _profile(mode="optional")
    issue = OptimizeIssueRequest(
        repo_agent_id="example-agent",
        goal="Improve quality.",
        observed_failures=("Failing case.",),
        candidate_budget=2,
        acknowledge_no_evidence=True,
    )

    resolution = resolve_verification(profile=profile, issue=issue)

    assert resolution.mode == "none"
    assert resolution.provenance == ("explicit_no_evidence",)
    assert resolution.quantitative_decision_allowed is False
