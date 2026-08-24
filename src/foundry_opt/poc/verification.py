from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, model_validator

from foundry_opt.repository_contracts import (
    AgentProfile,
    EvaluationGatePolicy,
    HardGuardrail,
    VerificationBundle,
)
from foundry_opt.contract_errors import BootstrapConfigError
from foundry_opt.models import FrozenModel
from foundry_opt.poc.config import IssueEvaluatorEntry, OptimizeIssueRequest
from foundry_opt.poc.issue import ISSUE_NAMED_CHECK_GUIDANCE
from foundry_opt.verification import VerificationCheckSpec, VerificationDatasetInput


VerificationResolutionMode = Literal["foundry_evaluation", "repository_checks", "none"]
DeploymentVerificationMode = VerificationResolutionMode
DeploymentVerificationStatus = Literal[
    "planned",
    "passed",
    "failed",
    "skipped",
    "unverified",
]
DeploymentWarningCode = Literal["deployment-unverified"]
VerificationProvenance = Literal[
    "issue_dataset",
    "issue_evaluators",
    "repository_default_bundle",
    "runtime_metadata_defaults",
    "issue_repository_checks",
    "repository_default_checks",
    "explicit_no_evidence",
    "no_verification_inputs",
]


class FoundryEvaluationPlan(FrozenModel):
    development_definition_id: str = Field(min_length=1, max_length=512)
    development_dataset_id: str = Field(min_length=1, max_length=2048)
    development_evaluator_ids: tuple[str, ...]
    validating_definition_id: str = Field(min_length=1, max_length=512)
    validating_dataset_id: str = Field(min_length=1, max_length=2048)
    validating_evaluator_ids: tuple[str, ...]

    @model_validator(mode="after")
    def validate_lists(self) -> "FoundryEvaluationPlan":
        if not self.development_evaluator_ids:
            raise ValueError("development_evaluator_ids must not be empty")
        if not self.validating_evaluator_ids:
            raise ValueError("validating_evaluator_ids must not be empty")
        return self


class DeploymentGuardrail(FrozenModel):
    name: str = Field(min_length=1, max_length=256)
    score: float | None = Field(default=None, ge=0)
    required_pass_rate: float = Field(ge=0, le=1)
    passed: bool


class DeploymentVerificationWarning(FrozenModel):
    code: DeploymentWarningCode
    message: str = Field(min_length=1, max_length=1024)


class DeploymentVerificationCheckResult(FrozenModel):
    kind: Literal["command", "check"]
    value: str
    status: DeploymentVerificationStatus
    detail: str | None = Field(default=None, max_length=1024)
    url: str | None = Field(default=None, max_length=2048)


