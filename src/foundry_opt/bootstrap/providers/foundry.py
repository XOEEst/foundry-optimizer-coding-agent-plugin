from __future__ import annotations

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

from foundry_opt.bootstrap.contracts import BootstrapAction, BootstrapPlan, BootstrapReceipt, FingerprintRecord, RedactedStatusInfo
from foundry_opt.bootstrap.errors import BootstrapProviderError
from foundry_opt.models import FrozenModel

_CONTENT_SAFETY_ID = 'azureai://built-in/evaluators/content_safety'
_IMMUTABLE_DATASET_URI_PREFIX = 'azureai://accounts/'
_PROVIDER_STATE_SCHEMA_VERSION = 1
_MAX_PROVIDER_STATE_BYTES = 32768


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
    pass


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

    def _beta(self, attr: str) -> object:
        beta = getattr(self._client, 'beta', None)
        if beta is None:
            raise FoundryUnsupportedCapabilityError('beta preview surface unavailable', kind='unsupported_preview')
        value = getattr(beta, attr, None)
        if value is None:
            raise FoundryUnsupportedCapabilityError('required beta preview operation unavailable', kind='unsupported_preview')
        return value

    def _classify_error(self, exc: BaseException) -> FoundryAdapterError:
        if isinstance(exc, FoundryAdapterError):
            return exc
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
        normalized = {'name': data.get('name'), 'version': data.get('version'), 'id': dataset_id, 'type': data.get('type'), 'data_uri': data.get('data_uri') or data.get('dataUri'), 'content_fingerprint': fingerprint, 'raw': _plain(data)}
        return normalized

    def create_or_adopt_dataset(self, *, dataset_name: str, dataset_version: str, dataset_content_uri: str, dataset_type: str, connection_name: str | None = None, description: str | None = None, tags: Mapping[str, str] | None = None) -> Mapping[str, object]:
        expected_fingerprint = _fingerprint_dataset_content(dataset_content_uri, dataset_type)
        existing = self.get_dataset(dataset_name, dataset_version)
        if existing is not None:
            if existing.get('type') != dataset_type or existing.get('data_uri') != dataset_content_uri:
                raise FoundryPrerequisiteError('existing dataset version does not match requested content', kind='prerequisite')
            return {'created': False, 'adopted': True, 'replayed': False, 'dataset': {**existing, 'content_fingerprint': expected_fingerprint}, 'resource_id': str(existing['id'])}
        payload_cls = FileDatasetVersion if dataset_type == 'uri_file' else FolderDatasetVersion
        payload = payload_cls(data_uri=dataset_content_uri, type=dataset_type, connection_name=connection_name, description=description, tags=dict(tags or {}))
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
        }

    def resolve_builtin_content_safety(self) -> Mapping[str, object]:
        for item in self.inventory_evaluators(include_builtin=True):
            if item.get('id') == _CONTENT_SAFETY_ID and item.get('evaluator_type') == 'builtin':
                return item
        raise FoundryUnsupportedCapabilityError('built-in content safety evaluator unavailable', kind='unsupported_preview')

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
        if len(payload) < 4:
            raise FoundryPrerequisiteError('dataset action diagnostics are incomplete', kind='prerequisite')
        return str(payload[0]), str(payload[1]), str(payload[2]), str(payload[3])

    def _fingerprints_for_dataset(self, action_id: str, dataset: Mapping[str, object]) -> tuple[FingerprintRecord, FingerprintRecord]:
        before = FingerprintRecord(label=f'{action_id}:before', sha256=str(dataset.get('content_fingerprint') or hashlib.sha256(str(dataset.get('id')).encode('utf-8')).hexdigest()))
        after = FingerprintRecord(label=f'{action_id}:after', sha256=str(dataset.get('content_fingerprint') or hashlib.sha256(_canonical_json(dataset).encode('utf-8')).hexdigest()))
        return before, after

    def _build_resource_record(self, *, action_id: str, result: Mapping[str, object], rollback_order: int | None) -> _ResourceRecord:
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
        )

    def _provider_state_from_receipt(self, receipt: BootstrapReceipt, resources: Sequence[_ResourceRecord]) -> dict[str, object]:
        resource_state = [
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
            }
            for item in resources
        ]
        state: dict[str, object] = {
            'schema_version': _PROVIDER_STATE_SCHEMA_VERSION,
            'receipt_hash': receipt.receipt_hash,
            'operation_id': receipt.operation_id,
            'repository_identity': receipt.repository_identity,
            'plan_hash': receipt.plan_hash,
            'binding_hash': hashlib.sha256(_canonical_json({'receipt_hash': receipt.receipt_hash, 'operation_id': receipt.operation_id, 'repository_identity': receipt.repository_identity, 'plan_hash': receipt.plan_hash}).encode('utf-8')).hexdigest(),
            'resources': resource_state,
            'rollback_order': [item.resource_id for item in sorted((resource for resource in resources if resource.disposition == 'created' and resource.rollback_order is not None), key=lambda item: int(item.rollback_order or 0), reverse=True)],
        }
        encoded = _canonical_json(state).encode('utf-8')
        if len(encoded) > _MAX_PROVIDER_STATE_BYTES:
            raise FoundryPrerequisiteError('provider state exceeds safe persisted size', kind='prerequisite')
        return json.loads(encoded.decode('utf-8'))

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
        expected = hashlib.sha256(_canonical_json({'receipt_hash': receipt.receipt_hash, 'operation_id': receipt.operation_id, 'repository_identity': receipt.repository_identity, 'plan_hash': receipt.plan_hash}).encode('utf-8')).hexdigest()
        if state.get('binding_hash') != expected:
            raise FoundryPrerequisiteError('provider state binding hash mismatch', kind='prerequisite')

    def _resource_records_from_state(self, state: Mapping[str, object]) -> tuple[_ResourceRecord, ...]:
        resources = state.get('resources')
        if not isinstance(resources, Sequence) or isinstance(resources, (str, bytes, bytearray)):
            raise FoundryPrerequisiteError('provider state resources are invalid', kind='prerequisite')
        records: list[_ResourceRecord] = []
        for item in resources:
            if not isinstance(item, Mapping):
                raise FoundryPrerequisiteError('provider state resource entry is invalid', kind='prerequisite')
            records.append(_ResourceRecord(action_id=str(item.get('action_id') or ''), resource_id=str(item.get('id') or ''), name=str(item.get('name') or ''), version=str(item.get('version') or ''), kind=str(item.get('kind') or ''), disposition=str(item.get('disposition') or ''), fingerprint=str(item.get('fingerprint')) if item.get('fingerprint') is not None else None, rollback_order=int(item['rollback_order']) if isinstance(item.get('rollback_order'), int) else None, resource_type=str(item.get('resource_type')) if item.get('resource_type') is not None else None))
        return tuple(records)

    def _current_resource_records(self, receipt: BootstrapReceipt) -> tuple[_ResourceRecord, ...]:
        if self._provider_state is None:
            raise FoundryPrerequisiteError('provider state unavailable for receipt export', kind='prerequisite')
        self._validate_provider_state_binding(receipt, self._provider_state)
        return self._resource_records_from_state(self._provider_state)

    def restore_provider_state(self, mapping: Mapping[str, object]) -> None:
        encoded = _canonical_json(mapping).encode('utf-8')
        if len(encoded) > _MAX_PROVIDER_STATE_BYTES:
            raise FoundryPrerequisiteError('provider state exceeds safe persisted size', kind='prerequisite')
        state = json.loads(encoded.decode('utf-8'))
        self._resource_records_from_state(state)
        if state.get('schema_version') != _PROVIDER_STATE_SCHEMA_VERSION:
            raise FoundryPrerequisiteError('provider state schema_version mismatch', kind='prerequisite')
        for field in ('receipt_hash', 'operation_id', 'repository_identity', 'plan_hash', 'binding_hash'):
            value = state.get(field)
            if not isinstance(value, str) or not value:
                raise FoundryPrerequisiteError(f'provider state {field} is invalid', kind='prerequisite')
        expected = hashlib.sha256(_canonical_json({'receipt_hash': state['receipt_hash'], 'operation_id': state['operation_id'], 'repository_identity': state['repository_identity'], 'plan_hash': state['plan_hash']}).encode('utf-8')).hexdigest()
        if state.get('binding_hash') != expected:
            raise FoundryPrerequisiteError('provider state binding hash mismatch', kind='prerequisite')
        self._provider_state = state

    def apply_resources(self, plan: BootstrapPlan) -> BootstrapReceipt:
        created: list[str] = []
        adopted: list[str] = []
        changed: list[str] = []
        skipped: list[str] = []
        created_resource_ids: list[str] = []
        resource_records: list[_ResourceRecord] = []
        before_fingerprints: list[FingerprintRecord] = []
        after_fingerprints: list[FingerprintRecord] = []
        try:
            for action in self.plan_resources(plan):
                if action.kind != 'dataset':
                    skipped.append(action.action_id)
                    continue
                dataset_name, dataset_version, dataset_uri, dataset_type = self._dataset_request_from_action(action)
                result = self.create_or_adopt_dataset(dataset_name=dataset_name, dataset_version=dataset_version, dataset_content_uri=dataset_uri, dataset_type=dataset_type)
                dataset = result['dataset']
                before_fp, after_fp = self._fingerprints_for_dataset(action.action_id, dataset)
                before_fingerprints.append(before_fp)
                after_fingerprints.append(after_fp)
                resource_id = str(result['resource_id'])
                if result['created']:
                    created.append(action.action_id)
                    created_resource_ids.append(resource_id)
                    resource_records.append(self._build_resource_record(action_id=action.action_id, result=result, rollback_order=len(created_resource_ids)))
                elif result['adopted']:
                    adopted.append(action.action_id)
                    resource_records.append(self._build_resource_record(action_id=action.action_id, result=result, rollback_order=None))
                elif result.get('replayed'):
                    changed.append(action.action_id)
                else:
                    skipped.append(action.action_id)
            receipt = BootstrapReceipt.create(operation_id=plan.operation_id, runtime_repository=plan.runtime_repository, runtime_commit=plan.runtime_commit, repository_identity=plan.repository_identity, plan_hash=plan.plan_hash, before_fingerprints=tuple(before_fingerprints), after_fingerprints=tuple(after_fingerprints), created_actions=tuple(created), adopted_actions=tuple(adopted), changed_actions=tuple(changed), skipped_actions=tuple(skipped), compensation_required_actions=tuple(created_resource_ids))
            self._provider_state = self._provider_state_from_receipt(receipt, resource_records)
        except Exception as exc:
            compensation_receipt = BootstrapReceipt.create(operation_id=plan.operation_id, runtime_repository=plan.runtime_repository, runtime_commit=plan.runtime_commit, repository_identity=plan.repository_identity, plan_hash=plan.plan_hash, before_fingerprints=tuple(before_fingerprints), after_fingerprints=tuple(after_fingerprints), created_actions=tuple(created), adopted_actions=tuple(adopted), changed_actions=tuple(changed), skipped_actions=tuple(skipped), compensation_required_actions=tuple(created_resource_ids), error_info=RedactedStatusInfo(code='apply_failed', summary='resource apply failed'))
            if created or adopted:
                self._provider_state = self._provider_state_from_receipt(compensation_receipt, resource_records)
            try:
                self.rollback_resources(compensation_receipt)
            except FoundryRollbackError as rollback_exc:
                raise rollback_exc from None
            raise self._classify_error(exc) from None
        return receipt

    def verify_resources(self, receipt: BootstrapReceipt) -> bool:
        resources = self._current_resource_records(receipt)
        for resource in resources:
            if resource.kind != 'dataset' or not resource.resource_id.startswith(_IMMUTABLE_DATASET_URI_PREFIX):
                continue
            live = self.get_dataset(resource.name, resource.version)
            if live is None:
                return False
            if str(live.get('id')) != resource.resource_id:
                return False
            if resource.resource_type and str(live.get('type')) != resource.resource_type:
                return False
            if resource.fingerprint is not None and str(live.get('content_fingerprint')) != resource.fingerprint:
                return False
        return True

    def rollback_resources(self, receipt: BootstrapReceipt) -> None:
        deleter = getattr(self._client.datasets, 'delete', None)
        if not callable(deleter):
            return
        failures: list[str] = []
        try:
            resources = self._current_resource_records(receipt)
        except FoundryAdapterError as exc:
            raise FoundryRollbackError(str(exc), kind='prerequisite', retryable=False) from exc
        by_id = {resource.resource_id: resource for resource in resources if resource.disposition == 'created'}
        for resource_id in receipt.compensation_required_actions:
            resource = by_id.get(resource_id)
            if resource is None or resource.kind != 'dataset' or not resource.resource_id.startswith(_IMMUTABLE_DATASET_URI_PREFIX):
                continue
            try:
                deleter(resource.name, resource.version)
            except Exception:
                failures.append(resource_id)
        if failures:
            raise FoundryRollbackError('rollback failed', kind='platform', retryable=False)

    def verify_rollback(self, receipt: BootstrapReceipt) -> bool:
        resources = self._current_resource_records(receipt)
        for resource in resources:
            live = self.get_dataset(resource.name, resource.version)
            if resource.disposition == 'created':
                if live is not None:
                    return False
            elif resource.disposition == 'adopted':
                if live is None or str(live.get('id')) != resource.resource_id:
                    return False
        return True


__all__ = [name for name in globals() if name.startswith('Foundry') or name == 'FoundryAdapter']
