from __future__ import annotations

from pathlib import Path

import pytest

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
from foundry_opt.contract_errors import BootstrapConfigError
from foundry_opt.poc.config import IssueEvaluatorEntry, OptimizeIssueRequest
from foundry_opt.poc.verification import deployment_evaluator_ids, resolve_verification
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


def _inline_split_bundle() -> VerificationBundle:
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
        development_evaluator_ids=development_evaluator_ids,
        validating_evaluator_ids=validating_evaluator_ids,
    )


def _inline_split_profile() -> BootstrapSidecar:
    profile = _profile(bundle=_inline_split_bundle())
    document = profile.model_dump(mode="json")
    document["hard_guardrails"] = [
        {
            "schema_version": 1,
            "evaluator_name": "advisory_safety",
            "required_pass_rate": 1.0,
            "required": True,
        }
    ]
    return BootstrapSidecar.from_document(document)


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
            IssueEvaluatorEntry(
                evaluator_id="azureml://registries/azureml/evaluators/builtin.task_completion/versions/19",
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
    assert resolution.foundry_evaluation.development_dataset_id.endswith(
        "/data/dev/versions/9"
    )
    assert resolution.foundry_evaluation.validating_dataset_id.endswith(
        "/data/validating/versions/1"
    )
    assert resolution.foundry_evaluation.development_evaluator_ids == (
        "azureai://built-in/evaluators/safety",
        "azureml://registries/azureml/evaluators/builtin.task_completion/versions/19",
    )
    assert resolution.foundry_evaluation.validating_evaluator_ids == (
        "azureai://built-in/evaluators/safety",
        "azureml://registries/azureml/evaluators/builtin.task_completion/versions/19",
    )
    assert resolution.provenance == ("issue_dataset", "issue_evaluators")
    assert resolution.quantitative_decision_allowed is True


def test_resolver_reuses_default_datasets_for_evaluator_only_issue_override() -> None:
    profile = _profile(bundle=_bundle())
    issue = OptimizeIssueRequest(
        repo_agent_id="example-agent",
        goal="Improve task completion.",
        primary_metric="task_completion",
        observed_failures=("Failing case.",),
        candidate_budget=2,
        issue_evaluators=(
            IssueEvaluatorEntry(
                evaluator_id="azureml://registries/azureml/evaluators/builtin.task_completion/versions/19",
            ),
        ),
    )

    resolution = resolve_verification(profile=profile, issue=issue)

    assert resolution.mode == "foundry_evaluation"
    assert resolution.foundry_evaluation is not None
    assert resolution.foundry_evaluation.source == "issue"
    assert resolution.foundry_evaluation.development_dataset_id.endswith(
        "/data/development/versions/1"
    )
    assert resolution.foundry_evaluation.validating_dataset_id.endswith(
        "/data/validating/versions/1"
    )
    assert resolution.foundry_evaluation.development_evaluator_ids == (
        "azureai://built-in/evaluators/safety",
        "azureml://registries/azureml/evaluators/builtin.task_completion/versions/19",
    )
    assert resolution.foundry_evaluation.validating_evaluator_ids == (
        "azureai://built-in/evaluators/safety",
        "azureml://registries/azureml/evaluators/builtin.task_completion/versions/19",
    )
    assert resolution.warnings == ()


def test_resolver_deduplicates_issue_evaluators_against_defaults() -> None:
    profile = _profile(bundle=_bundle())
    issue = OptimizeIssueRequest(
        repo_agent_id="example-agent",
        goal="Reweight safety.",
        observed_failures=("Failing case.",),
        candidate_budget=2,
        issue_evaluators=(
            IssueEvaluatorEntry(
                evaluator_id="azureai://built-in/evaluators/safety",
                weight=2.0,
            ),
        ),
    )

    resolution = resolve_verification(profile=profile, issue=issue)

    assert resolution.foundry_evaluation is not None
    assert resolution.foundry_evaluation.development_evaluator_ids == (
        "azureai://built-in/evaluators/safety",
    )
    assert resolution.foundry_evaluation.validating_evaluator_ids == (
        "azureai://built-in/evaluators/safety",
    )


def test_resolver_uses_repository_default_bundle_when_no_issue_override_exists() -> None:
    profile = _profile(bundle=_bundle())

    resolution = resolve_verification(profile=profile)

    assert resolution.mode == "foundry_evaluation"
    assert resolution.foundry_evaluation is not None
    assert resolution.foundry_evaluation.source == "repository_default_bundle"
    assert resolution.provenance == ("repository_default_bundle",)


def test_resolver_preserves_definition_scoped_evaluator_ids_for_each_split() -> None:
    profile = _inline_split_profile()

    resolution = resolve_verification(profile=profile)

    assert resolution.foundry_evaluation is not None
    assert resolution.foundry_evaluation.development_evaluator_ids == (
        "advisory_safety_7124618c-5a0d-49b0-a9dc-ad55e4c32030",
        "policy_coverage_9d3e2d8b-81e6-436b-96a3-b46a46ef6dce",
        "azureml://registries/azureml/evaluators/builtin.task_completion/versions/19",
    )
    assert resolution.foundry_evaluation.validating_evaluator_ids == (
        "advisory_safety_4cef6e56-2b2e-4150-9331-da56485dac56",
        "policy_coverage_030f008b-0351-4ae3-8d6b-bb112ffee5c4",
        "azureml://registries/azureml/evaluators/builtin.task_completion/versions/19",
    )


def test_explicit_split_evaluators_do_not_duplicate_embedded_guardrails() -> None:
    bundle = _inline_split_bundle()

    assert deployment_evaluator_ids(
        bundle=bundle,
        hard_guardrails=(
            HardGuardrail(
                evaluator_name="advisory_safety",
                required_pass_rate=1.0,
            ),
        ),
    ) == bundle.development_evaluator_ids


def test_inline_defaults_allow_only_exact_common_uri_issue_overrides() -> None:
    profile = _inline_split_profile()
    common_uri = (
        "azureml://registries/azureml/evaluators/"
        "builtin.task_completion/versions/19"
    )
    issue = OptimizeIssueRequest(
        repo_agent_id="example-agent",
        goal="Keep task completion as the primary evaluator.",
        observed_failures=("A known task completion case fails.",),
        candidate_budget=2,
        issue_evaluators=(IssueEvaluatorEntry(evaluator_id=common_uri),),
    )

    resolution = resolve_verification(profile=profile, issue=issue)

    assert resolution.foundry_evaluation is not None
    assert resolution.foundry_evaluation.development_evaluator_ids == (
        profile.verification.bundle.resolved_development_evaluator_ids
    )
    assert resolution.foundry_evaluation.validating_evaluator_ids == (
        profile.verification.bundle.resolved_validating_evaluator_ids
    )


def test_inline_defaults_reject_ambiguous_uri_issue_override() -> None:
    profile = _inline_split_profile()
    issue = OptimizeIssueRequest(
        repo_agent_id="example-agent",
        goal="Reweight safety.",
        observed_failures=("A safety case fails.",),
        candidate_budget=2,
        issue_evaluators=(
            IssueEvaluatorEntry(
                evaluator_id="azureai://built-in/evaluators/safety",
            ),
        ),
    )

    with pytest.raises(
        BootstrapConfigError,
        match="definition-scoped criteria",
    ):
        resolve_verification(profile=profile, issue=issue)


def test_split_evaluator_ids_must_be_complete_and_ordered() -> None:
    bundle = _inline_split_bundle()
    document = bundle.model_dump(mode="json")
    document["validating_evaluator_ids"] = []

    with pytest.raises(
        BootstrapConfigError,
        match="development and validating evaluator IDs must be provided together",
    ):
        VerificationBundle.from_document(document)

    document = bundle.model_dump(mode="json")
    development_ids = list(document["development_evaluator_ids"])
    development_ids.reverse()
    document["development_evaluator_ids"] = development_ids

    with pytest.raises(
        BootstrapConfigError,
        match="development evaluator IDs must match the default objective order",
    ):
        VerificationBundle.from_document(document)


def test_explicit_split_evaluator_ids_must_cover_hard_guardrails() -> None:
    profile = _inline_split_profile()
    document = profile.model_dump(mode="json")
    document["hard_guardrails"] = [
        {
            "schema_version": 1,
            "evaluator_name": "missing_guardrail",
            "required_pass_rate": 1.0,
            "required": True,
        }
    ]

    with pytest.raises(
        BootstrapConfigError,
        match="must cover every hard guardrail; missing: missing_guardrail",
    ):
        BootstrapSidecar.from_document(document)


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


def test_resolver_prefers_evaluator_only_override_over_issue_checks() -> None:
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

    assert resolution.mode == "foundry_evaluation"
    assert resolution.foundry_evaluation is not None
    assert resolution.foundry_evaluation.development_dataset_id.endswith(
        "/data/development/versions/1"
    )
    assert resolution.provenance == (
        "issue_evaluators",
        "repository_default_bundle",
    )
    assert resolution.warnings == ()
    assert resolution.quantitative_decision_allowed is True


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


def test_resolver_rejects_issue_named_checks_even_if_validation_is_bypassed() -> None:
    profile = _profile(bundle=_bundle())
    issue = OptimizeIssueRequest(
        repo_agent_id="example-agent",
        goal="Improve quality.",
        observed_failures=("Failing case.",),
        candidate_budget=2,
    ).model_copy(
        update={
            "verification_checks": (
                VerificationCheckSpec(kind="check", value="CI / unit-tests"),
            ),
        }
    )

    with pytest.raises(BootstrapConfigError, match="command: .*check:"):
        resolve_verification(profile=profile, issue=issue)


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
