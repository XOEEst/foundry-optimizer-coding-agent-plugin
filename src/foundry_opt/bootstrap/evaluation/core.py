from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from foundry_opt.bootstrap.canonical import canonical_sha256, safe_persisted_document
from foundry_opt.bootstrap.contracts import (
    BootstrapAction,
    BootstrapReceipt,
    DefaultEvaluatorBundle,
    EvaluatorNormalization,
    EvaluatorReference,
    ImmutableDatasetReference,
    ImmutableDefinitionReference,
    IssueEvaluatorRequest,
    MAX_ISSUE_EVALUATORS,
    ResolvedEvaluator,
    ResolvedWeightedObjective,
)
from foundry_opt.bootstrap.errors import BootstrapConfigError, BootstrapPlanError

_TRACE_MIN_GENERATED_SAMPLES = 15
_MIN_DEVELOPMENT_COUNT = 10
_MIN_VALIDATING_COUNT = 5
_DEVELOPMENT_RATIO = 2 / 3
_ALLOWED_PROVENANCE = "auto_generated_unreviewed"
_SCALAR_KINDS = {"scalar"}
_PASS_FAIL_KINDS = {"pass_fail"}


def _require_mapping(value: object, *, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise BootstrapConfigError(f"{field} must be a mapping")
    return value


def _require_string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BootstrapConfigError(f"{field} must be a non-empty string")
    return value.strip()


def _require_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BootstrapConfigError(f"{field} must be an integer")
    return value


def _require_finite_number(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BootstrapConfigError(f"{field} must be numeric")
    numeric = float(value)
    if not math.isfinite(numeric):
        raise BootstrapConfigError(f"{field} must be finite")
    return numeric


def _require_positive_finite(value: object, *, field: str) -> float:
    numeric = _require_finite_number(value, field=field)
    if numeric <= 0:
        raise BootstrapConfigError(f"{field} must be positive")
    return numeric


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BootstrapConfigError("expected string")
    text = value.strip()
    return text or None


@dataclass(frozen=True)
class AssetSuitability:
    asset_id: str
    kind: Literal["dataset", "evaluator", "definition"]
    suitable: bool
    reasons: tuple[str, ...]
    version: str | None = None


@dataclass(frozen=True)
class DatasetSplitResult:
    algorithm_version: str
    split_hash: str
    development: tuple[str, ...]
    validating: tuple[str, ...]


@dataclass(frozen=True)
class EvaluatorLifecycleResult:
    action: Literal["reuse", "generate", "replace"]
    bundle: DefaultEvaluatorBundle
    lineage_hash: str
    status: str
    replacement_receipt: BootstrapReceipt | None = None


@dataclass(frozen=True)
class ScoringEvidence:
    subject: Literal["baseline", "candidate", "validating"]
    aggregate_score: float
    evaluator_scores: tuple[float, ...]
    score_hash: str


def assess_dataset_suitability(candidate: Mapping[str, object], *, expected_schema: str, requires_labels: bool | None = None) -> AssetSuitability:
    item = _require_mapping(candidate, field="dataset")
    dataset_id = _require_string(item.get("dataset_id"), field="dataset.dataset_id")
    reasons: list[str] = []
    if _require_string(item.get("schema"), field="dataset.schema") != expected_schema:
        reasons.append("schema_mismatch")
    if not bool(item.get("relevant", False)):
        reasons.append("not_relevant")
    version = _optional_string(item.get("version"))
    if version is None or not bool(item.get("immutable_version", False)):
        reasons.append("not_immutable")
    sample_count = _require_int(item.get("sample_count"), field="dataset.sample_count")
    if sample_count < (_MIN_DEVELOPMENT_COUNT + _MIN_VALIDATING_COUNT):
        reasons.append("insufficient_samples")
    if requires_labels is True and not bool(item.get("has_labels", False)):
        reasons.append("missing_labels")
    split = _optional_string(item.get("split_compatibility"))
    if split not in {"development_validating", "both", "compatible"}:
        reasons.append("split_incompatible")
    return AssetSuitability(asset_id=dataset_id, kind="dataset", suitable=not reasons, reasons=tuple(reasons), version=version)


def assess_definition_suitability(candidate: Mapping[str, object], *, expected_schema: str) -> AssetSuitability:
    item = _require_mapping(candidate, field="definition")
    definition_id = _require_string(item.get("definition_id"), field="definition.definition_id")
    reasons: list[str] = []
    if _require_string(item.get("schema"), field="definition.schema") != expected_schema:
        reasons.append("schema_mismatch")
    if not bool(item.get("relevant", False)):
        reasons.append("not_relevant")
    version = _optional_string(item.get("version"))
    if version is None or not bool(item.get("immutable_version", False)):
        reasons.append("not_immutable")
    return AssetSuitability(asset_id=definition_id, kind="definition", suitable=not reasons, reasons=tuple(reasons), version=version)


def assess_evaluator_suitability(candidate: Mapping[str, object], *, expected_schema: str) -> AssetSuitability:
    item = _require_mapping(candidate, field="evaluator")
    evaluator_id = _require_string(item.get("evaluator_id"), field="evaluator.evaluator_id")
    reasons: list[str] = []
    if _require_string(item.get("schema"), field="evaluator.schema") != expected_schema:
        reasons.append("schema_mismatch")
    if not bool(item.get("relevant", False)):
        reasons.append("not_relevant")
    version = _optional_string(item.get("version"))
    if version is None or not bool(item.get("immutable_version", False)):
        reasons.append("not_immutable")
    return AssetSuitability(asset_id=evaluator_id, kind="evaluator", suitable=not reasons, reasons=tuple(reasons), version=version)


def choose_dataset_strategy(asset: Mapping[str, object]) -> Literal["trace", "synthetic_only"]:
    item = _require_mapping(asset, field="dataset_strategy")
    generated = _require_int(item.get("generated_samples"), field="dataset_strategy.generated_samples")
    if generated >= _TRACE_MIN_GENERATED_SAMPLES:
        return "trace"
    return "synthetic_only"


def split_dataset_rows(rows: Sequence[Mapping[str, object]]) -> DatasetSplitResult:
    canonical_groups: dict[str, Mapping[str, object]] = {}
    for raw in rows:
        item = _require_mapping(raw, field="dataset_row")
        row_id = _require_string(item.get("row_id"), field="dataset_row.row_id")
        group = _optional_string(item.get("group_id")) or row_id.casefold()
        category = _optional_string(item.get("category")) or ""
        dedupe_key = canonical_sha256({"group_id": group.casefold(), "category": category.casefold()})
        current = canonical_groups.get(dedupe_key)
        candidate = {"row_id": row_id, "group_id": group, "category": category}
        if current is None or row_id.casefold() < str(current["row_id"]).casefold():
            canonical_groups[dedupe_key] = candidate
    ordered_groups = [canonical_groups[key] for key in sorted(canonical_groups)]
    if len(ordered_groups) < (_MIN_DEVELOPMENT_COUNT + _MIN_VALIDATING_COUNT):
        raise BootstrapConfigError("dataset requires at least 15 unique grouped rows")
    categories = {row["category"] for row in ordered_groups if row["category"]}
    development_target = max(_MIN_DEVELOPMENT_COUNT, math.ceil(len(ordered_groups) * _DEVELOPMENT_RATIO))
    validating_target = max(_MIN_VALIDATING_COUNT, len(ordered_groups) - development_target)
    development_target = min(len(ordered_groups) - _MIN_VALIDATING_COUNT, development_target)
    validating_target = len(ordered_groups) - development_target
    if validating_target < _MIN_VALIDATING_COUNT:
        raise BootstrapConfigError("dataset split cannot satisfy validating minimum")
    development_rows = list(ordered_groups[:development_target])
    validating_rows = list(ordered_groups[development_target:])
    if {row["group_id"] for row in development_rows} & {row["group_id"] for row in validating_rows}:
        raise BootstrapConfigError("development and validating splits must not overlap")
    if categories:
        dev_categories = {row["category"] for row in development_rows if row["category"]}
        val_categories = {row["category"] for row in validating_rows if row["category"]}
        if not categories.issubset(dev_categories | val_categories):
            raise BootstrapConfigError("dataset split lost category coverage")
    split_payload = {
        "algorithm_version": "evaluation-core-split/v1",
        "development": development_rows,
        "validating": validating_rows,
    }
    split_hash = canonical_sha256(split_payload)
    return DatasetSplitResult(
        algorithm_version="evaluation-core-split/v1",
        split_hash=split_hash,
        development=tuple(str(row["row_id"]) for row in development_rows),
        validating=tuple(str(row["row_id"]) for row in validating_rows),
    )


def validate_generated_rubric(document: Mapping[str, object]) -> None:
    item = _require_mapping(document, field="rubric")
    dimensions = item.get("dimensions")
    if not isinstance(dimensions, Sequence) or isinstance(dimensions, (str, bytes)) or not dimensions:
        raise BootstrapConfigError("rubric.dimensions must be a non-empty sequence")
    seen: set[str] = set()
    for index, dimension in enumerate(dimensions):
        part = _require_mapping(dimension, field=f"rubric.dimensions[{index}]")
        name = _require_string(part.get("name"), field=f"rubric.dimensions[{index}].name")
        key = name.casefold()
        if key in seen:
            raise BootstrapConfigError("rubric dimensions must be unique")
        seen.add(key)
        _require_positive_finite(part.get("weight"), field=f"rubric.dimensions[{index}].weight")
        if bool(part.get("pass_fail", False)):
            if "required_inputs" not in part:
                raise BootstrapConfigError("pass/fail dimensions must declare required_inputs")
        else:
            scalar_range = part.get("scalar_range")
            scalar = _require_mapping(scalar_range, field=f"rubric.dimensions[{index}].scalar_range")
            minimum = _require_finite_number(scalar.get("min"), field=f"rubric.dimensions[{index}].scalar_range.min")
            maximum = _require_finite_number(scalar.get("max"), field=f"rubric.dimensions[{index}].scalar_range.max")
            if maximum <= minimum:
                raise BootstrapConfigError("scalar ranges must increase")
            if "threshold" not in part:
                raise BootstrapConfigError("scalar dimensions must declare threshold")
            if "required_inputs" not in part:
                raise BootstrapConfigError("scalar dimensions must declare required_inputs")


def validate_activation(*, cases: Sequence[Mapping[str, object]], guardrails: Sequence[Mapping[str, object]], generated_bundle: Mapping[str, object] | None = None) -> None:
    if not cases:
        raise BootstrapConfigError("activation requires executable cases")
    finite_scores: list[float] = []
    for index, case in enumerate(cases):
        item = _require_mapping(case, field=f"cases[{index}]")
        if not bool(item.get("executable", False)):
            raise BootstrapConfigError("activation requires executable cases")
        score = _require_finite_number(item.get("score"), field=f"cases[{index}].score")
        finite_scores.append(score)
    if max(finite_scores) - min(finite_scores) <= 0:
        raise BootstrapConfigError("activation requires measurable headroom")
    safety_rates = []
    for index, guardrail in enumerate(guardrails):
        item = _require_mapping(guardrail, field=f"guardrails[{index}]")
        if _require_string(item.get("name"), field=f"guardrails[{index}].name").casefold() == "content safety":
            safety_rates.append(_require_finite_number(item.get("pass_rate"), field=f"guardrails[{index}].pass_rate"))
    if not safety_rates or any(rate != 1.0 for rate in safety_rates):
        raise BootstrapConfigError("Content Safety must be 100%")
    if generated_bundle is not None:
        bundle = _require_mapping(generated_bundle, field="generated_bundle")
        if _require_string(bundle.get("provenance"), field="generated_bundle.provenance") != _ALLOWED_PROVENANCE:
            raise BootstrapConfigError("generated bundle provenance must be auto_generated_unreviewed")


def resolve_issue_evaluators(
    request_document: Mapping[str, object] | None,
    *,
    metadata_by_id: Mapping[str, Mapping[str, object]],
    fallback_objective: ResolvedWeightedObjective,
    max_evaluators: int = MAX_ISSUE_EVALUATORS,
) -> ResolvedWeightedObjective:
    if request_document is None:
        return fallback_objective
    request = IssueEvaluatorRequest.from_document(request_document)
    if len(request.evaluators) > max_evaluators:
        raise BootstrapConfigError("issue evaluator list exceeds sidecar maximum")
    explicit_weights = [entry.weight is not None for entry in request.evaluators]
    resolved: list[ResolvedEvaluator] = []
    for entry in request.evaluators:
        metadata = metadata_by_id.get(entry.evaluator_id)
        if metadata is None:
            raise BootstrapConfigError(f"unknown evaluator id: {entry.evaluator_id}")
        normalization = _resolve_normalization(metadata)
        provenance = _require_string(metadata.get("provenance"), field="metadata.provenance")
        weight = entry.weight if entry.weight is not None else 1.0
        resolved.append(
            ResolvedEvaluator(
                reference=EvaluatorReference(evaluator_id=entry.evaluator_id, provenance=provenance),  # type: ignore[arg-type]
                normalization=normalization,
                weight=weight,
            )
        )
    if not all(explicit_weights) and any(explicit_weights):
        resolved = [item.model_copy(update={"weight": item.weight}) for item in resolved]
    return ResolvedWeightedObjective.create(tuple(resolved))


def _resolve_normalization(metadata: Mapping[str, object]) -> EvaluatorNormalization:
    item = _require_mapping(metadata, field="metadata")
    kind = _require_string(item.get("kind"), field="metadata.kind")
    if kind in _PASS_FAIL_KINDS:
        return EvaluatorNormalization(kind="pass_fail")
    if kind in _SCALAR_KINDS:
        bounds = _require_mapping(item.get("bounds"), field="metadata.bounds")
        return EvaluatorNormalization(
            kind="scalar",
            source_min=_require_finite_number(bounds.get("min"), field="metadata.bounds.min"),
            source_max=_require_finite_number(bounds.get("max"), field="metadata.bounds.max"),
        )
    raise BootstrapConfigError(f"unsupported evaluator normalization kind: {kind}")


def normalize_issue_scores(objective: ResolvedWeightedObjective, raw_scores: Mapping[str, object]) -> ScoringEvidence:
    evaluator_scores: list[float] = []
    for evaluator in objective.evaluators:
        raw = raw_scores.get(evaluator.reference.evaluator_id)
        if raw is None:
            raise BootstrapConfigError(f"missing raw score for evaluator {evaluator.reference.evaluator_id}")
        score = _require_finite_number(raw, field=f"raw_scores[{evaluator.reference.evaluator_id}]")
        if evaluator.normalization.kind == "pass_fail":
            normalized = 1.0 if score > 0 else 0.0
        else:
            span = evaluator.normalization.source_max - evaluator.normalization.source_min  # type: ignore[operator]
            normalized = (score - evaluator.normalization.source_min) / span  # type: ignore[operator]
            normalized = min(1.0, max(0.0, normalized))
        evaluator_scores.append(normalized)
    aggregate = sum(score * evaluator.weight for score, evaluator in zip(evaluator_scores, objective.evaluators, strict=True))
    payload = {"objective_hash": objective.objective_hash, "scores": evaluator_scores, "aggregate": aggregate}
    return ScoringEvidence(subject="validating", aggregate_score=aggregate, evaluator_scores=tuple(evaluator_scores), score_hash=canonical_sha256(payload))


def build_scoring_evidence(subject: Literal["baseline", "candidate", "validating"], objective: ResolvedWeightedObjective, raw_scores: Mapping[str, object]) -> ScoringEvidence:
    evidence = normalize_issue_scores(objective, raw_scores)
    return ScoringEvidence(subject=subject, aggregate_score=evidence.aggregate_score, evaluator_scores=evidence.evaluator_scores, score_hash=evidence.score_hash)


def select_default_deployment_contract(defaults: Mapping[str, object]) -> Mapping[str, object]:
    return safe_persisted_document(dict(_require_mapping(defaults, field="defaults")))


def choose_default_evaluator_bundle(
    *,
    existing_bundle: DefaultEvaluatorBundle | None,
    generated_bundle: DefaultEvaluatorBundle | None,
    split_result: DatasetSplitResult,
    definitions: tuple[ImmutableDefinitionReference, ImmutableDefinitionReference],
    replace_actions: Sequence[BootstrapAction] = (),
    replacement_success: bool = True,
) -> EvaluatorLifecycleResult:
    if existing_bundle is not None:
        return EvaluatorLifecycleResult(action="reuse", bundle=existing_bundle, lineage_hash=canonical_sha256(existing_bundle.model_dump(mode="json")), status="reused_existing")
    if generated_bundle is None:
        raise BootstrapConfigError("generated evaluator bundle required when no suitable existing bundle exists")
    expected_dataset_ids = {
        f"azureai://accounts/generated/projects/evaluation/data/{row_id}/versions/v1"
        for row_id in split_result.development + split_result.validating
    }
    bundle_dataset_ids = {item.dataset_id for item in generated_bundle.datasets}
    if bundle_dataset_ids != expected_dataset_ids:
        raise BootstrapConfigError("generated evaluator bundle must be created after dataset split")
    if tuple(generated_bundle.definitions) != definitions:
        raise BootstrapConfigError("generated evaluator bundle must preserve explicit definitions")
    receipt = None
    action: Literal["generate", "replace"] = "generate"
    status = "generated_pending_activation"
    if replace_actions:
        action = "replace"
        if replacement_success:
            receipt = BootstrapReceipt.create(
                operation_id="evaluation-replace",
                runtime_repository="https://github.com/example/runtime.git",
                runtime_commit="a" * 40,
                repository_identity="org/repo",
                plan_hash=canonical_sha256({"actions": [item.model_dump(mode="json") for item in replace_actions]}),
                created_actions=tuple(action.action_id for action in replace_actions),
            )
            status = "replaced"
        else:
            status = "rollback_preserved_old_contract"
    return EvaluatorLifecycleResult(
        action=action,
        bundle=generated_bundle,
        lineage_hash=canonical_sha256(generated_bundle.model_dump(mode="json")),
        status=status,
        replacement_receipt=receipt,
    )
