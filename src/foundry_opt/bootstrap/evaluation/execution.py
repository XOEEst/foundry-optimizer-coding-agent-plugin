"""Approval-bound evaluation onboarding request contract (`contract_version: 3`).

One reviewed request per agent authorizes the whole onboarding run. It carries only
*deterministic requested* names, deterministic generation job identifiers, reviewed reuse
candidates, static sidecar policy, and explicit fail-closed bounds. It deliberately does not
carry dynamic immutable identifiers, generated sample counts, split hashes, or activation
scores: those are produced by the staged provider state machine at apply time and recorded in
the receipt and provider state as an :class:`EvaluationFinalization`, which must satisfy every
pre-approved bound before any repository mutation happens.
"""

from __future__ import annotations

import json
import math
from collections.abc import Sequence
from typing import Annotated, Literal, Self

from pydantic import Field, StrictBool, StrictInt, StringConstraints, ValidationInfo, field_validator, model_validator

from foundry_opt.bootstrap.canonical import canonical_sha256, safe_persisted_document
from foundry_opt.bootstrap.contracts import (
    ActivationBinding,
    AgentId,
    BindingClassification,
    BootstrapAction,
    BootstrapDocument,
    DatasetUri,
    DecisionPolicy,
    DeploymentSettings,
    EvaluationDefinitionId,
    EvaluatorIdentifier,
    EvaluatorNormalization,
    EvaluatorProvenance,
    FoundryProjectSettings,
    HardGuardrail,
    RuntimeProtocolSettings,
    Sha256,
    VersionedEvaluatorUri,
)
from foundry_opt.bootstrap.errors import BootstrapConfigError
from foundry_opt.bootstrap.evaluation.core import (
    KNOWN_SAFETY_EVALUATORS,
    LEGACY_AGGREGATE_SAFETY_NAME,
    MIN_DEVELOPMENT_CASES,
    MIN_VALIDATING_CASES,
    REQUIRED_SAFETY_EVALUATORS,
    TARGET_SAMPLE_COUNT,
    TRACE_MIN_GENERATED_SAMPLES,
    assert_required_safety_coverage,
    canonical_safety_name,
    deterministic_split_targets,
    validate_activation,
)
from foundry_opt.optimize_job.safety import UnsafeCheckpointContentError

EXECUTION_CONTRACT_VERSION = 3

Identifier = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")]
ResourceName = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]
ResourceVersion = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]
OperationId = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")]
RepoRelativePath = Annotated[str, StringConstraints(min_length=1, max_length=240)]
StopReason = Annotated[str, StringConstraints(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._:/-]*$")]
StorageUri = Annotated[str, StringConstraints(min_length=1, max_length=400, pattern=r"^https://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+$")]

DatasetRole = Literal["development", "validating"]
DatasetStrategy = Literal["trace", "synthetic_only"]
DatasetType = Literal["uri_file", "uri_folder"]
EvaluatorKind = Literal["builtin", "custom"]
EvaluatorRole = Literal["objective", "guardrail"]
SafetyName = Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]*$")]
GenerationKind = Literal["dataset_trace", "dataset_synthetic"]
NormalizationKind = Literal["scalar", "pass_fail"]
ReuseDecision = Literal["reuse_existing_assets", "generate_new_assets"]
OnboardingStage = Literal["inventory", "generation", "split", "evaluator", "definitions", "activation", "cleanup"]

ONBOARDING_STAGES: tuple[OnboardingStage, ...] = (
    "inventory",
    "generation",
    "split",
    "evaluator",
    "definitions",
    "activation",
    "cleanup",
)
ONBOARDING_ACTION_KIND = "evaluation_onboarding"

_STOPPED_BINDINGS = frozenset({"ready-unbound", "not-ready"})
_UNSEALED_HASH = "0" * 64
_MAX_ACTION_PAYLOAD_BYTES = 16384
_MAX_EVALUATORS = 8
_MAX_ACTIVATION_ENTRIES = 64


def _require_finite(value: float, *, field: str) -> float:
    numeric = float(value)
    if not math.isfinite(numeric):
        raise BootstrapConfigError(f"{field} must be finite")
    return numeric


def _jsonable(value: object) -> object:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def assert_no_persisted_content(document: object, *, field: str) -> None:
    """Fail closed when a document could carry raw customer or evaluation content."""

    try:
        safe_persisted_document(document)
    except UnsafeCheckpointContentError as exc:
        raise BootstrapConfigError(f"{field} contains prohibited raw content: {exc}") from exc


class _SealedDocument(BootstrapDocument):
    """Base for documents sealed with a canonical hash of their own body."""

    _hash_field: str = "contract_hash"

    @classmethod
    def _seal(cls, values: dict[str, object], *, hash_field: str) -> Self:
        payload = {key: _jsonable(value) for key, value in values.items() if key != hash_field}
        body = cls.model_validate({**payload, hash_field: _UNSEALED_HASH}, context={"unsealed": True}).model_dump(
            mode="json",
            exclude={hash_field},
        )
        return cls.model_validate({**body, hash_field: canonical_sha256(body)})


