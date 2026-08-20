from __future__ import annotations

import hashlib
import math
import os
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from foundry_opt.bootstrap.contracts import BootstrapLock, RootRegistry
from foundry_opt.bootstrap.workflow_integration import (
    RegistrySelection,
    resolve_registry_selection,
)
from foundry_opt.poc.auth import (
    AuthError,
    GitHubActionsOidcConfig,
    build_client_assertion_credential,
)
from foundry_opt.poc.bootstrap import (
    BootstrapReceipt,
    read_bootstrap_receipt,
    load_shared_pin,
)
from foundry_opt.poc.config import (
    AgentMetadata,
    RepositoryPolicy,
    SharedPin,
    load_agent_metadata,
    load_repository_policy,
)
from foundry_opt.poc.foundry import (
    AzureProjectsEvaluationBackend,
    CleanupError,
    ContractError,
    DeadlineError,
    DraftReference,
    DraftUnavailableError,
    EvaluationContract,
    EvaluationEvidence,
    FoundryPocClient,
    HostedDefinition,
    RegularVersionReference,
    RouteFingerprint,
    RouteModeError,
    ServiceError,
)
from foundry_opt.poc.runtime import (
    BOOTSTRAP_RECEIPT_ENV,
    DEFAULT_METADATA_PATH,
    DEFAULT_PIN_PATH,
    DEFAULT_POLICY_PATH,
    RuntimeIntegrationError,
    build_hosted_definition,
    build_oidc_config,
    load_deadline_seconds,
    select_oidc_principal,
)
from foundry_opt.poc.source import PackagedSource, package_git_source


DEPLOYMENT_ROOT_ENV = "FOUNDRY_OPT_DEPLOY_ROOT"
DEFAULT_DEPLOYMENT_ENVIRONMENT = "foundry-production"
REGISTERED_CLIENT_ID_ENV = "FOUNDRY_OPT_DEPLOYMENT_CLIENT_ID"
REGISTERED_TENANT_ID_ENV = "AZURE_TENANT_ID"
RELEASE_COMMIT_METADATA_KEY = "foundry_opt_release_commit"
REPOSITORY_METADATA_KEY = "foundry_opt_repository"
SOURCE_ROOT_METADATA_KEY = "foundry_opt_source_root"
SOURCE_TREE_METADATA_KEY = "foundry_opt_source_tree_sha256"

_COMMIT_PATTERN = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DeploymentError(RuntimeError):
    """The merge-time Foundry deployment could not complete safely."""


class DeploymentGuardrailError(DeploymentError):
    def __init__(
        self,
        message: str,
        *,
        evaluation_link: str,
        guardrails: tuple["DeploymentGuardrail", ...],
    ) -> None:
        super().__init__(message)
        self.evaluation_link = evaluation_link
        self.guardrails = guardrails


class DeploymentPostPublishError(DeploymentError):
    def __init__(
        self,
        message: str,
        *,
        reference: RegularVersionReference,
    ) -> None:
        super().__init__(f"{message}; created regular version {reference.version}")
        self.reference = reference


class DeploymentSupersededError(DeploymentError):
    def __init__(
        self,
        *,
        release_commit: str,
        current_main_commit: str,
    ) -> None:
        super().__init__(
            "release commit was superseded before regular publication"
        )
        self.release_commit = release_commit
        self.current_main_commit = current_main_commit


class DeploymentGuardrail(_FrozenModel):
    name: str = Field(min_length=1, max_length=256)
    score: float | None = Field(default=None, ge=0)
    required_pass_rate: float = Field(ge=0, le=1)
    passed: bool


class DeploymentPreflight(_FrozenModel):
    status: Literal["ready"] = "ready"
    repository: str
    release_commit: str = Field(pattern=_COMMIT_PATTERN)
    project_endpoint: str
    agent_name: str
    previous_version: str | None = None
    route_mode: Literal["service-managed-latest"] = "service-managed-latest"
    deployment_environment: str
    deployment_client_id: str
    source_root: str


class DeploymentReceipt(_FrozenModel):
    status: Literal["published"] = "published"
    repository: str
    release_commit: str = Field(pattern=_COMMIT_PATTERN)
    project_endpoint: str
    agent_name: str
    previous_version: str | None = None
    published_version: str
    operation_id: str
    reconciled: bool
    source_root: str
    source_tree_sha256: str = Field(pattern=_SHA256_PATTERN)
    source_zip_sha256: str = Field(pattern=_SHA256_PATTERN)
    evaluation_link: str
    guardrails: tuple[DeploymentGuardrail, ...]
    draft_cleanup_complete: Literal[True] = True
    route_mode: Literal["service-managed-latest"] = "service-managed-latest"
    route_mutated: Literal[False] = False
    latest_verified: Literal[True] = True


class DeploymentSettings(_FrozenModel):
    repository_root: Path
    policy: RepositoryPolicy
    metadata: AgentMetadata
    pin: SharedPin
    bootstrap_receipt: BootstrapReceipt
    release_commit: str = Field(pattern=_COMMIT_PATTERN)
    artifact_root: Path
    deadline_seconds: float = Field(gt=0)
    deployment_environment: str

    @field_validator("repository_root", "artifact_root")
    @classmethod
    def validate_absolute_paths(cls, value: Path) -> Path:
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("deployment paths must be absolute")
        return path


