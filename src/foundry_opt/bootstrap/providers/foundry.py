from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import time
from typing import Any

from azure.core.exceptions import ClientAuthenticationError, HttpResponseError, ServiceRequestError
from azure.core.polling import LROPoller
from azure.core.credentials import TokenCredential
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import FileDatasetVersion, FolderDatasetVersion

from foundry_opt.bootstrap.errors import BootstrapProviderError
from foundry_opt.models import FrozenModel


def _as_mapping(value: object) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return value
    as_dict = getattr(value, 'as_dict', None)
    if callable(as_dict):
        data = as_dict()
        if isinstance(data, Mapping):
            return data
    raise BootstrapProviderError(f'expected mapping-compatible SDK value, got {type(value).__name__}')


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
    if hasattr(value, '__dict__'):
        return {k: _plain(v) for k, v in vars(value).items() if not k.startswith('_')}
    return repr(value)


def _required_text(mapping: Mapping[str, object], key: str) -> str:
    value = mapping.get(key)
    if not isinstance(value, str) or not value:
        raise BootstrapProviderError(f'missing required string field {key!r}')
    return value


def _optional_text(mapping: Mapping[str, object], key: str) -> str | None:
    value = mapping.get(key)
    return value if isinstance(value, str) and value else None


def _sdk_error_code(exc: BaseException) -> str | None:
    if isinstance(exc, HttpResponseError):
        error = getattr(exc, 'error', None)
        code = getattr(error, 'code', None)
        if isinstance(code, str) and code:
            return code
    return None


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


@dataclass(frozen=True, slots=True)
class FoundryOperationHandle:
    operation_id: str
    job_kind: str
    job_id: str | None
    status: str
    created: bool


class FoundryCapabilityProbe(FrozenModel):
    beta_generation_supported: bool
    preview_required: bool
    app_insights_available: bool
    reasons: tuple[str, ...] = ()
    region: str | None = None
    model_deployments: tuple[str, ...] = ()


