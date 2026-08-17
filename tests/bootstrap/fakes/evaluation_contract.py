"""Deterministic builders for approved evaluation onboarding contracts used by tests."""

from __future__ import annotations

from hashlib import sha256
from typing import Any

from foundry_opt.bootstrap.contracts import (
    DecisionPolicy,
    DeploymentSettings,
    EvaluatorNormalization,
    FoundryProjectSettings,
    HardGuardrail,
    RuntimeProtocolSettings,
)
from foundry_opt.bootstrap.evaluation.core import REQUIRED_SAFETY_EVALUATORS
from foundry_opt.bootstrap.evaluation.execution import (
    ActivationPlan,
    DatasetPlan,
    DefinitionPlan,
    EvaluationOnboardingRequest,
    EvaluatorPlan,
    OnboardingBounds,
    ReplacementLineage,
    SidecarPolicy,
    TelemetryProbe,
)

LEGACY_AGGREGATE_SAFETY_ID = "azureai://built-in/evaluators/content_safety"
QUALITY_EVALUATOR_ID = "azureai://accounts/example/projects/example/evaluators/quality-eval/versions/2"
DEVELOPMENT_DATASET_ID = "azureai://accounts/example/projects/example/data/dev-set/versions/1"
VALIDATING_DATASET_ID = "azureai://accounts/example/projects/example/data/val-set/versions/1"
PROJECT_ENDPOINT = "https://example.services.ai.azure.com/api/projects/example"
ACCOUNT_RESOURCE_ID = (
    "/subscriptions/33333333-3333-3333-3333-333333333333/resourceGroups/example"
    "/providers/Microsoft.CognitiveServices/accounts/example"
)
MODEL_DEPLOYMENT = "baseline-model"
GENERATION_FINGERPRINT = "b" * 64
DATASET_JOB_ID = "foundry-datagen-synthetic-000000000000000000000001"
TRACE_JOB_ID = "foundry-datagen-trace-000000000000000000000001"
RUBRIC_JOB_ID = "foundry-evalgen-rubric-000000000000000000000001"


def dataset_rows(count: int = 30) -> tuple[dict[str, str], ...]:
    return tuple({"row_id": f"case-{index:03d}", "group_id": f"group-{index:03d}"} for index in range(1, count + 1))


def build_sidecar_policy(
    *,
    repo_agent_id: str = "app",
    root: str = "app",
    binding_classification: str = "bound-aligned",
    path: str | None = None,
) -> SidecarPolicy:
    return SidecarPolicy(
        path=path or f"{root}/.foundry/foundry-opt.yaml",
        source_root=root,
        package_root=root,
        editable_paths=(f"{root}/main.py",),
        runtime=RuntimeProtocolSettings(
            kind="hosted",
            runtime="python_3_13",
            entrypoint=("python", "main.py"),
            dependency_resolution="remote_build",
            protocol_name="responses",
            protocol_version="2.0.0",
        ),
        foundry_project=FoundryProjectSettings(
            project_endpoint=PROJECT_ENDPOINT,
            account_resource_id=ACCOUNT_RESOURCE_ID,
            agent_name="example-agent",
            model_deployment_aliases=(MODEL_DEPLOYMENT,),
        ),
        baseline_model=MODEL_DEPLOYMENT,
        allowed_models=(MODEL_DEPLOYMENT,),
        min_candidates=2,
        max_candidates=2,
        primary_metric="quality",
        decision_policy=DecisionPolicy(minimum_aggregate_delta=0.01, focused_cases_required=True, max_regressions=0),
        hard_guardrails=tuple(
            HardGuardrail(evaluator_name=name, required_pass_rate=1.0, required=True)
            for name in REQUIRED_SAFETY_EVALUATORS
        ),
        deployment=DeploymentSettings(
            environment="foundry-production",
            enabled=binding_classification == "bound-aligned",
            require_aligned_binding=True,
        ),
        max_issue_evaluators=8,
    )