class OnboardingBounds(BootstrapDocument):
    """Reviewed, fail-closed bounds every dynamic onboarding output must satisfy."""

    target_sample_count: Annotated[StrictInt, Field(ge=15, le=1000)] = TARGET_SAMPLE_COUNT
    minimum_development_cases: Annotated[StrictInt, Field(ge=MIN_DEVELOPMENT_CASES, le=1000)] = MIN_DEVELOPMENT_CASES
    minimum_validating_cases: Annotated[StrictInt, Field(ge=MIN_VALIDATING_CASES, le=1000)] = MIN_VALIDATING_CASES
    telemetry_minimum_samples: Annotated[StrictInt, Field(ge=TRACE_MIN_GENERATED_SAMPLES, le=1000)] = TRACE_MIN_GENERATED_SAMPLES
    maximum_generated_sample_count: Annotated[StrictInt, Field(ge=15, le=10000)] = 200
    maximum_evaluators: Annotated[StrictInt, Field(ge=1, le=_MAX_EVALUATORS)] = _MAX_EVALUATORS
    required_safety_pass_rate: float = 1.0
    required_safety_evaluators: tuple[SafetyName, ...] = REQUIRED_SAFETY_EVALUATORS
    require_measurable_headroom: StrictBool = True
    allowed_dataset_types: tuple[DatasetType, ...] = ("uri_file",)
    allowed_provenance: tuple[EvaluatorProvenance, ...] = ("reused_existing", "auto_generated_unreviewed")

    @model_validator(mode="after")
    def _validate_bounds(self) -> Self:
        if self.required_safety_pass_rate != 1.0:
            raise BootstrapConfigError("the built-in safety bundle must be a 100% hard guardrail")
        assert_required_safety_coverage(self.required_safety_evaluators, field="bounds.required_safety_evaluators")
        if len(set(self.required_safety_evaluators)) != len(self.required_safety_evaluators):
            raise BootstrapConfigError("required_safety_evaluators must be unique")
        for name in self.required_safety_evaluators:
            if name not in KNOWN_SAFETY_EVALUATORS and name != LEGACY_AGGREGATE_SAFETY_NAME:
                raise BootstrapConfigError(f"unknown safety evaluator name: {name}")
        if self.require_measurable_headroom is not True:
            raise BootstrapConfigError("measurable headroom is a required activation gate")
        if self.target_sample_count < self.minimum_development_cases + self.minimum_validating_cases:
            raise BootstrapConfigError("target_sample_count cannot be below the 10/5 split minimums")
        if self.maximum_generated_sample_count < self.target_sample_count:
            raise BootstrapConfigError("maximum_generated_sample_count must allow the reviewed target")
        if not self.allowed_dataset_types or not self.allowed_provenance:
            raise BootstrapConfigError("allowed dataset types and provenance must not be empty")
        if "issue_supplied_existing" in self.allowed_provenance:
            raise BootstrapConfigError("issue-supplied evaluators never enter the repository default bundle")
        return self

    def split_targets(self, total_cases: int) -> tuple[int, int]:
        development, validating = deterministic_split_targets(total_cases)
        if development < self.minimum_development_cases or validating < self.minimum_validating_cases:
            raise BootstrapConfigError("split targets violate the reviewed minimum case counts")
        return development, validating


class TelemetryProbe(BootstrapDocument):
    """Reviewed trace-availability evidence: counts and a window identifier only."""

    prerequisites_available: StrictBool
    useful_sample_count: Annotated[StrictInt, Field(ge=0, le=100000)]
    telemetry_window: Annotated[str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9]+$")]
    eligible: StrictBool

    @model_validator(mode="after")
    def _validate_probe(self) -> Self:
        expected = self.prerequisites_available and self.useful_sample_count >= TRACE_MIN_GENERATED_SAMPLES
        if self.eligible != expected:
            raise BootstrapConfigError(
                "trace eligibility must be derived from prerequisites and the "
                f"{TRACE_MIN_GENERATED_SAMPLES}+ useful sample threshold"
            )
        return self