class FoundryAdapter:
    def __init__(
        self,
        project_endpoint: str,
        credential: TokenCredential,
        *,
        client: object | None = None,
        time_source: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
        default_poll_interval: float = 1.0,
    ) -> None:
        self._project_endpoint = project_endpoint
        self._credential = credential
        self._client = client if client is not None else AIProjectClient(project_endpoint, credential)
        self._time = time_source or time.monotonic
        self._sleep = sleep or time.sleep
        self._default_poll_interval = default_poll_interval

    def _beta(self, attr: str) -> object:
        beta = getattr(self._client, 'beta', None)
        if beta is None:
            raise FoundryUnsupportedCapabilityError('azure-ai-projects beta surface is unavailable', kind='unsupported_preview')
        value = getattr(beta, attr, None)
        if value is None:
            raise FoundryUnsupportedCapabilityError(f'azure-ai-projects beta.{attr} is unavailable', kind='unsupported_preview')
        return value

    def _classify_error(self, exc: BaseException) -> FoundryAdapterError:
        if isinstance(exc, FoundryAdapterError):
            return exc
        if isinstance(exc, ServiceRequestError):
            return FoundryNetworkError('foundry request failed before reaching the service', kind='network', retryable=True)
        if isinstance(exc, ClientAuthenticationError):
            return FoundryPermissionError('foundry authentication failed', kind='permission', retryable=False)
        if isinstance(exc, HttpResponseError):
            status = getattr(exc, 'status_code', None)
            code = _sdk_error_code(exc)
            message = str(exc)
            lowered = message.lower()
            if status in {401, 403}:
                return FoundryPermissionError(message, kind='permission', retryable=False, status_code=status, code=code)
            if status == 404 and ('feature' in lowered or 'preview' in lowered):
                return FoundryUnsupportedCapabilityError(message, kind='unsupported_preview', retryable=False, status_code=status, code=code)
            if status == 400 and ('region' in lowered or 'location' in lowered):
                return FoundryRegionUnsupportedError(message, kind='unsupported_region', retryable=False, status_code=status, code=code)
            if status == 412 or 'prerequisite' in lowered or 'app insights' in lowered:
                return FoundryPrerequisiteError(message, kind='prerequisite', retryable=False, status_code=status, code=code)
            return FoundryPlatformError(message, kind='platform', retryable=status is not None and status >= 500, status_code=status, code=code)
        return FoundryPlatformError(str(exc), kind='platform', retryable=False)

    def probe_generation_capability(self, *, generation_model_deployment_name: str | None = None) -> FoundryCapabilityProbe:
        try:
            beta_datasets = self._beta('datasets')
            beta_evaluators = self._beta('evaluators')
            connections = list(self._client.connections.list())
            deployments = list(self._client.deployments.list())
        except Exception as exc:
            raise self._classify_error(exc) from exc
        reasons: list[str] = []
        connection_rows = [_plain(_as_mapping(item)) for item in connections]
        app_insights = any('applicationinsights' in str(row.get('type', '')).casefold() or 'appinsights' in str(row.get('name', '')).casefold() for row in connection_rows)
        if not app_insights:
            reasons.append('application insights connection unavailable')
        deployment_names = tuple(sorted(str(_as_mapping(item).get('name')) for item in deployments if _as_mapping(item).get('name')))
        if generation_model_deployment_name and generation_model_deployment_name not in deployment_names:
            reasons.append(f'generation deployment {generation_model_deployment_name!r} unavailable')
        endpoint_bits = self._project_endpoint.split('.')
        region = endpoint_bits[0].split('https://')[-1] if endpoint_bits else None
        return FoundryCapabilityProbe(
            beta_generation_supported=hasattr(beta_datasets, 'begin_create_generation_job') and hasattr(beta_evaluators, 'begin_create_generation_job') and app_insights,
            preview_required=True,
            app_insights_available=app_insights,
            reasons=tuple(reasons),
            region=region,
            model_deployments=deployment_names,
        )

    def inventory_agents(self) -> tuple[Mapping[str, object], ...]:
        try:
            agents = list(self._client.agents.list())
        except Exception as exc:
            raise self._classify_error(exc) from exc
        rows: list[Mapping[str, object]] = []
        for agent in agents:
            data = _plain(_as_mapping(agent))
            versions = _as_mapping(agent).get('versions')
            latest = None
            if isinstance(versions, Mapping):
                latest_node = versions.get('latest')
                if isinstance(latest_node, Mapping):
                    latest = latest_node.get('version')
            rows.append({'name': data.get('name'), 'id': data.get('id'), 'state': data.get('state'), 'latest_version': latest, 'raw': data})
        return tuple(rows)

    def inventory_agent_versions(self, agent_name: str) -> tuple[Mapping[str, object], ...]:
        try:
            versions = list(self._client.agents.list_versions(agent_name))
        except Exception as exc:
            raise self._classify_error(exc) from exc
        return tuple({'name': _as_mapping(v).get('name'), 'version': _as_mapping(v).get('version'), 'status': _as_mapping(v).get('status'), 'draft': bool(_as_mapping(v).get('draft')), 'raw': _plain(_as_mapping(v))} for v in versions)

    def inventory_datasets(self) -> tuple[Mapping[str, object], ...]:
        try:
            datasets = list(self._client.datasets.list())
        except Exception as exc:
            raise self._classify_error(exc) from exc
        return tuple({'name': _as_mapping(d).get('name'), 'version': _as_mapping(d).get('version'), 'id': _as_mapping(d).get('id'), 'type': _as_mapping(d).get('type'), 'raw': _plain(_as_mapping(d))} for d in datasets)

    def inventory_dataset_versions(self, dataset_name: str) -> tuple[Mapping[str, object], ...]:
        try:
            versions = list(self._client.datasets.list_versions(dataset_name))
        except Exception as exc:
            raise self._classify_error(exc) from exc
        return tuple({'name': _as_mapping(v).get('name'), 'version': _as_mapping(v).get('version'), 'id': _as_mapping(v).get('id'), 'type': _as_mapping(v).get('type'), 'raw': _plain(_as_mapping(v))} for v in versions)

    def inventory_evaluators(self, *, include_builtin: bool = True) -> tuple[Mapping[str, object], ...]:
        try:
            evaluators = list(self._beta('evaluators').list(type='all' if include_builtin else 'custom'))
        except Exception as exc:
            raise self._classify_error(exc) from exc
        return tuple({'name': _as_mapping(e).get('name'), 'version': _as_mapping(e).get('version'), 'id': _as_mapping(e).get('id'), 'evaluator_type': _as_mapping(e).get('evaluator_type'), 'display_name': _as_mapping(e).get('display_name'), 'raw': _plain(_as_mapping(e))} for e in evaluators)

    def inventory_evaluator_versions(self, evaluator_name: str, *, include_builtin: bool = True) -> tuple[Mapping[str, object], ...]:
        try:
            evaluators = list(self._beta('evaluators').list_versions(evaluator_name, type='all' if include_builtin else 'custom'))
        except Exception as exc:
            raise self._classify_error(exc) from exc
        return tuple({'name': _as_mapping(e).get('name'), 'version': _as_mapping(e).get('version'), 'id': _as_mapping(e).get('id'), 'evaluator_type': _as_mapping(e).get('evaluator_type'), 'raw': _plain(_as_mapping(e))} for e in evaluators)

    def inventory_connections(self) -> tuple[Mapping[str, object], ...]:
        try:
            connections = list(self._client.connections.list())
        except Exception as exc:
            raise self._classify_error(exc) from exc
        return tuple({'name': _as_mapping(c).get('name'), 'id': _as_mapping(c).get('id'), 'type': _as_mapping(c).get('type'), 'is_default': bool(_as_mapping(c).get('is_default')), 'raw': _plain(_as_mapping(c))} for c in connections)

    def inventory_model_deployments(self) -> tuple[Mapping[str, object], ...]:
        try:
            deployments = list(self._client.deployments.list())
        except Exception as exc:
            raise self._classify_error(exc) from exc
        return tuple({'name': _as_mapping(d).get('name'), 'type': _as_mapping(d).get('type'), 'raw': _plain(_as_mapping(d))} for d in deployments)

    def get_dataset(self, dataset_name: str, dataset_version: str) -> Mapping[str, object] | None:
        for item in self.inventory_dataset_versions(dataset_name):
            if item.get('version') == dataset_version:
                return item
        return None

    def create_or_adopt_dataset(self, *, dataset_name: str, dataset_version: str, dataset_content_uri: str, dataset_type: str, connection_name: str | None = None, description: str | None = None, tags: Mapping[str, str] | None = None) -> Mapping[str, object]:
        existing = self.get_dataset(dataset_name, dataset_version)
        if existing is not None:
            return {'adopted': True, 'dataset': existing}
        payload_cls = FileDatasetVersion if dataset_type == 'uri_file' else FolderDatasetVersion
        payload = payload_cls({'dataUri': dataset_content_uri, 'type': dataset_type, 'connectionName': connection_name, 'description': description, 'tags': dict(tags or {})})
        create_version = getattr(self._client.datasets, 'create_version', None)
        if not callable(create_version):
            raise FoundryUnsupportedCapabilityError('dataset create_version is unavailable in installed SDK', kind='unsupported_preview')
        try:
            created = create_version(dataset_name, dataset_version, payload)
        except TypeError:
            try:
                created = create_version(dataset_name, payload, version=dataset_version)
            except Exception as exc:
                raise self._classify_error(exc) from exc
        except Exception as exc:
            raise self._classify_error(exc) from exc
        return {'adopted': False, 'dataset': {'name': _as_mapping(created).get('name'), 'version': _as_mapping(created).get('version'), 'id': _as_mapping(created).get('id'), 'raw': _plain(_as_mapping(created))}}

    def resolve_builtin_content_safety(self) -> Mapping[str, object]:
        for item in self.inventory_evaluators(include_builtin=True):
            name = str(item.get('name') or '')
            display_name = str(item.get('display_name') or '')
            if name.casefold() == 'content_safety' or display_name.casefold() == 'content safety':
                return item
        raise FoundryUnsupportedCapabilityError('built-in Content Safety evaluator is unavailable', kind='unsupported_preview')

    def _operation_id(self, kind: str, payload: Mapping[str, object]) -> str:
        digest = hashlib.sha256(repr(sorted((str(k), repr(v)) for k, v in payload.items())).encode('utf-8')).hexdigest()[:24]
        return f'foundry-{kind}-{digest}'

    def create_dataset_generation_job(self, request: Mapping[str, object]) -> FoundryOperationHandle:
        operation_id = str(request.get('operation_id') or self._operation_id('dataset-generation', request))
        create = self._beta('datasets').begin_create_generation_job
        try:
            poller = create(dict(request), operation_id=operation_id)
        except Exception as exc:
            raise self._classify_error(exc) from exc
        job_id = getattr(poller, '_job_id', None) or getattr(poller, 'job_id', None)
        if job_id is None:
            details = getattr(poller, 'details', None)
            if isinstance(details, Mapping):
                job_id = details.get('id')
        return FoundryOperationHandle(operation_id=operation_id, job_kind='dataset_generation', job_id=str(job_id) if job_id else None, status='queued', created=True)

    def create_evaluator_generation_job(self, request: Mapping[str, object]) -> FoundryOperationHandle:
        traces = request.get('trace_sources') or request.get('traces')
        if traces and not (request.get('agent_source') or request.get('prompt_source') or request.get('dataset_source')):
            raise FoundryPrerequisiteError('traces require companion agent, prompt, or dataset source', kind='prerequisite')
        operation_id = str(request.get('operation_id') or self._operation_id('evaluator-generation', request))
        create = self._beta('evaluators').begin_create_generation_job
        try:
            poller = create(dict(request), operation_id=operation_id)
        except Exception as exc:
            raise self._classify_error(exc) from exc
        job_id = getattr(poller, '_job_id', None) or getattr(poller, 'job_id', None)
        return FoundryOperationHandle(operation_id=operation_id, job_kind='evaluator_generation', job_id=str(job_id) if job_id else None, status='queued', created=True)

    def poll_generation_job(self, handle: FoundryOperationHandle, *, persist_before_poll: Callable[[FoundryOperationHandle], None] | None = None, deadline_monotonic: float | None = None, poll_interval: float | None = None) -> Mapping[str, object]:
        if persist_before_poll is not None:
            persist_before_poll(handle)
        poll_interval = self._default_poll_interval if poll_interval is None else poll_interval
        getter = self._beta('datasets').get_generation_job if handle.job_kind == 'dataset_generation' else self._beta('evaluators').get_generation_job
        if not handle.job_id:
            raise FoundryPrerequisiteError('cannot poll generation job without a job id', kind='prerequisite')
        while True:
            try:
                job = getter(handle.job_id)
            except Exception as exc:
                raise self._classify_error(exc) from exc
            data = _as_mapping(job)
            status = str(data.get('status') or '').casefold()
            if status in {'succeeded', 'failed', 'cancelled'}:
                return self._normalize_job_result(handle.job_kind, data)
            if deadline_monotonic is not None and self._time() >= deadline_monotonic:
                raise FoundryOperationDeadlineError(f'polling deadline exceeded for {handle.operation_id}', kind='deadline', retryable=True)
            self._sleep(poll_interval)

    def resume_generation_job(self, job_kind: str, job_id: str, operation_id: str) -> Mapping[str, object]:
        handle = FoundryOperationHandle(operation_id=operation_id, job_kind=job_kind, job_id=job_id, status='queued', created=False)
        return self.poll_generation_job(handle)

    def _normalize_job_result(self, job_kind: str, job: Mapping[str, object]) -> Mapping[str, object]:
        result = job.get('result')
        plain_result = _plain(result) if result is not None else None
        normalized: MutableMapping[str, object] = {
            'job_id': job.get('id'),
            'status': job.get('status'),
            'created_at': _plain(job.get('created_at')),
            'finished_at': _plain(job.get('finished_at')),
            'error': _plain(job.get('error')),
            'result': plain_result,
        }
        if job_kind == 'dataset_generation' and isinstance(result, Mapping):
            outputs = result.get('outputs')
            dataset_outputs = [item for item in outputs if isinstance(item, Mapping) and item.get('type') == 'dataset'] if isinstance(outputs, Sequence) else []
            normalized['generated_samples'] = result.get('generated_samples')
            normalized['output_datasets'] = tuple({'id': item.get('id'), 'name': item.get('name'), 'version': item.get('version')} for item in dataset_outputs)
        elif job_kind == 'evaluator_generation' and isinstance(result, Mapping):
            normalized['saved_evaluator'] = {'id': result.get('id'), 'name': result.get('name'), 'version': result.get('version'), 'display_name': result.get('display_name')}
        return normalized


__all__ = [
    'FoundryAdapter',
    'FoundryAdapterError',
    'FoundryCapabilityProbe',
    'FoundryNetworkError',
    'FoundryOperationDeadlineError',
    'FoundryOperationHandle',
    'FoundryPermissionError',
    'FoundryPlatformError',
    'FoundryPrerequisiteError',
    'FoundryRegionUnsupportedError',
    'FoundryUnsupportedCapabilityError',
]
