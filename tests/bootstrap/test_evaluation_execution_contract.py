from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from foundry_opt.bootstrap.canonical import canonical_sha256
from foundry_opt.bootstrap.contracts import ActivationBinding, EvaluatorNormalization
from foundry_opt.bootstrap.errors import BootstrapConfigError
from foundry_opt.bootstrap.evaluation.core import (
    LEGACY_AGGREGATE_SAFETY_ID,
    REQUIRED_SAFETY_EVALUATORS,
    canonical_safety_name,
    validate_generated_rubric,
)
from foundry_opt.bootstrap.evaluation.execution import (
    ActivationCaseFinalization,
    ActivationFinalization,
    DatasetFinalization,
    DefinitionFinalization,
    EXECUTION_CONTRACT_VERSION,
    EvaluationFinalization,
    EvaluationOnboardingRequest,
    EvaluatorFinalization,
    ONBOARDING_ACTION_KIND,
    OnboardingBounds,
    SplitFinalization,
    assert_no_persisted_content,
    finalization_binding_hash,
)
from foundry_opt.optimize_job.safety import _FORBIDDEN_KEY_PARTS
from tests.bootstrap.fakes.evaluation_contract import (
    DEVELOPMENT_DATASET_ID,
    GENERATION_FINGERPRINT,
    QUALITY_EVALUATOR_ID,
    RUBRIC_JOB_ID,
    VALIDATING_DATASET_ID,
    build_contract,
)
from tests.bootstrap.fakes.foundry_env import SAFETY_CATALOG_VERSION, registry_evaluator_id

CONTRACT_ERRORS = (BootstrapConfigError, ValidationError)


def _reseal(payload: dict[str, object], *, hash_field: str) -> dict[str, object]:
    body = {key: value for key, value in payload.items() if key != hash_field}
    return {**body, hash_field: canonical_sha256(body)}


