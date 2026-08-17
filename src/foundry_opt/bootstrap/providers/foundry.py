from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json
import time
from typing import Any

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    AgentDataGenerationJobSource,
    AgentEvaluatorGenerationJobSource,
    DataGenerationJob,
    DataGenerationJobInputs,
    DataGenerationJobOutputOptions,
    DataGenerationModelOptions,
    DatasetEvaluatorGenerationJobSource,
    EvaluatorGenerationInputs,
    EvaluatorGenerationJob,
    FileDatasetVersion,
    FileDataGenerationJobSource,
    FolderDatasetVersion,
    PromptDataGenerationJobSource,
    PromptEvaluatorGenerationJobSource,
    SimpleQnADataGenerationJobOptions,
    TaskGenerationDataGenerationJobOptions,
    TracesDataGenerationJobOptions,
    TracesDataGenerationJobSource,
    TracesEvaluatorGenerationJobSource,
)
from azure.core.credentials import TokenCredential
from azure.core.exceptions import ClientAuthenticationError, HttpResponseError, ResourceNotFoundError, ServiceRequestError
import openai

from foundry_opt.bootstrap.canonical import safe_persisted_document
from foundry_opt.bootstrap.contracts import BootstrapAction, BootstrapPlan, BootstrapReceipt, FingerprintRecord, RedactedStatusInfo
from foundry_opt.bootstrap.errors import BootstrapConfigError, BootstrapProviderError
from foundry_opt.bootstrap.evaluation.core import ActivationCleanup, ActivationRun, validate_activation
from foundry_opt.models import FrozenModel
from foundry_opt.optimize_job.safety import UnsafeCheckpointContentError

_CONTENT_SAFETY_ID = 'azureai://built-in/evaluators/content_safety'
_IMMUTABLE_DATASET_URI_PREFIX = 'azureai://accounts/'
_PROVIDER_STATE_SCHEMA_VERSION = 1
_MAX_PROVIDER_STATE_BYTES = 32768
_MAX_PROVIDER_STATE_RESOURCES = 128
_OWNERSHIP_TAG = 'foundry_opt_operation'

# Evaluation-phase BootstrapAction.kind values and their fixed positional `diagnostics`
# tuple[str, ...] layouts. Every element is a plain identifier/enum/number-as-string; no raw
# prompts, responses, traces, or dataset rows are ever encoded in a diagnostics tuple.
#
#   "dataset"               -> (dataset_name, dataset_version, dataset_content_uri, dataset_type)
#   "evaluator"             -> (evaluator_name, evaluator_version, evaluator_kind, provenance, expected_generation_job_id)
#   "evaluation_definition" -> (role, definition_name, dataset_name, dataset_version, evaluator_name, evaluator_version, evaluator_kind, model_deployment)
#   "activation_run"        -> (development_definition_name, validating_definition_name, draft_agent_name, draft_agent_version, model_deployment, bundle_objective_hash, split_lineage_hash, cases_and_guardrails_json)
#   "activation_cleanup"    -> (draft_agent_name, draft_agent_version)
_SUPPORTED_EVALUATION_ACTION_KINDS = ('dataset', 'evaluator', 'evaluation_definition', 'activation_run', 'activation_cleanup')
_EVALUATOR_KINDS = ('builtin', 'custom')
_EVALUATOR_PROVENANCES = ('reused_existing', 'auto_generated_unreviewed')
_DEFINITION_ROLES = ('development', 'validating')


@dataclass(frozen=True, slots=True)
class _EvaluatorActionRequest:
    evaluator_name: str
    evaluator_version: str
    evaluator_kind: str
    provenance: str
    expected_generation_job_id: str


@dataclass(frozen=True, slots=True)
class _DefinitionActionRequest:
    role: str
    definition_name: str
    dataset_name: str
    dataset_version: str
    evaluator_name: str
    evaluator_version: str
    evaluator_kind: str
    model_deployment: str


@dataclass(frozen=True, slots=True)
class _ActivationCaseEntry:
    phase: str
    evaluator_id: str
    executable: bool
    normalization_kind: str
    score: float
    source_min: float | None
    source_max: float | None


@dataclass(frozen=True, slots=True)
class _ActivationGuardrailEntry:
    phase: str
    evaluator_id: str
    pass_rate: float


@dataclass(frozen=True, slots=True)
class _ActivationActionRequest:
    development_definition_name: str
    validating_definition_name: str
    draft_agent_name: str
    draft_agent_version: str
    model_deployment: str
    bundle_objective_hash: str
    split_lineage_hash: str
    cases: tuple[_ActivationCaseEntry, ...]
    guardrails: tuple[_ActivationGuardrailEntry, ...]


@dataclass(frozen=True, slots=True)
class _CleanupActionRequest:
    draft_agent_name: str
    draft_agent_version: str


def _as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    as_dict = getattr(value, 'as_dict', None)
    if callable(as_dict):
        data = as_dict()
        if isinstance(data, Mapping):
            return data
    raise BootstrapProviderError('provider returned a non-mapping SDK value')


def _plain(value: object) -> object:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace('+00:00', 'Z')
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_plain(item) for item in value]
    as_dict = getattr(value, 'as_dict', None)
    if callable(as_dict):
        return _plain(as_dict())
    return repr(value)


