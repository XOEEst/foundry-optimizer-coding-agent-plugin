from __future__ import annotations

import pytest

from foundry_opt.bootstrap.errors import BootstrapConfigError
from foundry_opt.bootstrap.evaluation.inventory import (
    assess_agent_inventory,
    preview_split_lineage,
    deterministic_generation_handle_id,
    deterministic_generation_operation_id,
    generation_context_fingerprint,
)
from tests.bootstrap.fakes.evaluation_contract import dataset_rows

_SCHEMA = "agent-evaluation/v1"


def _dataset_candidate(dataset_id: str, *, sample_count: int = 30, suitable: bool = True) -> dict[str, object]:
    return {
        "dataset_id": dataset_id,
        "schema": _SCHEMA if suitable else "other/v1",
        "relevant": suitable,
        "version": "1",
        "immutable_version": True,
        "sample_count": sample_count,
        "split_compatibility": "development_validating",
    }


def _evaluator_candidate(evaluator_id: str, *, suitable: bool = True) -> dict[str, object]:
    return {
        "evaluator_id": evaluator_id,
        "schema": _SCHEMA if suitable else "other/v1",
        "relevant": suitable,
        "version": "2",
        "immutable_version": True,
    }


def _assess(**overrides: object):
    payload: dict[str, object] = {
        "repo_agent_id": "app",
        "binding_classification": "bound-aligned",
        "agent_name": "example-agent",
        "agent_version": "1",
        "model_deployment": "baseline-model",
        "generation_mode": "reuse_reviewed_sources",
        "source_paths": ["app/main.py"],
        "trace_window": "P14D",
        "expected_schema": _SCHEMA,
    }
    payload.update(overrides)
    return assess_agent_inventory(**payload)


def test_inventory_reuses_suitable_existing_assets_before_generation() -> None:
    assessment = _assess(
        dataset_candidates=(_dataset_candidate("dev"), _dataset_candidate("val")),
        evaluator_candidates=(_evaluator_candidate("quality"),),
    )

    assert assessment.reuse_decision == "reuse_existing_assets"
    assert assessment.planned_generation_operation_ids == ()
    assert all(item.suitable for item in assessment.datasets)


def test_inventory_generates_when_existing_assets_are_unsuitable() -> None:
    assessment = _assess(
        dataset_candidates=(_dataset_candidate("dev", suitable=False), _dataset_candidate("val", sample_count=4)),
        evaluator_candidates=(_evaluator_candidate("quality", suitable=False),),
    )

    assert assessment.reuse_decision == "generate_new_assets"
    assert len(assessment.planned_generation_operation_ids) == 2
    assert [item.reasons for item in assessment.datasets] == [("schema_mismatch", "not_relevant"), ("insufficient_samples",)]


def test_fourteen_useful_traces_choose_synthetic_only() -> None:
    assessment = _assess(trace_prerequisites_available=True, useful_trace_samples=14)

    assert assessment.dataset_strategy == "synthetic_only"
    assert assessment.telemetry_probe.eligible is False
    assert assessment.blockers == ("traces:insufficient:14",)
    assert assessment.planned_generation_operation_ids[0].startswith("foundry-datagen-synthetic-")


def test_fifteen_useful_traces_allow_trace_generation() -> None:
    assessment = _assess(trace_prerequisites_available=True, useful_trace_samples=15)

    assert assessment.dataset_strategy == "trace"
    assert assessment.telemetry_probe.eligible is True
    assert assessment.planned_generation_operation_ids[0].startswith("foundry-datagen-trace-")


def test_missing_prerequisites_force_synthetic_only_even_with_many_samples() -> None:
    assessment = _assess(trace_prerequisites_available=False, useful_trace_samples=500)

    assert assessment.dataset_strategy == "synthetic_only"
    assert assessment.telemetry_probe.eligible is False


def test_target_thirty_plans_twenty_ten_split() -> None:
    assessment = _assess(target_sample_count=30)

    assert (assessment.planned_development_cases, assessment.planned_validating_cases) == (20, 10)


def test_ready_unbound_agents_stop_before_inventory_generation() -> None:
    assessment = _assess(binding_classification="ready-unbound")

    assert assessment.stopped is True
    assert assessment.reuse_decision is None
    assert assessment.planned_generation_operation_ids == ()
    assert assessment.blockers == ("binding:ready-unbound",)


def test_generation_identifiers_and_fingerprints_are_deterministic() -> None:
    payload = {"repo_agent_id": "app", "target": 30}
    first = deterministic_generation_operation_id("dataset_synthetic", payload)
    second = deterministic_generation_operation_id("dataset_synthetic", dict(payload))
    trace = deterministic_generation_operation_id("dataset_trace", payload)

    assert first == second
    assert first != trace
    assert deterministic_generation_handle_id(first) == f"{first}.handle"
    with pytest.raises(BootstrapConfigError, match="unsupported generation job kind"):
        deterministic_generation_operation_id("mystery", payload)  # type: ignore[arg-type]

    fingerprint = generation_context_fingerprint(
        repo_agent_id="app",
        agent_name="example-agent",
        agent_version="1",
        model_deployment="baseline-model",
        generation_mode="reuse_reviewed_sources",
        source_paths=["b.py", "a.py"],
    )
    assert fingerprint == generation_context_fingerprint(
        repo_agent_id="app",
        agent_name="example-agent",
        agent_version="1",
        model_deployment="baseline-model",
        generation_mode="reuse_reviewed_sources",
        source_paths=["a.py", "b.py"],
    )
    assert len(fingerprint) == 64


def test_split_preview_keeps_related_groups_together_and_reports_counts_only() -> None:
    rows = [
        {
            "row_id": f"case-{index:03d}",
            "group_id": f"group-{index // 2:03d}",
            "category": "alpha" if (index // 2) % 2 else "beta",
        }
        for index in range(1, 31)
    ]
    preview = preview_split_lineage(rows)

    assert preview.development_case_count + preview.validating_case_count == 30
    assert preview.overlap == "none"
    assert len(preview.split_lineage_hash) == 64
    # The preview carries no row identifiers at all: only counts and lineage hashes.
    assert set(preview.model_dump(mode="json")) == {
        "schema_version",
        "algorithm_version",
        "split_hash",
        "split_lineage_hash",
        "development_case_count",
        "validating_case_count",
        "overlap",
    }


def test_split_lineage_is_stable_across_row_order() -> None:
    first = preview_split_lineage(dataset_rows(30))
    second = preview_split_lineage(tuple(reversed(dataset_rows(30))))

    assert first == second
