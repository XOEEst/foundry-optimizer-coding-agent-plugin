"""Staged provider state machine tests: inventory -> ... -> activation -> cleanup.

Every dynamic immutable identifier is produced here, at apply time, and recorded in the
receipt/provider state. No live Azure, GitHub, or Foundry mutation happens: the adapter is
driven entirely by the in-repository SDK fakes.
"""

from __future__ import annotations

import threading

import pytest

from foundry_opt.bootstrap.contracts import BootstrapAction, BootstrapPlan
from foundry_opt.bootstrap.evaluation.core import LEGACY_AGGREGATE_SAFETY_ID, REQUIRED_SAFETY_EVALUATORS
from foundry_opt.bootstrap.evaluation.execution import (
    EvaluationFinalization,
    ONBOARDING_ACTION_KIND,
    ONBOARDING_STAGES,
    OnboardingBounds,
)
from foundry_opt.bootstrap.providers.foundry import (
    FoundryAdapter,
    FoundryPrerequisiteError,
    FoundryUnsupportedCapabilityError,
)
from tests.bootstrap.fakes.evaluation_contract import (
    QUALITY_EVALUATOR_ID,
    RUBRIC_JOB_ID,
    build_contract,
)
from tests.bootstrap.fakes.foundry_env import (
    MALFORMED_RUBRIC,
    SAFETY_CATALOG_VERSION,
    SAFETY_EVALUATOR_NAMES,
    Credential,
    build_fake_adapter,
    onboarding_definition_criteria,
    registry_evaluator_id,
)

RUNTIME_SHA = "a" * 40


def _plan(contract, *, operation_id: str = "op-onboarding") -> BootstrapPlan:
    return BootstrapPlan.create(
        operation_id=operation_id,
        runtime_repository="https://github.com/example/runtime.git",
        runtime_commit=RUNTIME_SHA,
        repository_identity="org/repo",
        actions=contract.composite_action(),
    )


def _finalization(adapter: FoundryAdapter, receipt) -> EvaluationFinalization:
    state = adapter.export_provider_state(receipt)
    ledger = state["onboarding"]["evaluations:app:onboarding"]
    return EvaluationFinalization.model_validate(ledger["finalization"])


def _definition_creates(fakes) -> list:
    """Activation definitions only; the synthetic generation stage also creates an eval object."""

    return [call for call in fakes["evals"].create_calls if call["name"] in {"dev-def", "val-def"}]


def test_generated_path_runs_every_stage_and_records_dynamic_ids(tmp_path) -> None:
    contract = build_contract()
    adapter, fakes = build_fake_adapter()

    receipt = adapter.apply_resources(_plan(contract))
    finalization = _finalization(adapter, receipt)

    state = adapter.export_provider_state(receipt)
    stages = state["onboarding"]["evaluations:app:onboarding"]["stages"]
    assert tuple(sorted(stages)) == tuple(sorted(ONBOARDING_STAGES))
    assert finalization.reuse_decision == "generate_new_assets"
    assert finalization.dataset_strategy == "synthetic_only"
    assert finalization.generated_sample_count == 30
    assert (finalization.split.development_case_count, finalization.split.validating_case_count) == (20, 10)
    assert finalization.dataset_for("development").dataset_id.endswith("/data/dev-set/versions/1")
    assert finalization.dataset_for("validating").dataset_id.endswith("/data/val-set/versions/1")
    assert finalization.objective_evaluators[0].provenance == "auto_generated_unreviewed"
    assert finalization.objective_evaluators[0].generation_operation_id == RUBRIC_JOB_ID
    # The safety bundle is resolved from the live catalog: individual registry evaluators with
    # immutable versioned ids, never a fabricated aggregate.
    assert [item.safety_name for item in finalization.guardrail_evaluators][:5] == list(REQUIRED_SAFETY_EVALUATORS)
    assert finalization.guardrail_evaluators[0].evaluator_id == registry_evaluator_id("violence")
    assert all(item.evaluator_id.startswith("azureml://registries/") for item in finalization.guardrail_evaluators)
    assert finalization.definition_for("development").definition_id != finalization.definition_for("validating").definition_id
    assert finalization.activation.status == "succeeded"
    assert finalization.activation.cleanup_completed is True
    assert fakes["agents"].delete_version_calls == [("draft-agent", "1")]
    assert "evaluations:app:onboarding:dataset:generation-source" in receipt.created_actions
    state = adapter.export_provider_state(receipt)
    source = next(
        item
        for item in state["resources"]
        if item["action_id"] == "evaluations:app:onboarding:dataset:generation-source"
    )
    assert source["ownership_tag"] == "data_generation_job_id"
    assert source["ownership_token"] == "datagen-fake-1"
    generation_definition = next(
        call
        for call in fakes["evals"].create_calls
        if call["name"] == "app-synthetic-generation"
    )
    assert generation_definition["testing_criteria"] == [
        {
            "type": "azure_ai_evaluator",
            "name": "coherence",
            "evaluator_name": "builtin.coherence",
            "initialization_parameters": {
                "deployment_name": "baseline-model",
            },
            "data_mapping": {
                "query": "{{item.query}}",
                "response": "{{sample.output_text}}",
            },
        }
    ]
    finalization.verify_against_contract(contract)
    # The immutable ids exist only in the receipt/provider state, never in the approved plan.
    approved_payload = contract.composite_action()[0].diagnostics[2]
    assert finalization.dataset_for("development").dataset_id not in approved_payload
    assert finalization.definition_for("development").definition_id not in approved_payload