class DatasetPlan(BootstrapDocument):
    """Deterministic dataset naming, reviewed reuse candidates, and the generation job id."""

    requested_development_name: ResourceName
    requested_validating_name: ResourceName
    requested_version: ResourceVersion
    dataset_type: DatasetType = "uri_file"
    connection_name: ResourceName | None = None
    generation_kind: GenerationKind
    generation_job_id: OperationId
    source_fingerprint: Sha256
    agent_name: ResourceName
    agent_version: ResourceVersion
    generation_model_deployment: ResourceName
    reuse_development_dataset_id: DatasetUri | None = None
    reuse_validating_dataset_id: DatasetUri | None = None

    @model_validator(mode="after")
    def _validate_plan(self) -> Self:
        if self.requested_development_name == self.requested_validating_name:
            raise BootstrapConfigError("development and validating dataset names must differ")
        reuse = (self.reuse_development_dataset_id, self.reuse_validating_dataset_id)
        if any(reuse) and not all(reuse):
            raise BootstrapConfigError("dataset reuse requires both development and validating candidates")
        if all(reuse) and self.reuse_development_dataset_id == self.reuse_validating_dataset_id:
            raise BootstrapConfigError("development and validating reuse candidates must be distinct")
        return self

    @property
    def reuse_candidates(self) -> tuple[str, str] | None:
        if self.reuse_development_dataset_id and self.reuse_validating_dataset_id:
            return (self.reuse_development_dataset_id, self.reuse_validating_dataset_id)
        return None


class EvaluatorPlan(BootstrapDocument):
    """Deterministic evaluator naming plus the required built-in safety bundle."""

    requested_name: ResourceName
    requested_version: ResourceVersion
    generation_job_id: OperationId | None = None
    reuse_evaluator_id: VersionedEvaluatorUri | None = None
    # Safety evaluators are requested by canonical built-in name; their immutable registry ids
    # are discovered at apply time and recorded in the receipt finalization.
    required_safety_evaluators: tuple[SafetyName, ...] = REQUIRED_SAFETY_EVALUATORS
    objective_normalization: EvaluatorNormalization
    objective_weight: float = 1.0

    @field_validator("objective_weight")
    @classmethod
    def _validate_weight(cls, value: float) -> float:
        numeric = _require_finite(value, field="objective_weight")
        if numeric <= 0:
            raise BootstrapConfigError("objective_weight must be positive")
        return numeric

    @model_validator(mode="after")
    def _validate_plan(self) -> Self:
        assert_required_safety_coverage(self.required_safety_evaluators, field="evaluator_plan.required_safety_evaluators")
        if len(set(self.required_safety_evaluators)) != len(self.required_safety_evaluators):
            raise BootstrapConfigError("required_safety_evaluators must be unique")
        if (self.reuse_evaluator_id is None) == (self.generation_job_id is None):
            raise BootstrapConfigError(
                "the default evaluator must either reuse one reviewed immutable evaluator or "
                "declare exactly one deterministic rubric generation job"
            )
        return self


class DefinitionPlan(BootstrapDocument):
    requested_development_name: ResourceName
    requested_validating_name: ResourceName
    model_deployment: ResourceName

    @model_validator(mode="after")
    def _validate_plan(self) -> Self:
        if self.requested_development_name == self.requested_validating_name:
            raise BootstrapConfigError("development and validating definition names must differ")
        return self


class ActivationPlan(BootstrapDocument):
    draft_agent_name: ResourceName
    draft_agent_version: ResourceVersion
    model_deployment: ResourceName


class SidecarPolicy(BootstrapDocument):
    """Static, reviewed sidecar content; immutable ids and lineage are filled from the receipt."""

    path: RepoRelativePath
    source_root: Annotated[str, StringConstraints(min_length=1, max_length=240)]
    package_root: Annotated[str, StringConstraints(min_length=1, max_length=240)]
    editable_paths: tuple[RepoRelativePath, ...]
    runtime: RuntimeProtocolSettings
    foundry_project: FoundryProjectSettings
    baseline_model: ResourceName
    allowed_models: tuple[ResourceName, ...]
    min_candidates: Annotated[StrictInt, Field(ge=1, le=64)]
    max_candidates: Annotated[StrictInt, Field(ge=1, le=64)]
    primary_metric: Identifier
    decision_policy: DecisionPolicy
    hard_guardrails: tuple[HardGuardrail, ...]
    deployment: DeploymentSettings
    max_issue_evaluators: Annotated[StrictInt, Field(ge=1, le=_MAX_EVALUATORS)] = _MAX_EVALUATORS

    @model_validator(mode="after")
    def _validate_policy(self) -> Self:
        if self.min_candidates > self.max_candidates:
            raise BootstrapConfigError("candidate bounds must be ordered")
        if not self.editable_paths:
            raise BootstrapConfigError("editable_paths must not be empty")
        if not self.hard_guardrails:
            raise BootstrapConfigError("hard_guardrails must not be empty")
        if self.deployment.require_aligned_binding is not True:
            raise BootstrapConfigError("sidecar deployment must require aligned binding")
        return self


class ReplacementLineage(BootstrapDocument):
    """Previous immutable contract retained until an explicit replacement activates."""

    previous_bundle_objective_hash: Sha256
    previous_sidecar_sha256: Sha256
    previous_development_definition_id: EvaluationDefinitionId
    previous_validating_definition_id: EvaluationDefinitionId