class DeploymentVerification(FrozenModel):
    mode: DeploymentVerificationMode
    status: DeploymentVerificationStatus
    evaluation_gate_policy: EvaluationGatePolicy | None = None
    objective_hash: str | None = None
    evaluation_id: str | None = None
    dataset_id: str | None = None
    evaluator_ids: tuple[str, ...] = ()
    check_results: tuple[DeploymentVerificationCheckResult, ...] = ()
    evaluation_link: str | None = None
    guardrails: tuple[DeploymentGuardrail, ...] = ()
    unverified_deployment: bool
    warning: DeploymentVerificationWarning | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "DeploymentVerification":
        if self.mode == "foundry_evaluation":
            if self.check_results:
                raise ValueError(
                    "foundry_evaluation deployment verification cannot carry repository check results"
                )
            if self.evaluation_id is None or self.dataset_id is None:
                raise ValueError(
                    "foundry_evaluation deployment verification requires evaluation identifiers"
                )
            if not self.evaluator_ids:
                raise ValueError(
                    "foundry_evaluation deployment verification requires evaluator_ids"
                )
            if self.unverified_deployment:
                raise ValueError(
                    "foundry_evaluation deployment verification cannot be unverified"
                )
            if self.warning is not None:
                raise ValueError(
                    "foundry_evaluation deployment verification cannot carry warnings"
                )
        elif self.mode == "repository_checks":
            if (
                self.objective_hash is not None
                or self.evaluation_id is not None
                or self.dataset_id is not None
                or self.evaluator_ids
                or self.evaluation_link is not None
                or self.guardrails
            ):
                raise ValueError(
                    "repository_checks deployment verification cannot carry Foundry evaluation identifiers"
                )
            if not self.check_results:
                raise ValueError(
                    "repository_checks deployment verification requires check results"
                )
            if self.unverified_deployment:
                raise ValueError(
                    "repository_checks deployment verification cannot be marked unverified"
                )
            if self.warning is not None:
                raise ValueError(
                    "repository_checks deployment verification cannot carry warnings"
                )
        else:
            if (
                self.objective_hash is not None
                or self.evaluation_id is not None
                or self.dataset_id is not None
                or self.evaluator_ids
                or self.check_results
                or self.evaluation_link is not None
                or self.guardrails
            ):
                raise ValueError(
                    "none deployment verification cannot carry evaluation or repository-check evidence"
                )
            if not self.unverified_deployment:
                raise ValueError(
                    "none deployment verification must be marked unverified"
                )
            if self.warning is None:
                raise ValueError(
                    "none deployment verification requires an explicit warning"
                )
        return self


class FoundryEvaluationSelection(FrozenModel):
    source: Literal["issue", "repository_default_bundle", "runtime_metadata"]
    defaults: FoundryEvaluationPlan
    issue_dataset: VerificationDatasetInput | None = None
    issue_evaluators: tuple[IssueEvaluatorEntry, ...] = ()

    @model_validator(mode="after")
    def validate_shape(self) -> "FoundryEvaluationSelection":
        if self.source == "issue":
            if not self.issue_evaluators:
                raise ValueError("issue foundry selections require evaluators")
        else:
            if self.issue_dataset is not None or self.issue_evaluators:
                raise ValueError(
                    "repository foundry selections cannot carry issue inputs"
                )
        return self

    @property
    def override_evaluator_ids(self) -> tuple[str, ...]:
        if not self.issue_evaluators:
            return ()
        return tuple(entry.evaluator_id for entry in self.issue_evaluators)

    @property
    def development_dataset_id(self) -> str:
        if self.issue_dataset is not None:
            return self.issue_dataset.dataset_id_or_uri
        return self.defaults.development_dataset_id

    @property
    def development_evaluator_ids(self) -> tuple[str, ...]:
        return _merge_evaluator_ids(
            self.defaults.development_evaluator_ids,
            self.override_evaluator_ids,
        )

    @property
    def validating_dataset_id(self) -> str:
        return self.defaults.validating_dataset_id

    @property
    def validating_evaluator_ids(self) -> tuple[str, ...]:
        return _merge_evaluator_ids(
            self.defaults.validating_evaluator_ids,
            self.override_evaluator_ids,
        )


class RepositoryChecksSelection(FrozenModel):
    source: Literal["issue", "repository"]
    checks: tuple[VerificationCheckSpec, ...]

    @model_validator(mode="after")
    def validate_checks(self) -> "RepositoryChecksSelection":
        if not self.checks:
            raise ValueError("repository checks selections require at least one check")
        return self


