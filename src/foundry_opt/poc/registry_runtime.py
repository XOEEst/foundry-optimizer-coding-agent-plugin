from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from foundry_opt.poc.config import (
    AgentMetadata,
    CapabilityRequirement,
    EvaluationContract,
    HostedRuntimeContract,
    ModelDeploymentContract,
    RepositoryPolicy,
)
from foundry_opt.poc.runtime_errors import RuntimeIntegrationError
from foundry_opt.repository_contracts import RepositoryRegistry
from foundry_opt.repository_selection import RegistrySelection


_UNUSED_VERIFICATION_TOKEN = "verification-not-configured"
_MAX_GITHUB_EVENT_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class AgentRuntimeContracts:
    hosted_runtime: HostedRuntimeContract
    model_deployments: tuple[ModelDeploymentContract, ...]
    development_evaluation: EvaluationContract
    validating_evaluation: EvaluationContract


@dataclass(frozen=True, slots=True)
class RegisteredRepositoryContext:
    repository_identity: str
    repository_id: int
    default_branch: str
    tenant_id: str
    subscription_id: str
    client_id: str
    oidc_subject_prefix: str


def resolve_registered_repository_context(
    registry: RepositoryRegistry,
    *,
    environment: Mapping[str, str],
    account_resource_id: str,
) -> RegisteredRepositoryContext:
    repository_identity = _required_environment_value(
        environment,
        "GITHUB_REPOSITORY",
    )
    repository_id_text = _required_environment_value(
        environment,
        "GITHUB_REPOSITORY_ID",
    )
    try:
        repository_id = int(repository_id_text)
    except ValueError as error:
        raise RuntimeIntegrationError(
            "GITHUB_REPOSITORY_ID must be a positive integer"
        ) from error
    if repository_id <= 0:
        raise RuntimeIntegrationError(
            "GITHUB_REPOSITORY_ID must be a positive integer"
        )
    client_id = registry.identity.client_id
    if not client_id:
        raise RuntimeIntegrationError(
            "registered optimizer identity is missing its client id"
        )
    return RegisteredRepositoryContext(
        repository_identity=repository_identity,
        repository_id=repository_id,
        default_branch=_default_branch(
            environment,
            repository_identity=repository_identity,
            repository_id=repository_id,
        ),
        tenant_id=_required_environment_value(environment, "AZURE_TENANT_ID"),
        subscription_id=subscription_id_from_resource_id(account_resource_id),
        client_id=client_id,
        oidc_subject_prefix=_oidc_subject_prefix(
            registry,
            environment=environment,
            repository_identity=repository_identity,
            repository_id=repository_id,
        ),
    )


def build_repository_policy_from_registry_selection(
    selection: RegistrySelection,
) -> RepositoryPolicy:
    sidecar = selection.sidecar
    required_guardrails = [
        {
            "metric": guardrail.evaluator_name,
            "required_pass_rate": guardrail.required_pass_rate,
        }
        for guardrail in sidecar.hard_guardrails
        if guardrail.required
    ]
    if not required_guardrails:
        raise RuntimeIntegrationError(
            "registered agent requires at least one mandatory hard guardrail"
        )
    return RepositoryPolicy.model_validate(
        {
            "schema_version": 1,
            "source_root": sidecar.package_root,
            "editable_paths": sidecar.editable_paths,
            "min_candidates": sidecar.min_candidates,
            "max_candidates": sidecar.max_candidates,
            "baseline_model": sidecar.baseline_model,
            "allowed_models": sidecar.allowed_models,
            "primary_metric": sidecar.primary_metric,
            "decision_rules": {
                "minimum_aggregate_delta": (
                    sidecar.decision_policy.minimum_aggregate_delta
                ),
                "focused_cases_required": (
                    sidecar.decision_policy.focused_cases_required
                ),
                "max_regressions": sidecar.decision_policy.max_regressions,
            },
            "hard_guardrails": required_guardrails,
            "metadata_path": selection.config_path,
        }
    )