def _finalization(
    *,
    contract: EvaluationOnboardingRequest,
    reuse_decision: str = "generate_new_assets",
    dataset_strategy: str = "synthetic_only",
    generated_sample_count: int = 30,
    development_cases: int = 20,
    validating_cases: int = 10,
    quality_score: float = 0.8,
    safety_pass_rate: float = 1.0,
    degraded_safety_name: str | None = None,
    safety_names: tuple[str, ...] = REQUIRED_SAFETY_EVALUATORS,
    provenance: str = "auto_generated_unreviewed",
    evaluator_id: str = QUALITY_EVALUATOR_ID,
    cleanup_completed: bool = True,
) -> EvaluationFinalization:
    normalization = EvaluatorNormalization(kind="scalar", source_min=0.0, source_max=1.0)
    cases = []
    for phase in ("development", "validating"):
        cases.append(
            ActivationCaseFinalization(
                phase=phase,
                evaluator_id=evaluator_id,
                executable=True,
                normalization_kind="scalar",
                score=quality_score,
                pass_rate=1.0,
                source_min=0.0,
                source_max=1.0,
            )
        )
        for safety_name in safety_names:
            rate = safety_pass_rate if degraded_safety_name in (None, safety_name) else 1.0
            cases.append(
                ActivationCaseFinalization(
                    phase=phase,
                    evaluator_id=registry_evaluator_id(safety_name),
                    executable=True,
                    normalization_kind="pass_fail",
                    score=1.0 if rate == 1.0 else 0.0,
                    pass_rate=rate,
                )
            )
    from foundry_opt.bootstrap.contracts import EvaluatorReference, ResolvedEvaluator, ResolvedWeightedObjective

    objective_hash = ResolvedWeightedObjective.create(
        [
            ResolvedEvaluator(
                reference=EvaluatorReference(evaluator_id=evaluator_id, provenance=provenance),
                normalization=normalization,
                weight=1.0,
            )
        ]
    ).objective_hash
    return EvaluationFinalization.create(
        repo_agent_id=contract.repo_agent_id,
        contract_hash=contract.contract_hash,
        reuse_decision=reuse_decision,
        dataset_strategy=dataset_strategy,
        generated_sample_count=generated_sample_count,
        generation_context_fingerprint=GENERATION_FINGERPRINT,
        datasets=(
            DatasetFinalization(
                role="development",
                dataset_name="dev-set",
                dataset_version="1",
                dataset_id=DEVELOPMENT_DATASET_ID,
                dataset_type="uri_file",
                case_count=development_cases,
                disposition="created",
            ),
            DatasetFinalization(
                role="validating",
                dataset_name="val-set",
                dataset_version="1",
                dataset_id=VALIDATING_DATASET_ID,
                dataset_type="uri_file",
                case_count=validating_cases,
                disposition="created",
            ),
        ),
        split=SplitFinalization(
            algorithm_version="evaluation-core-split/v4",
            split_hash="c" * 64,
            split_lineage_hash="d" * 64,
            development_case_count=development_cases,
            validating_case_count=validating_cases,
        ),
        evaluators=(
            EvaluatorFinalization(
                role="objective",
                evaluator_name="quality-eval",
                evaluator_version="2",
                evaluator_id=evaluator_id,
                evaluator_kind="custom",
                provenance=provenance,
                generation_operation_id=RUBRIC_JOB_ID if provenance == "auto_generated_unreviewed" else None,
                normalization=normalization,
                weight=1.0,
                disposition="created" if provenance == "auto_generated_unreviewed" else "adopted",
            ),
            *(
                EvaluatorFinalization(
                    role="guardrail",
                    evaluator_name=f"builtin.{safety_name}",
                    evaluator_version=SAFETY_CATALOG_VERSION,
                    evaluator_id=registry_evaluator_id(safety_name),
                    evaluator_kind="builtin",
                    provenance="reused_existing",
                    normalization=EvaluatorNormalization(kind="pass_fail"),
                    weight=1.0,
                    disposition="adopted",
                    safety_name=safety_name,
                )
                for safety_name in safety_names
            ),
        ),
        definitions=(
            DefinitionFinalization(role="development", definition_name="dev-def", definition_id="eval_development", disposition="created"),
            DefinitionFinalization(role="validating", definition_name="val-def", definition_id="eval_validating", disposition="created"),
        ),
        activation=ActivationFinalization(
            status="succeeded",
            development_run_id="run-1",
            validating_run_id="run-2",
            draft_agent_name="draft-agent",
            draft_agent_version="1",
            cases=tuple(cases),
            cleanup_completed=cleanup_completed,
        ),
        bundle_objective_hash=objective_hash,
    )


def test_contract_emits_one_approval_bound_composite_action() -> None:
    contract = build_contract()

    actions = contract.composite_action()

    assert contract.contract_version == EXECUTION_CONTRACT_VERSION
    assert len(actions) == 1
    action = actions[0]
    assert action.kind == ONBOARDING_ACTION_KIND
    assert action.action_id == "evaluations:app:onboarding"
    assert action.target_agent_id == "app"
    assert action.diagnostics[0] == "app"
    assert action.diagnostics[1] == contract.contract_hash
    payload = json.loads(action.diagnostics[2])
    # Only deterministic requested names, job ids, reuse candidates, policy, and bounds.
    assert payload["dataset_plan"]["requested_development_name"] == "dev-set"
    assert payload["dataset_plan"]["generation_job_id"].startswith("foundry-datagen-")
    assert payload["evaluator_plan"]["generation_job_id"] == RUBRIC_JOB_ID
    assert payload["bounds"]["required_safety_pass_rate"] == 1.0
    assert "dataset_id" not in json.dumps(payload["dataset_plan"])[len("{"):] or payload["dataset_plan"]["reuse_development_dataset_id"] is None