def build_contract(
    *,
    repo_agent_id: str = "app",
    root: str = "app",
    binding_classification: str = "bound-aligned",
    reuse: bool = False,
    generation_kind: str = "dataset_synthetic",
    useful_trace_samples: int = 0,
    trace_prerequisites: bool = True,
    target_sample_count: int = 30,
    replacement: ReplacementLineage | None = None,
    stop_reason: str | None = None,
    bounds: OnboardingBounds | None = None,
    sidecar_path: str | None = None,
    agent_name: str = "example-agent",
    agent_version: str = "1",
) -> EvaluationOnboardingRequest:
    if binding_classification in {"ready-unbound", "not-ready"}:
        return EvaluationOnboardingRequest.create(
            repo_agent_id=repo_agent_id,
            binding_classification=binding_classification,
            stop_reason=stop_reason or "ready-unbound agents stop before evaluation onboarding",
        )
    return EvaluationOnboardingRequest.create(
        repo_agent_id=repo_agent_id,
        binding_classification=binding_classification,
        bounds=bounds or OnboardingBounds(target_sample_count=target_sample_count),
        telemetry_probe=TelemetryProbe(
            prerequisites_available=trace_prerequisites,
            useful_sample_count=useful_trace_samples,
            telemetry_window="P14D",
            eligible=trace_prerequisites and useful_trace_samples >= 15,
        ),
        dataset_plan=DatasetPlan(
            requested_development_name="dev-set",
            requested_validating_name="val-set",
            requested_version="1",
            dataset_type="uri_file",
            connection_name="foundry-default",
            generation_kind=generation_kind,
            generation_job_id=TRACE_JOB_ID if generation_kind == "dataset_trace" else DATASET_JOB_ID,
            source_fingerprint=GENERATION_FINGERPRINT,
            agent_name=agent_name,
            agent_version=agent_version,
            generation_model_deployment=MODEL_DEPLOYMENT,
            reuse_development_dataset_id=DEVELOPMENT_DATASET_ID if reuse else None,
            reuse_validating_dataset_id=VALIDATING_DATASET_ID if reuse else None,
        ),
        evaluator_plan=EvaluatorPlan(
            requested_name="quality-eval",
            requested_version="2",
            generation_job_id=None if reuse else RUBRIC_JOB_ID,
            reuse_evaluator_id=QUALITY_EVALUATOR_ID if reuse else None,
            objective_normalization=EvaluatorNormalization(kind="scalar", source_min=0.0, source_max=1.0),
            objective_weight=1.0,
        ),
        definition_plan=DefinitionPlan(
            requested_development_name="dev-def",
            requested_validating_name="val-def",
            model_deployment=MODEL_DEPLOYMENT,
        ),
        activation_plan=ActivationPlan(
            draft_agent_name="draft-agent",
            draft_agent_version="1",
            model_deployment=MODEL_DEPLOYMENT,
        ),
        sidecar_policy=build_sidecar_policy(
            repo_agent_id=repo_agent_id,
            root=root,
            binding_classification=binding_classification,
            path=sidecar_path,
        ),
        replacement=replacement,
    )


def evaluation_agent_payload(
    contract: EvaluationOnboardingRequest | None,
    *,
    repo_agent_id: str = "app",
    root: str = "app",
    reuse: bool | None = None,
    replacement_intent: bool = False,
    target_sample_count: int = 30,
) -> dict[str, Any]:
    reusing = reuse
    if reusing is None:
        reusing = bool(
            contract is not None
            and contract.dataset_plan is not None
            and contract.dataset_plan.reuse_candidates is not None
        )
    payload: dict[str, Any] = {
        "schema_version": 1,
        "repo_agent_id": repo_agent_id,
        "sidecar_path": f"{root}/.foundry/foundry-opt.yaml",
        "project_endpoint": PROJECT_ENDPOINT,
        "account_resource_id": ACCOUNT_RESOURCE_ID,
        "agent_name": "example-agent",
        "agent_version": "1",
        "existing_dataset_ids": [DEVELOPMENT_DATASET_ID, VALIDATING_DATASET_ID] if reusing else [],
        "existing_evaluator_ids": [QUALITY_EVALUATOR_ID] if reusing else [],
        "existing_definition_ids": [],
        "generation_mode": "reuse_reviewed_sources",
        "generation_sources": [{"schema_version": 1, "kind": "reviewed_file", "path": f"{root}/main.py"}],
        "model_deployment": MODEL_DEPLOYMENT,
        "trace_window": "P14D",
        "connection_name": "foundry-default",
        "target_sample_count": target_sample_count,
        "replacement_intent": replacement_intent,
    }
    if contract is not None:
        payload["onboarding_contract"] = contract.model_dump(mode="json")
    return payload


def sidecar_sha256(document_bytes: bytes) -> str:
    return sha256(document_bytes).hexdigest()


__all__ = [
    "ACCOUNT_RESOURCE_ID",
    "LEGACY_AGGREGATE_SAFETY_ID",
    "DATASET_JOB_ID",
    "DEVELOPMENT_DATASET_ID",
    "GENERATION_FINGERPRINT",
    "MODEL_DEPLOYMENT",
    "PROJECT_ENDPOINT",
    "QUALITY_EVALUATOR_ID",
    "RUBRIC_JOB_ID",
    "TRACE_JOB_ID",
    "VALIDATING_DATASET_ID",
    "build_contract",
    "build_sidecar_policy",
    "dataset_rows",
    "evaluation_agent_payload",
    "sidecar_sha256",
]