class EvaluationOnboardingRequest(_SealedDocument):
    """One approval-bound composite onboarding request for one selected agent."""

    contract_version: Literal[3] = EXECUTION_CONTRACT_VERSION
    repo_agent_id: AgentId
    binding_classification: BindingClassification
    stop_reason: StopReason | None = None
    bounds: OnboardingBounds = OnboardingBounds()
    telemetry_probe: TelemetryProbe | None = None
    dataset_plan: DatasetPlan | None = None
    evaluator_plan: EvaluatorPlan | None = None
    definition_plan: DefinitionPlan | None = None
    activation_plan: ActivationPlan | None = None
    sidecar_policy: SidecarPolicy | None = None
    replacement: ReplacementLineage | None = None
    contract_hash: Sha256

    @property
    def stopped(self) -> bool:
        return self.binding_classification in _STOPPED_BINDINGS

    @classmethod
    def create(cls, **values: object) -> "EvaluationOnboardingRequest":
        return cls._seal(dict(values), hash_field="contract_hash")

    @model_validator(mode="after")
    def _validate_contract(self, info: ValidationInfo) -> Self:
        body = self.model_dump(mode="json", exclude={"contract_hash"})
        assert_no_persisted_content(body, field="evaluation onboarding contract")
        unsealed = bool((info.context or {}).get("unsealed")) and self.contract_hash == _UNSEALED_HASH
        if not unsealed and self.contract_hash != canonical_sha256(body):
            raise BootstrapConfigError("contract_hash does not match the reviewed onboarding contract")
        if self.stopped:
            if self.stop_reason is None:
                raise BootstrapConfigError("stopped agents must record an explicit stop reason")
            if any(
                item is not None
                for item in (
                    self.telemetry_probe,
                    self.dataset_plan,
                    self.evaluator_plan,
                    self.definition_plan,
                    self.activation_plan,
                    self.sidecar_policy,
                    self.replacement,
                )
            ):
                raise BootstrapConfigError(
                    "ready-unbound/not-ready agents must stop before evaluation generation and activation"
                )
            return self
        if self.stop_reason is not None:
            raise BootstrapConfigError("only ready-unbound/not-ready agents may declare a stop reason")
        missing = [
            name
            for name, value in (
                ("telemetry_probe", self.telemetry_probe),
                ("dataset_plan", self.dataset_plan),
                ("evaluator_plan", self.evaluator_plan),
                ("definition_plan", self.definition_plan),
                ("activation_plan", self.activation_plan),
                ("sidecar_policy", self.sidecar_policy),
            )
            if value is None
        ]
        if missing:
            raise BootstrapConfigError(f"executable onboarding contracts require: {', '.join(missing)}")
        assert self.dataset_plan is not None and self.evaluator_plan is not None
        assert self.definition_plan is not None and self.activation_plan is not None
        assert self.sidecar_policy is not None and self.telemetry_probe is not None
        if self.dataset_plan.dataset_type not in self.bounds.allowed_dataset_types:
            raise BootstrapConfigError("requested dataset type is outside the reviewed bounds")
        if self.dataset_plan.generation_kind == "dataset_trace" and not self.telemetry_probe.eligible:
            raise BootstrapConfigError(
                f"trace generation requires {TRACE_MIN_GENERATED_SAMPLES}+ useful samples and available prerequisites"
            )
        if self.evaluator_plan.reuse_evaluator_id is not None and "reused_existing" not in self.bounds.allowed_provenance:
            raise BootstrapConfigError("evaluator reuse is outside the reviewed provenance bounds")
        if self.evaluator_plan.generation_job_id is not None and "auto_generated_unreviewed" not in self.bounds.allowed_provenance:
            raise BootstrapConfigError("rubric generation is outside the reviewed provenance bounds")
        if self.definition_plan.model_deployment != self.activation_plan.model_deployment:
            raise BootstrapConfigError("definitions and activation must use one model deployment")
        guardrails = {item.evaluator_name.strip().casefold(): item for item in self.sidecar_policy.hard_guardrails if item.required}
        for name in self.evaluator_plan.required_safety_evaluators:
            guardrail = guardrails.get(name) or guardrails.get(f"builtin.{name}")
            if guardrail is None or guardrail.required_pass_rate != self.bounds.required_safety_pass_rate:
                raise BootstrapConfigError(
                    f"sidecar hard guardrails must require the built-in safety evaluator {name!r} at 100%"
                )
        if self.sidecar_policy.deployment.enabled and self.binding_classification != "bound-aligned":
            raise BootstrapConfigError("deployment may only be enabled for bound-aligned agents")
        if self.sidecar_policy.max_issue_evaluators > self.bounds.maximum_evaluators:
            raise BootstrapConfigError("max_issue_evaluators exceeds the reviewed evaluator bound")
        return self

    def action_payload_json(self) -> str:
        payload = self.model_dump(mode="json")
        assert_no_persisted_content(payload, field="onboarding action payload")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > _MAX_ACTION_PAYLOAD_BYTES:
            raise BootstrapConfigError("onboarding action payload exceeds the safe persisted bound")
        return encoded

    def composite_action(self) -> tuple[BootstrapAction, ...]:
        """Return the single approval-bound composite action for this agent (none when stopped)."""

        if self.stopped:
            return ()
        return (
            BootstrapAction(
                action_id=f"evaluations:{self.repo_agent_id}:onboarding",
                phase="evaluations",
                stage="planned",
                kind=ONBOARDING_ACTION_KIND,
                target_agent_id=self.repo_agent_id,
                diagnostics=(self.repo_agent_id, self.contract_hash, self.action_payload_json()),
            ),
        )