def test_contract_never_fabricates_dynamic_immutable_ids() -> None:
    contract = build_contract()
    payload = json.loads(contract.composite_action()[0].diagnostics[2])
    encoded = json.dumps(payload)

    for fabricated in ("split_lineage_hash", "definition_id", "run_id", "\"case_count\"", "\"dataset_id\""):
        assert fabricated not in encoded
    # `maximum_generated_sample_count` is a bound, not an outcome: the actual count is only
    # ever recorded in the receipt-derived finalization.
    assert "generated_sample_count" not in json.dumps(payload["dataset_plan"])
    assert payload["bounds"]["maximum_generated_sample_count"] >= payload["bounds"]["target_sample_count"]
    # Reuse candidates are the only immutable ids the contract may carry, and only when the
    # reviewer approved reusing them.
    assert payload["dataset_plan"]["reuse_development_dataset_id"] is None
    reuse_payload = json.loads(build_contract(reuse=True).composite_action()[0].diagnostics[2])
    assert reuse_payload["dataset_plan"]["reuse_development_dataset_id"] == DEVELOPMENT_DATASET_ID


def test_contract_hash_binds_the_reviewed_document() -> None:
    contract = build_contract()
    payload = contract.model_dump(mode="json")
    payload["bounds"]["target_sample_count"] = 15
    with pytest.raises(CONTRACT_ERRORS, match="contract_hash does not match"):
        EvaluationOnboardingRequest.model_validate(payload)


def test_bounds_cannot_weaken_the_fail_closed_gates() -> None:
    with pytest.raises(CONTRACT_ERRORS, match="safety bundle must be a 100%"):
        OnboardingBounds(required_safety_pass_rate=0.99)
    with pytest.raises(CONTRACT_ERRORS, match="measurable headroom"):
        OnboardingBounds(require_measurable_headroom=False)
    with pytest.raises(CONTRACT_ERRORS):
        OnboardingBounds(minimum_development_cases=5)
    with pytest.raises(CONTRACT_ERRORS):
        OnboardingBounds(telemetry_minimum_samples=14)
    with pytest.raises(CONTRACT_ERRORS, match="issue-supplied"):
        OnboardingBounds(allowed_provenance=("issue_supplied_existing",))


@pytest.mark.parametrize("dropped", REQUIRED_SAFETY_EVALUATORS)
def test_bounds_cannot_drop_a_required_safety_evaluator(dropped: str) -> None:
    remaining = tuple(name for name in REQUIRED_SAFETY_EVALUATORS if name != dropped)

    with pytest.raises(CONTRACT_ERRORS, match=f"missing: {dropped}"):
        OnboardingBounds(required_safety_evaluators=remaining)

    # Extra safety evaluators beyond the required minimum stay allowed.
    assert OnboardingBounds(
        required_safety_evaluators=(*REQUIRED_SAFETY_EVALUATORS, "protected_material")
    ).required_safety_evaluators[-1] == "protected_material"
    with pytest.raises(CONTRACT_ERRORS, match="unknown safety evaluator name"):
        OnboardingBounds(required_safety_evaluators=(*REQUIRED_SAFETY_EVALUATORS, "not_a_safety_evaluator"))


def test_canonical_safety_names_accept_real_catalog_shapes() -> None:
    assert canonical_safety_name(registry_evaluator_id("violence")) == "violence"
    assert canonical_safety_name("azureml://registries/azureml/evaluators/builtin.self_harm/versions/3") == "self_harm"
    assert canonical_safety_name("", "builtin.hate_unfairness") == "hate_unfairness"
    assert canonical_safety_name("", "indirect_attack") == "indirect_attack"
    assert canonical_safety_name(LEGACY_AGGREGATE_SAFETY_ID) == "content_safety"
    # Non-safety built-ins and custom evaluators are not guardrails.
    assert canonical_safety_name(registry_evaluator_id("coherence")) is None
    assert canonical_safety_name(QUALITY_EVALUATOR_ID) is None


def test_trace_generation_requires_fifteen_useful_samples_at_review_time() -> None:
    with pytest.raises(CONTRACT_ERRORS, match=r"15\+ useful samples"):
        build_contract(generation_kind="dataset_trace", useful_trace_samples=14)

    eligible = build_contract(generation_kind="dataset_trace", useful_trace_samples=15)
    assert eligible.telemetry_probe is not None and eligible.telemetry_probe.eligible is True
    assert eligible.dataset_plan is not None and eligible.dataset_plan.generation_kind == "dataset_trace"

    synthetic = build_contract(useful_trace_samples=14)
    assert synthetic.dataset_plan is not None and synthetic.dataset_plan.generation_kind == "dataset_synthetic"