@dataclass(frozen=True, slots=True)
class RegisteredDeploymentSettings:
    repository_root: Path
    selection: RegistrySelection
    policy: RepositoryPolicy
    metadata: AgentMetadata
    oidc_config: GitHubActionsOidcConfig
    release_commit: str
    artifact_root: Path
    deadline_seconds: float


@dataclass(frozen=True, slots=True)
class DeploymentHandle:
    settings: DeploymentSettings
    service: "DeploymentService"
    close: Callable[[], None]


class DeploymentService:
    def __init__(
        self,
        *,
        client: FoundryPocClient,
        policy: RepositoryPolicy,
        metadata: AgentMetadata,
        deadline_seconds: float,
        monotonic: Callable[[], float] = time.monotonic,
        freshness_check: Callable[[str], None] | None = None,
    ) -> None:
        self._client = client
        self._policy = policy
        self._metadata = metadata
        self._deadline_seconds = _finite_positive(
            deadline_seconds,
            "deadline_seconds",
        )
        self._monotonic = monotonic
        self._freshness_check = freshness_check

    def preflight(
        self,
        *,
        repository: str,
        release_commit: str,
        deployment_environment: str,
        deployment_client_id: str,
    ) -> DeploymentPreflight:
        route = self._client.require_service_managed_latest(
            self._metadata.agent_name,
            deadline_monotonic=self._deadline(),
        )
        return DeploymentPreflight(
            repository=repository,
            release_commit=release_commit,
            project_endpoint=self._metadata.project_endpoint,
            agent_name=self._metadata.agent_name,
            previous_version=route.latest_version,
            deployment_environment=deployment_environment,
            deployment_client_id=deployment_client_id,
            source_root=self._policy.source_root,
        )

    def publish(
        self,
        *,
        repository: str,
        release_commit: str,
        packaged: PackagedSource,
    ) -> DeploymentReceipt:
        if packaged.commit != release_commit:
            raise DeploymentError(
                "packaged source commit does not match the requested release commit"
            )
        if packaged.source_root != self._policy.source_root:
            raise DeploymentError(
                "packaged source root does not match repository policy"
            )
        route_before = self._client.require_service_managed_latest(
            self._metadata.agent_name,
            deadline_monotonic=self._deadline(),
        )
        operation_id = deployment_operation_id(
            repository=repository,
            agent_name=self._metadata.agent_name,
            release_commit=release_commit,
        )
        definition = build_hosted_definition(
            self._metadata,
            self._policy.baseline_model,
        )
        evidence = self._evaluate_deployment_draft(
            packaged=packaged,
            definition=definition,
            operation_id=operation_id,
        )
        guardrails = self._deployment_guardrails(evidence)
        failed = tuple(item.name for item in guardrails if not item.passed)
        if failed:
            raise DeploymentGuardrailError(
                "deployment hard guardrails failed: " + ", ".join(failed),
                evaluation_link=evidence.report_url,
                guardrails=guardrails,
            )
        route_after_draft = self._client.require_service_managed_latest(
            self._metadata.agent_name,
            deadline_monotonic=self._deadline(),
        )
        if route_after_draft.latest_version != route_before.latest_version:
            raise DeploymentError(
                "Foundry latest regular version changed during draft validation"
            )
        if self._freshness_check is not None:
            self._freshness_check(release_commit)
        matching_latest = self._matching_latest_version(
            route=route_after_draft,
            packaged=packaged,
        )
        if matching_latest is not None:
            return self._receipt(
                repository=repository,
                release_commit=release_commit,
                packaged=packaged,
                route_before=route_before,
                reference=matching_latest,
                reconciled=True,
                evidence=evidence,
                guardrails=guardrails,
            )
        provenance = {
            RELEASE_COMMIT_METADATA_KEY: release_commit,
            REPOSITORY_METADATA_KEY: repository,
            SOURCE_ROOT_METADATA_KEY: packaged.source_root,
            SOURCE_TREE_METADATA_KEY: packaged.tree_sha256,
        }
        published = self._client.create_regular_version(
            self._metadata.agent_name,
            definition,
            packaged.archive_bytes,
            operation_id=operation_id,
            provenance=provenance,
            description=f"Deploy {repository}@{release_commit[:12]}",
            deadline_monotonic=self._deadline(),
        )
        try:
            active = self._client.wait_for_regular_version_active(
                published,
                deadline_monotonic=self._deadline(),
            )
            downloaded = self._client.download_regular_version_code(
                active,
                deadline_monotonic=self._deadline(),
            )
            if downloaded != packaged.archive_bytes:
                raise ContractError(
                    "published regular version did not contain the exact source ZIP"
                )
            latest = self._client.assert_regular_version_is_latest(
                active,
                deadline_monotonic=self._deadline(),
            )
        except (
            AuthError,
            ContractError,
            DeadlineError,
            RouteModeError,
            ServiceError,
        ) as error:
            raise DeploymentPostPublishError(
                str(error),
                reference=published,
            ) from error
        if latest.latest_version != active.version:
            raise DeploymentPostPublishError(
                "published regular version was not confirmed as latest",
                reference=active,
            )
        return self._receipt(
            repository=repository,
            release_commit=release_commit,
            packaged=packaged,
            route_before=route_before,
            reference=active,
            reconciled=published.reconciled,
            evidence=evidence,
            guardrails=guardrails,
        )

    def _matching_latest_version(
        self,
        *,
        route: RouteFingerprint,
        packaged: PackagedSource,
    ) -> RegularVersionReference | None:
        if route.latest_version is None:
            return None
        try:
            reference = self._client.get_regular_version(
                self._metadata.agent_name,
                route.latest_version,
                deadline_monotonic=self._deadline(),
            )
        except ContractError:
            return None
        if reference.code_sha256 != packaged.zip_sha256:
            return None
        downloaded = self._client.download_regular_version_code(
            reference,
            deadline_monotonic=self._deadline(),
        )
        if downloaded != packaged.archive_bytes:
            raise ContractError(
                "latest regular version hash matched but source bytes differed"
            )
        self._client.assert_regular_version_is_latest(
            reference,
            deadline_monotonic=self._deadline(),
        )
        return reference

    def _receipt(
        self,
        *,
        repository: str,
        release_commit: str,
        packaged: PackagedSource,
        route_before: RouteFingerprint,
        reference: RegularVersionReference,
        reconciled: bool,
        evidence: EvaluationEvidence,
        guardrails: tuple[DeploymentGuardrail, ...],
    ) -> DeploymentReceipt:
        return DeploymentReceipt(
            repository=repository,
            release_commit=release_commit,
            project_endpoint=self._metadata.project_endpoint,
            agent_name=self._metadata.agent_name,
            previous_version=route_before.latest_version,
            published_version=reference.version,
            operation_id=reference.operation_id,
            reconciled=reconciled,
            source_root=packaged.source_root,
            source_tree_sha256=packaged.tree_sha256,
            source_zip_sha256=packaged.zip_sha256,
            evaluation_link=evidence.report_url,
            guardrails=guardrails,
        )

    def _evaluate_deployment_draft(
        self,
        *,
        packaged: PackagedSource,
        definition: HostedDefinition,
        operation_id: str,
    ) -> EvaluationEvidence:
        reference: DraftReference | None = None
        try:
            try:
                reference = self._client.create_draft(
                    self._metadata.agent_name,
                    definition,
                    packaged.archive_bytes,
                    ownership_token=f"{operation_id}-draft",
                    deadline_monotonic=self._deadline(),
                )
            except DraftUnavailableError as error:
                self._cleanup_draft(error.owned_version)
                raise
            active = self._client.poll_version_active(
                reference,
                deadline_monotonic=self._deadline(),
            )
            downloaded = self._client.download_code(
                active,
                deadline_monotonic=self._deadline(),
            )
            if downloaded != packaged.archive_bytes:
                raise ContractError(
                    "deployment draft did not contain the exact source ZIP"
                )
            contract = self._metadata.development_evaluation
            return self._client.run_evaluation(
                active,
                EvaluationContract(
                    evaluation_id=contract.resolved_evaluation_id,
                    dataset_id=contract.dataset_id,
                    evaluator_ids=contract.custom_evaluator_ids,
                    run_name=f"deploy-{operation_id}",
                ),
                deadline_monotonic=self._deadline(),
            )
        finally:
            if reference is not None:
                self._cleanup_draft(reference)

    def _cleanup_draft(self, reference: DraftReference) -> None:
        try:
            self._client.delete_exact_owned_version(
                reference,
                deadline_monotonic=self._deadline(),
            )
        except CleanupError:
            raise
        except (
            AuthError,
            ContractError,
            DeadlineError,
            RouteModeError,
            ServiceError,
        ) as error:
            raise CleanupError(
                "deployment draft cleanup failed",
                reference=reference,
            ) from error

    def _deployment_guardrails(
        self,
        evidence: EvaluationEvidence,
    ) -> tuple[DeploymentGuardrail, ...]:
        results: list[DeploymentGuardrail] = []
        for specification in self._policy.hard_guardrails:
            metric = _metric_by_name(evidence, specification.metric)
            score = metric.score
            passed = (
                metric.passed
                and score is not None
                and score >= specification.required_pass_rate
            )
            results.append(
                DeploymentGuardrail(
                    name=specification.metric,
                    score=score,
                    required_pass_rate=specification.required_pass_rate,
                    passed=passed,
                )
            )
        return tuple(results)

    def _deadline(self) -> float:
        return self._monotonic() + self._deadline_seconds