def build_agent_runtime_contracts(
    selection: RegistrySelection,
    *,
    include_evaluation: bool,
    development_evaluator_ids: tuple[str, ...] | None = None,
    validating_evaluator_ids: tuple[str, ...] | None = None,
) -> AgentRuntimeContracts:
    sidecar = selection.sidecar
    hosted_runtime = HostedRuntimeContract(
        kind="hosted",
        runtime=sidecar.runtime.runtime,
        entry_point=sidecar.runtime.entrypoint,
        dependency_resolution=sidecar.runtime.dependency_resolution,
        protocol_name=sidecar.runtime.protocol_name,
        protocol_version=sidecar.runtime.protocol_version,
        cpu=sidecar.runtime.cpu or "1",
        memory=sidecar.runtime.memory or "2Gi",
        model_environment_variable=(
            sidecar.runtime.model_environment_variable
            or "AZURE_AI_MODEL_DEPLOYMENT_NAME"
        ),
    )
    aliases = tuple(
        dict.fromkeys(
            (
                sidecar.baseline_model,
                *sidecar.allowed_models,
                *sidecar.foundry_project.model_deployment_aliases,
            )
        )
    )
    model_deployments = tuple(
        ModelDeploymentContract(
            alias=alias,
            deployment_name=alias,
            model_format="OpenAI",
            model_name=alias,
            model_version="pinned",
            required_capabilities=(
                CapabilityRequirement(
                    name=sidecar.runtime.protocol_name,
                    enabled=True,
                ),
            ),
        )
        for alias in aliases
    )
    bundle = sidecar.verification.bundle if include_evaluation else None
    if bundle is None:
        development_evaluation = EvaluationContract(
            name="development",
            split="development",
            resolved_evaluation_id=_UNUSED_VERIFICATION_TOKEN,
            dataset_id=_UNUSED_VERIFICATION_TOKEN,
        )
        validating_evaluation = EvaluationContract(
            name="validating",
            split="validating",
            resolved_evaluation_id=_UNUSED_VERIFICATION_TOKEN,
            dataset_id=_UNUSED_VERIFICATION_TOKEN,
        )
    else:
        resolved_development_ids = (
            bundle.resolved_development_evaluator_ids
            if development_evaluator_ids is None
            else development_evaluator_ids
        )
        resolved_validating_ids = (
            bundle.resolved_validating_evaluator_ids
            if validating_evaluator_ids is None
            else validating_evaluator_ids
        )
        development_evaluation = EvaluationContract(
            name="development",
            split="development",
            resolved_evaluation_id=bundle.development_definition.definition_id,
            dataset_id=bundle.development_dataset.dataset_id,
            custom_evaluator_ids=resolved_development_ids,
        )
        validating_evaluation = EvaluationContract(
            name="validating",
            split="validating",
            resolved_evaluation_id=bundle.validating_definition.definition_id,
            dataset_id=bundle.validating_dataset.dataset_id,
            custom_evaluator_ids=resolved_validating_ids,
        )
    return AgentRuntimeContracts(
        hosted_runtime=hosted_runtime,
        model_deployments=model_deployments,
        development_evaluation=development_evaluation,
        validating_evaluation=validating_evaluation,
    )


def build_agent_metadata_from_registry_selection(
    selection: RegistrySelection,
    *,
    repository_identity: str,
    repository_id: int,
    default_branch: str,
    tenant_id: str,
    subscription_id: str,
    client_id: str,
    client_id_variable: str,
    oidc_subject_prefix: str,
    oidc_role: str,
    oidc_environment: str,
    include_evaluation: bool,
    development_evaluator_ids: tuple[str, ...] | None = None,
    validating_evaluator_ids: tuple[str, ...] | None = None,
) -> AgentMetadata:
    sidecar = selection.sidecar
    contracts = build_agent_runtime_contracts(
        selection,
        include_evaluation=include_evaluation,
        development_evaluator_ids=development_evaluator_ids,
        validating_evaluator_ids=validating_evaluator_ids,
    )
    variable_alias = f"{oidc_role}_client_id"
    subject = f"{oidc_subject_prefix}:environment:{oidc_environment}"
    return AgentMetadata(
        repository_identity=repository_identity,
        repository_id=repository_id,
        default_branch=default_branch,
        project_endpoint=sidecar.foundry_project.project_endpoint,
        foundry_account_resource_id=sidecar.foundry_project.account_resource_id,
        agent_name=sidecar.foundry_project.agent_name,
        hosted_runtime=contracts.hosted_runtime,
        oidc={
            "issuer": "https://token.actions.githubusercontent.com",
            "audience": "api://AzureADTokenExchange",
            "tenant_id": tenant_id,
            "subscription_id": subscription_id,
            "repository_id_claim": str(repository_id),
            "workflow_variables": (
                {
                    "alias": variable_alias,
                    "name": client_id_variable,
                    "value": client_id,
                    "scope": "environment",
                    "environment": oidc_environment,
                },
            ),
            "principals": (
                {
                    "role": oidc_role,
                    "client_id": client_id,
                    "client_id_variable": variable_alias,
                    "environment": oidc_environment,
                    "subject": subject,
                    "direct_oidc_subject": subject,
                    "subjects": (
                        {
                            "name": "environment",
                            "subject": subject,
                            "environment": oidc_environment,
                        },
                    ),
                },
            ),
        },
        model_deployments=contracts.model_deployments,
        development_evaluation=contracts.development_evaluation,
        validating_evaluation=contracts.validating_evaluation,
    )