def test_evaluator_plan_requires_exactly_one_reuse_or_generation_source() -> None:
    contract = build_contract()
    payload = contract.model_dump(mode="json")
    payload["evaluator_plan"]["reuse_evaluator_id"] = QUALITY_EVALUATOR_ID
    with pytest.raises(CONTRACT_ERRORS, match="either reuse one reviewed immutable evaluator"):
        EvaluationOnboardingRequest.model_validate(_reseal(payload, hash_field="contract_hash"))


def test_contract_requires_every_safety_evaluator_as_a_hard_guardrail() -> None:
    contract = build_contract()
    payload = contract.model_dump(mode="json")
    payload["sidecar_policy"]["hard_guardrails"][0]["required_pass_rate"] = 0.9
    with pytest.raises(CONTRACT_ERRORS, match="built-in safety evaluator .* at 100%"):
        EvaluationOnboardingRequest.model_validate(_reseal(payload, hash_field="contract_hash"))

    missing = contract.model_dump(mode="json")
    missing["sidecar_policy"]["hard_guardrails"] = missing["sidecar_policy"]["hard_guardrails"][:-1]
    with pytest.raises(CONTRACT_ERRORS, match="built-in safety evaluator"):
        EvaluationOnboardingRequest.model_validate(_reseal(missing, hash_field="contract_hash"))


def test_deployment_cannot_be_enabled_for_a_diverged_binding() -> None:
    contract = build_contract(binding_classification="bound-diverged")
    assert contract.sidecar_policy is not None and contract.sidecar_policy.deployment.enabled is False
    payload = contract.model_dump(mode="json")
    payload["sidecar_policy"]["deployment"]["enabled"] = True
    with pytest.raises(CONTRACT_ERRORS, match="only be enabled for bound-aligned"):
        EvaluationOnboardingRequest.model_validate(_reseal(payload, hash_field="contract_hash"))


def test_ready_unbound_agents_stop_before_generation_and_activation() -> None:
    stopped = build_contract(binding_classification="ready-unbound")

    assert stopped.stopped is True
    assert stopped.composite_action() == ()
    assert stopped.dataset_plan is None and stopped.evaluator_plan is None and stopped.sidecar_policy is None

    payload = stopped.model_dump(mode="json")
    payload["dataset_plan"] = build_contract().model_dump(mode="json")["dataset_plan"]
    with pytest.raises(CONTRACT_ERRORS, match="must stop before evaluation generation"):
        EvaluationOnboardingRequest.model_validate(_reseal(payload, hash_field="contract_hash"))


def test_finalization_verifies_against_the_approved_contract() -> None:
    contract = build_contract()
    finalization = _finalization(contract=contract)

    finalization.verify_against_contract(contract)

    assert finalization.objective_evaluators[0].provenance == "auto_generated_unreviewed"
    assert [item.safety_name for item in finalization.guardrail_evaluators] == list(REQUIRED_SAFETY_EVALUATORS)
    assert finalization.safety_evaluator_ids == tuple(
        registry_evaluator_id(name) for name in REQUIRED_SAFETY_EVALUATORS
    )


def test_finalization_rejects_a_foreign_contract() -> None:
    contract = build_contract()
    other = build_contract(repo_agent_id="other", root="other")
    finalization = _finalization(contract=contract)

    with pytest.raises(BootstrapConfigError, match="does not belong to the approved onboarding contract"):
        finalization.verify_against_contract(other)


def test_finalization_split_must_match_the_deterministic_target() -> None:
    contract = build_contract()
    with pytest.raises(BootstrapConfigError, match="deterministic about two-thirds/one-third target"):
        _finalization(contract=contract, development_cases=25, validating_cases=5).verify_against_contract(contract)


def test_finalization_enforces_minimum_split_counts() -> None:
    contract = build_contract()
    with pytest.raises(CONTRACT_ERRORS):
        _finalization(contract=contract, development_cases=9, validating_cases=5)