def test_reuse_path_skips_generation_and_adopts_reviewed_assets() -> None:
    contract = build_contract(reuse=True)
    adapter, fakes = build_fake_adapter(reuse=True)

    receipt = adapter.apply_resources(_plan(contract, operation_id="op-reuse"))
    finalization = _finalization(adapter, receipt)

    assert finalization.reuse_decision == "reuse_existing_assets"
    assert fakes["dataset_jobs"].create_calls == []
    assert fakes["evaluator_jobs"].create_calls == []
    assert finalization.objective_evaluators[0].provenance == "reused_existing"
    assert finalization.objective_evaluators[0].evaluator_id == QUALITY_EVALUATOR_ID
    assert [item.disposition for item in finalization.datasets] == ["adopted", "adopted"]
    assert fakes["datasets"].create_calls == []
    finalization.verify_against_contract(contract)


def test_preexisting_generated_evaluator_is_adopted_not_rollback_owned() -> None:
    contract = build_contract()
    adapter, fakes = build_fake_adapter()
    fakes["evaluator_jobs"]._job()

    receipt = adapter.apply_resources(_plan(contract, operation_id="op-existing-generated"))

    objective_action = "evaluations:app:onboarding:evaluator:objective"
    assert objective_action in receipt.adopted_actions
    assert objective_action not in receipt.created_actions


def test_fourteen_useful_trace_samples_fail_closed_without_configuring_a_partial_dataset() -> None:
    contract = build_contract(generation_kind="dataset_trace", useful_trace_samples=20)
    adapter, fakes = build_fake_adapter(generated_samples=14)

    with pytest.raises(FoundryPrerequisiteError, match=r"15\+ are required"):
        adapter.apply_resources(_plan(contract, operation_id="op-trace-14"))

    # No split dataset version was registered from the partial trace output.
    assert fakes["datasets"].create_calls == []
    assert fakes["evals"].create_calls == []


def test_fifteen_useful_trace_samples_are_accepted() -> None:
    contract = build_contract(generation_kind="dataset_trace", useful_trace_samples=20)
    adapter, _ = build_fake_adapter(generated_samples=15)

    receipt = adapter.apply_resources(_plan(contract, operation_id="op-trace-15"))
    finalization = _finalization(adapter, receipt)

    assert finalization.dataset_strategy == "trace"
    assert finalization.generated_sample_count == 15
    assert (finalization.split.development_case_count, finalization.split.validating_case_count) == (10, 5)
    finalization.verify_against_contract(contract)


def test_malformed_generated_rubric_blocks_activation_and_rolls_back() -> None:
    contract = build_contract()
    adapter, fakes = build_fake_adapter(rubric=MALFORMED_RUBRIC)

    with pytest.raises(FoundryPrerequisiteError, match="rubric failed structural validation"):
        adapter.apply_resources(_plan(contract, operation_id="op-rubric"))

    assert _definition_creates(fakes) == []
    # Created-only rollback removed the two split datasets registered before the rubric gate.
    assert sorted(fakes["datasets"].delete_calls) == [
        ("dev-set", "1"),
        ("generated-set", "1"),
        ("val-set", "1"),
    ]