class DatasetFinalization(BootstrapDocument):
    """A dynamic immutable dataset version discovered or created during apply."""

    role: DatasetRole
    dataset_name: ResourceName
    dataset_version: ResourceVersion
    dataset_id: DatasetUri
    dataset_type: DatasetType
    case_count: Annotated[StrictInt, Field(ge=1, le=100000)]
    disposition: Literal["created", "adopted"]


class SplitFinalization(BootstrapDocument):
    algorithm_version: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    split_hash: Sha256
    split_lineage_hash: Sha256
    development_case_count: Annotated[StrictInt, Field(ge=MIN_DEVELOPMENT_CASES, le=100000)]
    validating_case_count: Annotated[StrictInt, Field(ge=MIN_VALIDATING_CASES, le=100000)]
    overlap: Literal["none"] = "none"


class EvaluatorFinalization(BootstrapDocument):
    role: EvaluatorRole
    evaluator_name: Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")]
    evaluator_version: ResourceVersion
    evaluator_id: EvaluatorIdentifier
    evaluator_kind: EvaluatorKind
    provenance: EvaluatorProvenance
    generation_operation_id: OperationId | None = None
    normalization: EvaluatorNormalization
    weight: float = 1.0
    disposition: Literal["created", "adopted"]
    safety_name: SafetyName | None = None

    @model_validator(mode="after")
    def _validate_evaluator(self) -> Self:
        if self.provenance == "issue_supplied_existing":
            raise BootstrapConfigError("issue-supplied evaluators never enter the repository default bundle")
        if (self.provenance == "auto_generated_unreviewed") != (self.generation_operation_id is not None):
            raise BootstrapConfigError("generated evaluators require exactly one generation operation id")
        if self.role == "guardrail":
            resolved = self.safety_name or canonical_safety_name(self.evaluator_id, self.evaluator_name)
            if resolved is None:
                raise BootstrapConfigError(
                    "guardrail evaluators must resolve to a canonical built-in safety evaluator"
                )
            if self.safety_name is not None and self.safety_name != canonical_safety_name(self.evaluator_id, self.evaluator_name):
                raise BootstrapConfigError("guardrail safety_name must match the resolved built-in safety evaluator")
            if self.evaluator_kind != "builtin" or self.provenance != "reused_existing":
                raise BootstrapConfigError("safety guardrails are always reused built-in evaluators")
        elif self.safety_name is not None:
            raise BootstrapConfigError("only guardrail evaluators carry a safety_name")
        return self


class DefinitionFinalization(BootstrapDocument):
    role: DatasetRole
    definition_name: ResourceName
    definition_id: EvaluationDefinitionId
    disposition: Literal["created", "adopted"]


class ActivationCaseFinalization(BootstrapDocument):
    """One structural, already-scored activation measurement. Never raw model content."""

    phase: DatasetRole
    evaluator_id: EvaluatorIdentifier
    executable: StrictBool
    normalization_kind: NormalizationKind
    score: float
    pass_rate: float
    source_min: float | None = None
    source_max: float | None = None

    @model_validator(mode="after")
    def _validate_case(self) -> Self:
        _require_finite(self.score, field="activation score")
        if not 0.0 <= _require_finite(self.pass_rate, field="activation pass_rate") <= 1.0:
            raise BootstrapConfigError("activation pass_rate must be between 0 and 1")
        if self.normalization_kind == "scalar":
            if self.source_min is None or self.source_max is None:
                raise BootstrapConfigError("scalar activation measurements require numeric bounds")
            if _require_finite(self.source_max, field="source_max") <= _require_finite(self.source_min, field="source_min"):
                raise BootstrapConfigError("scalar activation bounds must increase")
        elif self.source_min is not None or self.source_max is not None:
            raise BootstrapConfigError("pass_fail activation measurements cannot carry scalar bounds")
        return self