@pytest.mark.parametrize("degraded", REQUIRED_SAFETY_EVALUATORS)
def test_any_safety_evaluator_below_one_hundred_percent_blocks_activation(degraded: str) -> None:
    contract = build_contract()
    finalization = _finalization(contract=contract, safety_pass_rate=0.99, degraded_safety_name=degraded)

    with pytest.raises(BootstrapConfigError, match=f"safety evaluator {degraded} must pass at 100%"):
        finalization.verify_against_contract(contract)


def test_partial_safety_bundle_fails_closed() -> None:
    contract = build_contract()
    partial = tuple(name for name in REQUIRED_SAFETY_EVALUATORS if name != "indirect_attack")

    with pytest.raises(CONTRACT_ERRORS, match="missing: indirect_attack"):
        _finalization(contract=contract, safety_names=partial)


def test_safety_evaluator_measured_in_only_one_phase_fails_closed() -> None:
    contract = build_contract()
    finalization = _finalization(contract=contract)
    payload = finalization.model_dump(mode="json")
    payload["activation"]["cases"] = [
        case
        for case in payload["activation"]["cases"]
        if not (case["phase"] == "validating" and case["evaluator_id"] == registry_evaluator_id("violence"))
    ]
    tampered = EvaluationFinalization.model_validate(_reseal(payload, hash_field="finalization_hash"))

    with pytest.raises(BootstrapConfigError, match="must be measured in both phases"):
        tampered.verify_against_contract(contract)


def test_legacy_aggregate_safety_evaluator_is_accepted_when_a_project_returns_it() -> None:
    contract = build_contract()
    finalization = _finalization(contract=contract, safety_names=("content_safety",))

    # The aggregate covers the whole bundle, so coverage passes without the five individuals.
    finalization.verify_against_contract(contract)
    assert [item.safety_name for item in finalization.guardrail_evaluators] == ["content_safety"]


def test_finalization_requires_measurable_headroom() -> None:
    contract = build_contract()
    finalization = _finalization(contract=contract, quality_score=1.0)
    with pytest.raises(BootstrapConfigError, match="measurable headroom"):
        finalization.verify_against_contract(contract)


def test_finalization_requires_completed_cleanup() -> None:
    contract = build_contract()
    with pytest.raises(CONTRACT_ERRORS, match="cleaned up"):
        _finalization(contract=contract, cleanup_completed=False)


def test_trace_finalization_below_threshold_fails_closed() -> None:
    contract = build_contract(generation_kind="dataset_trace", useful_trace_samples=20)
    finalization = _finalization(
        contract=contract,
        dataset_strategy="trace",
        generated_sample_count=14,
        development_cases=20,
        validating_cases=10,
    )
    with pytest.raises(BootstrapConfigError, match=r"15\+ useful samples"):
        finalization.verify_against_contract(contract)


def test_synthetic_only_finalization_cannot_claim_trace_strategy() -> None:
    contract = build_contract()
    finalization = _finalization(contract=contract, dataset_strategy="trace", generated_sample_count=30)
    with pytest.raises(BootstrapConfigError, match="approved trace generation plan"):
        finalization.verify_against_contract(contract)


def test_generated_evaluator_lineage_must_match_the_approved_job() -> None:
    contract = build_contract()
    finalization = _finalization(contract=contract)
    payload = finalization.model_dump(mode="json")
    payload["evaluators"][0]["generation_operation_id"] = "foundry-evalgen-rubric-999999999999999999999999"
    tampered = EvaluationFinalization.model_validate(_reseal(payload, hash_field="finalization_hash"))
    with pytest.raises(BootstrapConfigError, match="approved generation job"):
        tampered.verify_against_contract(contract)


