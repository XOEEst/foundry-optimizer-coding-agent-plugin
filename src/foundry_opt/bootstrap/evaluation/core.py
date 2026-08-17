from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError, model_validator

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
_MIN_DEVELOPMENT_CASES = 10
_MIN_VALIDATING_CASES = 5
_DEVELOPMENT_RATIO = 2 / 3
_ALLOWED_PROVENANCE = "auto_generated_unreviewed"
# Live Foundry projects expose individual built-in safety evaluators from the shared registry
# (for example `azureml://registries/azureml/evaluators/builtin.violence/versions/3`). There is
# no aggregate `content_safety` built-in in the catalogs observed so far, so the required
# guardrail is a *bundle* resolved by canonical name. The legacy aggregate id is honored only
# when a project actually returns it.
LEGACY_AGGREGATE_SAFETY_ID = "azureai://built-in/evaluators/content_safety"
LEGACY_AGGREGATE_SAFETY_NAME = "content_safety"
REQUIRED_SAFETY_EVALUATORS: tuple[str, ...] = (
    "violence",
    "sexual",
    "self_harm",
    "hate_unfairness",
    "indirect_attack",
)
OPTIONAL_SAFETY_EVALUATORS: tuple[str, ...] = ("protected_material",)
KNOWN_SAFETY_EVALUATORS: tuple[str, ...] = (*REQUIRED_SAFETY_EVALUATORS, *OPTIONAL_SAFETY_EVALUATORS)
_BUILTIN_NAME_PREFIX = "builtin."
_SPLIT_ALGORITHM_VERSION = "evaluation-core-split/v4"
TRACE_MIN_GENERATED_SAMPLES = _TRACE_MIN_GENERATED_SAMPLES
MIN_DEVELOPMENT_CASES = _MIN_DEVELOPMENT_CASES
MIN_VALIDATING_CASES = _MIN_VALIDATING_CASES
DEVELOPMENT_RATIO = _DEVELOPMENT_RATIO
SPLIT_ALGORITHM_VERSION = _SPLIT_ALGORITHM_VERSION
TARGET_SAMPLE_COUNT = 30


def canonical_safety_name(evaluator_id: str, evaluator_name: str | None = None) -> str | None:
    """Return the canonical safety evaluator name for a catalog id/name, or None.

    Accepts the immutable registry shape
    (`azureml://registries/azureml/evaluators/builtin.violence/versions/3`), the plain or
    `builtin.`-prefixed catalog name, and the legacy aggregate id when a project returns it.
    """

    candidates: list[str] = []
    if evaluator_name:
        candidates.append(evaluator_name)
    if evaluator_id:
        segments = [segment for segment in evaluator_id.split("/") if segment]
        if "versions" in segments:
            index = segments.index("versions")
            if index >= 1:
                candidates.append(segments[index - 1])
        elif segments:
            candidates.append(segments[-1])
    for candidate in candidates:
        normalized = candidate.strip().casefold()
        if normalized.startswith(_BUILTIN_NAME_PREFIX):
            normalized = normalized[len(_BUILTIN_NAME_PREFIX):]
        normalized = normalized.replace("-", "_")
        if normalized == LEGACY_AGGREGATE_SAFETY_NAME:
            return LEGACY_AGGREGATE_SAFETY_NAME
        if normalized in KNOWN_SAFETY_EVALUATORS:
            return normalized
    return None


def assert_required_safety_coverage(
    names: Sequence[str],
    *,
    required: Sequence[str] = REQUIRED_SAFETY_EVALUATORS,
    field: str = "safety bundle",
) -> None:
    """Fail closed unless the resolved safety names cover the required bundle."""

    resolved = {str(name).strip().casefold() for name in names if name}
    if LEGACY_AGGREGATE_SAFETY_NAME in resolved:
        # The aggregate evaluator covers the whole bundle, but only when a project really
        # returns it; it is never assumed.
        return
    missing = [name for name in required if name not in resolved]
    if missing:
        raise BootstrapConfigError(
            f"{field} must cover every required safety evaluator; missing: {', '.join(sorted(missing))}"
        )
