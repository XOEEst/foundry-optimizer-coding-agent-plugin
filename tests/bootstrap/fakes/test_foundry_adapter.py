from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

import pytest
from azure.core.exceptions import HttpResponseError, ServiceRequestError

from foundry_opt.bootstrap.contracts import BootstrapAction, BootstrapPlan, TemplatePayloadSpec
from foundry_opt.bootstrap.providers.foundry import (
    FoundryAdapter,
    FoundryOperationDeadlineError,
    FoundryOperationHandle,
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


class _PollingMethod:
    def __init__(self, operation_location: str = 'https://poll/job-1') -> None:
        self._operation_location = operation_location


class _Poller:
    def __init__(self, result_sequence: list[object], continuation: str = 'ct-1') -> None:
        self._results = list(result_sequence)
        self._continuation = continuation
        self._polling = _PollingMethod()

    def continuation_token(self) -> str:
        return self._continuation

    def polling_method(self) -> _PollingMethod:
        return self._polling

    def result(self, timeout: int = 0) -> object:
        del timeout
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _Jobs:
    def __init__(self, create_results: list[object] | None = None, list_result: list[object] | None = None) -> None:
        self.create_calls: list[tuple[object, str | None, str | None]] = []
        self.create_results = list(create_results or [])
        self.list_result = list_result or []

    def begin_create_generation_job(self, job: object, *, operation_id: str | None = None, continuation_token: str | None = None, **kwargs: object) -> object:
        del kwargs
        self.create_calls.append((job, operation_id, continuation_token))
        result = self.create_results.pop(0)
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
    def __init__(self, items: list[object], versions: list[object] | None = None, gets: dict[tuple[str, str], object] | None = None) -> None:
        self.items = items
        self.versions = versions or []
        self.gets = gets or {}
        self.create_calls: list[tuple[str, str, object]] = []
        self.delete_calls: list[tuple[str, str]] = []

    def list(self, **kwargs: object) -> list[object]:
        del kwargs
        return list(self.items)

    def list_versions(self, name: str, **kwargs: object) -> list[object]:
        del kwargs
        return [item for item in self.versions if _SdkValue(item.as_dict()).as_dict().get('name') == name] if self.versions and hasattr(self.versions[0], 'as_dict') else list(self.versions)

    def get(self, name: str, version: str, **kwargs: object) -> object:
        del kwargs
        return self.gets[(name, version)]

    def create_or_update(self, name: str, version: str, dataset_version: object, **kwargs: object) -> object:
        del kwargs
        self.create_calls.append((name, version, dataset_version))
        return _SdkValue({'name': name, 'version': version, 'id': f'azureai://accounts/a/projects/p/data/{name}/versions/{version}', 'type': getattr(dataset_version, 'type', None), 'dataUri': getattr(dataset_version, 'data_uri', None)})

    def delete_version(self, name: str, version: str, **kwargs: object) -> None:
        del kwargs
        self.delete_calls.append((name, version))


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


class _Project:
    def __init__(self, payload: Mapping[str, object]) -> None:
        self.payload = payload

    def get_metadata(self) -> Mapping[str, object]:
        return self.payload


class _Beta:
    def __init__(self, datasets: object | None = None, evaluators: object | None = None) -> None:
        if datasets is not None:
            self.datasets = datasets
        if evaluators is not None:
            self.evaluators = evaluators


class _Client:
    def __init__(self, *, beta: object | None = None, agents: object | None = None, datasets: object | None = None, connections: object | None = None, deployments: object | None = None, project: object | None = None) -> None:
        self.beta = beta
        self.agents = agents or _Agents([])
        self.datasets = datasets or _Datasets([])
        self.connections = connections or _Connections([])
        self.deployments = deployments or _Deployments([])
        self.project = project


def _plan() -> BootstrapPlan:
    return BootstrapPlan.create(
        operation_id='f' * 64,
        runtime_repository='https://github.com/example/runtime.git',
        runtime_commit='a' * 40,
        repository_identity='org/repo',
        actions=(BootstrapAction(action_id='dataset-a:1', phase='evaluations', stage='planned', kind='dataset', diagnostics=('dataset-a','1','https://blob/data.jsonl','uri_file')),),
    )


def test_capability_probe_modes_and_appinsights() -> None:
    client = _Client(
        beta=_Beta(datasets=_Jobs(create_results=[_Poller([])]), evaluators=_Jobs(create_results=[_Poller([])], list_result=[_SdkValue({'name': 'cs', 'version': '1', 'id': 'azureai://built-in/evaluators/content-safety', 'evaluator_type': 'builtin'})])),
        connections=_Connections([_SdkValue({'name': 'default', 'id': 'c1', 'type': 'AppInsights'})]),
        deployments=_Deployments([_SdkValue({'name': 'gpt-4o', 'type': 'model'})]),
        project=_Project({'region': 'westus3'}),
    )
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=client)
    synthetic = adapter.probe_generation_capability(mode='dataset_synthetic', generation_model_deployment_name='gpt-4o')
    traces = adapter.probe_generation_capability(mode='dataset_traces', generation_model_deployment_name='gpt-4o')
    assert synthetic.supported is True
    assert traces.app_insights_available is True
    assert traces.region == 'westus3'


