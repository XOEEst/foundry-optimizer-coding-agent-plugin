from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
from azure.core.exceptions import HttpResponseError, ServiceRequestError

from foundry_opt.bootstrap.providers.foundry import (
    FoundryAdapter,
    FoundryOperationDeadlineError,
    FoundryPrerequisiteError,
    FoundryRegionUnsupportedError,
    FoundryUnsupportedCapabilityError,
)


class _Cred:
    def get_token(self, *scopes: str, **kwargs: object) -> str:
        del scopes, kwargs
        return 'token'


class _SdkValue:
    def __init__(self, payload: Mapping[str, object]) -> None:
        self._payload = dict(payload)

    def as_dict(self) -> Mapping[str, object]:
        return dict(self._payload)


class _Poller:
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id


class _Jobs:
    def __init__(self, create_result: _Poller | Exception | None = None, get_results: list[object] | None = None, list_result: list[object] | None = None) -> None:
        self.create_calls: list[tuple[object, str | None]] = []
        self.get_calls: list[str] = []
        self.create_result = create_result or _Poller('job-1')
        self.get_results = list(get_results or [])
        self.list_result = list_result or []

    def begin_create_generation_job(self, job: object, *, operation_id: str | None = None, **kwargs: object) -> _Poller:
        del kwargs
        self.create_calls.append((job, operation_id))
        if isinstance(self.create_result, Exception):
            raise self.create_result
        return self.create_result

    def get_generation_job(self, job_id: str, **kwargs: object) -> object:
        del kwargs
        self.get_calls.append(job_id)
        result = self.get_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def list(self, **kwargs: object) -> list[object]:
        del kwargs
        return list(self.list_result)

    def list_versions(self, name: str, **kwargs: object) -> list[object]:
        del name, kwargs
        return list(self.list_result)


class _Agents:
    def __init__(self, items: list[object], versions: list[object] | None = None) -> None:
        self.items = items
        self.versions = versions or []

    def list(self, **kwargs: object) -> list[object]:
        del kwargs
        return list(self.items)

    def list_versions(self, agent_name: str, **kwargs: object) -> list[object]:
        del agent_name, kwargs
        return list(self.versions)


class _Datasets:
    def __init__(self, items: list[object], versions: list[object] | None = None) -> None:
        self.items = items
        self.versions = versions or []
        self.create_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def list(self, **kwargs: object) -> list[object]:
        del kwargs
        return list(self.items)

    def list_versions(self, name: str, **kwargs: object) -> list[object]:
        del name, kwargs
        return list(self.versions)

    def create_version(self, *args: object, **kwargs: object) -> object:
        self.create_calls.append((args, kwargs))
        return _SdkValue({'name': args[0], 'version': kwargs.get('version', args[1] if len(args) > 1 and isinstance(args[1], str) else '1'), 'id': 'dataset-id'})


class _Connections:
    def __init__(self, items: list[object]) -> None:
        self.items = items

    def list(self, **kwargs: object) -> list[object]:
        del kwargs
        return list(self.items)


class _Deployments:
    def __init__(self, items: list[object]) -> None:
        self.items = items

    def list(self, **kwargs: object) -> list[object]:
        del kwargs
        return list(self.items)


class _Beta:
    def __init__(self, datasets: object | None = None, evaluators: object | None = None) -> None:
        if datasets is not None:
            self.datasets = datasets
        if evaluators is not None:
            self.evaluators = evaluators


class _Client:
    def __init__(self, *, beta: object | None = None, agents: object | None = None, datasets: object | None = None, connections: object | None = None, deployments: object | None = None) -> None:
        self.beta = beta
        self.agents = agents or _Agents([])
        self.datasets = datasets or _Datasets([])
        self.connections = connections or _Connections([])
        self.deployments = deployments or _Deployments([])


def test_inventory_and_probe_normalize_plain_mappings() -> None:
    client = _Client(
        beta=_Beta(evaluators=_Jobs(list_result=[_SdkValue({'name': 'content_safety', 'version': '1', 'id': 'builtin', 'evaluator_type': 'builtin', 'display_name': 'Content Safety'})]), datasets=_Jobs()),
        agents=_Agents([_SdkValue({'name': 'agent-a', 'id': 'a1', 'state': 'enabled', 'versions': {'latest': {'version': '7'}}})], versions=[_SdkValue({'name': 'agent-a', 'version': '7', 'status': 'active', 'draft': False})]),
        datasets=_Datasets([_SdkValue({'name': 'dataset-a', 'version': '15', 'id': 'd1', 'type': 'uri_file'})], versions=[_SdkValue({'name': 'dataset-a', 'version': '15', 'id': 'd1', 'type': 'uri_file'})]),
        connections=_Connections([_SdkValue({'name': 'appinsights', 'id': 'c1', 'type': 'ApplicationInsights', 'is_default': True})]),
        deployments=_Deployments([_SdkValue({'name': 'gpt-4o', 'type': 'model'})]),
    )
    adapter = FoundryAdapter('https://eastus.services.ai.azure.com/api/projects/demo', _Cred(), client=client)

    probe = adapter.probe_generation_capability(generation_model_deployment_name='gpt-4o')
    assert probe.beta_generation_supported is True
    assert adapter.inventory_agents()[0]['latest_version'] == '7'
    assert adapter.inventory_agent_versions('agent-a')[0]['raw']['version'] == '7'
    assert adapter.inventory_datasets()[0]['raw']['name'] == 'dataset-a'
    assert adapter.inventory_evaluators()[0]['raw']['display_name'] == 'Content Safety'
    assert adapter.resolve_builtin_content_safety()['name'] == 'content_safety'
    assert isinstance(adapter.inventory_agents()[0]['raw'], dict)