def test_reused_evaluator_must_be_the_approved_candidate() -> None:
    contract = build_contract(reuse=True)
    finalization = _finalization(contract=contract, reuse_decision="reuse_existing_assets", provenance="reused_existing", generated_sample_count=0)
    finalization.verify_against_contract(contract)

    payload = finalization.model_dump(mode="json")
    payload["evaluators"][0]["evaluator_id"] = "azureai://accounts/example/projects/example/evaluators/other/versions/1"
    payload["evaluators"][0]["evaluator_name"] = "other"
    payload["evaluators"][0]["evaluator_version"] = "1"
    for case in payload["activation"]["cases"]:
        if case["evaluator_id"] == QUALITY_EVALUATOR_ID:
            case["evaluator_id"] = "azureai://accounts/example/projects/example/evaluators/other/versions/1"
    tampered = EvaluationFinalization.model_validate(_reseal(payload, hash_field="finalization_hash"))
    with pytest.raises(BootstrapConfigError, match="reviewed reuse candidate"):
        tampered.verify_against_contract(contract)


def test_reused_datasets_must_be_approved_candidates() -> None:
    contract = build_contract(reuse=True)
    finalization = _finalization(contract=contract, reuse_decision="reuse_existing_assets", provenance="reused_existing", generated_sample_count=0)
    payload = finalization.model_dump(mode="json")
    payload["datasets"][0]["dataset_id"] = "azureai://accounts/example/projects/example/data/other/versions/1"
    tampered = EvaluationFinalization.model_validate(_reseal(payload, hash_field="finalization_hash"))
    with pytest.raises(BootstrapConfigError, match="outside the reviewed reuse candidates"):
        tampered.verify_against_contract(contract)


def test_finalization_hash_binds_the_recorded_payload() -> None:
    contract = build_contract()
    finalization = _finalization(contract=contract)
    payload = finalization.model_dump(mode="json")
    payload["generated_sample_count"] = 29
    with pytest.raises(CONTRACT_ERRORS, match="finalization_hash does not match"):
        EvaluationFinalization.model_validate(payload)


def test_finalization_binding_hash_covers_plan_approval_receipt_and_runtime_sha() -> None:
    contract = build_contract()
    finalization = _finalization(contract=contract)
    binding = ActivationBinding(
        operation_id="op-1",
        plan_hash="a" * 64,
        approval_hash="b" * 64,
        receipt_hash="c" * 64,
        runtime_commit="d" * 40,
    )

    first = finalization_binding_hash(binding=binding, finalization=finalization)
    assert first == finalization_binding_hash(binding=binding, finalization=finalization)
    for field, value in (
        ("plan_hash", "e" * 64),
        ("approval_hash", "e" * 64),
        ("receipt_hash", "e" * 64),
        ("runtime_commit", "e" * 40),
        ("operation_id", "op-2"),
    ):
        assert finalization_binding_hash(binding=binding.model_copy(update={field: value}), finalization=finalization) != first


def test_malformed_rubric_document_is_rejected() -> None:
    with pytest.raises(BootstrapConfigError, match="rubric.dimensions"):
        validate_generated_rubric({"dimensions": []})
    with pytest.raises(BootstrapConfigError, match="scalar ranges must increase"):
        validate_generated_rubric(
            {
                "dimensions": [
                    {
                        "name": "quality",
                        "weight": 1.0,
                        "required_inputs": ["reference"],
                        "scalar_range": {"min": 1.0, "max": 0.0},
                        "threshold": 0.5,
                    }
                ]
            }
        )


def test_contract_and_finalization_never_persist_raw_content() -> None:
    contract = build_contract()
    finalization = _finalization(contract=contract)

    def _keys(value: object) -> set[str]:
        if isinstance(value, dict):
            return set(value) | {key for child in value.values() for key in _keys(child)}
        if isinstance(value, list):
            return {key for child in value for key in _keys(child)}
        return set()

    for document in (contract.model_dump(mode="json"), finalization.model_dump(mode="json")):
        normalized = {part for key in _keys(document) for part in key.replace("-", "_").split("_")}
        assert not normalized & _FORBIDDEN_KEY_PARTS
        assert_no_persisted_content(document, field="document")

    with pytest.raises(BootstrapConfigError, match="prohibited raw content"):
        assert_no_persisted_content({"prompt": "hello"}, field="probe")
    with pytest.raises(BootstrapConfigError, match="prohibited raw content"):
        assert_no_persisted_content({"traces": ["span"]}, field="probe")
