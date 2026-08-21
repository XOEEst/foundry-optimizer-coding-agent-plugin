from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, model_validator

from foundry_opt.bootstrap.contracts import (
    BootstrapSidecar,
    EvaluationGatePolicy,
)
from foundry_opt.models import FrozenModel
from foundry_opt.poc.config import IssueEvaluatorEntry, OptimizeIssueRequest
from foundry_opt.verification import VerificationCheckSpec, VerificationDatasetInput


VerificationResolutionMode = Literal["foundry_evaluation", "repository_checks", "none"]
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


class FoundryEvaluationSelection(FrozenModel):
    source: Literal["issue", "repository_default_bundle", "runtime_metadata"]
    defaults: FoundryEvaluationPlan
    issue_dataset: VerificationDatasetInput | None = None
    issue_evaluators: tuple[IssueEvaluatorEntry, ...] = ()

    @model_validator(mode="after")
    def validate_shape(self) -> "FoundryEvaluationSelection":
        if self.source == "issue":
            if self.issue_dataset is None or not self.issue_evaluators:
                raise ValueError("issue foundry selections require dataset and evaluators")
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
        return self.override_evaluator_ids or self.defaults.development_evaluator_ids

    @property
    def validating_dataset_id(self) -> str:
        return self.defaults.validating_dataset_id

    @property
    def validating_evaluator_ids(self) -> tuple[str, ...]:
        return self.override_evaluator_ids or self.defaults.validating_evaluator_ids


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
        acknowledge_no_evidence = bool(
            issue is not None and issue.acknowledge_no_evidence
        )
        defaults = _foundry_defaults(profile)
        verification = _verification_settings(profile)
        has_issue_foundry_inputs = (
            issue_dataset is not None or bool(issue_evaluators)
        )

        if has_issue_foundry_inputs:
            if issue_dataset is not None and issue_evaluators:
                return VerificationResolution(
                    mode="foundry_evaluation",
                    evaluation_gate_policy=verification.evaluation_gate_policy,
                    foundry_evaluation=FoundryEvaluationSelection(
                        source="issue",
                        defaults=defaults,
                        issue_dataset=issue_dataset,
                        issue_evaluators=issue_evaluators,
                    ),
                    provenance=("issue_dataset", "issue_evaluators"),
                    warnings=tuple(warnings),
                    quantitative_decision_allowed=True,
                )
            if issue_evaluators:
                warnings.append(
                    "issue-supplied evaluators were ignored because no exact verification dataset was provided"
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


__all__ = [
    "DefaultVerificationResolver",
    "FoundryEvaluationPlan",
    "FoundryEvaluationSelection",
    "RepositoryChecksSelection",
    "VerificationProvenance",
    "VerificationResolution",
    "VerificationResolver",
    "verification_mode_allowed",
    "verification_mode_blocker",
    "resolve_verification",
]
