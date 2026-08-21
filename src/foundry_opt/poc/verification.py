from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import Field, model_validator

from foundry_opt.bootstrap.contracts import (
    BootstrapSidecar,
    EvaluationGatePolicy,
    VerificationBundle,
)
from foundry_opt.models import FrozenModel
from foundry_opt.poc.config import IssueEvaluatorEntry, OptimizeIssueRequest
from foundry_opt.verification import VerificationCheckSpec, VerificationDatasetInput


VerificationResolutionMode = Literal["foundry_evaluation", "repository_checks", "none"]
VerificationProvenance = Literal[
    "issue_dataset",
    "issue_evaluators",
    "repository_default_bundle",
    "issue_repository_checks",
    "repository_default_checks",
    "explicit_no_evidence",
    "no_verification_inputs",
]


class FoundryEvaluationSelection(FrozenModel):
    source: Literal["issue", "repository_default_bundle"]
    issue_dataset: VerificationDatasetInput | None = None
    issue_evaluators: tuple[IssueEvaluatorEntry, ...] = ()
    repository_bundle: VerificationBundle | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "FoundryEvaluationSelection":
        if self.source == "issue":
            if self.issue_dataset is None or not self.issue_evaluators:
                raise ValueError("issue foundry selections require dataset and evaluators")
            if self.repository_bundle is not None:
                raise ValueError(
                    "issue foundry selections cannot also carry a repository bundle"
                )
        else:
            if self.repository_bundle is None:
                raise ValueError(
                    "repository foundry selections require a repository bundle"
                )
            if self.issue_dataset is not None or self.issue_evaluators:
                raise ValueError(
                    "repository foundry selections cannot carry issue inputs"
                )
        return self


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
        profile: BootstrapSidecar,
        issue: OptimizeIssueRequest | None = None,
    ) -> VerificationResolution: ...


class DefaultVerificationResolver:
    def resolve(
        self,
        *,
        profile: BootstrapSidecar,
        issue: OptimizeIssueRequest | None = None,
    ) -> VerificationResolution:
        warnings: list[str] = []
        issue_dataset = None if issue is None else issue.verification_dataset
        issue_evaluators = None if issue is None else issue.issue_evaluators
        issue_checks = None if issue is None else issue.verification_checks
        acknowledge_no_evidence = bool(
            issue is not None and issue.acknowledge_no_evidence
        )

        if issue_dataset is not None or issue_evaluators is not None:
            if issue_dataset is not None and issue_evaluators:
                return VerificationResolution(
                    mode="foundry_evaluation",
                    evaluation_gate_policy=profile.verification.evaluation_gate_policy,
                    foundry_evaluation=FoundryEvaluationSelection(
                        source="issue",
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
        elif profile.verification.bundle is not None:
            return VerificationResolution(
                mode="foundry_evaluation",
                evaluation_gate_policy=profile.verification.evaluation_gate_policy,
                foundry_evaluation=FoundryEvaluationSelection(
                    source="repository_default_bundle",
                    repository_bundle=profile.verification.bundle,
                ),
                provenance=("repository_default_bundle",),
                warnings=tuple(warnings),
                quantitative_decision_allowed=True,
            )

        if issue_checks:
            return VerificationResolution(
                mode="repository_checks",
                evaluation_gate_policy=profile.verification.evaluation_gate_policy,
                repository_checks=RepositoryChecksSelection(
                    source="issue",
                    checks=issue_checks,
                ),
                provenance=("issue_repository_checks",),
                warnings=tuple(warnings),
                quantitative_decision_allowed=False,
            )
        if profile.verification.repository_checks:
            return VerificationResolution(
                mode="repository_checks",
                evaluation_gate_policy=profile.verification.evaluation_gate_policy,
                repository_checks=RepositoryChecksSelection(
                    source="repository",
                    checks=profile.verification.repository_checks,
                ),
                provenance=("repository_default_checks",),
                warnings=tuple(warnings),
                quantitative_decision_allowed=False,
            )

        return VerificationResolution(
            mode="none",
            evaluation_gate_policy=profile.verification.evaluation_gate_policy,
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
    profile: BootstrapSidecar,
    issue: OptimizeIssueRequest | None = None,
) -> VerificationResolution:
    return DefaultVerificationResolver().resolve(profile=profile, issue=issue)


__all__ = [
    "DefaultVerificationResolver",
    "FoundryEvaluationSelection",
    "RepositoryChecksSelection",
    "VerificationProvenance",
    "VerificationResolution",
    "VerificationResolver",
    "resolve_verification",
]