class VerificationResolution(FrozenModel):
    mode: VerificationResolutionMode
    evaluation_gate_policy: EvaluationGatePolicy
    foundry_evaluation: FoundryEvaluationSelection | None = None
    repository_checks: RepositoryChecksSelection | None = None
    provenance: tuple[VerificationProvenance, ...]
    warnings: tuple[str, ...] = Field(default_factory=tuple)
    quantitative_decision_allowed: bool

    @model_validator(mode="after")
    def validate_shape(self) -> "VerificationResolution":
        if self.mode == "foundry_evaluation":
            if self.foundry_evaluation is None or self.repository_checks is not None:
                raise ValueError("foundry_evaluation mode requires only foundry inputs")
            if not self.quantitative_decision_allowed:
                raise ValueError(
                    "foundry_evaluation mode must allow quantitative decisions"
                )
        elif self.mode == "repository_checks":
            if self.repository_checks is None or self.foundry_evaluation is not None:
                raise ValueError(
                    "repository_checks mode requires only repository checks"
                )
            if self.quantitative_decision_allowed:
                raise ValueError(
                    "repository_checks mode cannot allow quantitative decisions"
                )
        else:
            if self.foundry_evaluation is not None or self.repository_checks is not None:
                raise ValueError("none mode cannot carry verification inputs")
            if self.quantitative_decision_allowed:
                raise ValueError("none mode cannot allow quantitative decisions")
        return self


@runtime_checkable
class VerificationResolver(Protocol):
    def resolve(
        self,
        *,
        profile: object,
        issue: OptimizeIssueRequest | None = None,
    ) -> VerificationResolution: ...


class DefaultVerificationResolver:
    def resolve(
        self,
        *,
        profile: object,
        issue: OptimizeIssueRequest | None = None,
    ) -> VerificationResolution:
        warnings: list[str] = []
        issue_dataset = None if issue is None else issue.verification_dataset
        issue_evaluators = None if issue is None else issue.issue_evaluators
        issue_checks = None if issue is None else issue.verification_checks
        _reject_issue_named_checks(issue_checks)
        acknowledge_no_evidence = bool(
            issue is not None and issue.acknowledge_no_evidence
        )
        defaults = _foundry_defaults(profile)
        verification = _verification_settings(profile)
        has_issue_foundry_inputs = (
            issue_dataset is not None or bool(issue_evaluators)
        )

        if has_issue_foundry_inputs:
            if issue_evaluators and defaults is not None:
                provenance: list[VerificationProvenance] = []
                if issue_dataset is not None:
                    provenance.append("issue_dataset")
                provenance.append("issue_evaluators")
                if issue_dataset is None:
                    provenance.append(
                        "repository_default_bundle"
                        if verification.bundle is not None
                        else "runtime_metadata_defaults"
                    )
                return VerificationResolution(
                    mode="foundry_evaluation",
                    evaluation_gate_policy=verification.evaluation_gate_policy,
                    foundry_evaluation=FoundryEvaluationSelection(
                        source="issue",
                        defaults=defaults,
                        issue_dataset=issue_dataset,
                        issue_evaluators=issue_evaluators,
                    ),
                    provenance=tuple(provenance),
                    warnings=tuple(warnings),
                    quantitative_decision_allowed=True,
                )
            if issue_evaluators:
                warnings.append(
                    "issue-supplied evaluators were ignored because repository/runtime Foundry defaults were unavailable"
                )
            if issue_dataset is not None:
                warnings.append(
                    "issue-supplied verification dataset was ignored because no exact evaluator IDs were provided"
                )
        if issue_checks:
            return VerificationResolution(
                mode="repository_checks",
                evaluation_gate_policy=verification.evaluation_gate_policy,
                repository_checks=RepositoryChecksSelection(
                    source="issue",
                    checks=issue_checks,
                ),
                provenance=("issue_repository_checks",),
                warnings=tuple(warnings),
                quantitative_decision_allowed=False,
            )
        if acknowledge_no_evidence:
            warnings.append(
                "No approved quantitative or repository verification evidence is available; any selected proposal remains unverified."
            )
            return VerificationResolution(
                mode="none",
                evaluation_gate_policy=verification.evaluation_gate_policy,
                provenance=("explicit_no_evidence",),
                warnings=tuple(warnings),
                quantitative_decision_allowed=False,
            )
        if defaults is not None and not has_issue_foundry_inputs:
            return VerificationResolution(
                mode="foundry_evaluation",
                evaluation_gate_policy=verification.evaluation_gate_policy,
                foundry_evaluation=FoundryEvaluationSelection(
                    source=(
                        "repository_default_bundle"
                        if verification.bundle is not None
                        else "runtime_metadata"
                    ),
                    defaults=defaults,
                ),
                provenance=(
                    ("repository_default_bundle",)
                    if verification.bundle is not None
                    else ("runtime_metadata_defaults",)
                ),
                warnings=tuple(warnings),
                quantitative_decision_allowed=True,
            )
        if verification.repository_checks:
            return VerificationResolution(
                mode="repository_checks",
                evaluation_gate_policy=verification.evaluation_gate_policy,
                repository_checks=RepositoryChecksSelection(
                    source="repository",
                    checks=verification.repository_checks,
                ),
                provenance=("repository_default_checks",),
                warnings=tuple(warnings),
                quantitative_decision_allowed=False,
            )

        return VerificationResolution(
            mode="none",
            evaluation_gate_policy=verification.evaluation_gate_policy,
            provenance=(
                ("explicit_no_evidence",)
                if acknowledge_no_evidence
                else ("no_verification_inputs",)
            ),
            warnings=tuple(warnings),
            quantitative_decision_allowed=False,
        )