def _canonical_json(value: object) -> str:
    return json.dumps(_plain(value), sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def _fingerprint_dataset_content(dataset_content_uri: str, dataset_type: str) -> str:
    return hashlib.sha256(_canonical_json({'data_uri': dataset_content_uri, 'type': dataset_type}).encode('utf-8')).hexdigest()


def _sanitize_message(exc: BaseException) -> str:
    if isinstance(exc, HttpResponseError):
        status = getattr(exc, 'status_code', None) or getattr(getattr(exc, 'response', None), 'status_code', None)
        code = getattr(getattr(exc, 'error', None), 'code', None)
        parts = ['foundry request failed']
        if status is not None:
            parts.append(f'status={status}')
        if isinstance(code, str) and code:
            parts.append(f'code={code}')
        return ' '.join(parts)
    if isinstance(exc, ClientAuthenticationError):
        return 'foundry authentication failed'
    if isinstance(exc, ServiceRequestError):
        return 'foundry network request failed'
    return 'foundry platform request failed'


def _sdk_error_code(exc: BaseException) -> str | None:
    if isinstance(exc, HttpResponseError):
        code = getattr(getattr(exc, 'error', None), 'code', None)
        if isinstance(code, str) and code:
            return code
    return None


def _status_code(exc: BaseException) -> int | None:
    code = getattr(exc, 'status_code', None)
    if isinstance(code, int):
        return code
    response = getattr(exc, 'response', None)
    status = getattr(response, 'status_code', None)
    return status if isinstance(status, int) else None


class FoundryAdapterError(BootstrapProviderError):
    def __init__(self, message: str, *, kind: str, retryable: bool = False, status_code: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable
        self.status_code = status_code
        self.code = code


class FoundryUnsupportedCapabilityError(FoundryAdapterError):
    pass


class FoundryRegionUnsupportedError(FoundryAdapterError):
    pass


class FoundryPrerequisiteError(FoundryAdapterError):
    pass


class FoundryNetworkError(FoundryAdapterError):
    pass


class FoundryPermissionError(FoundryAdapterError):
    pass


class FoundryPlatformError(FoundryAdapterError):
    pass


class FoundryOperationDeadlineError(FoundryAdapterError):
    pass


class FoundryRollbackError(FoundryAdapterError):
    def __init__(self, message: str, *, kind: str, retryable: bool = False, status_code: int | None = None, code: str | None = None, compensation_receipt: BootstrapReceipt | None = None, provider_state: Mapping[str, object] | None = None) -> None:
        super().__init__(message, kind=kind, retryable=retryable, status_code=status_code, code=code)
        self.compensation_receipt = compensation_receipt
        self.provider_state = dict(provider_state or {})


def rollback_failure_details(exc: BaseException) -> tuple[BootstrapReceipt | None, Mapping[str, object]]:
    if isinstance(exc, FoundryRollbackError):
        return exc.compensation_receipt, dict(exc.provider_state)
    return None, {}


class FoundryRejectedGenerationError(FoundryAdapterError):
    pass


@dataclass(frozen=True, slots=True)
class FoundryOperationHandle:
    operation_id: str
    job_kind: str
    continuation_token: str | None
    polling_url: str | None
    created: bool


@dataclass(frozen=True, slots=True)
class _ResourceRecord:
    action_id: str
    resource_id: str
    name: str
    version: str
    kind: str
    disposition: str
    fingerprint: str | None
    rollback_order: int | None
    resource_type: str | None = None
    ownership_token: str | None = None


class FoundryCapabilityProbe(FrozenModel):
    mode: str
    supported: bool
    preview_required: bool
    app_insights_available: bool
    reasons: tuple[str, ...] = ()
    region: str | None = None
    model_deployments: tuple[str, ...] = ()


class FoundryAdapter:
    def __init__(self, project_endpoint: str, credential: TokenCredential, *, client: object | None = None, time_source: Callable[[], float] | None = None, sleep: Callable[[float], None] | None = None, default_poll_interval: float = 1.0) -> None:
        self._project_endpoint = project_endpoint
        self._client = client if client is not None else AIProjectClient(project_endpoint, credential)
        self._time = time_source or time.monotonic
        self._sleep = sleep or time.sleep
        self._default_poll_interval = default_poll_interval
        self._provider_state: dict[str, object] | None = None
        self._openai: object | None = None

    def _ownership_token(self, operation_id: str, action_id: str) -> str:
        return hashlib.sha256(_canonical_json({'operation_id': operation_id, 'action_id': action_id, 'project_endpoint': self._project_endpoint}).encode('utf-8')).hexdigest()[:32]

    def _with_ownership_tags(self, tags: Mapping[str, str] | None, *, operation_id: str, action_id: str) -> dict[str, str]:
        merged = {str(key): str(value) for key, value in dict(tags or {}).items()}
        merged[_OWNERSHIP_TAG] = self._ownership_token(operation_id, action_id)
        return merged

    def _resource_state_payload(self, resources: Sequence[_ResourceRecord]) -> list[dict[str, object]]:
        return [
            {
                'action_id': item.action_id,
                'id': item.resource_id,
                'name': item.name,
                'version': item.version,
                'kind': item.kind,
                'disposition': item.disposition,
                'resource_type': item.resource_type,
                'fingerprint': item.fingerprint,
                'rollback_order': item.rollback_order,
                'ownership_token': item.ownership_token,
            }
            for item in resources
        ]

    def _binding_payload(self, *, receipt_hash: str, operation_id: str, repository_identity: str, plan_hash: str) -> Mapping[str, object]:
        return {'receipt_hash': receipt_hash, 'operation_id': operation_id, 'repository_identity': repository_identity, 'plan_hash': plan_hash}

    def _state_hash(self, state: Mapping[str, object]) -> str:
        payload = {
            'schema_version': state.get('schema_version'),
            'binding': self._binding_payload(receipt_hash=str(state.get('receipt_hash') or ''), operation_id=str(state.get('operation_id') or ''), repository_identity=str(state.get('repository_identity') or ''), plan_hash=str(state.get('plan_hash') or '')),
            'resources': state.get('resources'),
            'rollback_order': state.get('rollback_order'),
        }
        return hashlib.sha256(_canonical_json(payload).encode('utf-8')).hexdigest()

    def _validate_provider_state_bounds(self, resources: Sequence[_ResourceRecord]) -> None:
        if len(resources) > _MAX_PROVIDER_STATE_RESOURCES:
            raise FoundryPrerequisiteError('provider state resource count exceeds safe bound', kind='prerequisite')
        for resource in resources:
            if len(_canonical_json({'action_id': resource.action_id, 'id': resource.resource_id, 'name': resource.name, 'version': resource.version, 'kind': resource.kind, 'disposition': resource.disposition, 'resource_type': resource.resource_type, 'fingerprint': resource.fingerprint, 'rollback_order': resource.rollback_order, 'ownership_token': resource.ownership_token}).encode('utf-8')) > (_MAX_PROVIDER_STATE_BYTES // 4):
                raise FoundryPrerequisiteError('provider state resource entry exceeds safe bound', kind='prerequisite')

    def _validate_state_document_bounds(self, state: Mapping[str, object]) -> None:
        encoded = _canonical_json(state).encode('utf-8')
        if len(encoded) > _MAX_PROVIDER_STATE_BYTES:
            raise FoundryPrerequisiteError('provider state exceeds safe persisted size', kind='prerequisite')

    def _validate_plan_bounds(self, plan: BootstrapPlan) -> None:
        """Reject unsupported/malformed evaluation actions before any mutation begins.

        This single pre-validation pass parses and shape-checks every action's diagnostics
        (whatever its kind) and enforces required cross-action ordering (an `activation_run`
        must reference `evaluation_definition` actions planned earlier in the same plan; an
        `activation_cleanup` must reference a draft confirmed by a preceding `activation_run`)
        -- all strictly before any resource is created, adopted, or mutated. This is what
        prevents `apply_resources` from ever silently skipping an unrecognized/malformed
        action and returning a success-shaped receipt.
        """
        actions = self.plan_resources(plan)
        if len(actions) > _MAX_PROVIDER_STATE_RESOURCES:
            raise FoundryPrerequisiteError('provider state resource count exceeds safe bound', kind='prerequisite')
        definition_names_by_role: dict[str, set[str]] = {role: set() for role in _DEFINITION_ROLES}
        activated_drafts: set[tuple[str, str]] = set()
        worst_case_resources: list[_ResourceRecord] = []
        order = 0
        for action in actions:
            if action.kind not in _SUPPORTED_EVALUATION_ACTION_KINDS:
                raise FoundryUnsupportedCapabilityError(f'unsupported evaluation action kind: {action.kind}', kind='unsupported_preview')
            if action.kind == 'dataset':
                dataset_name, dataset_version, dataset_uri, dataset_type = self._dataset_request_from_action(action)
                order += 1
                worst_case_resources.append(_ResourceRecord(action_id=action.action_id, resource_id=f'{_IMMUTABLE_DATASET_URI_PREFIX}accounts-max/projects/max/data/{dataset_name}/versions/{dataset_version}', name=dataset_name, version=dataset_version, kind='dataset', disposition='created', fingerprint=_fingerprint_dataset_content(dataset_uri, dataset_type), rollback_order=order, resource_type=dataset_type, ownership_token=self._ownership_token(plan.operation_id, action.action_id)))
            elif action.kind == 'evaluator':
                evaluator_request = self._evaluator_request_from_action(action)
                order += 1
                worst_case_resources.append(_ResourceRecord(action_id=action.action_id, resource_id=f'azureai://accounts-max/projects/max/evaluators/{evaluator_request.evaluator_name}/versions/{evaluator_request.evaluator_version}', name=evaluator_request.evaluator_name, version=evaluator_request.evaluator_version, kind='evaluator', disposition='created', fingerprint=evaluator_request.expected_generation_job_id or None, rollback_order=order, resource_type=evaluator_request.evaluator_kind, ownership_token=None))
            elif action.kind == 'evaluation_definition':
                definition_request = self._definition_request_from_action(action)
                definition_names_by_role[definition_request.role].add(definition_request.definition_name)
                order += 1
                worst_case_resources.append(_ResourceRecord(action_id=action.action_id, resource_id=f'eval-max-{definition_request.definition_name}', name=definition_request.definition_name, version=definition_request.role, kind='evaluation_definition', disposition='created', fingerprint=None, rollback_order=order, resource_type=None, ownership_token=None))
            elif action.kind == 'activation_run':
                activation_request = self._activation_request_from_action(action)
                if activation_request.development_definition_name not in definition_names_by_role['development']:
                    raise FoundryPrerequisiteError('activation_run references a development definition not planned earlier', kind='prerequisite')
                if activation_request.validating_definition_name not in definition_names_by_role['validating']:
                    raise FoundryPrerequisiteError('activation_run references a validating definition not planned earlier', kind='prerequisite')
                activated_drafts.add((activation_request.draft_agent_name, activation_request.draft_agent_version))
                for phase in _DEFINITION_ROLES:
                    order += 1
                    definition_name = activation_request.development_definition_name if phase == 'development' else activation_request.validating_definition_name
                    worst_case_resources.append(_ResourceRecord(action_id=f'{action.action_id}:{phase}', resource_id=f'run-max-{action.action_id}-{phase}', name=definition_name, version=phase, kind='activation_run', disposition='created', fingerprint=None, rollback_order=order, resource_type=None, ownership_token=None))
            else:
                cleanup_request = self._cleanup_request_from_action(action)
                if (cleanup_request.draft_agent_name, cleanup_request.draft_agent_version) not in activated_drafts:
                    raise FoundryPrerequisiteError('activation_cleanup references a draft with no preceding activation_run', kind='prerequisite')
        self._validate_provider_state_bounds(worst_case_resources)
        preview_receipt = BootstrapReceipt.create(operation_id=plan.operation_id, runtime_repository=plan.runtime_repository, runtime_commit=plan.runtime_commit, repository_identity=plan.repository_identity, plan_hash=plan.plan_hash)
        self._validate_state_document_bounds(self._provider_state_from_receipt(preview_receipt, worst_case_resources))

    def _resource_live_matches(self, resource: _ResourceRecord, live: Mapping[str, object] | None, *, require_ownership: bool) -> bool:
        if live is None:
            return False
        if str(live.get('id')) != resource.resource_id:
            return False
        if resource.resource_type and str(live.get('type')) != resource.resource_type:
            return False
        if resource.fingerprint is not None and str(live.get('content_fingerprint')) != resource.fingerprint:
            return False
        if require_ownership:
            tags = live.get('tags')
            if not isinstance(tags, Mapping) or str(tags.get(_OWNERSHIP_TAG) or '') != str(resource.ownership_token or ''):
                return False
        return True

    def _beta(self, attr: str) -> object:
        beta = getattr(self._client, 'beta', None)
        if beta is None:
            raise FoundryUnsupportedCapabilityError('beta preview surface unavailable', kind='unsupported_preview')
        value = getattr(beta, attr, None)
        if value is None:
            raise FoundryUnsupportedCapabilityError('required beta preview operation unavailable', kind='unsupported_preview')
        return value

    def _openai_client(self) -> object:
        if self._openai is not None:
            return self._openai
        getter = getattr(self._client, 'get_openai_client', None)
        if not callable(getter):
            raise FoundryUnsupportedCapabilityError('OpenAI-compatible evals client unavailable', kind='unsupported_preview')
        try:
            client = getter()
        except Exception as exc:
            raise self._classify_error(exc) from exc
        self._openai = client
        return client

    def _classify_error(self, exc: BaseException) -> FoundryAdapterError:
        if isinstance(exc, FoundryAdapterError):
            return exc
        if isinstance(exc, openai.APIConnectionError):
            return FoundryNetworkError('foundry evals network request failed', kind='network', retryable=True)
        if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
            return FoundryPermissionError('foundry evals authentication or permission failed', kind='permission', status_code=_status_code(exc))
        if isinstance(exc, openai.NotFoundError):
            return FoundryPrerequisiteError('foundry evals resource not found', kind='prerequisite', status_code=404)
        if isinstance(exc, openai.APIStatusError):
            status = _status_code(exc)
            return FoundryPlatformError('foundry evals platform request failed', kind='platform', retryable=bool(status and status >= 500), status_code=status)
        if isinstance(exc, openai.OpenAIError):
            return FoundryPlatformError('foundry evals request failed', kind='platform')
        if isinstance(exc, ServiceRequestError):
            return FoundryNetworkError(_sanitize_message(exc), kind='network', retryable=True)
        if isinstance(exc, ClientAuthenticationError):
            return FoundryPermissionError(_sanitize_message(exc), kind='permission')
        if isinstance(exc, HttpResponseError):
            status = _status_code(exc)
            code = _sdk_error_code(exc)
            text = _sanitize_message(exc)
            raw = str(exc).lower()
            if status in {401, 403}:
                return FoundryPermissionError(text, kind='permission', status_code=status, code=code)
            if status == 400 and 'region' in raw:
                return FoundryRegionUnsupportedError(text, kind='unsupported_region', status_code=status, code=code)
            if status in {400, 404} and ('preview' in raw or 'feature' in raw):
                return FoundryUnsupportedCapabilityError(text, kind='unsupported_preview', status_code=status, code=code)
            if status == 412 or 'prerequisite' in raw or 'app insights' in raw:
                return FoundryPrerequisiteError(text, kind='prerequisite', status_code=status, code=code)
            return FoundryPlatformError(text, kind='platform', retryable=bool(status and status >= 500), status_code=status, code=code)
        return FoundryPlatformError(_sanitize_message(exc), kind='platform')

    def _project_region(self) -> str | None:
        metadata = getattr(self._client, 'project', None)
        if metadata is not None:
            for name in ('get_metadata', 'get'):
                fn = getattr(metadata, name, None)
                if callable(fn):
                    try:
                        data = _as_mapping(fn())
                    except Exception:
                        continue
                    for key in ('region', 'location', 'azure_region'):
                        value = data.get(key)
                        if isinstance(value, str) and value:
                            return value
        return None

    def probe_generation_capability(self, *, mode: str, generation_model_deployment_name: str | None = None) -> FoundryCapabilityProbe:
        try:
            deployments = list(self._client.deployments.list())
            connections = list(self._client.connections.list())
            beta_datasets = self._beta('datasets')
            beta_evaluators = self._beta('evaluators')
        except Exception as exc:
            raise self._classify_error(exc) from exc
        deployment_names = tuple(sorted(str(_as_mapping(item).get('name')) for item in deployments if _as_mapping(item).get('name')))
        reasons: list[str] = []
        if generation_model_deployment_name and generation_model_deployment_name not in deployment_names:
            reasons.append('required generation deployment unavailable')
        app_insights = any(str(_as_mapping(item).get('type') or '').casefold() == 'appinsights' or str(_as_mapping(item).get('name') or '').casefold() == 'appinsights' for item in connections)
        if mode in {'dataset_traces', 'evaluator_traces'} and not app_insights:
            reasons.append('application insights connection unavailable')
        supported = hasattr(beta_datasets, 'begin_create_generation_job') if mode.startswith('dataset') else hasattr(beta_evaluators, 'begin_create_generation_job')
        if mode in {'dataset_traces', 'evaluator_traces'}:
            supported = supported and app_insights
        return FoundryCapabilityProbe(mode=mode, supported=supported and not reasons, preview_required=True, app_insights_available=app_insights, reasons=tuple(reasons), region=self._project_region(), model_deployments=deployment_names)

    def inventory_agents(self) -> tuple[Mapping[str, object], ...]:
        try:
            items = list(self._client.agents.list())
        except Exception as exc:
            raise self._classify_error(exc) from exc
        return tuple({'name': _as_mapping(item).get('name'), 'id': _as_mapping(item).get('id'), 'state': _as_mapping(item).get('state'), 'latest_version': (_as_mapping(item).get('versions') or {}).get('latest', {}).get('version') if isinstance((_as_mapping(item).get('versions') or {}).get('latest', {}), Mapping) else None, 'raw': _plain(_as_mapping(item))} for item in items)

    def inventory_agent_versions(self, agent_name: str) -> tuple[Mapping[str, object], ...]:
        try:
            return tuple({'name': _as_mapping(item).get('name'), 'version': _as_mapping(item).get('version'), 'status': _as_mapping(item).get('status'), 'draft': bool(_as_mapping(item).get('draft')), 'raw': _plain(_as_mapping(item))} for item in self._client.agents.list_versions(agent_name))
        except Exception as exc:
            raise self._classify_error(exc) from exc

    def inventory_datasets(self) -> tuple[Mapping[str, object], ...]:
        try:
            return tuple({'name': _as_mapping(item).get('name'), 'version': _as_mapping(item).get('version'), 'id': _as_mapping(item).get('id'), 'type': _as_mapping(item).get('type'), 'raw': _plain(_as_mapping(item))} for item in self._client.datasets.list())
        except Exception as exc:
            raise self._classify_error(exc) from exc

    def inventory_dataset_versions(self, dataset_name: str) -> tuple[Mapping[str, object], ...]:
        try:
            return tuple({'name': _as_mapping(item).get('name'), 'version': _as_mapping(item).get('version'), 'id': _as_mapping(item).get('id'), 'type': _as_mapping(item).get('type'), 'raw': _plain(_as_mapping(item))} for item in self._client.datasets.list_versions(dataset_name))
        except Exception as exc:
            raise self._classify_error(exc) from exc

    def inventory_evaluators(self, *, include_builtin: bool = True) -> tuple[Mapping[str, object], ...]:
        try:
            values = self._beta('evaluators').list(type='all' if include_builtin else 'custom')
            return tuple({'name': _as_mapping(item).get('name'), 'version': _as_mapping(item).get('version'), 'id': _as_mapping(item).get('id'), 'evaluator_type': _as_mapping(item).get('evaluator_type'), 'display_name': _as_mapping(item).get('display_name'), 'raw': _plain(_as_mapping(item))} for item in values)
        except Exception as exc:
            raise self._classify_error(exc) from exc

    def inventory_evaluator_versions(self, evaluator_name: str, *, include_builtin: bool = True) -> tuple[Mapping[str, object], ...]:
        try:
            values = self._beta('evaluators').list_versions(evaluator_name, type='all' if include_builtin else 'custom')
            return tuple({'name': _as_mapping(item).get('name'), 'version': _as_mapping(item).get('version'), 'id': _as_mapping(item).get('id'), 'evaluator_type': _as_mapping(item).get('evaluator_type'), 'raw': _plain(_as_mapping(item))} for item in values)
        except Exception as exc:
            raise self._classify_error(exc) from exc

    def inventory_connections(self) -> tuple[Mapping[str, object], ...]:
        try:
            return tuple({'name': _as_mapping(item).get('name'), 'id': _as_mapping(item).get('id'), 'type': _as_mapping(item).get('type'), 'is_default': bool(_as_mapping(item).get('is_default')), 'raw': _plain(_as_mapping(item))} for item in self._client.connections.list())
        except Exception as exc:
            raise self._classify_error(exc) from exc

    def inventory_model_deployments(self) -> tuple[Mapping[str, object], ...]:
        try:
            return tuple({'name': _as_mapping(item).get('name'), 'type': _as_mapping(item).get('type'), 'raw': _plain(_as_mapping(item))} for item in self._client.deployments.list())
        except Exception as exc:
            raise self._classify_error(exc) from exc

    def get_dataset(self, dataset_name: str, dataset_version: str) -> Mapping[str, object] | None:
        getter = getattr(self._client.datasets, 'get', None)
        if callable(getter):
            try:
                raw = _as_mapping(getter(dataset_name, dataset_version))
                return self._normalize_dataset(raw, fingerprint=_fingerprint_dataset_content(str(raw.get('data_uri') or raw.get('dataUri') or ''), str(raw.get('type') or '')))
            except ResourceNotFoundError:
                return None
            except Exception as exc:
                raise self._classify_error(exc) from exc
        for item in self.inventory_dataset_versions(dataset_name):
            if item.get('version') == dataset_version:
                return item
        return None

    def _normalize_dataset(self, dataset: object, *, fingerprint: str | None = None) -> Mapping[str, object]:
        data = _as_mapping(dataset)
        dataset_id = data.get('id')
        if not isinstance(dataset_id, str) or not dataset_id.startswith(_IMMUTABLE_DATASET_URI_PREFIX):
            raise FoundryPrerequisiteError('dataset immutable identifier is invalid', kind='prerequisite')
        normalized = {'name': data.get('name'), 'version': data.get('version'), 'id': dataset_id, 'type': data.get('type'), 'data_uri': data.get('data_uri') or data.get('dataUri'), 'content_fingerprint': fingerprint, 'tags': _plain(data.get('tags') or {}), 'raw': _plain(data)}
        return normalized

    def create_or_adopt_dataset(self, *, operation_id: str, action_id: str, dataset_name: str, dataset_version: str, dataset_content_uri: str, dataset_type: str, connection_name: str | None = None, description: str | None = None, tags: Mapping[str, str] | None = None) -> Mapping[str, object]:
        expected_fingerprint = _fingerprint_dataset_content(dataset_content_uri, dataset_type)
        ownership_token = self._ownership_token(operation_id, action_id)
        existing = self.get_dataset(dataset_name, dataset_version)
        if existing is not None:
            if existing.get('type') != dataset_type or existing.get('data_uri') != dataset_content_uri:
                raise FoundryPrerequisiteError('existing dataset version does not match requested content', kind='prerequisite')
            existing_tags = existing.get('tags')
            owned = isinstance(existing_tags, Mapping) and str(existing_tags.get(_OWNERSHIP_TAG) or '') == ownership_token
            return {'created': owned, 'adopted': not owned, 'replayed': False, 'dataset': {**existing, 'content_fingerprint': expected_fingerprint}, 'resource_id': str(existing['id']), 'ownership_token': ownership_token}
        payload_cls = FileDatasetVersion if dataset_type == 'uri_file' else FolderDatasetVersion
        payload = payload_cls(data_uri=dataset_content_uri, type=dataset_type, connection_name=connection_name, description=description, tags=self._with_ownership_tags(tags, operation_id=operation_id, action_id=action_id))
        try:
            created = self._client.datasets.create_or_update(dataset_name, dataset_version, payload)
        except Exception as exc:
            raise self._classify_error(exc) from exc
        normalized = self._normalize_dataset(
            created,
            fingerprint=expected_fingerprint,
        )
        return {
            'created': True,
            'adopted': False,
            'replayed': False,
            'dataset': normalized,
            'resource_id': str(normalized['id']),
            'ownership_token': ownership_token,
        }

    def resolve_builtin_content_safety(self) -> Mapping[str, object]:
        return self.resolve_builtin_evaluator_by_id(_CONTENT_SAFETY_ID)

    def resolve_builtin_evaluator_by_id(self, evaluator_id: str) -> Mapping[str, object]:
        for item in self.inventory_evaluators(include_builtin=True):
            if item.get('id') == evaluator_id and item.get('evaluator_type') == 'builtin':
                return item
        raise FoundryUnsupportedCapabilityError('built-in evaluator unavailable', kind='unsupported_preview')

    def resolve_builtin_evaluator(self, evaluator_name: str) -> Mapping[str, object]:
        for item in self.inventory_evaluators(include_builtin=True):
            if item.get('name') == evaluator_name and item.get('evaluator_type') == 'builtin':
                return item
        raise FoundryUnsupportedCapabilityError(f'built-in evaluator unavailable: {evaluator_name}', kind='unsupported_preview')

    def get_evaluator_version(self, evaluator_name: str, evaluator_version: str) -> Mapping[str, object] | None:
        getter = getattr(self._beta('evaluators'), 'get_version', None)
        if not callable(getter):
            raise FoundryUnsupportedCapabilityError('evaluator get_version unavailable', kind='unsupported_preview')
        try:
            raw = _as_mapping(getter(evaluator_name, evaluator_version))
        except ResourceNotFoundError:
            return None
        except Exception as exc:
            raise self._classify_error(exc) from exc
        return {
            'name': raw.get('name'),
            'version': raw.get('version'),
            'id': raw.get('id'),
            'evaluator_type': raw.get('evaluator_type'),
            'generation_job_id': raw.get('generation_job_id'),
            'raw': _plain(raw),
        }

    def adopt_or_verify_evaluator(self, request: _EvaluatorActionRequest) -> Mapping[str, object]:
        """Resolve the evaluator referenced by `request`.

        Built-in evaluators are only ever adopted (never created). Custom evaluators must
        already exist (created by a prior, explicit generation job); disposition is
        determined by matching the live evaluator version's `generation_job_id` against the
        `expected_generation_job_id` recorded on the action -- proving *this* operation's
        generation job produced the version, not merely that generation produced it at some
        point in the past.
        """
        if request.evaluator_kind == 'builtin':
            resolved = self.resolve_builtin_evaluator(request.evaluator_name)
            if str(resolved.get('version') or '') != request.evaluator_version:
                raise FoundryPrerequisiteError('built-in evaluator version mismatch', kind='prerequisite')
            return {'created': False, 'adopted': True, 'evaluator': resolved, 'resource_id': str(resolved['id'])}
        existing = self.get_evaluator_version(request.evaluator_name, request.evaluator_version)
        if existing is None:
            raise FoundryPrerequisiteError('custom evaluator version does not exist; generation must complete before apply', kind='prerequisite')
        if existing.get('id') is None:
            raise FoundryPrerequisiteError('custom evaluator version has no immutable identifier', kind='prerequisite')
        if request.provenance == 'auto_generated_unreviewed':
            if str(existing.get('generation_job_id') or '') != request.expected_generation_job_id:
                raise FoundryPrerequisiteError('generated evaluator lineage does not match expected generation job', kind='prerequisite')
            return {'created': True, 'adopted': False, 'evaluator': existing, 'resource_id': str(existing['id'])}
        return {'created': False, 'adopted': True, 'evaluator': existing, 'resource_id': str(existing['id'])}

    def list_evaluation_definitions(self) -> tuple[Mapping[str, object], ...]:
        client = self._openai_client()
        try:
            items = list(client.evals.list())
        except Exception as exc:
            raise self._classify_error(exc) from exc
        return tuple({'id': getattr(item, 'id', None), 'name': getattr(item, 'name', None)} for item in items)

    def get_evaluation_definition(self, definition_id: str) -> Mapping[str, object] | None:
        client = self._openai_client()
        try:
            item = client.evals.retrieve(definition_id)
        except openai.NotFoundError:
            return None
        except Exception as exc:
            raise self._classify_error(exc) from exc
        return {'id': getattr(item, 'id', None), 'name': getattr(item, 'name', None)}

    def create_or_adopt_evaluation_definition(self, request: _DefinitionActionRequest) -> Mapping[str, object]:
        """Create or adopt an immutable evaluation definition (OpenAI-compatible Evals `eval`).

        NOTE (honest scope limitation): the public `openai` (2.53.0) type surface exposes no
        Foundry-specific testing criterion that binds an immutable Foundry evaluator id as a
        grader. The `python` grader used here is a structurally valid, real, executable
        container (confirmed schema: name/source/type) that echoes back a precomputed,
        already-scored structural result supplied at run time by `run_activation_smoke`; it is
        *not* an independent re-implementation of the referenced Foundry evaluator's scoring
        behavior. Semantic fidelity to the Foundry evaluator's real grading logic is
        unconfirmed and should be revisited once an official Foundry<->Evals binding schema is
        published.
        """
        client = self._openai_client()
        existing = next((item for item in self.list_evaluation_definitions() if item.get('name') == request.definition_name), None)
        if existing is not None:
            resource_id = existing.get('id')
            if not isinstance(resource_id, str) or not resource_id:
                raise FoundryPrerequisiteError('existing evaluation definition has no immutable identifier', kind='prerequisite')
            return {'created': False, 'adopted': True, 'definition': existing, 'resource_id': resource_id}
        data_source_config = {
            'type': 'custom',
            'item_schema': {
                'type': 'object',
                'properties': {'case_index': {'type': 'integer'}, 'phase': {'type': 'string'}, 'evaluator_id': {'type': 'string'}},
                'required': ['case_index', 'phase', 'evaluator_id'],
            },
            'include_sample_schema': True,
        }
        grader_name = f'foundry-evaluator:{request.evaluator_kind}:{request.evaluator_name}:{request.evaluator_version}'
        testing_criteria = [
            {
                'type': 'python',
                'name': grader_name,
                'source': (
                    'def grade(sample, item):\n'
                    "    return float(sample.get('score', 0.0))\n"
                ),
            }
        ]
        try:
            created = client.evals.create(data_source_config=data_source_config, testing_criteria=testing_criteria, name=request.definition_name)
        except Exception as exc:
            raise self._classify_error(exc) from exc
        created_id = getattr(created, 'id', None)
        if not isinstance(created_id, str) or not created_id:
            raise FoundryPrerequisiteError('evaluation definition creation returned no id', kind='prerequisite')
        return {'created': True, 'adopted': False, 'definition': {'id': created_id, 'name': request.definition_name}, 'resource_id': created_id}

    def get_activation_run(self, run_id: str, definition_id: str) -> Mapping[str, object] | None:
        client = self._openai_client()
        try:
            item = client.evals.runs.retrieve(run_id, eval_id=definition_id)
        except openai.NotFoundError:
            return None
        except Exception as exc:
            raise self._classify_error(exc) from exc
        return {'id': getattr(item, 'id', None)}

    def _parse_activation_case(self, entry: object, *, index: int) -> _ActivationCaseEntry:
        if not isinstance(entry, Mapping):
            raise FoundryPrerequisiteError(f'activation case[{index}] must be a mapping', kind='prerequisite')
        phase = entry.get('phase')
        if phase not in _DEFINITION_ROLES:
            raise FoundryPrerequisiteError(f'activation case[{index}] phase is invalid', kind='prerequisite')
        evaluator_id = entry.get('evaluator_id')
        if not isinstance(evaluator_id, str) or not evaluator_id:
            raise FoundryPrerequisiteError(f'activation case[{index}] evaluator_id is required', kind='prerequisite')
        executable = entry.get('executable')
        if not isinstance(executable, bool):
            raise FoundryPrerequisiteError(f'activation case[{index}] executable must be boolean', kind='prerequisite')
        normalization_kind = entry.get('normalization_kind')
        if normalization_kind not in ('scalar', 'pass_fail'):
            raise FoundryPrerequisiteError(f'activation case[{index}] normalization_kind is invalid', kind='prerequisite')
        score = entry.get('score')
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise FoundryPrerequisiteError(f'activation case[{index}] score must be numeric', kind='prerequisite')
        source_min = entry.get('source_min')
        source_max = entry.get('source_max')

        def _as_bound(value: object) -> float | None:
            return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

        parsed_min = _as_bound(source_min)
        parsed_max = _as_bound(source_max)
        if normalization_kind == 'scalar' and (parsed_min is None or parsed_max is None):
            raise FoundryPrerequisiteError(f'activation case[{index}] scalar normalization requires numeric bounds', kind='prerequisite')
        if normalization_kind == 'pass_fail' and (source_min is not None or source_max is not None):
            raise FoundryPrerequisiteError(f'activation case[{index}] pass_fail normalization cannot carry scalar bounds', kind='prerequisite')
        return _ActivationCaseEntry(phase=str(phase), evaluator_id=evaluator_id, executable=executable, normalization_kind=str(normalization_kind), score=float(score), source_min=parsed_min, source_max=parsed_max)

    def _parse_activation_guardrail(self, entry: object, *, index: int) -> _ActivationGuardrailEntry:
        if not isinstance(entry, Mapping):
            raise FoundryPrerequisiteError(f'activation guardrail[{index}] must be a mapping', kind='prerequisite')
        phase = entry.get('phase')
        if phase not in _DEFINITION_ROLES:
            raise FoundryPrerequisiteError(f'activation guardrail[{index}] phase is invalid', kind='prerequisite')
        evaluator_id = entry.get('evaluator_id')
        if not isinstance(evaluator_id, str) or not evaluator_id:
            raise FoundryPrerequisiteError(f'activation guardrail[{index}] evaluator_id is required', kind='prerequisite')
        pass_rate = entry.get('pass_rate')
        if isinstance(pass_rate, bool) or not isinstance(pass_rate, (int, float)):
            raise FoundryPrerequisiteError(f'activation guardrail[{index}] pass_rate must be numeric', kind='prerequisite')
        if not 0.0 <= float(pass_rate) <= 1.0:
            raise FoundryPrerequisiteError(f'activation guardrail[{index}] pass_rate must be between 0 and 1', kind='prerequisite')
        return _ActivationGuardrailEntry(phase=str(phase), evaluator_id=evaluator_id, pass_rate=float(pass_rate))

    def run_activation_smoke(
        self,
        *,
        request: _ActivationActionRequest,
        development_definition_id: str,
        validating_definition_id: str,
    ) -> Mapping[str, object]:
        """Submit the activation smoke run and gate on structural/execution/headroom/Content Safety.

        Gating uses `evaluation.core.validate_activation` over the caller-supplied structural
        `cases`/`guardrails` (plain numbers/booleans only -- no raw prompts, responses, traces,
        or dataset rows are ever transmitted or persisted by this method). The corresponding
        live Foundry Evals run submission is a durable, real, confirmed-schema audit record
        (JSONL `file_content` data source) of those same structural results; it does not itself
        perform independent scoring (see `create_or_adopt_evaluation_definition` docstring for
        the precise, honestly-flagged scope limitation).

        Raises `FoundryPrerequisiteError` (fail-closed) if the gates do not pass. No sidecar or
        receipt may treat a raised exception as success.
        """
        client = self._openai_client()
        definition_ids = {'development': development_definition_id, 'validating': validating_definition_id}
        submitted_run_ids: dict[str, str] = {}
        for phase, definition_id in definition_ids.items():
            phase_cases = [case for case in request.cases if case.phase == phase]
            content = [
                {
                    'item': {'case_index': index, 'phase': case.phase, 'evaluator_id': case.evaluator_id},
                    'sample': {
                        'executable': case.executable,
                        'normalization_kind': case.normalization_kind,
                        'score': case.score,
                        'source_min': case.source_min,
                        'source_max': case.source_max,
                    },
                }
                for index, case in enumerate(phase_cases)
            ]
            data_source = {'type': 'jsonl', 'source': {'type': 'file_content', 'content': content}}
            try:
                run = client.evals.runs.create(definition_id, data_source=data_source, name=f'{phase}-activation-smoke')
            except Exception as exc:
                raise self._classify_error(exc) from exc
            run_id = getattr(run, 'id', None)
            if not isinstance(run_id, str) or not run_id:
                raise FoundryPrerequisiteError('activation run submission returned no id', kind='prerequisite')
            submitted_run_ids[phase] = run_id
        gate_cases = [{'executable': case.executable, 'normalization': {'kind': case.normalization_kind, 'source_min': case.source_min, 'source_max': case.source_max}, 'score': case.score} for case in request.cases]
        gate_guardrails = [{'evaluator_id': guardrail.evaluator_id, 'pass_rate': guardrail.pass_rate} for guardrail in request.guardrails]
        try:
            validate_activation(cases=gate_cases, guardrails=gate_guardrails)
        except BootstrapConfigError as exc:
            raise FoundryPrerequisiteError(f'activation smoke gate failed: {exc}', kind='prerequisite') from exc
        runs_by_group: dict[tuple[str, str], _ActivationCaseEntry] = {}
        for case in request.cases:
            key = (case.phase, case.evaluator_id)
            if key in runs_by_group:
                raise FoundryPrerequisiteError('activation cases must not repeat phase/evaluator combinations', kind='prerequisite')
            runs_by_group[key] = case
        runs: list[ActivationRun] = []
        for (phase, evaluator_id), case in runs_by_group.items():
            passed = bool(case.score == 1.0) if case.normalization_kind == 'pass_fail' else None
            runs.append(ActivationRun(phase=phase, evaluator_id=evaluator_id, executable=case.executable, score=case.score, normalization_kind=case.normalization_kind, source_min=case.source_min, source_max=case.source_max, passed=passed))
        return {'runs': runs, 'submitted_run_ids': submitted_run_ids, 'definition_ids': definition_ids}

    def cleanup_activation_draft(self, *, draft_agent_name: str, draft_agent_version: str) -> Mapping[str, object]:
        deleter = getattr(self._client.agents, 'delete_version', None)
        if not callable(deleter):
            raise FoundryUnsupportedCapabilityError('agent draft version deletion unavailable', kind='unsupported_preview')
        try:
            deleter(draft_agent_name, draft_agent_version, force=True)
        except ResourceNotFoundError:
            pass
        except Exception as exc:
            raise self._classify_error(exc) from exc
        return {'draft_agent_name': draft_agent_name, 'draft_agent_version': draft_agent_version, 'completed': True}

    def _operation_id(self, kind: str, payload: Mapping[str, object]) -> str:
        digest = hashlib.sha256(_canonical_json({'kind': kind, 'payload': payload}).encode('utf-8')).hexdigest()[:24]
        return f'foundry-{kind}-{digest}'

    def _build_dataset_generation_job(self, request: Mapping[str, object]) -> DataGenerationJob:
        if 'inputs' in request:
            inputs = request['inputs']
            if not isinstance(inputs, Mapping):
                raise FoundryPrerequisiteError('data generation inputs must be a mapping', kind='prerequisite')
            return DataGenerationJob(inputs=DataGenerationJobInputs(**inputs))
        sources_payload = request.get('sources')
        options_payload = request.get('options')
        if not isinstance(sources_payload, Sequence) or isinstance(sources_payload, (str, bytes, bytearray)) or not isinstance(options_payload, Mapping):
            raise FoundryPrerequisiteError('data generation request must use nested inputs/sources/options schema', kind='prerequisite')
        sources: list[object] = []
        for source in sources_payload:
            if not isinstance(source, Mapping):
                raise FoundryPrerequisiteError('data generation source must be a mapping', kind='prerequisite')
            source_type = source.get('type')
            if source_type == 'agent':
                sources.append(AgentDataGenerationJobSource(**source))
            elif source_type == 'prompt':
                sources.append(PromptDataGenerationJobSource(**source))
            elif source_type == 'traces':
                sources.append(TracesDataGenerationJobSource(**source))
            elif source_type == 'file':
                sources.append(FileDataGenerationJobSource(**source))
            else:
                raise FoundryUnsupportedCapabilityError('unsupported data generation source type', kind='unsupported_preview')
        options_payload = dict(options_payload)
        if 'model_options' in options_payload and isinstance(options_payload['model_options'], Mapping):
            options_payload['model_options'] = DataGenerationModelOptions(**options_payload['model_options'])
        option_type = options_payload.get('type')
        if option_type == 'simple_qna':
            options = SimpleQnADataGenerationJobOptions(**options_payload)
        elif option_type == 'traces':
            options = TracesDataGenerationJobOptions(**options_payload)
        elif option_type == 'task_generation':
            options = TaskGenerationDataGenerationJobOptions(**options_payload)
        else:
            raise FoundryUnsupportedCapabilityError('unsupported data generation options type', kind='unsupported_preview')
        output_options = DataGenerationJobOutputOptions(name=request.get('output_name'), description=request.get('output_description'), tags=request.get('output_tags') or {}) if request.get('output_name') else None
        inputs = DataGenerationJobInputs(name=str(request.get('name') or request.get('job_name') or 'foundry-opt-data-generation'), sources=sources, options=options, scenario=str(request.get('scenario', 'evaluation')), output_options=output_options)
        return DataGenerationJob(inputs=inputs)

    def _build_evaluator_generation_job(self, request: Mapping[str, object]) -> EvaluatorGenerationJob:
        if 'inputs' in request:
            inputs = request['inputs']
            if not isinstance(inputs, Mapping):
                raise FoundryPrerequisiteError('evaluator generation inputs must be a mapping', kind='prerequisite')
            request_sources = inputs.get('sources')
        else:
            request_sources = request.get('sources')
        if not isinstance(request_sources, Sequence) or isinstance(request_sources, (str, bytes, bytearray)):
            raise FoundryPrerequisiteError('evaluator generation request must use nested inputs/sources schema', kind='prerequisite')
        built_sources: list[object] = []
        companion = False
        for source in request_sources:
            if not isinstance(source, Mapping):
                raise FoundryPrerequisiteError('evaluator generation source must be a mapping', kind='prerequisite')
            source_type = source.get('type')
            if source_type == 'agent':
                companion = True
                built_sources.append(AgentEvaluatorGenerationJobSource(**source))
            elif source_type == 'prompt':
                companion = True
                built_sources.append(PromptEvaluatorGenerationJobSource(**source))
            elif source_type == 'dataset':
                companion = True
                built_sources.append(DatasetEvaluatorGenerationJobSource(**source))
            elif source_type == 'traces':
                built_sources.append(TracesEvaluatorGenerationJobSource(**source))
            else:
                raise FoundryUnsupportedCapabilityError('unsupported evaluator generation source type', kind='unsupported_preview')
        if any(isinstance(source, TracesEvaluatorGenerationJobSource) for source in built_sources) and not companion:
            raise FoundryPrerequisiteError('traces require companion agent, prompt, or dataset source', kind='prerequisite')
        if 'inputs' in request:
            return EvaluatorGenerationJob(inputs=EvaluatorGenerationInputs(**dict(request['inputs'])))
        inputs = EvaluatorGenerationInputs(sources=built_sources, model=str(request.get('model')), evaluator_name=str(request.get('evaluator_name')), evaluator_display_name=request.get('evaluator_display_name'), evaluator_description=request.get('evaluator_description'))
        return EvaluatorGenerationJob(inputs=inputs)

    def _poller_seam(self, poller: object) -> tuple[str | None, str | None]:
        token = None
        url = None
        continuation = getattr(poller, 'continuation_token', None)
        if callable(continuation):
            value = continuation()
            token = value if isinstance(value, str) and value else None
        polling_method = getattr(poller, 'polling_method', lambda: None)()
        if polling_method is not None:
            value = getattr(polling_method, '_operation_location', None) or getattr(polling_method, 'operation_location', None)
            if isinstance(value, str) and value:
                url = value
        return token, url

    def create_dataset_generation_job(self, request: Mapping[str, object]) -> FoundryOperationHandle:
        request = dict(request)
        request.setdefault('scenario', 'evaluation')
        request.setdefault('name', request.get('job_name') or 'foundry-opt-data-generation')
        operation_id = str(request.get('operation_id') or self._operation_id('dataset-generation', request))
        try:
            poller = self._beta('datasets').begin_create_generation_job(self._build_dataset_generation_job(request), operation_id=operation_id)
        except Exception as exc:
            raise self._classify_error(exc) from exc
        token, url = self._poller_seam(poller)
        return FoundryOperationHandle(operation_id=operation_id, job_kind='dataset_generation', continuation_token=token, polling_url=url, created=True)

    def create_evaluator_generation_job(self, request: Mapping[str, object]) -> FoundryOperationHandle:
        request = dict(request)
        request.setdefault('scenario', 'evaluation')
        request.setdefault('name', request.get('job_name') or 'foundry-opt-data-generation')
        operation_id = str(request.get('operation_id') or self._operation_id('evaluator-generation', request))
        try:
            poller = self._beta('evaluators').begin_create_generation_job(self._build_evaluator_generation_job(request), operation_id=operation_id)
        except Exception as exc:
            raise self._classify_error(exc) from exc
        token, url = self._poller_seam(poller)
        return FoundryOperationHandle(operation_id=operation_id, job_kind='evaluator_generation', continuation_token=token, polling_url=url, created=True)

    def poll_generation_job(self, handle: FoundryOperationHandle, *, persist_before_poll: Callable[[FoundryOperationHandle], None] | None = None, deadline_monotonic: float | None = None, poll_interval: float | None = None) -> Mapping[str, object]:
        if persist_before_poll is not None:
            persist_before_poll(handle)
        poll_interval = self._default_poll_interval if poll_interval is None else poll_interval
        beta_group = self._beta('datasets') if handle.job_kind == 'dataset_generation' else self._beta('evaluators')
        if not handle.continuation_token:
            raise FoundryPrerequisiteError('continuation token required to resume generation job', kind='prerequisite')
        try:
            poller = beta_group.begin_create_generation_job(None, continuation_token=handle.continuation_token, operation_id=handle.operation_id)
        except Exception as exc:
            raise self._classify_error(exc) from None
        while True:
            if poller.done():
                try:
                    job = poller.result()
                except Exception as exc:
                    raise self._classify_error(exc) from None
                return self._normalize_job_result(handle.job_kind, _as_mapping(job))
            if deadline_monotonic is not None and self._time() >= deadline_monotonic:
                raise FoundryOperationDeadlineError('polling deadline exceeded', kind='deadline', retryable=True)
            self._sleep(poll_interval)

    def resume_generation_job(self, handle: FoundryOperationHandle, *, persist_before_poll: Callable[[FoundryOperationHandle], None] | None = None, deadline_monotonic: float | None = None, poll_interval: float | None = None) -> Mapping[str, object]:
        return self.poll_generation_job(handle, persist_before_poll=persist_before_poll, deadline_monotonic=deadline_monotonic, poll_interval=poll_interval)

    def _normalize_job_result(self, job_kind: str, job: Mapping[str, object]) -> Mapping[str, object]:
        result = job.get('result')
        normalized: MutableMapping[str, object] = {'job_id': job.get('id'), 'status': job.get('status'), 'created_at': _plain(job.get('created_at')), 'finished_at': _plain(job.get('finished_at')), 'error': _plain(job.get('error')), 'result': _plain(job)}
        if job_kind == 'dataset_generation':
            generated = job.get('generated_samples')
            normalized['generated_samples'] = generated
            if isinstance(generated, int) and generated < 15:
                normalized['outcome'] = 'rejected'
                normalized['output_datasets'] = ()
                return normalized
            outputs = job.get('outputs')
            accepted: list[Mapping[str, object]] = []
            if isinstance(outputs, Sequence) and not isinstance(outputs, (str, bytes, bytearray)):
                for item in outputs:
                    if isinstance(item, Mapping) and item.get('type') == 'dataset':
                        name = item.get('name')
                        version = item.get('version')
                        if isinstance(name, str) and isinstance(version, str):
                            dataset = self.get_dataset(name, version)
                            if dataset is not None:
                                accepted.append(dataset)
            normalized['outcome'] = 'accepted'
            normalized['output_datasets'] = tuple(accepted)
        else:
            normalized['saved_evaluator'] = {'id': job.get('id'), 'name': job.get('name'), 'version': job.get('version'), 'display_name': job.get('display_name')}
        return normalized

    def plan_resources(self, plan: BootstrapPlan) -> tuple[BootstrapAction, ...]:
        return tuple(action for action in plan.actions if action.phase == 'evaluations')

    def _dataset_request_from_action(self, action: BootstrapAction) -> tuple[str, str, str, str]:
        payload = action.diagnostics
        if len(payload) != 4:
            raise FoundryPrerequisiteError('dataset action diagnostics are incomplete', kind='prerequisite')
        dataset_name, dataset_version, dataset_uri, dataset_type = str(payload[0]), str(payload[1]), str(payload[2]), str(payload[3])
        if not dataset_name or not dataset_version or not dataset_uri or not dataset_type:
            raise FoundryPrerequisiteError('dataset action fields must be non-empty', kind='prerequisite')
        if len(dataset_uri.encode('utf-8')) > (_MAX_PROVIDER_STATE_BYTES // 4):
            raise FoundryPrerequisiteError('dataset action content uri exceeds safe persisted bound', kind='prerequisite')
        return dataset_name, dataset_version, dataset_uri, dataset_type

    def _evaluator_request_from_action(self, action: BootstrapAction) -> _EvaluatorActionRequest:
        payload = action.diagnostics
        if len(payload) != 5:
            raise FoundryPrerequisiteError('evaluator action diagnostics are incomplete', kind='prerequisite')
        evaluator_name, evaluator_version, evaluator_kind, provenance, expected_generation_job_id = (str(item) for item in payload)
        if not evaluator_name or not evaluator_version:
            raise FoundryPrerequisiteError('evaluator action name/version are required', kind='prerequisite')
        if evaluator_kind not in _EVALUATOR_KINDS:
            raise FoundryPrerequisiteError('evaluator action kind is invalid', kind='prerequisite')
        if provenance not in _EVALUATOR_PROVENANCES:
            raise FoundryPrerequisiteError('evaluator action provenance is invalid', kind='prerequisite')
        if evaluator_kind == 'builtin' and provenance != 'reused_existing':
            raise FoundryPrerequisiteError('built-in evaluators must be reused, never generated', kind='prerequisite')
        if provenance == 'auto_generated_unreviewed' and not expected_generation_job_id:
            raise FoundryPrerequisiteError('generated evaluator action requires expected generation job id', kind='prerequisite')
        if provenance == 'reused_existing' and expected_generation_job_id:
            raise FoundryPrerequisiteError('reused evaluator action must not carry a generation job id', kind='prerequisite')
        return _EvaluatorActionRequest(evaluator_name=evaluator_name, evaluator_version=evaluator_version, evaluator_kind=evaluator_kind, provenance=provenance, expected_generation_job_id=expected_generation_job_id)

    def _definition_request_from_action(self, action: BootstrapAction) -> _DefinitionActionRequest:
        payload = action.diagnostics
        if len(payload) != 8:
            raise FoundryPrerequisiteError('evaluation_definition action diagnostics are incomplete', kind='prerequisite')
        role, definition_name, dataset_name, dataset_version, evaluator_name, evaluator_version, evaluator_kind, model_deployment = (str(item) for item in payload)
        if role not in _DEFINITION_ROLES:
            raise FoundryPrerequisiteError('evaluation_definition action role is invalid', kind='prerequisite')
        if evaluator_kind not in _EVALUATOR_KINDS:
            raise FoundryPrerequisiteError('evaluation_definition action evaluator kind is invalid', kind='prerequisite')
        if not all((definition_name, dataset_name, dataset_version, evaluator_name, evaluator_version, model_deployment)):
            raise FoundryPrerequisiteError('evaluation_definition action fields must be non-empty', kind='prerequisite')
        return _DefinitionActionRequest(role=role, definition_name=definition_name, dataset_name=dataset_name, dataset_version=dataset_version, evaluator_name=evaluator_name, evaluator_version=evaluator_version, evaluator_kind=evaluator_kind, model_deployment=model_deployment)

    def _activation_request_from_action(self, action: BootstrapAction) -> _ActivationActionRequest:
        payload = action.diagnostics
        if len(payload) != 8:
            raise FoundryPrerequisiteError('activation_run action diagnostics are incomplete', kind='prerequisite')
        development_definition_name, validating_definition_name, draft_agent_name, draft_agent_version, model_deployment, bundle_objective_hash, split_lineage_hash, cases_json = (str(item) for item in payload)
        if not all((development_definition_name, validating_definition_name, draft_agent_name, draft_agent_version, model_deployment, bundle_objective_hash, split_lineage_hash)):
            raise FoundryPrerequisiteError('activation_run action fields must be non-empty', kind='prerequisite')
        if len(cases_json.encode('utf-8')) > _MAX_PROVIDER_STATE_BYTES:
            raise FoundryPrerequisiteError('activation_run case payload exceeds safe persisted bound', kind='prerequisite')
        try:
            decoded = json.loads(cases_json)
        except (TypeError, ValueError) as exc:
            raise FoundryPrerequisiteError('activation_run case payload is not valid JSON', kind='prerequisite') from exc
        if not isinstance(decoded, Mapping) or set(decoded.keys()) != {'cases', 'guardrails'}:
            raise FoundryPrerequisiteError('activation_run case payload must contain exactly cases and guardrails', kind='prerequisite')
        try:
            safe_persisted_document(decoded)
        except UnsafeCheckpointContentError as exc:
            raise FoundryPrerequisiteError('activation_run case payload contains prohibited content', kind='prerequisite') from exc
        raw_cases = decoded.get('cases')
        raw_guardrails = decoded.get('guardrails')
        if not isinstance(raw_cases, Sequence) or isinstance(raw_cases, (str, bytes, bytearray)) or not raw_cases:
            raise FoundryPrerequisiteError('activation_run requires a non-empty cases list', kind='prerequisite')
        if not isinstance(raw_guardrails, Sequence) or isinstance(raw_guardrails, (str, bytes, bytearray)):
            raise FoundryPrerequisiteError('activation_run guardrails must be a list', kind='prerequisite')
        cases = tuple(self._parse_activation_case(item, index=index) for index, item in enumerate(raw_cases))
        if {case.phase for case in cases} != set(_DEFINITION_ROLES):
            raise FoundryPrerequisiteError('activation_run cases must cover both development and validating phases', kind='prerequisite')
        if not any(case.phase == 'development' and case.evaluator_id == _CONTENT_SAFETY_ID for case in cases) or not any(case.phase == 'validating' and case.evaluator_id == _CONTENT_SAFETY_ID for case in cases):
            raise FoundryPrerequisiteError('activation_run cases must include content safety results for both phases', kind='prerequisite')
        guardrails = tuple(self._parse_activation_guardrail(item, index=index) for index, item in enumerate(raw_guardrails))
        return _ActivationActionRequest(
            development_definition_name=development_definition_name,
            validating_definition_name=validating_definition_name,
            draft_agent_name=draft_agent_name,
            draft_agent_version=draft_agent_version,
            model_deployment=model_deployment,
            bundle_objective_hash=bundle_objective_hash,
            split_lineage_hash=split_lineage_hash,
            cases=cases,
            guardrails=guardrails,
        )

    def _cleanup_request_from_action(self, action: BootstrapAction) -> _CleanupActionRequest:
        payload = action.diagnostics
        if len(payload) != 2:
            raise FoundryPrerequisiteError('activation_cleanup action diagnostics are incomplete', kind='prerequisite')
        draft_agent_name, draft_agent_version = str(payload[0]), str(payload[1])
        if not draft_agent_name or not draft_agent_version:
            raise FoundryPrerequisiteError('activation_cleanup action fields must be non-empty', kind='prerequisite')
        return _CleanupActionRequest(draft_agent_name=draft_agent_name, draft_agent_version=draft_agent_version)

    def _fingerprints_for_dataset(self, action_id: str, dataset: Mapping[str, object]) -> tuple[FingerprintRecord, FingerprintRecord]:
        before = FingerprintRecord(label=f'{action_id}:before', sha256=str(dataset.get('content_fingerprint') or hashlib.sha256(str(dataset.get('id')).encode('utf-8')).hexdigest()))
        after = FingerprintRecord(label=f'{action_id}:after', sha256=str(dataset.get('content_fingerprint') or hashlib.sha256(_canonical_json(dataset).encode('utf-8')).hexdigest()))
        return before, after

    def _build_dataset_resource_record(self, *, action_id: str, result: Mapping[str, object], rollback_order: int | None) -> _ResourceRecord:
        dataset = result.get('dataset')
        if not isinstance(dataset, Mapping):
            raise FoundryPrerequisiteError('dataset result missing dataset mapping', kind='prerequisite')
        return _ResourceRecord(
            action_id=action_id,
            resource_id=str(result['resource_id']),
            name=str(dataset.get('name') or ''),
            version=str(dataset.get('version') or ''),
            kind='dataset',
            disposition='created' if bool(result.get('created')) else 'adopted',
            fingerprint=str(dataset.get('content_fingerprint')) if dataset.get('content_fingerprint') else None,
            rollback_order=rollback_order,
            resource_type=str(dataset.get('type') or ''),
            ownership_token=str(result.get('ownership_token')) if result.get('ownership_token') else None,
        )

    def _build_evaluator_resource_record(self, *, action_id: str, request: _EvaluatorActionRequest, result: Mapping[str, object], rollback_order: int | None) -> _ResourceRecord:
        evaluator = result.get('evaluator')
        if not isinstance(evaluator, Mapping):
            raise FoundryPrerequisiteError('evaluator result missing evaluator mapping', kind='prerequisite')
        return _ResourceRecord(
            action_id=action_id,
            resource_id=str(result['resource_id']),
            name=str(evaluator.get('name') or request.evaluator_name),
            version=str(evaluator.get('version') or request.evaluator_version),
            kind='evaluator',
            disposition='created' if bool(result.get('created')) else 'adopted',
            fingerprint=request.expected_generation_job_id or None,
            rollback_order=rollback_order,
            resource_type=request.evaluator_kind,
            ownership_token=None,
        )

    def _build_definition_resource_record(self, *, action_id: str, request: _DefinitionActionRequest, result: Mapping[str, object], rollback_order: int | None) -> _ResourceRecord:
        definition = result.get('definition')
        if not isinstance(definition, Mapping):
            raise FoundryPrerequisiteError('evaluation definition result missing definition mapping', kind='prerequisite')
        return _ResourceRecord(
            action_id=action_id,
            resource_id=str(result['resource_id']),
            name=str(definition.get('name') or request.definition_name),
            version=request.role,
            kind='evaluation_definition',
            disposition='created' if bool(result.get('created')) else 'adopted',
            fingerprint=None,
            rollback_order=rollback_order,
            resource_type=None,
            ownership_token=None,
        )

    def _build_activation_run_resource_records(self, *, action_id: str, submitted_run_ids: Mapping[str, str], definition_ids: Mapping[str, str], rollback_order_start: int) -> list[_ResourceRecord]:
        records: list[_ResourceRecord] = []
        order = rollback_order_start
        for phase in _DEFINITION_ROLES:
            run_id = submitted_run_ids.get(phase)
            if run_id is None:
                continue
            records.append(_ResourceRecord(action_id=f'{action_id}:{phase}', resource_id=run_id, name=str(definition_ids.get(phase, '')), version=phase, kind='activation_run', disposition='created', fingerprint=None, rollback_order=order, resource_type=None, ownership_token=None))
            order += 1
        return records


    def _provider_state_from_receipt(self, receipt: BootstrapReceipt, resources: Sequence[_ResourceRecord]) -> dict[str, object]:
        self._validate_provider_state_bounds(resources)
        resource_state = self._resource_state_payload(resources)
        state: dict[str, object] = {
            'schema_version': _PROVIDER_STATE_SCHEMA_VERSION,
            'receipt_hash': receipt.receipt_hash,
            'operation_id': receipt.operation_id,
            'repository_identity': receipt.repository_identity,
            'plan_hash': receipt.plan_hash,
            'binding_hash': hashlib.sha256(_canonical_json(self._binding_payload(receipt_hash=receipt.receipt_hash, operation_id=receipt.operation_id, repository_identity=receipt.repository_identity, plan_hash=receipt.plan_hash)).encode('utf-8')).hexdigest(),
            'resources': resource_state,
            'rollback_order': [item.resource_id for item in sorted((resource for resource in resources if resource.disposition == 'created' and resource.rollback_order is not None), key=lambda item: int(item.rollback_order or 0), reverse=True)],
        }
        state['state_hash'] = self._state_hash(state)
        self._validate_state_document_bounds(state)
        return json.loads(_canonical_json(state))

    def export_provider_state(self, receipt: BootstrapReceipt) -> Mapping[str, object]:
        resources = self._current_resource_records(receipt)
        state = self._provider_state_from_receipt(receipt, resources)
        self._provider_state = state
        return state

    def _validate_provider_state_binding(self, receipt: BootstrapReceipt, state: Mapping[str, object]) -> None:
        if state.get('schema_version') != _PROVIDER_STATE_SCHEMA_VERSION:
            raise FoundryPrerequisiteError('provider state schema_version mismatch', kind='prerequisite')
        if state.get('receipt_hash') != receipt.receipt_hash or state.get('operation_id') != receipt.operation_id:
            raise FoundryPrerequisiteError('provider state receipt binding mismatch', kind='prerequisite')
        if state.get('repository_identity') != receipt.repository_identity or state.get('plan_hash') != receipt.plan_hash:
            raise FoundryPrerequisiteError('provider state repository binding mismatch', kind='prerequisite')
        expected = hashlib.sha256(_canonical_json(self._binding_payload(receipt_hash=receipt.receipt_hash, operation_id=receipt.operation_id, repository_identity=receipt.repository_identity, plan_hash=receipt.plan_hash)).encode('utf-8')).hexdigest()
        if state.get('binding_hash') != expected:
            raise FoundryPrerequisiteError('provider state binding hash mismatch', kind='prerequisite')
        if state.get('state_hash') != self._state_hash(state):
            raise FoundryPrerequisiteError('provider state hash mismatch', kind='prerequisite')

    def _resource_records_from_state(self, state: Mapping[str, object]) -> tuple[_ResourceRecord, ...]:
        resources = state.get('resources')
        if not isinstance(resources, Sequence) or isinstance(resources, (str, bytes, bytearray)):
            raise FoundryPrerequisiteError('provider state resources are invalid', kind='prerequisite')
        records: list[_ResourceRecord] = []
        for item in resources:
            if not isinstance(item, Mapping):
                raise FoundryPrerequisiteError('provider state resource entry is invalid', kind='prerequisite')
            records.append(_ResourceRecord(action_id=str(item.get('action_id') or ''), resource_id=str(item.get('id') or ''), name=str(item.get('name') or ''), version=str(item.get('version') or ''), kind=str(item.get('kind') or ''), disposition=str(item.get('disposition') or ''), fingerprint=str(item.get('fingerprint')) if item.get('fingerprint') is not None else None, rollback_order=int(item['rollback_order']) if isinstance(item.get('rollback_order'), int) else None, resource_type=str(item.get('resource_type')) if item.get('resource_type') is not None else None, ownership_token=str(item.get('ownership_token')) if item.get('ownership_token') is not None else None))
        self._validate_provider_state_bounds(records)
        return tuple(records)

    def _current_resource_records(self, receipt: BootstrapReceipt) -> tuple[_ResourceRecord, ...]:
        if self._provider_state is None:
            raise FoundryPrerequisiteError('provider state unavailable for receipt export', kind='prerequisite')
        self._validate_provider_state_binding(receipt, self._provider_state)
        return self._resource_records_from_state(self._provider_state)

    def restore_provider_state(self, mapping: Mapping[str, object]) -> None:
        encoded = _canonical_json(mapping).encode('utf-8')
        state = json.loads(encoded.decode('utf-8'))
        self._resource_records_from_state(state)
        if state.get('schema_version') != _PROVIDER_STATE_SCHEMA_VERSION:
            raise FoundryPrerequisiteError('provider state schema_version mismatch', kind='prerequisite')
        for field in ('receipt_hash', 'operation_id', 'repository_identity', 'plan_hash', 'binding_hash', 'state_hash'):
            value = state.get(field)
            if not isinstance(value, str) or not value:
                raise FoundryPrerequisiteError(f'provider state {field} is invalid', kind='prerequisite')
        expected = hashlib.sha256(_canonical_json(self._binding_payload(receipt_hash=state['receipt_hash'], operation_id=state['operation_id'], repository_identity=state['repository_identity'], plan_hash=state['plan_hash'])).encode('utf-8')).hexdigest()
        if state.get('binding_hash') != expected:
            raise FoundryPrerequisiteError('provider state binding hash mismatch', kind='prerequisite')
        if state.get('state_hash') != self._state_hash(state):
            raise FoundryPrerequisiteError('provider state hash mismatch', kind='prerequisite')
        self._validate_state_document_bounds(state)
        self._provider_state = state

    def _get_live_resource(self, resource: _ResourceRecord) -> Mapping[str, object] | None:
        if resource.kind == 'dataset':
            return self.get_dataset(resource.name, resource.version)
        if resource.kind == 'evaluator':
            if resource.resource_type == 'builtin':
                try:
                    return self.resolve_builtin_evaluator(resource.name)
                except FoundryUnsupportedCapabilityError:
                    return None
            return self.get_evaluator_version(resource.name, resource.version)
        if resource.kind == 'evaluation_definition':
            return self.get_evaluation_definition(resource.resource_id)
        if resource.kind == 'activation_run':
            return self.get_activation_run(resource.resource_id, resource.name)
        return None

    def _live_matches(self, resource: _ResourceRecord, live: Mapping[str, object] | None, *, require_ownership: bool) -> bool:
        if resource.kind == 'dataset':
            return self._resource_live_matches(resource, live, require_ownership=require_ownership)
        if live is None:
            return False
        if str(live.get('id') or '') != resource.resource_id:
            return False
        if resource.kind == 'evaluator' and resource.fingerprint is not None:
            if str(live.get('generation_job_id') or '') != resource.fingerprint:
                return False
        return True

    def _delete_created_resource(self, resource: _ResourceRecord) -> bool:
        try:
            if resource.kind == 'dataset':
                deleter = getattr(self._client.datasets, 'delete', None)
                if not callable(deleter):
                    return False
                deleter(resource.name, resource.version)
                return True
            if resource.kind == 'evaluator':
                if resource.resource_type == 'builtin':
                    return True
                self._beta('evaluators').delete_version(resource.name, resource.version)
                return True
            if resource.kind == 'evaluation_definition':
                self._openai_client().evals.delete(resource.resource_id)
                return True
            if resource.kind == 'activation_run':
                self._openai_client().evals.runs.delete(resource.resource_id, eval_id=resource.name)
                return True
        except Exception:
            return False
        return False

    def apply_resources(self, plan: BootstrapPlan) -> BootstrapReceipt:
        self._validate_plan_bounds(plan)
        created: list[str] = []
        adopted: list[str] = []
        changed: list[str] = []
        skipped: list[str] = []
        created_resource_ids: list[str] = []
        resource_records: list[_ResourceRecord] = []
        before_fingerprints: list[FingerprintRecord] = []
        after_fingerprints: list[FingerprintRecord] = []
        definition_ids_by_key: dict[tuple[str, str], str] = {}
        confirmed_activation_drafts: set[tuple[str, str]] = set()
        try:
            for action in self.plan_resources(plan):
                if action.kind == 'dataset':
                    dataset_name, dataset_version, dataset_uri, dataset_type = self._dataset_request_from_action(action)
                    result = self.create_or_adopt_dataset(operation_id=plan.operation_id, action_id=action.action_id, dataset_name=dataset_name, dataset_version=dataset_version, dataset_content_uri=dataset_uri, dataset_type=dataset_type)
                    dataset = result['dataset']
                    before_fp, after_fp = self._fingerprints_for_dataset(action.action_id, dataset)
                    before_fingerprints.append(before_fp)
                    after_fingerprints.append(after_fp)
                    resource_id = str(result['resource_id'])
                    if result['created']:
                        created.append(action.action_id)
                        created_resource_ids.append(resource_id)
                        resource_records.append(self._build_dataset_resource_record(action_id=action.action_id, result=result, rollback_order=len(created_resource_ids)))
                    elif result['adopted']:
                        adopted.append(action.action_id)
                        resource_records.append(self._build_dataset_resource_record(action_id=action.action_id, result=result, rollback_order=None))
                    elif result.get('replayed'):
                        changed.append(action.action_id)
                    else:
                        raise FoundryPrerequisiteError('dataset apply produced neither a created, adopted, nor replayed disposition', kind='prerequisite')
                elif action.kind == 'evaluator':
                    evaluator_request = self._evaluator_request_from_action(action)
                    result = self.adopt_or_verify_evaluator(evaluator_request)
                    resource_id = str(result['resource_id'])
                    if result['created']:
                        created.append(action.action_id)
                        created_resource_ids.append(resource_id)
                        resource_records.append(self._build_evaluator_resource_record(action_id=action.action_id, request=evaluator_request, result=result, rollback_order=len(created_resource_ids)))
                    elif result['adopted']:
                        adopted.append(action.action_id)
                        resource_records.append(self._build_evaluator_resource_record(action_id=action.action_id, request=evaluator_request, result=result, rollback_order=None))
                    else:
                        raise FoundryPrerequisiteError('evaluator apply produced neither a created nor adopted disposition', kind='prerequisite')
                elif action.kind == 'evaluation_definition':
                    definition_request = self._definition_request_from_action(action)
                    result = self.create_or_adopt_evaluation_definition(definition_request)
                    resource_id = str(result['resource_id'])
                    definition_ids_by_key[(definition_request.role, definition_request.definition_name)] = resource_id
                    if result['created']:
                        created.append(action.action_id)
                        created_resource_ids.append(resource_id)
                        resource_records.append(self._build_definition_resource_record(action_id=action.action_id, request=definition_request, result=result, rollback_order=len(created_resource_ids)))
                    elif result['adopted']:
                        adopted.append(action.action_id)
                        resource_records.append(self._build_definition_resource_record(action_id=action.action_id, request=definition_request, result=result, rollback_order=None))
                    else:
                        raise FoundryPrerequisiteError('evaluation_definition apply produced neither a created nor adopted disposition', kind='prerequisite')
                elif action.kind == 'activation_run':
                    activation_request = self._activation_request_from_action(action)
                    development_definition_id = definition_ids_by_key.get(('development', activation_request.development_definition_name))
                    validating_definition_id = definition_ids_by_key.get(('validating', activation_request.validating_definition_name))
                    if development_definition_id is None or validating_definition_id is None:
                        raise FoundryPrerequisiteError('activation_run definitions were not resolved earlier in this apply', kind='prerequisite')
                    result = self.run_activation_smoke(request=activation_request, development_definition_id=development_definition_id, validating_definition_id=validating_definition_id)
                    submitted_run_ids = result['submitted_run_ids']
                    definition_ids = result['definition_ids']
                    changed.append(action.action_id)
                    for record in self._build_activation_run_resource_records(action_id=action.action_id, submitted_run_ids=submitted_run_ids, definition_ids=definition_ids, rollback_order_start=len(created_resource_ids) + 1):
                        created.append(record.action_id)
                        created_resource_ids.append(record.resource_id)
                        resource_records.append(record)
                    confirmed_activation_drafts.add((activation_request.draft_agent_name, activation_request.draft_agent_version))
                else:
                    cleanup_request = self._cleanup_request_from_action(action)
                    if (cleanup_request.draft_agent_name, cleanup_request.draft_agent_version) not in confirmed_activation_drafts:
                        raise FoundryPrerequisiteError('activation_cleanup references a draft with no preceding activation_run in this apply', kind='prerequisite')
                    self.cleanup_activation_draft(draft_agent_name=cleanup_request.draft_agent_name, draft_agent_version=cleanup_request.draft_agent_version)
                    changed.append(action.action_id)
            receipt = BootstrapReceipt.create(operation_id=plan.operation_id, runtime_repository=plan.runtime_repository, runtime_commit=plan.runtime_commit, repository_identity=plan.repository_identity, plan_hash=plan.plan_hash, before_fingerprints=tuple(before_fingerprints), after_fingerprints=tuple(after_fingerprints), created_actions=tuple(created), adopted_actions=tuple(adopted), changed_actions=tuple(changed), skipped_actions=tuple(skipped), compensation_required_actions=tuple(created_resource_ids))
            self._provider_state = self._provider_state_from_receipt(receipt, resource_records)
        except Exception as exc:
            compensation_receipt = BootstrapReceipt.create(operation_id=plan.operation_id, runtime_repository=plan.runtime_repository, runtime_commit=plan.runtime_commit, repository_identity=plan.repository_identity, plan_hash=plan.plan_hash, before_fingerprints=tuple(before_fingerprints), after_fingerprints=tuple(after_fingerprints), created_actions=tuple(created), adopted_actions=tuple(adopted), changed_actions=tuple(changed), skipped_actions=tuple(skipped), compensation_required_actions=tuple(created_resource_ids), error_info=RedactedStatusInfo(code='apply_failed', summary='resource apply failed'))
            if created or adopted:
                self._provider_state = self._provider_state_from_receipt(compensation_receipt, resource_records)
            if not created_resource_ids:
                raise self._classify_error(exc) from None
            try:
                self.rollback_resources(compensation_receipt)
            except FoundryRollbackError as rollback_exc:
                raise rollback_exc from None
            raise self._classify_error(exc) from None
        return receipt

    def verify_resources(self, receipt: BootstrapReceipt) -> bool:
        resources = self._current_resource_records(receipt)
        for resource in resources:
            if resource.kind == 'dataset' and not resource.resource_id.startswith(_IMMUTABLE_DATASET_URI_PREFIX):
                continue
            live = self._get_live_resource(resource)
            if not self._live_matches(resource, live, require_ownership=(resource.disposition == 'created')):
                return False
        return True

    def rollback_resources(self, receipt: BootstrapReceipt) -> None:
        failures: list[str] = []
        try:
            resources = self._current_resource_records(receipt)
        except FoundryAdapterError as exc:
            raise FoundryRollbackError(str(exc), kind='prerequisite', retryable=False) from exc
        rollback_order = self._provider_state.get('rollback_order') if isinstance(self._provider_state, Mapping) else None
        if not isinstance(rollback_order, Sequence) or isinstance(rollback_order, (str, bytes, bytearray)):
            raise FoundryRollbackError('provider state rollback order is invalid', kind='prerequisite', retryable=False, compensation_receipt=receipt, provider_state=self.export_provider_state(receipt))
        by_id = {resource.resource_id: resource for resource in resources if resource.disposition == 'created'}
        failures_state = self.export_provider_state(receipt)
        for resource_id in rollback_order:
            resource = by_id.get(resource_id)
            if resource is None:
                continue
            if resource.kind == 'dataset' and not resource.resource_id.startswith(_IMMUTABLE_DATASET_URI_PREFIX):
                continue
            if resource.kind == 'evaluator' and resource.resource_type == 'builtin':
                continue  # built-in evaluators are always adopted, never created; never delete them
            live = self._get_live_resource(resource)
            if live is None:
                continue
            if not self._live_matches(resource, live, require_ownership=(resource.kind == 'dataset')):
                failures.append(resource_id)
                continue
            if not self._delete_created_resource(resource):
                failures.append(resource_id)
        if failures:
            raise FoundryRollbackError('rollback failed', kind='platform', retryable=False, compensation_receipt=receipt, provider_state=failures_state)

    def verify_rollback(self, receipt: BootstrapReceipt) -> bool:
        resources = self._current_resource_records(receipt)
        for resource in resources:
            if resource.disposition == 'created':
                if resource.kind == 'evaluator' and resource.resource_type == 'builtin':
                    continue
                live = self._get_live_resource(resource)
                if live is not None:
                    return False
        return True


__all__ = [name for name in globals() if name.startswith('Foundry') or name in {'rollback_failure_details', 'FoundryAdapter'}]