def load_deployment_settings(
    repository: Path,
    *,
    environment: Mapping[str, str] | None = None,
    release_commit: str | None = None,
    policy_path: Path | None = None,
    metadata_path: Path | None = None,
    pin_path: Path | None = None,
    bootstrap_receipt_path: Path | None = None,
    artifact_root: Path | None = None,
    deadline_seconds: float | str | None = None,
    deployment_environment: str = DEFAULT_DEPLOYMENT_ENVIRONMENT,
) -> DeploymentSettings:
    env = os.environ if environment is None else environment
    root = _repository_root(repository)
    resolved_policy = _existing_file(
        root / DEFAULT_POLICY_PATH if policy_path is None else policy_path,
        field="policy_path",
    )
    resolved_metadata = _existing_file(
        root / DEFAULT_METADATA_PATH if metadata_path is None else metadata_path,
        field="metadata_path",
    )
    resolved_pin = _existing_file(
        root / DEFAULT_PIN_PATH if pin_path is None else pin_path,
        field="pin_path",
    )
    receipt_value = (
        bootstrap_receipt_path
        if bootstrap_receipt_path is not None
        else _required_environment_path(env, BOOTSTRAP_RECEIPT_ENV)
    )
    resolved_receipt = _existing_file(receipt_value, field="bootstrap_receipt_path")
    policy = load_repository_policy(resolved_policy, metadata_path=resolved_metadata)
    metadata = load_agent_metadata(resolved_metadata)
    pin = load_shared_pin(resolved_pin)
    receipt = read_bootstrap_receipt(resolved_receipt)
    _validate_bootstrap_receipt(pin, receipt)
    expected_metadata = (root / policy.metadata_path).resolve(strict=False)
    if expected_metadata != resolved_metadata:
        raise RuntimeIntegrationError(
            "repository policy metadata_path does not match the loaded metadata file"
        )
    _validate_repository_environment(metadata, env)
    principal = select_oidc_principal(metadata, role="deployment")
    if principal.environment != deployment_environment:
        raise RuntimeIntegrationError(
            "deployment OIDC principal environment does not match the workflow environment"
        )
    variables = {
        variable.alias: variable
        for variable in metadata.oidc.workflow_variables
    }
    variable = variables.get(principal.client_id_variable)
    if variable is None:
        raise RuntimeIntegrationError(
            "deployment OIDC principal does not reference a workflow variable"
        )
    if variable.scope != "environment" or variable.environment != deployment_environment:
        raise RuntimeIntegrationError(
            "deployment client ID must be an environment-scoped workflow variable"
        )
    configured_client_id = env.get(variable.name)
    if env.get("GITHUB_ACTIONS", "").casefold() == "true" and not configured_client_id:
        raise RuntimeIntegrationError(
            f"GitHub environment variable {variable.name} is unavailable"
        )
    if configured_client_id and configured_client_id != principal.client_id:
        raise RuntimeIntegrationError(
            f"GitHub environment variable {variable.name} does not match trusted metadata"
        )
    selected_commit = _resolve_release_commit(root, release_commit)
    selected_artifact_root = _deployment_artifact_root(
        root,
        environment=env,
        explicit=artifact_root,
    )
    return DeploymentSettings(
        repository_root=root,
        policy=policy,
        metadata=metadata,
        pin=pin,
        bootstrap_receipt=receipt,
        release_commit=selected_commit,
        artifact_root=selected_artifact_root,
        deadline_seconds=load_deadline_seconds(
            environment=env,
            deadline_seconds=deadline_seconds,
        ),
        deployment_environment=deployment_environment,
    )