def test_wrong_flat_schema_rejected() -> None:
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=_Jobs(create_results=[]))))
    with pytest.raises(FoundryPrerequisiteError):
        adapter.create_dataset_generation_job({'agent_name': 'flat-only'})


def test_real_poller_continuation_resume_and_deadline() -> None:
    poller = _Poller([TimeoutError(), _SdkValue({'id': 'job-1', 'status': 'succeeded', 'created_at': datetime(2026, 1, 1, tzinfo=UTC), 'result': {'generated_samples': 15, 'outputs': []}})], continuation='resume-1')
    jobs = _Jobs(create_results=[poller, poller, poller])
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=jobs, evaluators=_Jobs(create_results=[]))), sleep=lambda _: None, time_source=iter([1.0, 1.5, 2.0]).__next__)
    handle = adapter.create_dataset_generation_job({'sources': [{'type': 'agent', 'agent_name': 'a', 'agent_version': '7'}], 'options': {'type': 'simple_qna', 'max_samples': 30, 'model_options': {'model': 'gpt-4o'}}, 'scenario': 'evaluation'})
    assert handle.continuation_token == 'resume-1'
    persisted: list[FoundryOperationHandle] = []
    resumed = adapter.resume_generation_job(handle, persist_before_poll=persisted.append, deadline_monotonic=3.0)
    assert persisted[0].operation_id == handle.operation_id
    assert resumed['generated_samples'] == 15
    assert jobs.create_calls[1][2] == 'resume-1'


def test_dataset_create_or_update_and_adopt_verification() -> None:
    existing = _SdkValue({'name': 'dataset-a', 'version': '1', 'id': 'azureai://accounts/a/projects/p/data/dataset-a/versions/1', 'type': 'uri_file', 'dataUri': 'https://blob/data.jsonl'})
    datasets = _Datasets([], gets={('dataset-a', '1'): existing})
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=_Jobs(create_results=[])), datasets=datasets))
    adopted = adapter.create_or_adopt_dataset(dataset_name='dataset-a', dataset_version='1', dataset_content_uri='https://blob/data.jsonl', dataset_type='uri_file')
    assert adopted['adopted'] is True
    created = adapter.create_or_adopt_dataset(dataset_name='dataset-b', dataset_version='2', dataset_content_uri='https://blob/data.jsonl', dataset_type='uri_file')
    assert created['created'] is True
    assert datasets.create_calls[0][0:2] == ('dataset-b', '2')


def test_generated_samples_below_15_rejected_with_no_outputs() -> None:
    poller = _Poller([_SdkValue({'id': 'job-1', 'status': 'succeeded', 'created_at': datetime(2026, 1, 1, tzinfo=UTC), 'result': {'generated_samples': 14, 'outputs': [{'type': 'dataset', 'name': 'gen', 'version': '15'}]}})])
    jobs = _Jobs(create_results=[poller, poller])
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=jobs, evaluators=_Jobs(create_results=[]))))
    result = adapter.resume_generation_job(adapter.create_dataset_generation_job({'sources': [{'type': 'agent', 'agent_name': 'a'}], 'options': {'type': 'simple_qna', 'max_samples': 30, 'model_options': {'model': 'gpt-4o'}}, 'scenario': 'evaluation'}))
    assert result['outcome'] == 'rejected'
    assert result['output_datasets'] == ()