def test_content_safety_below_one_hundred_percent_blocks_activation() -> None:
    contract = build_contract()
    adapter, fakes = build_fake_adapter(safety_pass_rate=0.9)

    with pytest.raises(FoundryPrerequisiteError, match="safety evaluator violence must pass at 100%"):
        adapter.apply_resources(_plan(contract, operation_id="op-safety"))

    # The owned draft is always cleaned up, and every resource created by this apply is
    # rolled back; nothing pre-existing is touched.
    assert fakes["agents"].delete_version_calls == [("draft-agent", "1")]
    assert sorted(fakes["datasets"].delete_calls) == [
        ("dev-set", "1"),
        ("generated-set", "1"),
        ("val-set", "1"),
    ]
    assert fakes["evaluator_jobs"].delete_version_calls == [("quality-eval", "2")]
    # Two activation definitions plus the synthetic generation eval object.
    assert len(fakes["evals"].delete_calls) == 3


@pytest.mark.parametrize("degraded", REQUIRED_SAFETY_EVALUATORS)
def test_each_required_safety_evaluator_is_independently_enforced(degraded: str) -> None:
    contract = build_contract()
    adapter, _ = build_fake_adapter(safety_pass_rate=0.9, degraded_safety_name=degraded)

    with pytest.raises(FoundryPrerequisiteError, match=f"safety evaluator {degraded} must pass at 100%"):
        adapter.apply_resources(_plan(contract, operation_id=f"op-safety-{degraded}"))


def test_missing_required_safety_evaluator_in_the_catalog_fails_closed() -> None:
    contract = build_contract()
    catalog = tuple(name for name in SAFETY_EVALUATOR_NAMES if name != "indirect_attack")
    adapter, fakes = build_fake_adapter(catalog_safety_names=catalog, safety_names=catalog)

    with pytest.raises(FoundryUnsupportedCapabilityError, match="indirect_attack"):
        adapter.apply_resources(_plan(contract, operation_id="op-missing-safety"))

    assert _definition_creates(fakes) == []


def test_project_without_the_fictitious_aggregate_still_resolves_the_bundle() -> None:
    adapter, _ = build_fake_adapter()

    bundle = adapter.resolve_safety_bundle()

    catalog_ids = {item["id"] for item in adapter.inventory_evaluators(include_builtin=True)}
    assert LEGACY_AGGREGATE_SAFETY_ID not in catalog_ids
    assert [item["safety_name"] for item in bundle][:5] == list(REQUIRED_SAFETY_EVALUATORS)
    assert bundle[0]["id"] == registry_evaluator_id("violence")
    assert bundle[0]["version"] == SAFETY_CATALOG_VERSION


def test_legacy_aggregate_is_used_only_when_the_project_returns_it() -> None:
    contract = build_contract()
    adapter, _ = build_fake_adapter(include_aggregate_safety=True)

    bundle = adapter.resolve_safety_bundle()
    assert [item["id"] for item in bundle] == [LEGACY_AGGREGATE_SAFETY_ID]

    receipt = adapter.apply_resources(_plan(contract, operation_id="op-aggregate"))
    finalization = _finalization(adapter, receipt)
    assert [item.safety_name for item in finalization.guardrail_evaluators] == ["content_safety"]
    finalization.verify_against_contract(contract)


def test_saturated_activation_without_headroom_blocks_activation() -> None:
    contract = build_contract()
    adapter, _ = build_fake_adapter(quality_score=1.0)

    with pytest.raises(FoundryPrerequisiteError, match="measurable headroom"):
        adapter.apply_resources(_plan(contract, operation_id="op-headroom"))


def test_execution_errors_in_a_criterion_fail_closed() -> None:
    contract = build_contract()
    adapter, _ = build_fake_adapter(errored_cases=1)

    with pytest.raises(FoundryPrerequisiteError, match="execution errors"):
        adapter.apply_resources(_plan(contract, operation_id="op-errored"))


def test_a_project_without_dataset_credentials_or_writer_fails_closed() -> None:
    """Without the injected writer the adapter falls back to the real upload path, which
    still fails closed when the project exposes no dataset credentials."""

    contract = build_contract()
    adapter, fakes = build_fake_adapter(split_writer_available=False)

    with pytest.raises(FoundryUnsupportedCapabilityError, match="dataset credentials are unavailable"):
        adapter.apply_resources(_plan(contract, operation_id="op-no-writer"))

    assert fakes["datasets"].create_calls == []