def create_deployment_handle(
    settings: DeploymentSettings,
    *,
    environment: Mapping[str, str] | None = None,
    require_freshness_check: bool = False,
    credential_builder: Callable[..., object] = build_client_assertion_credential,
    evaluation_backend_factory: Callable[..., AzureProjectsEvaluationBackend] = (
        AzureProjectsEvaluationBackend
    ),
    foundry_client_factory: Callable[..., FoundryPocClient] = FoundryPocClient,
) -> DeploymentHandle:
    credential = credential_builder(
        build_oidc_config(settings.metadata, role="deployment"),
        environment=environment,
    )
    backend = evaluation_backend_factory(
        project_endpoint=settings.metadata.project_endpoint,
        credential=credential,
    )
    client = foundry_client_factory(
        settings.metadata.project_endpoint,
        credential,
        evaluation_backend=backend,
    )

    def close() -> None:
        client.close()
        closer = getattr(credential, "close", None)
        if callable(closer):
            closer()

    return DeploymentHandle(
        settings=settings,
        service=DeploymentService(
            client=client,
            policy=settings.policy,
            metadata=settings.metadata,
            deadline_seconds=settings.deadline_seconds,
            freshness_check=(
                deployment_freshness_check(
                    repository=settings.metadata.repository_identity,
                    branch=settings.metadata.default_branch,
                    environment=environment,
                )
                if require_freshness_check
                else None
            ),
        ),
        close=close,
    )


def run_deployment_preflight(
    settings: DeploymentSettings,
    *,
    environment: Mapping[str, str] | None = None,
) -> DeploymentPreflight:
    handle = create_deployment_handle(settings, environment=environment)
    try:
        principal = select_oidc_principal(settings.metadata, role="deployment")
        return handle.service.preflight(
            repository=settings.metadata.repository_identity,
            release_commit=settings.release_commit,
            deployment_environment=settings.deployment_environment,
            deployment_client_id=principal.client_id,
        )
    finally:
        handle.close()