class ActivationFinalization(BootstrapDocument):
    status: Literal["succeeded"]
    development_run_id: Identifier
    validating_run_id: Identifier
    draft_agent_name: ResourceName
    draft_agent_version: ResourceVersion
    cases: tuple[ActivationCaseFinalization, ...]
    cleanup_completed: StrictBool
    draft_disposition: Literal["created"] = "created"
    package_tree_sha256: Sha256 | None = None
    package_zip_sha256: Sha256 | None = None
    draft_code_digest: Sha256 | None = None

    @model_validator(mode="after")
    def _validate_activation(self) -> Self:
        if not self.cases or len(self.cases) > _MAX_ACTIVATION_ENTRIES:
            raise BootstrapConfigError("activation measurements are empty or exceed the safe bound")
        if self.development_run_id == self.validating_run_id:
            raise BootstrapConfigError("development and validating activation runs must be distinct")
        if {case.phase for case in self.cases} != {"development", "validating"}:
            raise BootstrapConfigError("activation must cover development and validating phases")
        seen: set[tuple[str, str]] = set()
        for case in self.cases:
            key = (case.phase, case.evaluator_id)
            if key in seen:
                raise BootstrapConfigError("activation measurements must not repeat phase/evaluator combinations")
            seen.add(key)
        if self.cleanup_completed is not True:
            raise BootstrapConfigError("activation requires the owned draft to be cleaned up")
        return self

    def safety_pass_rates(self, safety_evaluator_ids: Sequence[str]) -> tuple[float, ...]:
        wanted = set(safety_evaluator_ids)
        return tuple(case.pass_rate for case in self.cases if case.evaluator_id in wanted)


