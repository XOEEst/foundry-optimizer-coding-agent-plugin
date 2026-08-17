from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from foundry_opt.bootstrap.canonical import canonical_sha256, safe_persisted_document
from foundry_opt.bootstrap.contracts import (
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
from foundry_opt.bootstrap.errors import BootstrapConfigError

_TRACE_MIN_GENERATED_SAMPLES = 15
_MIN_DEVELOPMENT_COUNT = 10
_MIN_VALIDATING_COUNT = 5
_DEVELOPMENT_RATIO = 2 / 3
_ALLOWED_PROVENANCE = "auto_generated_unreviewed"
_PROHIBITED_FIELD_PARTS = frozenset(
    {
        "content",
        "dataset",
        "dataset_rows",
        "prompt",
        "prompts",
        "raw",
        "response",
        "responses",
        "row",
        "rows",
        "trace",
        "traces",
    }
)


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


def _require_bool(value: object, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise BootstrapConfigError(f"{field} must be boolean")
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


def _normalized_key(value: str) -> str:
    return "".join(character.lower() if character.isalnum() else "_" for character in value)


def _reject_prohibited_fields(value: object, *, field: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise BootstrapConfigError(f"{field} keys must be strings")
            parts = {part for part in _normalized_key(key).split("_") if part}
            if parts & _PROHIBITED_FIELD_PARTS:
                raise BootstrapConfigError(f"{field} contains prohibited raw-content field {key!r}")
            _reject_prohibited_fields(child, field=f"{field}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _reject_prohibited_fields(child, field=f"{field}[{index}]")


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
    development_groups: tuple[str, ...]
    validating_groups: tuple[str, ...]


@dataclass(frozen=True)
class DeploymentDefaults:
    environment: str
    require_aligned_binding: bool
    enabled: bool
    hard_guardrail_names: tuple[str, ...]


@dataclass(frozen=True)
class ReplacementOperation:
    operation_id: str
    runtime_repository: str
    runtime_commit: str
    repository_identity: str


@dataclass(frozen=True)
class EvaluatorLifecycleResult:
    action: Literal["reuse", "generate", "replace"]
    active_bundle: DefaultEvaluatorBundle
    previous_bundle: DefaultEvaluatorBundle | None
    lineage_hash: str
    split_hash: str
    status: str
    activated_bundle: DefaultEvaluatorBundle
    replaced_bundle: DefaultEvaluatorBundle | None = None


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
    prerequisites_available = bool(item.get("prerequisites_available", False))
    if prerequisites_available and generated >= _TRACE_MIN_GENERATED_SAMPLES:
        return "trace"
    return "synthetic_only"


def split_dataset_rows(rows: Sequence[Mapping[str, object]]) -> DatasetSplitResult:
    grouped_rows: dict[str, dict[str, object]] = {}
    for raw in rows:
        item = _require_mapping(raw, field="dataset_row")
        row_id = _require_string(item.get("row_id"), field="dataset_row.row_id")
        group_id = _optional_string(item.get("group_id")) or row_id.casefold()
        category = _optional_string(item.get("category")) or ""
        entry = grouped_rows.setdefault(group_id.casefold(), {"group_id": group_id, "category": category, "row_ids": []})
        if category and not entry["category"]:
            entry["category"] = category
        entry["row_ids"].append(row_id)  # type: ignore[index]
    groups = [
        {
            "group_id": str(value["group_id"]),
            "category": str(value["category"]),
            "row_ids": tuple(sorted({str(row_id) for row_id in value["row_ids"]}, key=str.casefold)),
        }
        for _, value in sorted(grouped_rows.items(), key=lambda item: (str(item[1]["category"]).casefold(), str(item[1]["group_id"]).casefold()))
    ]
    if len(groups) < (_MIN_DEVELOPMENT_COUNT + _MIN_VALIDATING_COUNT):
        raise BootstrapConfigError("dataset requires at least 15 unique groups")
    total_groups = len(groups)
    development_target = max(_MIN_DEVELOPMENT_COUNT, math.ceil(total_groups * _DEVELOPMENT_RATIO))
    development_target = min(total_groups - _MIN_VALIDATING_COUNT, development_target)
    validating_target = total_groups - development_target
    if development_target < _MIN_DEVELOPMENT_COUNT or validating_target < _MIN_VALIDATING_COUNT:
        raise BootstrapConfigError("dataset split must satisfy 10/5 minimums")
    categories = defaultdict(list)
    for group in groups:
        categories[group["category"]].append(group)
    development_groups: list[dict[str, object]] = []
    validating_groups: list[dict[str, object]] = []
    ordered_categories = sorted(categories, key=str.casefold)
    for category in ordered_categories:
        bucket = sorted(categories[category], key=lambda item: (str(item["group_id"]).casefold(), item["row_ids"]))
        category_size = len(bucket)
        category_development_target = math.ceil(category_size * _DEVELOPMENT_RATIO)
        minimum_for_validating = 1 if category_size > 1 else 0
        category_development_target = min(category_size - minimum_for_validating, category_development_target)
        if development_target - len(development_groups) < category_development_target:
            category_development_target = max(0, development_target - len(development_groups))
        development_groups.extend(bucket[:category_development_target])
        validating_groups.extend(bucket[category_development_target:])
    remaining = [group for group in groups if group not in development_groups and group not in validating_groups]
    validating_groups.extend(remaining)
    if len(development_groups) < development_target:
        movable = [group for group in validating_groups if len(validating_groups) - 1 >= _MIN_VALIDATING_COUNT]
        while len(development_groups) < development_target and movable:
            development_groups.append(movable.pop(0))
            validating_groups = [group for group in validating_groups if group not in development_groups]
    if len(validating_groups) < _MIN_VALIDATING_COUNT:
        raise BootstrapConfigError("dataset split cannot satisfy validating minimum")
    development_groups = sorted(development_groups, key=lambda item: (str(item["category"]).casefold(), str(item["group_id"]).casefold()))
    validating_groups = sorted(validating_groups, key=lambda item: (str(item["category"]).casefold(), str(item["group_id"]).casefold()))
    if len(development_groups) != development_target:
        raise BootstrapConfigError("dataset split did not reach target development size")
    development_group_ids = {str(group["group_id"]) for group in development_groups}
    validating_group_ids = {str(group["group_id"]) for group in validating_groups}
    if development_group_ids & validating_group_ids:
        raise BootstrapConfigError("development and validating splits must not overlap")
    labeled_categories = {str(group["category"]) for group in groups if str(group["category"])}
    if labeled_categories:
        dev_categories = {str(group["category"]) for group in development_groups if str(group["category"])}
        val_categories = {str(group["category"]) for group in validating_groups if str(group["category"])}
        if not dev_categories:
            raise BootstrapConfigError("development split must retain labeled category coverage")
        if len(labeled_categories) > 1 and not val_categories:
            raise BootstrapConfigError("validating split must retain labeled category coverage")
        if not labeled_categories.issubset(dev_categories | val_categories):
            raise BootstrapConfigError("dataset split lost labeled category coverage")
    development_rows = tuple(row_id for group in development_groups for row_id in group["row_ids"])
    validating_rows = tuple(row_id for group in validating_groups for row_id in group["row_ids"])
    split_payload = {
        "algorithm_version": "evaluation-core-split/v2",
        "development_groups": [{"group_id": group["group_id"], "row_ids": group["row_ids"], "category": group["category"]} for group in development_groups],
        "validating_groups": [{"group_id": group["group_id"], "row_ids": group["row_ids"], "category": group["category"]} for group in validating_groups],
    }
    split_hash = canonical_sha256(split_payload)
    return DatasetSplitResult(
        algorithm_version="evaluation-core-split/v2",
        split_hash=split_hash,
        development=development_rows,
        validating=validating_rows,
        development_groups=tuple(str(group["group_id"]) for group in development_groups),
        validating_groups=tuple(str(group["group_id"]) for group in validating_groups),
    )


def validate_generated_rubric(document: Mapping[str, object]) -> None:
    item = _require_mapping(document, field="rubric")
    dimensions = item.get("dimensions")
    if not isinstance(dimensions, Sequence) or isinstance(dimensions, (str, bytes)) or not dimensions:
        raise BootstrapConfigError("rubric.dimensions must be a non-empty sequence")
    seen_names: set[str] = set()
    for index, dimension in enumerate(dimensions):
        part = _require_mapping(dimension, field=f"rubric.dimensions[{index}]")
        name = _require_string(part.get("name"), field=f"rubric.dimensions[{index}].name")
        normalized_name = name.casefold()
        if normalized_name in seen_names:
            raise BootstrapConfigError("rubric dimensions must be unique")
        seen_names.add(normalized_name)
        _require_positive_finite(part.get("weight"), field=f"rubric.dimensions[{index}].weight")
        required_inputs = part.get("required_inputs")
        if not isinstance(required_inputs, Sequence) or isinstance(required_inputs, (str, bytes)) or not required_inputs:
            raise BootstrapConfigError("rubric dimensions must declare non-empty required_inputs")
        normalized_inputs: set[str] = set()
        for input_index, raw_input in enumerate(required_inputs):
            required_input = _require_string(raw_input, field=f"rubric.dimensions[{index}].required_inputs[{input_index}]")
            if required_input.casefold() in normalized_inputs:
                raise BootstrapConfigError("rubric required_inputs must be unique")
            normalized_inputs.add(required_input.casefold())
        if bool(part.get("pass_fail", False)):
            threshold = _require_finite_number(part.get("threshold"), field=f"rubric.dimensions[{index}].threshold")
            if not 0.0 <= threshold <= 1.0:
                raise BootstrapConfigError("pass/fail threshold must be between 0 and 1")
            continue
        scalar = _require_mapping(part.get("scalar_range"), field=f"rubric.dimensions[{index}].scalar_range")
        minimum = _require_finite_number(scalar.get("min"), field=f"rubric.dimensions[{index}].scalar_range.min")
        maximum = _require_finite_number(scalar.get("max"), field=f"rubric.dimensions[{index}].scalar_range.max")
        if maximum <= minimum:
            raise BootstrapConfigError("scalar ranges must increase")
        threshold = _require_finite_number(part.get("threshold"), field=f"rubric.dimensions[{index}].threshold")
        if threshold < minimum or threshold > maximum:
            raise BootstrapConfigError("scalar threshold must lie within declared range")


def validate_activation(*, cases: Sequence[Mapping[str, object]], guardrails: Sequence[Mapping[str, object]], generated_bundle: Mapping[str, object] | None = None) -> None:
    if not cases:
        raise BootstrapConfigError("activation requires executable cases")
    saw_scalar_headroom = False
    saw_pass_fail_headroom = False
    for index, case in enumerate(cases):
        item = _require_mapping(case, field=f"cases[{index}]")
        if not bool(item.get("executable", False)):
            raise BootstrapConfigError("activation requires executable cases")
        normalization = _require_mapping(item.get("normalization"), field=f"cases[{index}].normalization")
        kind = _require_string(normalization.get("kind"), field=f"cases[{index}].normalization.kind")
        raw_score = item.get("score")
        if kind == "scalar":
            score = _require_finite_number(raw_score, field=f"cases[{index}].score")
            maximum = _require_finite_number(normalization.get("source_max"), field=f"cases[{index}].normalization.source_max")
            minimum = _require_finite_number(normalization.get("source_min"), field=f"cases[{index}].normalization.source_min")
            if maximum <= minimum:
                raise BootstrapConfigError("activation scalar normalization bounds must increase")
            if score < maximum:
                saw_scalar_headroom = True
        elif kind == "pass_fail":
            normalized = _normalize_pass_fail_score(raw_score, field=f"cases[{index}].score")
            if normalized == 0.0:
                saw_pass_fail_headroom = True
        else:
            raise BootstrapConfigError(f"unsupported activation normalization kind: {kind}")
    if not (saw_scalar_headroom or saw_pass_fail_headroom):
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
    resolved: list[ResolvedEvaluator] = []
    for entry in request.evaluators:
        metadata = metadata_by_id.get(entry.evaluator_id)
        if metadata is None:
            raise BootstrapConfigError(f"unknown evaluator id: {entry.evaluator_id}")
        normalization = _resolve_normalization(metadata)
        provenance = _require_string(metadata.get("provenance"), field="metadata.provenance")
        resolved.append(
            ResolvedEvaluator(
                reference=EvaluatorReference(evaluator_id=entry.evaluator_id, provenance=provenance),  # type: ignore[arg-type]
                normalization=normalization,
                weight=entry.weight if entry.weight is not None else 1.0,
            )
        )
    return ResolvedWeightedObjective.create(tuple(resolved))


def _resolve_normalization(metadata: Mapping[str, object]) -> EvaluatorNormalization:
    item = _require_mapping(metadata, field="metadata")
    kind = _require_string(item.get("kind"), field="metadata.kind")
    if kind == "pass_fail":
        return EvaluatorNormalization(kind="pass_fail")
    if kind == "scalar":
        bounds = _require_mapping(item.get("bounds"), field="metadata.bounds")
        minimum = _require_finite_number(bounds.get("min"), field="metadata.bounds.min")
        maximum = _require_finite_number(bounds.get("max"), field="metadata.bounds.max")
        if maximum <= minimum:
            raise BootstrapConfigError("metadata scalar bounds must increase")
        return EvaluatorNormalization(kind="scalar", source_min=minimum, source_max=maximum)
    raise BootstrapConfigError(f"unsupported evaluator normalization kind: {kind}")


def _normalize_pass_fail_score(value: object, *, field: str) -> float:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        numeric = float(value)
        if numeric in (0.0, 1.0):
            return numeric
        raise BootstrapConfigError(f"{field} must be boolean or binary numeric")
    raise BootstrapConfigError(f"{field} must be boolean or binary numeric")


def normalize_issue_scores(objective: ResolvedWeightedObjective, raw_scores: Mapping[str, object]) -> ScoringEvidence:
    evaluator_scores: list[float] = []
    for evaluator in objective.evaluators:
        raw = raw_scores.get(evaluator.reference.evaluator_id)
        if raw is None:
            raise BootstrapConfigError(f"missing raw score for evaluator {evaluator.reference.evaluator_id}")
        if evaluator.normalization.kind == "pass_fail":
            normalized = _normalize_pass_fail_score(raw, field=f"raw_scores[{evaluator.reference.evaluator_id}]")
        else:
            score = _require_finite_number(raw, field=f"raw_scores[{evaluator.reference.evaluator_id}]")
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


def select_default_deployment_contract(defaults: Mapping[str, object]) -> DeploymentDefaults:
    payload = dict(_require_mapping(defaults, field="defaults"))
    _reject_prohibited_fields(payload, field="defaults")
    safe_persisted_document(payload)
    hard_guardrails = payload.get("hard_guardrail_names")
    if not isinstance(hard_guardrails, Sequence) or isinstance(hard_guardrails, (str, bytes)) or not hard_guardrails:
        raise BootstrapConfigError("defaults.hard_guardrail_names must be a non-empty sequence")
    names = tuple(_require_string(item, field="defaults.hard_guardrail_names[]") for item in hard_guardrails)
    if len({name.casefold() for name in names}) != len(names):
        raise BootstrapConfigError("defaults.hard_guardrail_names must be unique")
    return DeploymentDefaults(
        environment=_require_string(payload.get("environment"), field="defaults.environment"),
        require_aligned_binding=_require_bool(payload.get("require_aligned_binding"), field="defaults.require_aligned_binding"),
        enabled=_require_bool(payload.get("enabled"), field="defaults.enabled"),
        hard_guardrail_names=names,
    )


def choose_default_evaluator_bundle(
    *,
    existing_bundle: DefaultEvaluatorBundle | None,
    generated_bundle: DefaultEvaluatorBundle | None,
    split_result: DatasetSplitResult,
    definitions: tuple[ImmutableDefinitionReference, ImmutableDefinitionReference],
    development_dataset: ImmutableDatasetReference,
    validating_dataset: ImmutableDatasetReference,
    split_lineage_hash: str,
    explicit_replace: bool = False,
    operation: ReplacementOperation | None = None,
    activation_succeeded: bool = True,
) -> EvaluatorLifecycleResult:
    expected_definitions = tuple(definitions)
    if existing_bundle is not None and not explicit_replace:
        return EvaluatorLifecycleResult(
            action="reuse",
            active_bundle=existing_bundle,
            previous_bundle=existing_bundle,
            lineage_hash=canonical_sha256(existing_bundle.model_dump(mode="json")),
            split_hash=split_lineage_hash,
            status="reused_existing",
            activated_bundle=existing_bundle,
            replaced_bundle=None,
        )
    if generated_bundle is None:
        raise BootstrapConfigError("generated evaluator bundle required when no suitable existing bundle exists")
    expected_dataset_ids = {development_dataset.dataset_id, validating_dataset.dataset_id}
    bundle_dataset_ids = {item.dataset_id for item in generated_bundle.datasets}
    if bundle_dataset_ids != expected_dataset_ids:
        raise BootstrapConfigError("generated evaluator bundle must use immutable development/validating dataset references")
    if tuple(generated_bundle.definitions) != expected_definitions:
        raise BootstrapConfigError("generated evaluator bundle must preserve explicit definitions")
    computed_split_hash = split_result.split_hash
    if computed_split_hash != split_result.split_hash or split_lineage_hash != split_result.split_hash:
        raise BootstrapConfigError("generated evaluator bundle split lineage hash mismatch")
    if explicit_replace:
        if existing_bundle is None:
            raise BootstrapConfigError("explicit replace requires an existing bundle")
        if operation is None:
            raise BootstrapConfigError("explicit replace requires operation metadata")
        if not activation_succeeded:
            return EvaluatorLifecycleResult(
                action="replace",
                active_bundle=existing_bundle,
                previous_bundle=existing_bundle,
                lineage_hash=canonical_sha256(existing_bundle.model_dump(mode="json")),
                split_hash=split_result.split_hash,
                status=f"activation_failed:{operation.operation_id}",
                activated_bundle=generated_bundle,
                replaced_bundle=existing_bundle,
            )
        lineage_hash = canonical_sha256(
            {
                "operation_id": operation.operation_id,
                "runtime_repository": operation.runtime_repository,
                "runtime_commit": operation.runtime_commit,
                "repository_identity": operation.repository_identity,
                "bundle_hash": canonical_sha256(generated_bundle.model_dump(mode="json")),
            }
        )
        return EvaluatorLifecycleResult(
            action="replace",
            active_bundle=generated_bundle,
            previous_bundle=existing_bundle,
            lineage_hash=lineage_hash,
            split_hash=split_result.split_hash,
            status=f"replaced:{operation.operation_id}",
            activated_bundle=generated_bundle,
            replaced_bundle=existing_bundle,
        )
    return EvaluatorLifecycleResult(
        action="generate",
        active_bundle=generated_bundle,
        previous_bundle=existing_bundle,
        lineage_hash=canonical_sha256(generated_bundle.model_dump(mode="json")),
        split_hash=split_result.split_hash,
        status="generated_pending_activation",
        activated_bundle=generated_bundle,
        replaced_bundle=existing_bundle,
    )