def test_unapproved_contract_payload_is_rejected_before_any_mutation() -> None:
    contract = build_contract()
    adapter, fakes = build_fake_adapter()
    action = contract.composite_action()[0]
    tampered = action.model_copy(update={"diagnostics": (action.diagnostics[0], "f" * 64, action.diagnostics[2])})
    plan = BootstrapPlan.create(
        operation_id="op-tampered",
        runtime_repository="https://github.com/example/runtime.git",
        runtime_commit=RUNTIME_SHA,
        repository_identity="org/repo",
        actions=(tampered,),
    )

    with pytest.raises(FoundryPrerequisiteError, match="does not match its approved identity"):
        adapter.apply_resources(plan)

    assert fakes["datasets"].create_calls == []
    assert fakes["dataset_jobs"].create_calls == []


def test_stopped_contract_cannot_carry_an_onboarding_action() -> None:
    adapter, _ = build_fake_adapter()
    stopped = build_contract(binding_classification="ready-unbound")
    action = BootstrapAction(
        action_id="evaluations:app:onboarding",
        phase="evaluations",
        stage="planned",
        kind=ONBOARDING_ACTION_KIND,
        target_agent_id="app",
        diagnostics=("app", stopped.contract_hash, stopped.action_payload_json()),
    )
    plan = BootstrapPlan.create(
        operation_id="op-stopped",
        runtime_repository="https://github.com/example/runtime.git",
        runtime_commit=RUNTIME_SHA,
        repository_identity="org/repo",
        actions=(action,),
    )

    with pytest.raises(FoundryPrerequisiteError, match="stopped agents must not carry"):
        adapter.apply_resources(plan)


def test_restart_resumes_recorded_stages_without_repeating_generation() -> None:
    contract = build_contract()
    adapter, fakes = build_fake_adapter()
    plan = _plan(contract, operation_id="op-restart")
    receipt = adapter.apply_resources(plan)
    state = adapter.export_provider_state(receipt)

    resumed = FoundryAdapter(
        "https://example.services.ai.azure.com/api/projects/example",
        Credential(),
        client=fakes["client"],
        split_writer=fakes["split_writer"],
        sleep=lambda _seconds: None,
    )
    resumed.set_agent_packages({contract.repo_agent_id: fakes["package"]})
    resumed.restore_provider_state(state)
    assert resumed.verify_resources(receipt) is True

    generation_calls = len(fakes["dataset_jobs"].create_calls)
    resumed.apply_resources(plan)
    # The recorded generation stage is reused instead of creating a second generation job.
    assert len(fakes["dataset_jobs"].create_calls) == generation_calls


def test_matching_existing_definitions_are_adopted_instead_of_recreated() -> None:
    contract = build_contract()
    adapter, fakes = build_fake_adapter(definitions_exist=True)

    receipt = adapter.apply_resources(_plan(contract, operation_id="op-adopt"))
    finalization = _finalization(adapter, receipt)

    assert _definition_creates(fakes) == []
    assert finalization.definition_for("development").definition_id == "eval_development"
    assert finalization.definition_for("validating").definition_id == "eval_validating"
    assert finalization.definition_for("development").disposition == "adopted"


@pytest.mark.parametrize(
    "drift",
    (
        {"evaluator_version": "9"},
        {"evaluator_name": "other-eval"},
        {"data_mapping": {"query": "{{item.other}}", "response": "{{sample.output_text}}"}},
        {"initialization_parameters": {"deployment_name": "other-model"}},
    ),
)
def test_a_name_collision_with_different_bindings_fails_closed(drift: dict) -> None:
    contract = build_contract()
    criteria = onboarding_definition_criteria()
    criteria[0] = {**criteria[0], **drift}
    adapter, fakes = build_fake_adapter(definitions_exist=True, existing_definition_criteria=criteria)

    with pytest.raises(FoundryPrerequisiteError, match="does not match the approved evaluator bindings"):
        adapter.apply_resources(_plan(contract, operation_id="op-drifted-definition"))

    assert _definition_creates(fakes) == []


def test_a_definition_missing_a_safety_criterion_is_never_adopted() -> None:
    contract = build_contract()
    criteria = [item for item in onboarding_definition_criteria() if item["name"] != "builtin.violence"]
    adapter, _fakes = build_fake_adapter(definitions_exist=True, existing_definition_criteria=criteria)

    with pytest.raises(FoundryPrerequisiteError, match="does not match the approved evaluator bindings"):
        adapter.apply_resources(_plan(contract, operation_id="op-missing-criterion"))