def subscription_id_from_resource_id(resource_id: str) -> str:
    parts = [part for part in resource_id.split("/") if part]
    for index, part in enumerate(parts[:-1]):
        if part.casefold() == "subscriptions":
            return parts[index + 1]
    raise RuntimeIntegrationError(
        "Foundry account resource id omitted the subscription id"
    )


def _required_environment_value(
    environment: Mapping[str, str],
    name: str,
) -> str:
    value = environment.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeIntegrationError(f"{name} is required")
    return value.strip()


def _default_branch(
    environment: Mapping[str, str],
    *,
    repository_identity: str,
    repository_id: int,
) -> str:
    configured = environment.get("FOUNDRY_OPT_DEFAULT_BRANCH")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    event_path = environment.get("GITHUB_EVENT_PATH")
    if not isinstance(event_path, str) or not event_path.strip():
        raise RuntimeIntegrationError(
            "FOUNDRY_OPT_DEFAULT_BRANCH or GITHUB_EVENT_PATH is required"
        )
    try:
        data = Path(event_path).read_bytes()
    except OSError as error:
        raise RuntimeIntegrationError(
            "GitHub event payload could not be read"
        ) from error
    if len(data) > _MAX_GITHUB_EVENT_BYTES:
        raise RuntimeIntegrationError(
            "GitHub event payload exceeds the size limit"
        )
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RuntimeIntegrationError(
            "GitHub event payload is invalid"
        ) from error
    repository = payload.get("repository") if isinstance(payload, Mapping) else None
    if not isinstance(repository, Mapping):
        raise RuntimeIntegrationError(
            "GitHub event payload is missing repository metadata"
        )
    if repository.get("full_name") not in {None, repository_identity}:
        raise RuntimeIntegrationError(
            "GitHub event repository does not match GITHUB_REPOSITORY"
        )
    if str(repository.get("id", repository_id)) != str(repository_id):
        raise RuntimeIntegrationError(
            "GitHub event repository id does not match GITHUB_REPOSITORY_ID"
        )
    default_branch = repository.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch.strip():
        raise RuntimeIntegrationError(
            "GitHub event repository default branch is invalid"
        )
    return default_branch.strip()


def _oidc_subject_prefix(
    registry: RepositoryRegistry,
    *,
    environment: Mapping[str, str],
    repository_identity: str,
    repository_id: int,
) -> str:
    configured = (
        registry.github.oidc_subject_prefix
        or f"repo:{repository_identity}"
    )
    owner, repository = repository_identity.split("/", 1)
    legacy = f"repo:{repository_identity}"
    owner_id = environment.get("GITHUB_REPOSITORY_OWNER_ID")
    immutable = (
        None
        if not isinstance(owner_id, str) or not owner_id.isdigit()
        else f"repo:{owner}@{owner_id}/{repository}@{repository_id}"
    )
    if configured != legacy and configured != immutable:
        raise RuntimeIntegrationError(
            "committed OIDC subject prefix does not match GitHub repository identity"
        )
    return configured