def test_traces_require_companion_source_and_content_safety_no_impersonation() -> None:
    evaluator_jobs = _Jobs(create_results=[] , list_result=[_SdkValue({'name': 'fake-content-safety', 'version': '1', 'id': 'azureai://accounts/a/projects/p/evaluators/content-safety/versions/1', 'evaluator_type': 'custom'}), _SdkValue({'name': 'content-safety', 'version': '1', 'id': 'azureai://built-in/evaluators/content-safety', 'evaluator_type': 'builtin'})])
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=evaluator_jobs)))
    with pytest.raises(FoundryPrerequisiteError):
        adapter.create_evaluator_generation_job({'sources': [{'type': 'traces', 'agent_name': 'a', 'start_time': datetime(2026,1,1,tzinfo=UTC)}], 'model': 'gpt-4o', 'evaluator_name': 'rubric'})
    resolved = adapter.resolve_builtin_content_safety()
    assert resolved['evaluator_type'] == 'builtin'
    assert resolved['id'] == 'azureai://built-in/evaluators/content-safety'


def test_plan_apply_rollback_lifecycle_and_canonical_operation_ids() -> None:
    datasets = _Datasets([], gets={('dataset-a', '1'): _SdkValue({'name': 'dataset-a', 'version': '1', 'id': 'azureai://accounts/a/projects/p/data/dataset-a/versions/1', 'type': 'uri_file', 'dataUri': 'https://blob/data.jsonl'})})
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=_Jobs(create_results=[])), datasets=datasets))
    first = adapter._operation_id('dataset-generation', {'b': 2, 'a': 1})
    second = adapter._operation_id('dataset-generation', {'a': 1, 'b': 2})
    assert first == second
    receipt = adapter.apply_resources(_plan())
    assert receipt.adopted_actions == ('dataset-a:1',)
    assert receipt.before_fingerprints and receipt.after_fingerprints
    assert adapter.verify_resources(receipt) is True
    adapter.rollback_resources(receipt)
    assert datasets.delete_calls == []


def test_plan_apply_partial_failure_rolls_back_only_created_assets() -> None:
    datasets = _Datasets([], gets={('dataset-new', '1'): _SdkValue({'name': 'dataset-new', 'version': '1', 'id': 'azureai://accounts/a/projects/p/data/dataset-new/versions/1', 'type': 'uri_file', 'dataUri': 'https://blob/new.jsonl'}), ('dataset-old', '1'): _SdkValue({'name': 'dataset-old', 'version': '1', 'id': 'azureai://accounts/a/projects/p/data/dataset-old/versions/1', 'type': 'uri_file', 'dataUri': 'https://blob/old.jsonl'})})
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=_Jobs(create_results=[])), datasets=datasets))
    created = adapter.create_or_adopt_dataset(dataset_name='dataset-new', dataset_version='1', dataset_content_uri='https://blob/new.jsonl', dataset_type='uri_file')
    receipt = adapter.apply_resources(BootstrapPlan.create(operation_id='e'*64, runtime_repository='https://github.com/example/runtime.git', runtime_commit='a'*40, repository_identity='org/repo', actions=(BootstrapAction(action_id='dataset-new:1', phase='evaluations', stage='planned', kind='dataset', diagnostics=('dataset-new','1','https://blob/new.jsonl','uri_file')),)))
    assert created['dataset']['id'] in receipt.created_actions or receipt.adopted_actions == ('dataset-new:1',)
    adapter.rollback_resources(receipt)
    if receipt.created_actions:
        assert datasets.delete_calls == [('dataset-new', '1')]


def test_region_and_network_errors_are_sanitized() -> None:
    response = type('Response', (), {'status_code': 400, 'reason': 'Bad Request', 'headers': {}})()
    region_error = HttpResponseError(message='Region unsupported raw body secret', response=response)
    jobs = _Jobs(create_results=[region_error])
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=jobs, evaluators=_Jobs(create_results=[]))))
    with pytest.raises(FoundryRegionUnsupportedError) as caught:
        adapter.create_dataset_generation_job({'sources': [{'type': 'agent', 'agent_name': 'a'}], 'options': {'type': 'simple_qna', 'max_samples': 30, 'model_options': {'model': 'gpt-4o'}}, 'scenario': 'evaluation'})
    assert 'secret' not in str(caught.value)
    jobs2 = _Jobs(create_results=[ServiceRequestError('network secret body')])
    adapter2 = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=jobs2, evaluators=_Jobs(create_results=[]))))
    with pytest.raises(Exception) as caught2:
        adapter2.create_dataset_generation_job({'sources': [{'type': 'agent', 'agent_name': 'a'}], 'options': {'type': 'simple_qna', 'max_samples': 30, 'model_options': {'model': 'gpt-4o'}}, 'scenario': 'evaluation'})
    assert 'secret' not in str(caught2.value)