def test_a_definition_with_a_different_data_source_scenario_is_never_adopted() -> None:
    contract = build_contract()
    adapter, _fakes = build_fake_adapter(
        definitions_exist=True,
        existing_definition_config={"type": "azure_ai_source", "scenario": "some_other_scenario"},
    )

    with pytest.raises(FoundryPrerequisiteError, match="does not match the approved evaluator bindings"):
        adapter.apply_resources(_plan(contract, operation_id="op-drifted-config"))


def test_created_only_rollback_preserves_adopted_assets() -> None:
    contract = build_contract()
    adapter, fakes = build_fake_adapter()
    receipt = adapter.apply_resources(_plan(contract, operation_id="op-rollback"))

    adapter.rollback_resources(receipt)

    assert sorted(fakes["datasets"].delete_calls) == [
        ("dev-set", "1"),
        ("generated-set", "1"),
        ("val-set", "1"),
    ]
    assert fakes["evaluator_jobs"].delete_version_calls == [("quality-eval", "2")]
    # Two activation definitions plus the synthetic generation eval object.
    assert len(fakes["evals"].delete_calls) == 3
    # Two activation runs plus the synthetic generation run.
    assert len(fakes["evals"].runs.delete_calls) == 3
    assert adapter.verify_rollback(receipt) is True


def test_bounds_reject_a_generation_overrun() -> None:
    contract = build_contract(bounds=OnboardingBounds(target_sample_count=30, maximum_generated_sample_count=30))
    adapter, _ = build_fake_adapter(generated_samples=45)

    with pytest.raises(FoundryPrerequisiteError, match="exceeds the approved bound"):
        adapter.apply_resources(_plan(contract, operation_id="op-overrun"))


def test_definitions_bind_real_azure_ai_evaluator_graders() -> None:
    contract = build_contract()
    adapter, fakes = build_fake_adapter()

    adapter.apply_resources(_plan(contract, operation_id="op-graders"))

    activation_definitions = [
        call for call in fakes["evals"].create_calls if call["name"] in {"dev-def", "val-def"}
    ]
    assert activation_definitions, "activation definitions were not created"
    for call in activation_definitions:
        assert call["data_source_config"] == {
            "type": "custom",
            "item_schema": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            "include_sample_schema": True,
        }
        criteria = call["testing_criteria"]
        assert all(item["type"] == "azure_ai_evaluator" for item in criteria)
        # No Python-grader passthrough remains.
        assert all("source" not in item for item in criteria)
        by_name = {item["name"]: item for item in criteria}
        for safety_name in REQUIRED_SAFETY_EVALUATORS:
            grader = by_name[f"builtin.{safety_name}"]
            assert grader["evaluator_name"] == f"builtin.{safety_name}"
            assert grader["evaluator_version"] == SAFETY_CATALOG_VERSION
            assert grader["data_mapping"] == {"query": "{{item.query}}", "response": "{{sample.output_text}}"}
        objective = by_name["quality-eval"]
        assert objective["evaluator_name"] == "quality-eval"
        assert objective["evaluator_version"] == "2"
        assert objective["data_mapping"] == {
            "query": "{{item.query}}",
            "response": "{{sample.output_items}}",
        }
        # AI-assisted evaluators are initialized with the judge deployment; safety built-ins
        # take no initialization parameters (matches the official SDK sample).
        assert objective["initialization_parameters"] == {"deployment_name": "baseline-model"}
        assert all("initialization_parameters" not in by_name[f"builtin.{name}"] for name in REQUIRED_SAFETY_EVALUATORS)


def test_custom_evaluator_initialization_uses_its_declared_schema() -> None:
    assert FoundryAdapter._evaluator_initialization_parameters(
        {
            "raw": {
                "definition": {
                    "type": "rubric",
                    "init_parameters": {
                        "required": ["model"],
                        "properties": {"model": {"type": "string"}},
                    },
                }
            }
        },
        "baseline-model",
    ) == {"model": "baseline-model"}


def test_activation_runs_use_target_completions_against_the_split_datasets() -> None:
    contract = build_contract()
    adapter, fakes = build_fake_adapter()

    adapter.apply_resources(_plan(contract, operation_id="op-target"))

    activation_runs = [call for call in fakes["runs"].create_calls if call[1]["type"] == "azure_ai_target_completions"]
    assert len(activation_runs) == 2
    sources = {call[1]["source"]["id"] for call in activation_runs}
    assert sources == {
        "azureai://accounts/example/projects/example/data/dev-set/versions/1",
        "azureai://accounts/example/projects/example/data/val-set/versions/1",
    }
    for _eval_id, data_source in activation_runs:
        assert data_source["source"]["type"] == "file_id"
        assert data_source["target"] == {"type": "azure_ai_agent", "name": "draft-agent", "version": "1"}
        assert data_source["input_messages"] == {
            "type": "template",
            "template": [
                {
                    "type": "message",
                    "role": "user",
                    "content": {
                        "type": "input_text",
                        "text": "{{item.query}}",
                    },
                }
            ],
        }


