"""Evaluation inventory, reuse assessment, and deterministic generation planning.

Inventory always runs before generation: existing immutable datasets, evaluators, and
definitions are assessed for reuse first, and only unsuitable inventories fall through to
generation. Trace-derived generation is eligible only at 15 or more useful samples; 14 or
fewer must select synthetic-only generation and must never configure the partial trace
output. Every generation handle and fingerprint is deterministic so a restarted operation
resolves the same identifiers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, Self

from pydantic import Field, StrictBool, StrictInt, model_validator

from foundry_opt.bootstrap.canonical import canonical_sha256
from foundry_opt.bootstrap.contracts import BootstrapDocument
from foundry_opt.bootstrap.errors import BootstrapConfigError
from foundry_opt.bootstrap.evaluation.core import (
    AssetSuitability,
    DatasetSplitResult,
    MIN_DEVELOPMENT_CASES,
    MIN_VALIDATING_CASES,
    SPLIT_ALGORITHM_VERSION,
    TARGET_SAMPLE_COUNT,
    TRACE_MIN_GENERATED_SAMPLES,
    assess_dataset_suitability,
    assess_definition_suitability,
    assess_evaluator_suitability,
    choose_dataset_strategy,
    compute_split_lineage_hash,
    deterministic_split_targets,
    split_dataset_rows,
)
from foundry_opt.bootstrap.evaluation.execution import TelemetryProbe

GenerationJobKind = Literal["dataset_trace", "dataset_synthetic", "evaluator_rubric"]

_JOB_KIND_PREFIX = {
    "dataset_trace": "datagen-trace",
    "dataset_synthetic": "datagen-synthetic",
    "evaluator_rubric": "evalgen-rubric",
}


def deterministic_generation_operation_id(kind: GenerationJobKind, payload: Mapping[str, object]) -> str:
    """Return a stable operation id so a restarted bootstrap resumes the same job."""

    if kind not in _JOB_KIND_PREFIX:
        raise BootstrapConfigError(f"unsupported generation job kind: {kind}")
    digest = canonical_sha256({"kind": kind, "payload": dict(payload)})
    return f"foundry-{_JOB_KIND_PREFIX[kind]}-{digest[:24]}"


def deterministic_generation_handle_id(operation_id: str) -> str:
    return f"{operation_id}.handle"


def generation_context_fingerprint(
    *,
    repo_agent_id: str,
    agent_name: str,
    agent_version: str,
    model_deployment: str,
    generation_mode: str,
    source_paths: Sequence[str],
) -> str:
    """Fingerprint the reviewed generation context without persisting its content."""

    return canonical_sha256(
        {
            "repo_agent_id": repo_agent_id,
            "agent_name": agent_name,
            "agent_version": agent_version,
            "model_deployment": model_deployment,
            "generation_mode": generation_mode,
            "source_paths": sorted(str(item) for item in source_paths),
        }
    )


class AssetAssessment(BootstrapDocument):
    asset_id: str
    kind: Literal["dataset", "evaluator", "definition"]
    suitable: StrictBool
    reasons: tuple[str, ...]
    version: str | None = None

    @classmethod
    def from_suitability(cls, value: AssetSuitability) -> "AssetAssessment":
        return cls(asset_id=value.asset_id, kind=value.kind, suitable=value.suitable, reasons=value.reasons, version=value.version)


class InventoryAssessment(BootstrapDocument):
    """Deterministic reuse/generation recommendation for one selected agent."""

    repo_agent_id: str
    stopped: StrictBool = False
    stop_reason: str | None = None
    datasets: tuple[AssetAssessment, ...] = ()
    evaluators: tuple[AssetAssessment, ...] = ()
    definitions: tuple[AssetAssessment, ...] = ()
    reuse_decision: Literal["reuse_existing_assets", "generate_new_assets"] | None = None
    dataset_strategy: Literal["trace", "synthetic_only"] | None = None
    telemetry_probe: TelemetryProbe | None = None
    target_sample_count: StrictInt = Field(default=TARGET_SAMPLE_COUNT, ge=0)
    planned_development_cases: StrictInt = Field(default=0, ge=0)
    planned_validating_cases: StrictInt = Field(default=0, ge=0)
    planned_generation_operation_ids: tuple[str, ...] = ()
    generation_context_fingerprint: str | None = None
    blockers: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_assessment(self) -> Self:
        if self.stopped and self.stop_reason is None:
            raise BootstrapConfigError("stopped inventory assessments require a stop reason")
        if self.stopped and (self.reuse_decision is not None or self.planned_generation_operation_ids):
            raise BootstrapConfigError("stopped agents must not plan evaluation generation")
        return self


def assess_agent_inventory(
    *,
    repo_agent_id: str,
    binding_classification: str,
    agent_name: str,
    agent_version: str,
    model_deployment: str,
    generation_mode: str,
    source_paths: Sequence[str],
    trace_window: str,
    target_sample_count: int = TARGET_SAMPLE_COUNT,
    expected_schema: str,
    dataset_candidates: Sequence[Mapping[str, object]] = (),
    evaluator_candidates: Sequence[Mapping[str, object]] = (),
    definition_candidates: Sequence[Mapping[str, object]] = (),
    trace_prerequisites_available: bool = False,
    useful_trace_samples: int = 0,
) -> InventoryAssessment:
    """Assess existing immutable assets first, then recommend generation when required."""

    if binding_classification in {"ready-unbound", "not-ready"}:
        return InventoryAssessment(
            repo_agent_id=repo_agent_id,
            stopped=True,
            stop_reason=f"binding classification {binding_classification} stops before evaluation onboarding",
            blockers=(f"binding:{binding_classification}",),
        )
    datasets = tuple(
        AssetAssessment.from_suitability(assess_dataset_suitability(item, expected_schema=expected_schema))
        for item in dataset_candidates
    )
    evaluators = tuple(
        AssetAssessment.from_suitability(assess_evaluator_suitability(item, expected_schema=expected_schema))
        for item in evaluator_candidates
    )
    definitions = tuple(
        AssetAssessment.from_suitability(assess_definition_suitability(item, expected_schema=expected_schema))
        for item in definition_candidates
    )
    suitable_datasets = tuple(item for item in datasets if item.suitable)
    suitable_evaluators = tuple(item for item in evaluators if item.suitable)
    reuse = len(suitable_datasets) >= 2 and bool(suitable_evaluators)
    probe = TelemetryProbe(
        prerequisites_available=bool(trace_prerequisites_available),
        useful_sample_count=int(useful_trace_samples),
        telemetry_window=trace_window,
        eligible=bool(trace_prerequisites_available) and int(useful_trace_samples) >= TRACE_MIN_GENERATED_SAMPLES,
    )
    strategy = choose_dataset_strategy(
        {
            "generated_samples": int(useful_trace_samples),
            "prerequisites_available": bool(trace_prerequisites_available),
        }
    )
    fingerprint = generation_context_fingerprint(
        repo_agent_id=repo_agent_id,
        agent_name=agent_name,
        agent_version=agent_version,
        model_deployment=model_deployment,
        generation_mode=generation_mode,
        source_paths=source_paths,
    )
    development_cases, validating_cases = deterministic_split_targets(int(target_sample_count))
    job_ids: tuple[str, ...] = ()
    if not reuse:
        dataset_kind: GenerationJobKind = "dataset_trace" if strategy == "trace" else "dataset_synthetic"
        dataset_job = deterministic_generation_operation_id(
            dataset_kind,
            {
                "repo_agent_id": repo_agent_id,
                "agent_name": agent_name,
                "agent_version": agent_version,
                "source_fingerprint": fingerprint,
                "target_sample_count": int(target_sample_count),
                "trace_window": trace_window if dataset_kind == "dataset_trace" else None,
            },
        )
        job_ids = (dataset_job,)
        if not suitable_evaluators:
            job_ids = (
                *job_ids,
                deterministic_generation_operation_id(
                    "evaluator_rubric",
                    {
                        "repo_agent_id": repo_agent_id,
                        "agent_name": agent_name,
                        "agent_version": agent_version,
                        "source_fingerprint": fingerprint,
                        "dataset_job": dataset_job,
                    },
                ),
            )
    blockers: list[str] = []
    if not reuse and strategy == "synthetic_only" and probe.useful_sample_count:
        blockers.append(f"traces:insufficient:{probe.useful_sample_count}")
    return InventoryAssessment(
        repo_agent_id=repo_agent_id,
        datasets=datasets,
        evaluators=evaluators,
        definitions=definitions,
        reuse_decision="reuse_existing_assets" if reuse else "generate_new_assets",
        dataset_strategy=strategy,
        telemetry_probe=probe,
        target_sample_count=int(target_sample_count),
        planned_development_cases=development_cases,
        planned_validating_cases=validating_cases,
        planned_generation_operation_ids=job_ids,
        generation_context_fingerprint=fingerprint,
        blockers=tuple(blockers),
    )


class SplitPreview(BootstrapDocument):
    """Deterministic split preview: counts and lineage hashes only, never row content."""

    algorithm_version: str
    split_hash: str
    split_lineage_hash: str
    development_case_count: StrictInt = Field(ge=0)
    validating_case_count: StrictInt = Field(ge=0)
    overlap: Literal["none"] = "none"


def preview_split_lineage(rows: Sequence[Mapping[str, object]]) -> SplitPreview:
    """Split normalized case identifiers deterministically and summarize the lineage.

    This mirrors exactly what the provider's split stage computes at apply time; it exists so
    reviewers can see the deterministic outcome before approving, without any row content
    entering the plan.
    """

    result: DatasetSplitResult = split_dataset_rows(rows)
    if len(result.development) < MIN_DEVELOPMENT_CASES or len(result.validating) < MIN_VALIDATING_CASES:
        raise BootstrapConfigError("deterministic split violated the 10/5 minimum case counts")
    if set(result.development) & set(result.validating):
        raise BootstrapConfigError("development and validating splits must not overlap")
    return SplitPreview(
        algorithm_version=result.algorithm_version or SPLIT_ALGORITHM_VERSION,
        split_hash=result.split_hash,
        split_lineage_hash=compute_split_lineage_hash(result),
        development_case_count=len(result.development),
        validating_case_count=len(result.validating),
    )


__all__ = [
    "AssetAssessment",
    "InventoryAssessment",
    "SplitPreview",
    "assess_agent_inventory",
    "deterministic_generation_handle_id",
    "deterministic_generation_operation_id",
    "generation_context_fingerprint",
    "preview_split_lineage",
]