class EvaluationFinalization(_SealedDocument):
    """Receipt-derived onboarding outcome carrying every dynamic immutable identifier."""

    contract_version: Literal[3] = EXECUTION_CONTRACT_VERSION
    repo_agent_id: AgentId
    contract_hash: Sha256
    reuse_decision: ReuseDecision
    dataset_strategy: DatasetStrategy
    generated_sample_count: Annotated[StrictInt, Field(ge=0, le=100000)] = 0
    generation_context_fingerprint: Sha256
    datasets: tuple[DatasetFinalization, ...]
    split: SplitFinalization
    evaluators: tuple[EvaluatorFinalization, ...]
    definitions: tuple[DefinitionFinalization, ...]
    activation: ActivationFinalization
    bundle_objective_hash: Sha256
    finalization_hash: Sha256

    @classmethod
    def create(cls, **values: object) -> "EvaluationFinalization":
        return cls._seal(dict(values), hash_field="finalization_hash")

    @property
    def objective_evaluators(self) -> tuple[EvaluatorFinalization, ...]:
        return tuple(item for item in self.evaluators if item.role == "objective")

    @property
    def guardrail_evaluators(self) -> tuple[EvaluatorFinalization, ...]:
        guardrails = tuple(item for item in self.evaluators if item.role == "guardrail")
        if not guardrails:
            raise BootstrapConfigError("the required built-in safety bundle is missing from the finalization")
        assert_required_safety_coverage(
            [item.safety_name or canonical_safety_name(item.evaluator_id, item.evaluator_name) or "" for item in guardrails],
            field="finalization safety bundle",
        )
        return guardrails

    @property
    def safety_evaluator_ids(self) -> tuple[str, ...]:
        return tuple(item.evaluator_id for item in self.guardrail_evaluators)

    def dataset_for(self, role: DatasetRole) -> DatasetFinalization:
        for item in self.datasets:
            if item.role == role:
                return item
        raise BootstrapConfigError(f"finalization has no {role} dataset")

    def definition_for(self, role: DatasetRole) -> DefinitionFinalization:
        for item in self.definitions:
            if item.role == role:
                return item
        raise BootstrapConfigError(f"finalization has no {role} definition")

    @model_validator(mode="after")
    def _validate_finalization(self, info: ValidationInfo) -> Self:
        body = self.model_dump(mode="json", exclude={"finalization_hash"})
        assert_no_persisted_content(body, field="evaluation finalization")
        unsealed = bool((info.context or {}).get("unsealed")) and self.finalization_hash == _UNSEALED_HASH
        if not unsealed and self.finalization_hash != canonical_sha256(body):
            raise BootstrapConfigError("finalization_hash does not match the recorded onboarding finalization")
        if sorted(item.role for item in self.datasets) != ["development", "validating"]:
            raise BootstrapConfigError("finalization requires exactly one development and one validating dataset")
        if self.dataset_for("development").dataset_id == self.dataset_for("validating").dataset_id:
            raise BootstrapConfigError("development and validating datasets must be distinct immutable references")
        if sorted(item.role for item in self.definitions) != ["development", "validating"]:
            raise BootstrapConfigError("finalization requires exactly one development and one validating definition")
        if self.definition_for("development").definition_id == self.definition_for("validating").definition_id:
            raise BootstrapConfigError("development and validating definitions must be distinct")
        if not self.objective_evaluators:
            raise BootstrapConfigError("finalization requires at least one objective evaluator")
        self.guardrail_evaluators  # noqa: B018 - fail closed when the safety bundle is missing or incomplete
        if len({item.evaluator_id for item in self.evaluators}) != len(self.evaluators):
            raise BootstrapConfigError("finalization evaluators must be unique")
        if self.dataset_for("development").case_count != self.split.development_case_count:
            raise BootstrapConfigError("development dataset case count must match the split lineage")
        if self.dataset_for("validating").case_count != self.split.validating_case_count:
            raise BootstrapConfigError("validating dataset case count must match the split lineage")
        evaluator_ids = {item.evaluator_id for item in self.evaluators}
        for case in self.activation.cases:
            if case.evaluator_id not in evaluator_ids:
                raise BootstrapConfigError("activation measurements must reference finalized evaluators")
        return self

    def verify_against_contract(self, contract: EvaluationOnboardingRequest) -> None:
        """Fail closed unless every dynamic output satisfies the pre-approved contract."""

        if contract.stopped:
            raise BootstrapConfigError("stopped agents must not produce an onboarding finalization")
        assert contract.dataset_plan is not None and contract.evaluator_plan is not None
        assert contract.definition_plan is not None and contract.activation_plan is not None
        if self.contract_hash != contract.contract_hash:
            raise BootstrapConfigError("finalization does not belong to the approved onboarding contract")
        if self.repo_agent_id.casefold() != contract.repo_agent_id.casefold():
            raise BootstrapConfigError("finalization repo_agent_id does not match the approved contract")
        bounds = contract.bounds
        if self.generation_context_fingerprint != contract.dataset_plan.source_fingerprint:
            raise BootstrapConfigError("finalization generation-context fingerprint must match the reviewed sources")
        if self.generated_sample_count > bounds.maximum_generated_sample_count:
            raise BootstrapConfigError("generated sample count exceeds the reviewed bound")
        if self.dataset_strategy == "trace":
            if contract.dataset_plan.generation_kind != "dataset_trace":
                raise BootstrapConfigError("trace datasets require an approved trace generation plan")
            if self.generated_sample_count < bounds.telemetry_minimum_samples:
                raise BootstrapConfigError(
                    f"trace datasets require {bounds.telemetry_minimum_samples}+ useful samples"
                )
        elif self.reuse_decision == "generate_new_assets" and self.generated_sample_count < bounds.minimum_development_cases + bounds.minimum_validating_cases:
            raise BootstrapConfigError("synthetic generation produced fewer cases than the reviewed minimum")
        for dataset in self.datasets:
            if dataset.dataset_type not in bounds.allowed_dataset_types:
                raise BootstrapConfigError("finalized dataset type is outside the reviewed bounds")
        expected_names = {
            "development": contract.dataset_plan.requested_development_name,
            "validating": contract.dataset_plan.requested_validating_name,
        }
        reuse_candidates = contract.dataset_plan.reuse_candidates
        for role, expected in expected_names.items():
            dataset = self.dataset_for(role)
            if self.reuse_decision == "generate_new_assets" and dataset.dataset_name != expected:
                raise BootstrapConfigError("finalized dataset name does not match the reviewed requested name")
            if self.reuse_decision == "reuse_existing_assets":
                if reuse_candidates is None:
                    raise BootstrapConfigError("dataset reuse requires reviewed reuse candidates")
                if dataset.dataset_id not in reuse_candidates:
                    raise BootstrapConfigError("reused dataset is outside the reviewed reuse candidates")
        total_cases = self.split.development_case_count + self.split.validating_case_count
        development_target, validating_target = bounds.split_targets(total_cases)
        if (self.split.development_case_count, self.split.validating_case_count) != (development_target, validating_target):
            raise BootstrapConfigError("split counts must match the deterministic about two-thirds/one-third target")
        if self.split.development_case_count < bounds.minimum_development_cases:
            raise BootstrapConfigError("development split violates the reviewed minimum")
        if self.split.validating_case_count < bounds.minimum_validating_cases:
            raise BootstrapConfigError("validating split violates the reviewed minimum")
        if self.split.overlap != "none":
            raise BootstrapConfigError("development and validating splits must not overlap")
        objective = self.objective_evaluators
        if len(objective) + 1 > bounds.maximum_evaluators:
            raise BootstrapConfigError("finalized evaluator count exceeds the reviewed bound")
        for evaluator in self.evaluators:
            if evaluator.provenance not in bounds.allowed_provenance:
                raise BootstrapConfigError("finalized evaluator provenance is outside the reviewed bounds")
        primary = objective[0]
        if contract.evaluator_plan.reuse_evaluator_id is not None:
            if primary.evaluator_id != contract.evaluator_plan.reuse_evaluator_id:
                raise BootstrapConfigError("reused evaluator is outside the reviewed reuse candidate")
            if primary.provenance != "reused_existing":
                raise BootstrapConfigError("reused evaluators must record reused_existing provenance")
        else:
            if primary.evaluator_name != contract.evaluator_plan.requested_name:
                raise BootstrapConfigError("generated evaluator name does not match the reviewed requested name")
            if primary.provenance != "auto_generated_unreviewed":
                raise BootstrapConfigError("generated evaluators must persist auto_generated_unreviewed provenance")
            if primary.generation_operation_id != contract.evaluator_plan.generation_job_id:
                raise BootstrapConfigError("generated evaluator lineage must match the approved generation job")
        if primary.normalization != contract.evaluator_plan.objective_normalization:
            raise BootstrapConfigError("finalized evaluator normalization must match the reviewed contract")
        resolved_safety = {
            item.safety_name or canonical_safety_name(item.evaluator_id, item.evaluator_name)
            for item in self.guardrail_evaluators
        }
        assert_required_safety_coverage(
            [name for name in resolved_safety if name],
            required=contract.evaluator_plan.required_safety_evaluators,
            field="finalized safety bundle",
        )
        assert_required_safety_coverage(
            [name for name in resolved_safety if name],
            required=bounds.required_safety_evaluators,
            field="finalized safety bundle",
        )
        expected_definitions = {
            "development": contract.definition_plan.requested_development_name,
            "validating": contract.definition_plan.requested_validating_name,
        }
        for role, expected in expected_definitions.items():
            if self.definition_for(role).definition_name != expected:
                raise BootstrapConfigError("finalized definition name does not match the reviewed requested name")
        if self.activation.draft_agent_name != contract.activation_plan.draft_agent_name:
            raise BootstrapConfigError("activation draft must match the reviewed draft agent")
        if self.activation.draft_agent_version != contract.activation_plan.draft_agent_version:
            raise BootstrapConfigError("activation draft version must match the reviewed draft agent")
        self.assert_activation_gates(bounds)

    def assert_activation_gates(self, bounds: OnboardingBounds) -> None:
        """Re-assert structural, execution, headroom, and full safety-bundle gates."""

        cases = [
            {
                "executable": case.executable,
                "normalization": {
                    "kind": case.normalization_kind,
                    "source_min": case.source_min,
                    "source_max": case.source_max,
                },
                "score": case.score,
            }
            for case in self.activation.cases
        ]
        safety_by_id = {item.evaluator_id: item for item in self.guardrail_evaluators}
        guardrails = [
            {
                "evaluator_id": case.evaluator_id,
                "safety_name": safety_by_id[case.evaluator_id].safety_name
                or canonical_safety_name(case.evaluator_id, safety_by_id[case.evaluator_id].evaluator_name),
                "pass_rate": case.pass_rate,
            }
            for case in self.activation.cases
            if case.evaluator_id in safety_by_id
        ]
        validate_activation(cases=cases, guardrails=guardrails)
        for evaluator in self.guardrail_evaluators:
            phases = {case.phase for case in self.activation.cases if case.evaluator_id == evaluator.evaluator_id}
            if phases != {"development", "validating"}:
                raise BootstrapConfigError(
                    f"safety evaluator {evaluator.safety_name or evaluator.evaluator_name} must be measured in both phases"
                )
        rates = self.activation.safety_pass_rates(self.safety_evaluator_ids)
        if not rates or any(rate != bounds.required_safety_pass_rate for rate in rates):
            raise BootstrapConfigError("every configured safety evaluator must pass at 100% in both activation phases")