def test_create_dataset_generation_job_is_idempotent_and_polls_14_of_15_samples() -> None:
    dataset_jobs = _Jobs(get_results=[
        _SdkValue({'id': 'job-1', 'status': 'in_progress', 'created_at': datetime(2026, 1, 1, tzinfo=UTC)}),
        _SdkValue({'id': 'job-1', 'status': 'succeeded', 'created_at': datetime(2026, 1, 1, tzinfo=UTC), 'finished_at': datetime(2026, 1, 1, 0, 1, tzinfo=UTC), 'result': {'generated_samples': 14, 'outputs': [{'type': 'dataset', 'id': 'out-1', 'name': 'generated', 'version': '15'}]}}),
    ])
    client = _Client(beta=_Beta(datasets=dataset_jobs, evaluators=_Jobs()))
    sleeps: list[float] = []
    persisted: list[str] = []
    adapter = FoundryAdapter('https://eastus.services.ai.azure.com/api/projects/demo', _Cred(), client=client, sleep=sleeps.append)
    handle = adapter.create_dataset_generation_job({'agent_name': 'agent-a', 'agent_version': '7', 'max_samples': 30})
    again = adapter.create_dataset_generation_job({'agent_name': 'agent-a', 'agent_version': '7', 'max_samples': 30})
    assert handle.operation_id == again.operation_id
    result = adapter.poll_generation_job(handle, persist_before_poll=lambda op: persisted.append(op.operation_id), deadline_monotonic=999999999.0)
    assert persisted == [handle.operation_id]
    assert result['generated_samples'] == 14
    assert result['output_datasets'] == ({'id': 'out-1', 'name': 'generated', 'version': '15'},)
    assert sleeps == [1.0]
    assert dataset_jobs.create_calls[0][1] == handle.operation_id


def test_resume_generation_job_and_deadline() -> None:
    dataset_jobs = _Jobs(get_results=[_SdkValue({'id': 'job-1', 'status': 'in_progress', 'created_at': datetime(2026, 1, 1, tzinfo=UTC)})])
    client = _Client(beta=_Beta(datasets=dataset_jobs, evaluators=_Jobs()))
    ticks = iter([10.0, 10.0])
    adapter = FoundryAdapter('https://eastus.services.ai.azure.com/api/projects/demo', _Cred(), client=client, time_source=lambda: next(ticks), sleep=lambda _: None)
    with pytest.raises(FoundryOperationDeadlineError):
        adapter.poll_generation_job(adapter.create_dataset_generation_job({'dataset_name': 'x'}), deadline_monotonic=10.0)

    dataset_jobs2 = _Jobs(get_results=[_SdkValue({'id': 'job-2', 'status': 'succeeded', 'created_at': datetime(2026, 1, 1, tzinfo=UTC), 'result': {'generated_samples': 15, 'outputs': []}})])
    adapter2 = FoundryAdapter('https://eastus.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=dataset_jobs2, evaluators=_Jobs())))
    resumed = adapter2.resume_generation_job('dataset_generation', 'job-2', 'op-2')
    assert resumed['generated_samples'] == 15


def test_traces_require_companion_source_for_evaluator_generation() -> None:
    adapter = FoundryAdapter('https://eastus.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(), evaluators=_Jobs())))
    with pytest.raises(FoundryPrerequisiteError):
        adapter.create_evaluator_generation_job({'trace_sources': [{'name': 'trace-a'}]})


def test_unsupported_capability_and_region_errors_are_typed() -> None:
    adapter = FoundryAdapter('https://eastus.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=None, evaluators=None)))
    with pytest.raises(FoundryUnsupportedCapabilityError):
        adapter.inventory_evaluators()

    response = type('Response', (), {'status_code': 400, 'reason': 'Bad Request', 'headers': {}})()
    error = HttpResponseError(message='Region unsupported', response=response)
    jobs = _Jobs(create_result=error)
    adapter2 = FoundryAdapter('https://eastus.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=jobs, evaluators=_Jobs())))
    with pytest.raises(FoundryRegionUnsupportedError):
        adapter2.create_dataset_generation_job({'dataset_name': 'region-test'})


def test_create_or_adopt_dataset_reconciles_existing_and_does_not_leak_sdk_objects() -> None:
    datasets = _Datasets([], versions=[_SdkValue({'name': 'dataset-a', 'version': '1', 'id': 'd1', 'type': 'uri_file'})])
    adapter = FoundryAdapter('https://eastus.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(), evaluators=_Jobs()), datasets=datasets))
    adopted = adapter.create_or_adopt_dataset(dataset_name='dataset-a', dataset_version='1', dataset_content_uri='https://blob/data.jsonl', dataset_type='uri_file')
    assert adopted['adopted'] is True
    created = adapter.create_or_adopt_dataset(dataset_name='dataset-b', dataset_version='2', dataset_content_uri='https://blob/data.jsonl', dataset_type='uri_file')
    assert created['adopted'] is False
    assert isinstance(created['dataset'], dict)
    args, kwargs = datasets.create_calls[0]
    assert args[0] == 'dataset-b'


def test_network_errors_are_typed() -> None:
    jobs = _Jobs(create_result=ServiceRequestError('network down'))
    adapter = FoundryAdapter('https://eastus.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=jobs, evaluators=_Jobs())))
    with pytest.raises(Exception) as caught:
        adapter.create_dataset_generation_job({'dataset_name': 'x'})
    assert caught.value.kind == 'network'