def publish_deployment(
    settings: DeploymentSettings,
    *,
    environment: Mapping[str, str] | None = None,
) -> DeploymentReceipt:
    packaged = package_git_source(
        settings.repository_root,
        commit=settings.release_commit,
        source_root=settings.policy.source_root,
        work_root=settings.artifact_root,
    )
    handle = create_deployment_handle(
        settings,
        environment=environment,
        require_freshness_check=True,
    )
    try:
        return handle.service.publish(
            repository=settings.metadata.repository_identity,
            release_commit=settings.release_commit,
            packaged=packaged,
        )
    finally:
        handle.close()


def load_registered_deployment_settings(
    repository: Path,
    *,
    repo_agent_id: str,
    exact_source: str,
    environment: Mapping[str, str] | None = None,
    artifact_root: Path | None = None,
    deadline_seconds: float | str | None = None,
) -> RegisteredDeploymentSettings:
    env = os.environ if environment is None else environment
    if env.get("GITHUB_ACTIONS", "").casefold() != "true":
        raise RuntimeIntegrationError(
            "registered deployment requires GitHub Actions OIDC"
        )
    root = _repository_root(repository)
    release_commit = _resolve_release_commit(root, exact_source)
    selection = resolve_registry_selection(root, repo_agent_id=repo_agent_id)
    sidecar = selection.sidecar
    if not sidecar.deployment.enabled:
        raise RuntimeIntegrationError("selected registered agent deployment is disabled")
    if (
        sidecar.deployment.require_aligned_binding
        and (
            sidecar.evaluation_lineage is None
            or sidecar.evaluation_lineage.activation_binding is None
        )
    ):
        raise RuntimeIntegrationError(
            "selected registered agent has no receipt-bound aligned activation"
        )
    if sidecar.default_evaluator_bundle is None:
        raise RuntimeIntegrationError(
            "selected registered agent has no activated repository default evaluator bundle"
        )

    registry_path = root / ".foundry-opt" / "registry.yaml"
    sidecar_path = root / selection.config_path
    lock_path = root / ".foundry-opt" / "bootstrap.lock.json"
    registry_bytes = registry_path.read_bytes()
    sidecar_bytes = sidecar_path.read_bytes()
    try:
        lock_bytes = lock_path.read_bytes()
    except OSError as error:
        raise RuntimeIntegrationError(
            "registered deployment managed lock is missing"
        ) from error
    if hashlib.sha256(registry_bytes).hexdigest() != selection.registry_hash:
        raise RuntimeIntegrationError(
            "registered deployment registry changed during selection"
        )
    if hashlib.sha256(sidecar_bytes).hexdigest() != selection.sidecar_hash:
        raise RuntimeIntegrationError(
            "registered deployment sidecar changed during selection"
        )
    if _normalized_text_bytes(registry_bytes) != _normalized_text_bytes(
        _git_bytes(
            root,
            "show",
            f"{release_commit}:.foundry-opt/registry.yaml",
        )
    ):
        raise RuntimeIntegrationError(
            "registered deployment registry does not match the exact source commit"
        )
    if _normalized_text_bytes(sidecar_bytes) != _normalized_text_bytes(
        _git_bytes(
            root,
            "show",
            f"{release_commit}:{selection.config_path}",
        )
    ):
        raise RuntimeIntegrationError(
            "registered deployment sidecar does not match the exact source commit"
        )
    if _normalized_text_bytes(lock_bytes) != _normalized_text_bytes(
        _git_bytes(
            root,
            "show",
            f"{release_commit}:.foundry-opt/bootstrap.lock.json",
        )
    ):
        raise RuntimeIntegrationError(
            "registered deployment managed lock does not match the exact source commit"
        )
    registry = RootRegistry.from_document(registry_bytes.decode("utf-8"))
    try:
        lock = BootstrapLock.model_validate_json(lock_bytes)
    except ValidationError as error:
        raise RuntimeIntegrationError(
            "registered deployment managed lock is invalid"
        ) from error
    if (
        lock.runtime_repository != registry.distribution.repository
        or lock.runtime_commit != registry.distribution.pin
    ):
        raise RuntimeIntegrationError(
            "registered deployment lock and distribution pin do not match"
        )
    managed = {entry.path: entry for entry in lock.managed_files}
    for path, content in (
        (".foundry-opt/registry.yaml", registry_bytes),
        (selection.config_path, sidecar_bytes),
    ):
        entry = managed.get(path)
        if entry is None or hashlib.sha256(content).hexdigest() != entry.applied_sha256:
            raise RuntimeIntegrationError(
                f"registered deployment managed digest does not match: {path}"
            )
    if registry.github.deployment_environment != sidecar.deployment.environment:
        raise RuntimeIntegrationError(
            "registry and sidecar deployment environments do not match"
        )
    if registry.identity.kind == "unresolved_migration":
        raise RuntimeIntegrationError(
            "registered deployment requires a resolved repository identity"
        )
    trusted_client_id = registry.identity.client_id
    if not trusted_client_id:
        raise RuntimeIntegrationError(
            "registered deployment identity is missing its client id"
        )
    configured_client_id = _required_environment_value(
        env,
        REGISTERED_CLIENT_ID_ENV,
    )
    if configured_client_id != trusted_client_id:
        raise RuntimeIntegrationError(
            "deployment client id does not match the committed registry identity"
        )

    repository_identity = _required_environment_value(env, "GITHUB_REPOSITORY")
    repository_id_text = _required_environment_value(env, "GITHUB_REPOSITORY_ID")
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
    default_branch = _required_environment_value(env, "GITHUB_REF_NAME")
    if env.get("GITHUB_REF") != f"refs/heads/{default_branch}":
        raise RuntimeIntegrationError(
            "registered deployment requires an exact branch push ref"
        )

    github_sha = env.get("GITHUB_SHA")
    if github_sha is not None and github_sha != release_commit:
        raise RuntimeIntegrationError(
            "GITHUB_SHA does not match the exact registered deployment source"
        )

    tenant_id = _required_environment_value(env, REGISTERED_TENANT_ID_ENV)
    subscription_id = _subscription_id(sidecar.foundry_project.account_resource_id)
    deployment_environment = sidecar.deployment.environment
    subject_prefix = _registered_oidc_subject_prefix(
        registry,
        environment=env,
        repository_identity=repository_identity,
        repository_id=repository_id,
    )
    expected_subject = (
        f"{subject_prefix}:environment:{deployment_environment}"
    )
    oidc_config = GitHubActionsOidcConfig(
        tenant_id=tenant_id,
        client_id=trusted_client_id,
        expected_subject=expected_subject,
        expected_repository_id=str(repository_id),
    )
    policy = _registered_repository_policy(selection)
    metadata = _registered_agent_metadata(
        selection,
        repository_identity=repository_identity,
        repository_id=repository_id,
        default_branch=default_branch,
        tenant_id=tenant_id,
        subscription_id=subscription_id,
        client_id=trusted_client_id,
        client_id_variable=registry.github.client_id_variable,
        oidc_subject_prefix=subject_prefix,
    )
    return RegisteredDeploymentSettings(
        repository_root=root,
        selection=selection,
        policy=policy,
        metadata=metadata,
        oidc_config=oidc_config,
        release_commit=release_commit,
        artifact_root=_deployment_artifact_root(
            root,
            environment=env,
            explicit=artifact_root,
        ),
        deadline_seconds=load_deadline_seconds(
            environment=env,
            deadline_seconds=deadline_seconds,
        ),
    )