_CAMEL_SEGMENT_RE = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")
_PROHIBITED_PART_SEQUENCES = (
    ("dataset", "row"),
    ("dataset", "rows"),
    ("token",),
    ("tokens",),
    ("prompt",),
    ("prompts",),
    ("response",),
    ("responses",),
    ("trace",),
    ("traces",),
    ("raw",),
    ("content",),
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


def _parse_bool(value: object, *, field: str) -> bool:
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


def _optional_string(value: object, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise BootstrapConfigError(f"{field} must be a string")
    text = value.strip()
    return text or None


def _key_parts(value: str) -> tuple[str, ...]:
    replaced = value.replace("-", "_")
    rough_parts = [part for part in replaced.split("_") if part]
    normalized_parts: list[str] = []
    for part in rough_parts:
        segments = _CAMEL_SEGMENT_RE.findall(part) or [part]
        normalized_parts.extend(segment.lower() for segment in segments if segment)
    return tuple(normalized_parts)


def _reject_prohibited_fields(value: object, *, field: str) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if not isinstance(key, str):
                raise BootstrapConfigError(f"{field} keys must be strings")
            parts = _key_parts(key)
            for prohibited in _PROHIBITED_PART_SEQUENCES:
                if len(parts) >= len(prohibited):
                    for index in range(len(parts) - len(prohibited) + 1):
                        if parts[index : index + len(prohibited)] == prohibited:
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
    normalized_groups: tuple[tuple[str, str, tuple[str, ...]], ...]


class DeploymentDefaults(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    environment: str
    require_aligned_binding: StrictBool
    enabled: StrictBool
    hard_guardrail_names: tuple[str, ...]


class ReplacementOperation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    operation_id: str = Field(min_length=1)
    runtime_repository: str = Field(pattern=r"^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?$")
    runtime_commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    repository_identity: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ActivationRun(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    phase: Literal["development", "validating"]
    evaluator_id: str
    executable: StrictBool
    score: float | StrictBool
    normalization_kind: Literal["scalar", "pass_fail"]
    source_min: float | None = None
    source_max: float | None = None
    passed: StrictBool | None = None


class ActivationCleanup(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    completed: StrictBool


class ActivationReceipt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    attempted: StrictBool
    activated: StrictBool
    status: Literal["succeeded", "failed"]
    operation_id: str
    runtime_repository: str
    runtime_commit: str
    repository_identity: str
    bundle_objective_hash: str
    split_lineage_hash: str
    development_definition_id: str
    validating_definition_id: str
    runs: list[ActivationRun]
    cleanup: ActivationCleanup
    detail: str | None = None

    @model_validator(mode="after")
    def validate_consistency(self) -> "ActivationReceipt":
        if self.activated and not self.attempted:
            raise ValueError("activated receipt requires attempted=true")
        if self.status == "succeeded" and not self.activated:
            raise ValueError("successful activation receipt requires activated=true")
        if self.status == "failed" and self.activated:
            raise ValueError("failed activation receipt cannot be activated")
        return self


@dataclass(frozen=True)
class EvaluatorLifecycleResult:
    action: Literal["reuse", "generate", "replace"]
    active_bundle: DefaultEvaluatorBundle
    previous_bundle: DefaultEvaluatorBundle | None
    lineage_hash: str
    split_hash: str
    status: str
    attempted_bundle: DefaultEvaluatorBundle
    activated_bundle: DefaultEvaluatorBundle | None
    retained_bundle: DefaultEvaluatorBundle | None


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
    if not _parse_bool(item.get("relevant", False), field="dataset.relevant"):
        reasons.append("not_relevant")
    version = _optional_string(item.get("version"), field="dataset.version")
    if version is None or not _parse_bool(item.get("immutable_version", False), field="dataset.immutable_version"):
        reasons.append("not_immutable")
    sample_count = _require_int(item.get("sample_count"), field="dataset.sample_count")
    if sample_count < (_MIN_DEVELOPMENT_CASES + _MIN_VALIDATING_CASES):
        reasons.append("insufficient_samples")
    if requires_labels is True and not _parse_bool(item.get("has_labels", False), field="dataset.has_labels"):
        reasons.append("missing_labels")
    split = _optional_string(item.get("split_compatibility"), field="dataset.split_compatibility")
    if split not in {"development_validating", "both", "compatible"}:
        reasons.append("split_incompatible")
    return AssetSuitability(asset_id=dataset_id, kind="dataset", suitable=not reasons, reasons=tuple(reasons), version=version)


def assess_definition_suitability(candidate: Mapping[str, object], *, expected_schema: str) -> AssetSuitability:
    item = _require_mapping(candidate, field="definition")
    definition_id = _require_string(item.get("definition_id"), field="definition.definition_id")
    reasons: list[str] = []
    if _require_string(item.get("schema"), field="definition.schema") != expected_schema:
        reasons.append("schema_mismatch")
    if not _parse_bool(item.get("relevant", False), field="definition.relevant"):
        reasons.append("not_relevant")
    version = _optional_string(item.get("version"), field="definition.version")
    if version is None or not _parse_bool(item.get("immutable_version", False), field="definition.immutable_version"):
        reasons.append("not_immutable")
    return AssetSuitability(asset_id=definition_id, kind="definition", suitable=not reasons, reasons=tuple(reasons), version=version)


def assess_evaluator_suitability(candidate: Mapping[str, object], *, expected_schema: str) -> AssetSuitability:
    item = _require_mapping(candidate, field="evaluator")
    evaluator_id = _require_string(item.get("evaluator_id"), field="evaluator.evaluator_id")
    reasons: list[str] = []
    if _require_string(item.get("schema"), field="evaluator.schema") != expected_schema:
        reasons.append("schema_mismatch")
    if not _parse_bool(item.get("relevant", False), field="evaluator.relevant"):
        reasons.append("not_relevant")
    version = _optional_string(item.get("version"), field="evaluator.version")
    if version is None or not _parse_bool(item.get("immutable_version", False), field="evaluator.immutable_version"):
        reasons.append("not_immutable")
    return AssetSuitability(asset_id=evaluator_id, kind="evaluator", suitable=not reasons, reasons=tuple(reasons), version=version)


def choose_dataset_strategy(asset: Mapping[str, object]) -> Literal["trace", "synthetic_only"]:
    item = _require_mapping(asset, field="dataset_strategy")
    generated = _require_int(item.get("generated_samples"), field="dataset_strategy.generated_samples")
    prerequisites_available = _parse_bool(item.get("prerequisites_available", False), field="dataset_strategy.prerequisites_available")
    if prerequisites_available and generated >= _TRACE_MIN_GENERATED_SAMPLES:
        return "trace"
    return "synthetic_only"


def _canonical_group_record(group_id: str, category: str, row_ids: Sequence[str]) -> tuple[str, str, tuple[str, ...]]:
    return (group_id, category, tuple(sorted(row_ids, key=str.casefold)))


def deterministic_split_targets(total_cases: int) -> tuple[int, int]:
    """Return the deterministic (development, validating) case targets for a case total.

    The split targets about two-thirds development and one-third validating while always
    preserving the 10/5 minimums; a 30-case dataset therefore targets 20/10.
    """

    if total_cases < (_MIN_DEVELOPMENT_CASES + _MIN_VALIDATING_CASES):
        raise BootstrapConfigError("dataset requires at least 15 unique cases")
    development = max(_MIN_DEVELOPMENT_CASES, round(total_cases * _DEVELOPMENT_RATIO))
    development = min(total_cases - _MIN_VALIDATING_CASES, development)
    validating = total_cases - development
    if development < _MIN_DEVELOPMENT_CASES or validating < _MIN_VALIDATING_CASES:
        raise BootstrapConfigError("dataset split must satisfy 10/5 minimum case counts")
    return development, validating


def split_dataset_rows(rows: Sequence[Mapping[str, object]]) -> DatasetSplitResult:
    row_to_group: dict[str, str] = {}
    grouped_rows: dict[str, dict[str, object]] = {}
    for raw in rows:
        item = _require_mapping(raw, field="dataset_row")
        row_id = _require_string(item.get("row_id"), field="dataset_row.row_id")
        group_id = _optional_string(item.get("group_id"), field="dataset_row.group_id") or row_id.casefold()
        category = _optional_string(item.get("category"), field="dataset_row.category") or ""
        row_key = row_id.casefold()
        group_key = group_id.casefold()
        existing_group = row_to_group.get(row_key)
        if existing_group is not None and existing_group != group_key:
            raise BootstrapConfigError("row_id must belong to exactly one group")
        row_to_group[row_key] = group_key
        entry = grouped_rows.setdefault(group_key, {"group_id": group_id, "category": category, "row_ids": {}})
        existing_category = str(entry["category"])
        if existing_category.casefold() != category.casefold():
            if existing_category and category:
                raise BootstrapConfigError("group metadata category conflicts across rows")
            if category:
                entry["category"] = category
        stored_rows: dict[str, str] = entry["row_ids"]  # type: ignore[assignment]
        if row_key in stored_rows and stored_rows[row_key] != row_id:
            raise BootstrapConfigError("row_id casefold collision detected")
        stored_rows[row_key] = row_id
    groups = [
        _canonical_group_record(str(value["group_id"]), str(value["category"]), tuple(sorted(cast_values := tuple(value["row_ids"].values()), key=str.casefold)))
        for _, value in sorted(grouped_rows.items(), key=lambda item: (str(item[1]["category"]).casefold(), str(item[1]["group_id"]).casefold()))
    ]
    if len(groups) < 1:
        raise BootstrapConfigError("dataset must not be empty")
    total_cases = sum(len(group[2]) for group in groups)
    target_development_cases, target_validating_cases = deterministic_split_targets(total_cases)
    sorted_groups = sorted(groups, key=lambda item: (item[1].casefold(), item[0].casefold(), item[2]))
    development_groups: list[tuple[str, str, tuple[str, ...]]] = []
    validating_groups: list[tuple[str, str, tuple[str, ...]]] = []
    development_cases = 0
    validating_cases = total_cases
    for group in sorted_groups:
        group_cases = len(group[2])
        projected_ratio = abs((development_cases + group_cases) - target_development_cases)
        current_ratio = abs(development_cases - target_development_cases)
        can_move = validating_cases - group_cases >= _MIN_VALIDATING_CASES
        if can_move and (development_cases < _MIN_DEVELOPMENT_CASES or projected_ratio <= current_ratio):
            development_groups.append(group)
            development_cases += group_cases
            validating_cases -= group_cases
        else:
            validating_groups.append(group)
    while development_cases < _MIN_DEVELOPMENT_CASES:
        if not validating_groups:
            raise BootstrapConfigError("dataset split must satisfy minimum development cases")
        candidate = validating_groups.pop(0)
        if validating_cases - len(candidate[2]) < _MIN_VALIDATING_CASES:
            raise BootstrapConfigError("dataset split cannot satisfy validating minimum")
        development_groups.append(candidate)
        development_cases += len(candidate[2])
        validating_cases -= len(candidate[2])
    while validating_cases < _MIN_VALIDATING_CASES:
        if not development_groups:
            raise BootstrapConfigError("dataset split must satisfy minimum validating cases")
        candidate = development_groups.pop()
        development_cases -= len(candidate[2])
        validating_cases += len(candidate[2])
        if development_cases < _MIN_DEVELOPMENT_CASES:
            raise BootstrapConfigError("dataset split cannot satisfy development minimum")
        validating_groups.append(candidate)
    development_groups = sorted(development_groups, key=lambda item: (item[1].casefold(), item[0].casefold(), item[2]))
    validating_groups = sorted(validating_groups, key=lambda item: (item[1].casefold(), item[0].casefold(), item[2]))
    development_group_ids = {group[0] for group in development_groups}
    validating_group_ids = {group[0] for group in validating_groups}
    if development_group_ids & validating_group_ids:
        raise BootstrapConfigError("development and validating splits must not overlap")
    labeled_categories = {group[1] for group in groups if group[1]}
    if labeled_categories:
        dev_categories = {group[1] for group in development_groups if group[1]}
        val_categories = {group[1] for group in validating_groups if group[1]}
        if not dev_categories:
            raise BootstrapConfigError("development split must retain labeled category coverage")
        if len(labeled_categories) > 1 and not val_categories:
            raise BootstrapConfigError("validating split must retain labeled category coverage")
    development_rows = tuple(row_id for _, _, row_ids in development_groups for row_id in row_ids)
    validating_rows = tuple(row_id for _, _, row_ids in validating_groups for row_id in row_ids)
    normalized_groups = tuple(sorted(groups, key=lambda item: (item[1].casefold(), item[0].casefold(), item[2])))
    split_payload = {
        "algorithm_version": "evaluation-core-split/v4",
        "normalized_groups": normalized_groups,
        "development_group_ids": tuple(group[0] for group in development_groups),
        "validating_group_ids": tuple(group[0] for group in validating_groups),
        "development_case_count": len(development_rows),
        "validating_case_count": len(validating_rows),
    }
    split_hash = canonical_sha256(split_payload)
    return DatasetSplitResult(
        algorithm_version="evaluation-core-split/v4",
        split_hash=split_hash,
        development=development_rows,
        validating=validating_rows,
        development_groups=tuple(group[0] for group in development_groups),
        validating_groups=tuple(group[0] for group in validating_groups),
        normalized_groups=normalized_groups,
    )


def compute_split_lineage_hash(split_result: DatasetSplitResult) -> str:
    development_group_ids = tuple(sorted(split_result.development_groups, key=str.casefold))
    validating_group_ids = tuple(sorted(split_result.validating_groups, key=str.casefold))
    if set(group.casefold() for group in development_group_ids) & set(group.casefold() for group in validating_group_ids):
        raise BootstrapConfigError("development and validating groups must not casefold-collide")
    if len({row.casefold() for row in split_result.development}) != len(split_result.development):
        raise BootstrapConfigError("development case IDs must not casefold-collide")
    if len({row.casefold() for row in split_result.validating}) != len(split_result.validating):
        raise BootstrapConfigError("validating case IDs must not casefold-collide")
    payload = {
        "algorithm_version": split_result.algorithm_version,
        "normalized_groups": tuple(sorted(split_result.normalized_groups, key=lambda item: (item[1].casefold(), item[0].casefold(), item[2]))),
        "development_group_ids": development_group_ids,
        "validating_group_ids": validating_group_ids,
        "development_case_ids": tuple(sorted(split_result.development, key=str.casefold)),
        "validating_case_ids": tuple(sorted(split_result.validating, key=str.casefold)),
        "development_case_count": len(split_result.development),
        "validating_case_count": len(split_result.validating),
    }
    return canonical_sha256(payload)


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
        if _parse_bool(part.get("pass_fail", False), field=f"rubric.dimensions[{index}].pass_fail"):
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
        if not _parse_bool(item.get("executable", False), field=f"cases[{index}].executable"):
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
    safety_names: list[str] = []
    for index, guardrail in enumerate(guardrails):
        item = _require_mapping(guardrail, field=f"guardrails[{index}]")
        evaluator_id = _require_string(item.get("evaluator_id"), field=f"guardrails[{index}].evaluator_id")
        safety_name = _optional_string(item.get("safety_name"), field=f"guardrails[{index}].safety_name") or canonical_safety_name(
            evaluator_id,
            _optional_string(item.get("evaluator_name"), field=f"guardrails[{index}].evaluator_name"),
        )
        if safety_name is None:
            continue
        # Every configured safety evaluator is a hard guardrail: a single sub-rate below 1.0
        # blocks activation.
        rate = _require_finite_number(item.get("pass_rate"), field=f"guardrails[{index}].pass_rate")
        if rate != 1.0:
            raise BootstrapConfigError(
                f"safety evaluator {safety_name} must pass at 100%; measured {rate}"
            )
        safety_names.append(safety_name)
    if not safety_names:
        raise BootstrapConfigError("activation requires the built-in safety evaluator bundle")
    assert_required_safety_coverage(safety_names, field="activation guardrails")
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
    if isinstance(value, int) and value in (0, 1):
        return float(value)
    if isinstance(value, float) and value in (0.0, 1.0):
        return value
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
    payload = _require_mapping(defaults, field="defaults")
    _reject_prohibited_fields(payload, field="defaults")
    safe_persisted_document(payload)
    try:
        return DeploymentDefaults.model_validate(payload)
    except ValidationError as exc:
        raise BootstrapConfigError(str(exc)) from exc


def _validate_activation_receipt(
    receipt: ActivationReceipt,
    *,
    operation: ReplacementOperation,
    generated_bundle: DefaultEvaluatorBundle,
    canonical_split_lineage: str,
    definitions: tuple[ImmutableDefinitionReference, ImmutableDefinitionReference],
) -> None:
    if receipt.operation_id != operation.operation_id:
        raise BootstrapConfigError("activation receipt must match operation_id")
    if receipt.runtime_repository != operation.runtime_repository:
        raise BootstrapConfigError("activation receipt must match runtime_repository")
    if receipt.runtime_commit != operation.runtime_commit:
        raise BootstrapConfigError("activation receipt must match runtime_commit")
    if receipt.repository_identity != operation.repository_identity:
        raise BootstrapConfigError("activation receipt must match repository_identity")
    if receipt.bundle_objective_hash != generated_bundle.objective.objective_hash:
        raise BootstrapConfigError("activation receipt must match bundle objective hash")
    if receipt.split_lineage_hash != canonical_split_lineage:
        raise BootstrapConfigError("activation receipt must match split lineage hash")
    development_definition, validating_definition = definitions
    if development_definition.definition_id == validating_definition.definition_id:
        raise BootstrapConfigError("development and validating definitions must be distinct")
    if receipt.development_definition_id != development_definition.definition_id:
        raise BootstrapConfigError("activation receipt must match development definition")
    if receipt.validating_definition_id != validating_definition.definition_id:
        raise BootstrapConfigError("activation receipt must match validating definition")
    phases = {run.phase for run in receipt.runs}
    if phases != {"development", "validating"}:
        raise BootstrapConfigError("activation receipt must include development and validating runs")
    safety_runs = [run for run in receipt.runs if canonical_safety_name(run.evaluator_id) is not None]
    if not safety_runs or any(run.passed is not True for run in safety_runs):
        raise BootstrapConfigError("activation receipt must include passing safety runs")
    assert_required_safety_coverage(
        [name for name in (canonical_safety_name(run.evaluator_id) for run in safety_runs) if name],
        field="activation receipt safety runs",
    )
    if receipt.cleanup.completed is not True:
        raise BootstrapConfigError("activation receipt cleanup must be completed")


def choose_default_evaluator_bundle(
    *,
    existing_bundle: DefaultEvaluatorBundle | None,
    generated_bundle: DefaultEvaluatorBundle | None,
    definitions: tuple[ImmutableDefinitionReference, ImmutableDefinitionReference],
    development_dataset: ImmutableDatasetReference,
    validating_dataset: ImmutableDatasetReference,
    persisted_split_lineage_hash: str,
    split_result: DatasetSplitResult | None = None,
    canonical_split_lineage_hash: str | None = None,
    explicit_replace: bool = False,
    operation: Mapping[str, object] | ReplacementOperation | None = None,
    activation_receipt: Mapping[str, object] | ActivationReceipt | None = None,
) -> EvaluatorLifecycleResult:
    expected_definitions = tuple(definitions)
    if (split_result is None) == (canonical_split_lineage_hash is None):
        raise BootstrapConfigError("exactly one of split_result or canonical_split_lineage_hash is required")
    canonical_split_lineage = (
        compute_split_lineage_hash(split_result) if split_result is not None else str(canonical_split_lineage_hash)
    )
    if development_dataset.dataset_id == validating_dataset.dataset_id:
        raise BootstrapConfigError("development and validating datasets must be distinct immutable references")
    if persisted_split_lineage_hash != canonical_split_lineage:
        raise BootstrapConfigError("generated evaluator bundle split lineage hash mismatch")
    if existing_bundle is not None and not explicit_replace:
        return EvaluatorLifecycleResult(
            action="reuse",
            active_bundle=existing_bundle,
            previous_bundle=existing_bundle,
            lineage_hash=canonical_sha256(existing_bundle.model_dump(mode="json")),
            split_hash=canonical_split_lineage,
            status="reused_existing",
            attempted_bundle=existing_bundle,
            activated_bundle=existing_bundle,
            retained_bundle=existing_bundle,
        )
    if generated_bundle is None:
        raise BootstrapConfigError("generated evaluator bundle required when no suitable existing bundle exists")
    expected_dataset_ids = {development_dataset.dataset_id, validating_dataset.dataset_id}
    bundle_dataset_ids = {item.dataset_id for item in generated_bundle.datasets}
    if bundle_dataset_ids != expected_dataset_ids:
        raise BootstrapConfigError("generated evaluator bundle must use immutable development/validating dataset references")
    if tuple(generated_bundle.definitions) != expected_definitions:
        raise BootstrapConfigError("generated evaluator bundle must preserve explicit definitions")
    parsed_operation = None
    if operation is not None:
        try:
            parsed_operation = operation if isinstance(operation, ReplacementOperation) else ReplacementOperation.model_validate(operation)
        except ValidationError as exc:
            raise BootstrapConfigError(str(exc)) from exc
    parsed_receipt = None
    if activation_receipt is not None:
        try:
            parsed_receipt = activation_receipt if isinstance(activation_receipt, ActivationReceipt) else ActivationReceipt.model_validate(activation_receipt)
        except ValidationError as exc:
            raise BootstrapConfigError(str(exc)) from exc
    if explicit_replace:
        if existing_bundle is None:
            raise BootstrapConfigError("explicit replace requires an existing bundle")
        if parsed_operation is None:
            raise BootstrapConfigError("explicit replace requires operation metadata")
        if parsed_receipt is None:
            return EvaluatorLifecycleResult(
                action="replace",
                active_bundle=existing_bundle,
                previous_bundle=existing_bundle,
                lineage_hash=canonical_sha256(existing_bundle.model_dump(mode="json")),
                split_hash=canonical_split_lineage,
                status=f"pending_activation:{parsed_operation.operation_id}",
                attempted_bundle=generated_bundle,
                activated_bundle=None,
                retained_bundle=existing_bundle,
            )
        _validate_activation_receipt(
            parsed_receipt,
            operation=parsed_operation,
            generated_bundle=generated_bundle,
            canonical_split_lineage=canonical_split_lineage,
            definitions=expected_definitions,
        )
        if parsed_receipt.status == "failed":
            return EvaluatorLifecycleResult(
                action="replace",
                active_bundle=existing_bundle,
                previous_bundle=existing_bundle,
                lineage_hash=canonical_sha256(existing_bundle.model_dump(mode="json")),
                split_hash=canonical_split_lineage,
                status=f"activation_failed:{parsed_operation.operation_id}",
                attempted_bundle=generated_bundle,
                activated_bundle=None,
                retained_bundle=existing_bundle,
            )
        lineage_hash = canonical_sha256(
            {
                "operation_id": parsed_operation.operation_id,
                "runtime_repository": parsed_operation.runtime_repository,
                "runtime_commit": parsed_operation.runtime_commit,
                "repository_identity": parsed_operation.repository_identity,
                "bundle_hash": canonical_sha256(generated_bundle.model_dump(mode="json")),
                "activation_status": parsed_receipt.status,
                "split_lineage_hash": canonical_split_lineage,
            }
        )
        return EvaluatorLifecycleResult(
            action="replace",
            active_bundle=generated_bundle,
            previous_bundle=existing_bundle,
            lineage_hash=lineage_hash,
            split_hash=canonical_split_lineage,
            status=f"replaced:{parsed_operation.operation_id}",
            attempted_bundle=generated_bundle,
            activated_bundle=generated_bundle,
            retained_bundle=existing_bundle,
        )
    if parsed_operation is not None and parsed_receipt is not None:
        _validate_activation_receipt(
            parsed_receipt,
            operation=parsed_operation,
            generated_bundle=generated_bundle,
            canonical_split_lineage=canonical_split_lineage,
            definitions=expected_definitions,
        )
        return EvaluatorLifecycleResult(
            action="generate",
            active_bundle=generated_bundle if parsed_receipt.status == "succeeded" else existing_bundle or generated_bundle,
            previous_bundle=existing_bundle,
            lineage_hash=canonical_sha256(generated_bundle.model_dump(mode="json")),
            split_hash=canonical_split_lineage,
            status="generated_activated" if parsed_receipt.status == "succeeded" else "generated_activation_failed",
            attempted_bundle=generated_bundle,
            activated_bundle=generated_bundle if parsed_receipt.status == "succeeded" else None,
            retained_bundle=existing_bundle,
        )
    return EvaluatorLifecycleResult(
        action="generate",
        active_bundle=existing_bundle or generated_bundle,
        previous_bundle=existing_bundle,
        lineage_hash=canonical_sha256(generated_bundle.model_dump(mode="json")),
        split_hash=canonical_split_lineage,
        status="generated_pending_activation",
        attempted_bundle=generated_bundle,
        activated_bundle=None,
        retained_bundle=existing_bundle,
    )