def resolve_verification(
    *,
    profile: object,
    issue: OptimizeIssueRequest | None = None,
) -> VerificationResolution:
    return DefaultVerificationResolver().resolve(profile=profile, issue=issue)


def verification_mode_allowed(resolution: VerificationResolution) -> bool:
    if resolution.mode == "foundry_evaluation":
        return True
    if resolution.mode == "repository_checks":
        return resolution.evaluation_gate_policy != "require_foundry_evaluation"
    return resolution.evaluation_gate_policy == "allow_no_evidence"


def verification_mode_blocker(resolution: VerificationResolution) -> str | None:
    if verification_mode_allowed(resolution):
        return None
    if resolution.mode == "repository_checks":
        return "repository checks are not allowed by the repository verification gate policy"
    return "qualitative-only proposals are not allowed by the repository verification gate policy"


def _verification_settings(profile: object) -> object:
    verification = getattr(profile, "verification", None)
    if verification is None:
        raise ValueError("verification profile is missing verification settings")
    return verification


def _reject_issue_named_checks(
    checks: tuple[VerificationCheckSpec, ...] | None,
) -> None:
    if any(check.kind == "check" for check in checks or ()):
        raise BootstrapConfigError(ISSUE_NAMED_CHECK_GUIDANCE)


def _foundry_defaults(profile: object) -> FoundryEvaluationPlan | None:
    explicit = getattr(profile, "foundry_evaluation_plan", None)
    if explicit is not None:
        return FoundryEvaluationPlan.model_validate(explicit)
    verification = _verification_settings(profile)
    bundle = getattr(verification, "bundle", None)
    if bundle is None:
        return None
    objective = bundle.default_evaluator_bundle.objective
    evaluator_ids = tuple(item.reference.evaluator_id for item in objective.evaluators)
    return FoundryEvaluationPlan(
        development_definition_id=bundle.development_definition.definition_id,
        development_dataset_id=bundle.development_dataset.dataset_id,
        development_evaluator_ids=evaluator_ids,
        validating_definition_id=bundle.validating_definition.definition_id,
        validating_dataset_id=bundle.validating_dataset.dataset_id,
        validating_evaluator_ids=evaluator_ids,
    )


def _merge_evaluator_ids(
    defaults: tuple[str, ...],
    issue_overrides: tuple[str, ...],
) -> tuple[str, ...]:
    merged: list[str] = []
    seen: set[str] = set()
    for evaluator_id in (*defaults, *issue_overrides):
        key = evaluator_id.casefold()
        if key in seen:
            continue
        seen.add(key)
        merged.append(evaluator_id)
    return tuple(merged)


def deployment_unverified_warning() -> DeploymentVerificationWarning:
    return DeploymentVerificationWarning(
        code="deployment-unverified",
        message=(
            "exact-source publication is permitted without Foundry evaluation or "
            "repository check evidence"
        ),
    )