def publish_registered_deployment(
    settings: RegisteredDeploymentSettings,
    *,
    environment: Mapping[str, str] | None = None,
    credential_builder: Callable[..., object] = build_client_assertion_credential,
    evaluation_backend_factory: Callable[..., AzureProjectsEvaluationBackend] = (
        AzureProjectsEvaluationBackend
    ),
    foundry_client_factory: Callable[..., FoundryPocClient] = FoundryPocClient,
    service_factory: Callable[..., DeploymentService] = DeploymentService,
) -> DeploymentReceipt:
    packaged = package_git_source(
        settings.repository_root,
        commit=settings.release_commit,
        source_root=settings.policy.source_root,
        work_root=settings.artifact_root,
    )
    credential = credential_builder(
        settings.oidc_config,
        environment=environment,
    )
    client: object | None = None
    try:
        backend = evaluation_backend_factory(
            project_endpoint=settings.metadata.project_endpoint,
            credential=credential,
        )
        client = foundry_client_factory(
            settings.metadata.project_endpoint,
            credential,
            evaluation_backend=backend,
        )
        service = service_factory(
            client=client,
            policy=settings.policy,
            metadata=settings.metadata,
            deadline_seconds=settings.deadline_seconds,
            freshness_check=deployment_freshness_check(
                repository=settings.metadata.repository_identity,
                branch=settings.metadata.default_branch,
                environment=environment,
            ),
        )
        return service.publish(
            repository=settings.metadata.repository_identity,
            release_commit=settings.release_commit,
            packaged=packaged,
        )
    finally:
        try:
            client_closer = getattr(client, "close", None)
            if callable(client_closer):
                client_closer()
        finally:
            credential_closer = getattr(credential, "close", None)
            if callable(credential_closer):
                credential_closer()


def deployment_operation_id(
    *,
    repository: str,
    agent_name: str,
    release_commit: str,
) -> str:
    subject = "\n".join((repository, agent_name, release_commit))
    digest = hashlib.sha256(subject.encode("utf-8")).hexdigest()[:32]
    return f"deploy-{digest}"


def _metric_by_name(evidence: EvaluationEvidence, name: str) -> object:
    key = name.casefold()
    for metric in evidence.metrics:
        if metric.name.casefold() == key:
            return metric
    raise ContractError("evaluation evidence omitted a policy-required metric")