def test_activation_submission_reconciles_a_run_before_create_returns() -> None:
    contract = build_contract()
    adapter, fakes = build_fake_adapter()
    original = fakes["runs"].create
    release = threading.Event()

    def _create_then_wait(eval_id, *, data_source, name=None):
        result = original(eval_id, data_source=data_source, name=name)
        if str(name or "").startswith("development-activation"):
            release.wait(60)
        return result

    fakes["runs"].create = _create_then_wait

    receipt = adapter.apply_resources(_plan(contract, operation_id="op-nonblocking-run"))
    release.set()

    assert receipt.error_info is None
    assert _finalization(adapter, receipt).activation.status == "succeeded"


def test_synthetic_generation_uses_the_real_agent_run_and_output_dataset_id() -> None:
    contract = build_contract()
    adapter, fakes = build_fake_adapter(generated_samples=30)

    receipt = adapter.apply_resources(_plan(contract, operation_id="op-synthetic"))
    finalization = _finalization(adapter, receipt)

    synthetic = [call for call in fakes["runs"].create_calls if call[1]["type"] == "azure_ai_synthetic_data_gen_preview"]
    assert len(synthetic) == 1
    params = synthetic[0][1]["item_generation_params"]
    assert params["type"] == "synthetic_data_gen_preview"
    assert params["samples_count"] == 30
    assert params["model_deployment_name"] == "baseline-model"
    assert params["output_dataset_name"] == "dev-set-source"
    assert params["prompt"]
    assert synthetic[0][1]["target"] == {"type": "azure_ai_agent", "name": "example-agent", "version": "1"}
    # The immutable id is read back from data_source.item_generation_params.output_dataset_id,
    # and the accepted sample count from the run's output items.
    generation_run_id = next(run_id for run_id, count in fakes["runs"].output_items.counts.items() if count == 30)
    assert (generation_run_id, "eval_1") in fakes["runs"].output_items.list_calls
    assert fakes["runs"].items[generation_run_id].data_source.item_generation_params["output_dataset_id"].endswith("/data/generated-set/versions/1")
    assert finalization.generated_sample_count == 30
    assert (finalization.split.development_case_count, finalization.split.validating_case_count) == (20, 10)


def test_trace_strategy_still_uses_the_beta_generation_job() -> None:
    contract = build_contract(generation_kind="dataset_trace", useful_trace_samples=20)
    adapter, fakes = build_fake_adapter(generated_samples=20)

    adapter.apply_resources(_plan(contract, operation_id="op-trace-job"))

    assert len(fakes["dataset_jobs"].create_calls) >= 1
    assert not [call for call in fakes["runs"].create_calls if call[1]["type"] == "azure_ai_synthetic_data_gen_preview"]


def test_generated_sample_count_falls_back_to_the_case_index_without_output_items() -> None:
    contract = build_contract()
    adapter, fakes = build_fake_adapter()

    class _NoOutputItemsApi:
        """A project whose client does not expose evals.runs.output_items.list."""

        counts: dict[str, int] = {}

    fakes["runs"].output_items = _NoOutputItemsApi()

    receipt = adapter.apply_resources(_plan(contract, operation_id="op-no-output-items"))

    assert _finalization(adapter, receipt).generated_sample_count == 30


def test_generated_dataset_row_count_is_authoritative_over_run_output_count() -> None:
    contract = build_contract()
    adapter, fakes = build_fake_adapter(generated_samples=30)
    fakes["runs"].synthetic_generated_samples = 22

    receipt = adapter.apply_resources(_plan(contract, operation_id="op-partial-output-items"))

    assert _finalization(adapter, receipt).generated_sample_count == 30


def test_synthetic_run_without_an_output_dataset_id_fails_closed() -> None:
    contract = build_contract()
    adapter, fakes = build_fake_adapter()
    fakes["runs"].synthetic_dataset_id = None

    with pytest.raises(FoundryPrerequisiteError, match="no output dataset id"):
        adapter.apply_resources(_plan(contract, operation_id="op-no-output-dataset"))

    assert fakes["runs"].delete_calls == [("eval_1", "run-1")]