def deployment_evaluator_ids(
    *,
    bundle: VerificationBundle,
    hard_guardrails: tuple[HardGuardrail, ...],
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (
                *(
                    evaluator.reference.evaluator_id
                    for evaluator in bundle.default_evaluator_bundle.objective.evaluators
                ),
                *(guardrail.evaluator_name for guardrail in hard_guardrails),
            )
        )
    )


def _usable_deployment_bundle(
    profile: AgentProfile,
) -> VerificationBundle | None:
    bundle = profile.verification.bundle
    if bundle is None:
        return None
    if (
        profile.deployment.require_aligned_binding
        and (
            profile.verification.lineage is None
            or profile.verification.lineage.activation_binding is None
        )
    ):
        return None
    return bundle


def resolve_deployment_verification(
    *,
    profile: AgentProfile,
) -> DeploymentVerification:
    policy = profile.verification.evaluation_gate_policy
    bundle = profile.verification.bundle

    if policy == "require_foundry_evaluation":
        if bundle is None:
            raise BootstrapConfigError(
                "deployment plans require an activated repository default evaluator bundle"
            )
        return DeploymentVerification(
            mode="foundry_evaluation",
            status="planned",
            evaluation_gate_policy=policy,
            objective_hash=bundle.default_evaluator_bundle.objective.objective_hash,
            evaluation_id=bundle.development_definition.definition_id,
            dataset_id=bundle.development_dataset.dataset_id,
            evaluator_ids=deployment_evaluator_ids(
                bundle=bundle,
                hard_guardrails=profile.hard_guardrails,
            ),
            unverified_deployment=False,
        )

    usable_bundle = _usable_deployment_bundle(profile)
    if usable_bundle is not None:
        return DeploymentVerification(
            mode="foundry_evaluation",
            status="planned",
            evaluation_gate_policy=policy,
            objective_hash=usable_bundle.default_evaluator_bundle.objective.objective_hash,
            evaluation_id=usable_bundle.development_definition.definition_id,
            dataset_id=usable_bundle.development_dataset.dataset_id,
            evaluator_ids=deployment_evaluator_ids(
                bundle=usable_bundle,
                hard_guardrails=profile.hard_guardrails,
            ),
            unverified_deployment=False,
        )

    checks = profile.verification.repository_checks
    if checks:
        if policy not in {"allow_repository_checks", "allow_no_evidence"}:
            raise BootstrapConfigError(
                "deployment plans cannot use repository checks under the current verification gate policy"
            )
        return DeploymentVerification(
            mode="repository_checks",
            status="planned",
            evaluation_gate_policy=policy,
            check_results=tuple(
                DeploymentVerificationCheckResult(
                    kind=check.kind,
                    value=check.value,
                    status="planned",
                )
                for check in checks
            ),
            unverified_deployment=False,
        )

    if policy == "allow_repository_checks":
        if not checks:
            raise BootstrapConfigError(
                "deployment plans require trusted repository checks when no usable Foundry evaluation bundle is available"
            )

    return DeploymentVerification(
        mode="none",
        status="unverified",
        evaluation_gate_policy=policy,
        unverified_deployment=True,
        warning=deployment_unverified_warning(),
    )


__all__ = [
    "DefaultVerificationResolver",
    "FoundryEvaluationPlan",
    "DeploymentGuardrail",
    "DeploymentVerification",
    "DeploymentVerificationCheckResult",
    "DeploymentVerificationMode",
    "DeploymentVerificationStatus",
    "DeploymentVerificationWarning",
    "FoundryEvaluationSelection",
    "RepositoryChecksSelection",
    "VerificationProvenance",
    "VerificationResolution",
    "VerificationResolver",
    "verification_mode_allowed",
    "verification_mode_blocker",
    "deployment_evaluator_ids",
    "deployment_unverified_warning",
    "resolve_verification",
    "resolve_deployment_verification",
]