def finalization_binding_hash(
    *,
    binding: ActivationBinding,
    finalization: EvaluationFinalization,
) -> str:
    """Bind the finalization payload to the parent plan, approval, receipt, and runtime SHA."""

    return canonical_sha256(
        {
            "operation_id": binding.operation_id,
            "plan_hash": binding.plan_hash,
            "approval_hash": binding.approval_hash,
            "receipt_hash": binding.receipt_hash,
            "runtime_commit": binding.runtime_commit,
            "contract_hash": finalization.contract_hash,
            "finalization_hash": finalization.finalization_hash,
        }
    )


__all__ = [
    "ActivationCaseFinalization",
    "ActivationFinalization",
    "ActivationPlan",
    "DatasetFinalization",
    "DatasetPlan",
    "DefinitionFinalization",
    "DefinitionPlan",
    "EXECUTION_CONTRACT_VERSION",
    "EvaluationFinalization",
    "EvaluationOnboardingRequest",
    "EvaluatorFinalization",
    "EvaluatorPlan",
    "ONBOARDING_ACTION_KIND",
    "ONBOARDING_STAGES",
    "OnboardingBounds",
    "ReplacementLineage",
    "SidecarPolicy",
    "SplitFinalization",
    "TelemetryProbe",
    "TraceProbe",
    "assert_no_persisted_content",
    "finalization_binding_hash",
]

# Backwards-compatible alias: the reviewed probe document is about trace availability.
TraceProbe = TelemetryProbe