def deployment_freshness_check(
    *,
    repository: str,
    branch: str,
    environment: Mapping[str, str] | None,
) -> Callable[[str], None] | None:
    env = os.environ if environment is None else environment
    if env.get("GITHUB_ACTIONS", "").casefold() != "true":
        return None
    token = env.get("GH_TOKEN") or env.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeIntegrationError(
            "GitHub token is required to verify the default branch before publication"
        )

    def check(release_commit: str) -> None:
        command_environment = os.environ.copy()
        command_environment.update(env)
        command_environment["GH_TOKEN"] = token
        try:
            completed = subprocess.run(
                [
                    "gh",
                    "api",
                    f"repos/{repository}/commits/{branch}",
                    "--jq",
                    ".sha",
                ],
                check=False,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=30,
                env=command_environment,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeIntegrationError(
                "GitHub default-branch verification timed out"
            ) from error
        except OSError as error:
            raise RuntimeIntegrationError(
                "GitHub default-branch verification could not run"
            ) from error
        if completed.returncode != 0:
            raise RuntimeIntegrationError(
                "GitHub default-branch verification failed"
            )
        current = completed.stdout.strip()
        if not _valid_commit(current):
            raise RuntimeIntegrationError(
                "GitHub default-branch verification returned an invalid commit"
            )
        if current != release_commit:
            raise DeploymentSupersededError(
                release_commit=release_commit,
                current_main_commit=current,
            )

    return check


def _registered_repository_policy(
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
            "registered deployment requires at least one mandatory hard guardrail"
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


def _registered_agent_metadata(
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
) -> AgentMetadata:
    sidecar = selection.sidecar
    bundle = sidecar.default_evaluator_bundle
    development_definition = sidecar.development_definition
    validating_definition = sidecar.validating_definition
    development_dataset = sidecar.development_dataset
    validating_dataset = sidecar.validating_dataset
    if (
        bundle is None
        or development_definition is None
        or validating_definition is None
        or development_dataset is None
        or validating_dataset is None
    ):
        raise RuntimeIntegrationError(
            "registered agent metadata requires an activated repository default evaluator bundle"
        )
    evaluator_ids = tuple(
        dict.fromkeys(
            (
                *(
                    evaluator.reference.evaluator_id
                    for evaluator in bundle.objective.evaluators
                ),
                *(guardrail.evaluator_name for guardrail in sidecar.hard_guardrails),
            )
        )
    )
    deployment_environment = sidecar.deployment.environment
    subject = f"{oidc_subject_prefix}:environment:{deployment_environment}"
    return AgentMetadata.model_validate(
        {
            "schema_version": 1,
            "repository_identity": repository_identity,
            "repository_id": repository_id,
            "default_branch": default_branch,
            "project_endpoint": sidecar.foundry_project.project_endpoint,
            "foundry_account_resource_id": (
                sidecar.foundry_project.account_resource_id
            ),
            "agent_name": sidecar.foundry_project.agent_name,
            "authentication_method": "oidc",
            "static_credentials_allowed": False,
            "hosted_runtime": {
                "kind": "hosted",
                "runtime": sidecar.runtime.runtime,
                "entry_point": sidecar.runtime.entrypoint,
                "dependency_resolution": sidecar.runtime.dependency_resolution,
                "protocol_name": sidecar.runtime.protocol_name,
                "protocol_version": sidecar.runtime.protocol_version,
                "cpu": sidecar.runtime.cpu or "1",
                "memory": sidecar.runtime.memory or "2Gi",
                "model_environment_variable": (
                    sidecar.runtime.model_environment_variable
                    or "AZURE_AI_MODEL_DEPLOYMENT_NAME"
                ),
            },
            "oidc": {
                "issuer": "https://token.actions.githubusercontent.com",
                "audience": "api://AzureADTokenExchange",
                "tenant_id": tenant_id,
                "subscription_id": subscription_id,
                "repository_id_claim": str(repository_id),
                "workflow_variables": (
                    {
                        "alias": "deployment",
                        "name": client_id_variable,
                        "value": client_id,
                        "scope": "environment",
                        "environment": deployment_environment,
                    },
                ),
                "principals": (
                    {
                        "role": "deployment",
                        "client_id": client_id,
                        "client_id_variable": "deployment",
                        "environment": deployment_environment,
                        "subjects": (
                            {
                                "name": "environment",
                                "subject": subject,
                                "environment": deployment_environment,
                            },
                        ),
                    },
                ),
            },
            "model_deployments": (
                {
                    "alias": sidecar.baseline_model,
                    "deployment_name": sidecar.baseline_model,
                    "model_format": "OpenAI",
                    "model_name": sidecar.baseline_model,
                    "model_version": "pinned",
                    "required_capabilities": (
                        {"name": "responses", "enabled": True},
                    ),
                },
            ),
            "development_evaluation": {
                "name": "development",
                "split": "development",
                "resolved_evaluation_id": development_definition.definition_id,
                "dataset_id": development_dataset.dataset_id,
                "custom_evaluator_ids": evaluator_ids,
            },
            "validating_evaluation": {
                "name": "validating",
                "split": "validating",
                "resolved_evaluation_id": validating_definition.definition_id,
                "dataset_id": validating_dataset.dataset_id,
                "custom_evaluator_ids": evaluator_ids,
            },
        }
    )


def _required_environment_value(
    environment: Mapping[str, str],
    name: str,
) -> str:
    value = environment.get(name)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeIntegrationError(f"{name} is required")
    return value.strip()


def _subscription_id(resource_id: str) -> str:
    parts = [part for part in resource_id.split("/") if part]
    for index, part in enumerate(parts[:-1]):
        if part.casefold() == "subscriptions":
            return parts[index + 1]
    raise RuntimeIntegrationError(
        "Foundry account resource id omitted the subscription id"
    )


def _registered_oidc_subject_prefix(
    registry: RootRegistry,
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


def _normalized_text_bytes(value: bytes) -> bytes:
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeIntegrationError(
            "registered deployment contract is not UTF-8"
        ) from error
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def _repository_root(repository: Path) -> Path:
    try:
        resolved = Path(repository).resolve(strict=True)
    except OSError as error:
        raise RuntimeIntegrationError("repository could not be resolved") from error
    discovered = _git_text(resolved, "rev-parse", "--show-toplevel")
    root = Path(discovered).resolve(strict=True)
    if root != resolved:
        raise RuntimeIntegrationError("repository must be the Git worktree root")
    return root


def _resolve_release_commit(repository: Path, requested: str | None) -> str:
    head = _git_text(repository, "rev-parse", "HEAD")
    selected = head if requested is None else requested
    if not _valid_commit(selected):
        raise RuntimeIntegrationError("release_commit must be a lowercase Git object ID")
    resolved = _git_text(repository, "rev-parse", "--verify", f"{selected}^{{commit}}")
    if resolved != selected:
        raise RuntimeIntegrationError("release_commit did not resolve to the exact commit")
    if head != selected:
        raise RuntimeIntegrationError(
            "repository HEAD does not match the requested release commit"
        )
    return selected


def _valid_commit(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) in {40, 64}
        and all(character in "0123456789abcdef" for character in value)
    )


def _deployment_artifact_root(
    repository: Path,
    *,
    environment: Mapping[str, str],
    explicit: Path | None,
) -> Path:
    if explicit is not None:
        value = explicit
    elif environment.get(DEPLOYMENT_ROOT_ENV):
        value = Path(environment[DEPLOYMENT_ROOT_ENV])
    elif environment.get("RUNNER_TEMP"):
        value = Path(environment["RUNNER_TEMP"]) / "foundry-opt-deploy"
    else:
        value = repository.parent / ".foundry-opt-deploy"
    try:
        resolved = value.resolve(strict=False)
        resolved.mkdir(parents=True, exist_ok=True)
        resolved = resolved.resolve(strict=True)
    except OSError as error:
        raise RuntimeIntegrationError(
            "deployment artifact root is unavailable"
        ) from error
    if resolved == repository or resolved.is_relative_to(repository):
        raise RuntimeIntegrationError(
            "deployment artifact root must live outside the repository"
        )
    return resolved


def _existing_file(path: Path, *, field: str) -> Path:
    try:
        resolved = Path(path).resolve(strict=True)
    except OSError as error:
        raise RuntimeIntegrationError(f"{field} could not be resolved") from error
    if not resolved.is_file():
        raise RuntimeIntegrationError(f"{field} must be a file")
    return resolved


def _required_environment_path(
    environment: Mapping[str, str],
    name: str,
) -> Path:
    value = environment.get(name)
    if not value:
        raise RuntimeIntegrationError(f"{name} is required")
    return Path(value)


def _validate_bootstrap_receipt(
    pin: SharedPin,
    receipt: BootstrapReceipt,
) -> None:
    expected = (
        (receipt.repository, pin.repository_url, "repository"),
        (receipt.commit, pin.commit, "commit"),
        (receipt.package_path, pin.package_path, "package_path"),
        (receipt.skill_path, pin.skill_path, "skill_path"),
        (receipt.lock_sha256, pin.uv_lock_sha256, "lock_sha256"),
    )
    for actual, trusted, field in expected:
        if actual != trusted:
            raise RuntimeIntegrationError(
                f"bootstrap receipt {field} does not match the shared pin"
            )


def _validate_repository_environment(
    metadata: AgentMetadata,
    environment: Mapping[str, str],
) -> None:
    repository = environment.get("GITHUB_REPOSITORY")
    if repository is not None and repository != metadata.repository_identity:
        raise RuntimeIntegrationError(
            "GitHub repository does not match trusted agent metadata"
        )
    repository_id = environment.get("GITHUB_REPOSITORY_ID")
    if repository_id is not None and repository_id != str(metadata.repository_id):
        raise RuntimeIntegrationError(
            "GitHub repository ID does not match trusted agent metadata"
        )


def _git_text(repository: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            env=_git_environment(),
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeIntegrationError("git command timed out") from error
    except OSError as error:
        raise RuntimeIntegrationError("git could not be executed") from error
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeIntegrationError(
            f"git command failed: {detail or 'unknown failure'}"
        )
    return completed.stdout.rstrip("\n")


def _git_bytes(repository: Path, *arguments: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
            env=_git_environment(),
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeIntegrationError("git command timed out") from error
    except OSError as error:
        raise RuntimeIntegrationError("git could not be executed") from error
    if completed.returncode != 0:
        raise RuntimeIntegrationError("git command failed")
    return completed.stdout


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_CONFIG",
        "PYTHONHOME",
        "PYTHONPATH",
    ):
        environment.pop(key, None)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    return environment


def _finite_positive(value: object, subject: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{subject} must be numeric")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{subject} must be positive and finite")
    return number
