from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
import json

import httpx
import openai
import pytest
from azure.core.exceptions import HttpResponseError, ResourceNotFoundError, ServiceRequestError

from foundry_opt.bootstrap.contracts import BootstrapAction, BootstrapPlan, TemplatePayloadSpec
from foundry_opt.bootstrap.providers.foundry import (
    FoundryAdapter,
    FoundryOperationDeadlineError,
    FoundryOperationHandle,
    FoundryPrerequisiteError,
    FoundryRegionUnsupportedError,
    FoundryRollbackError,
    FoundryUnsupportedCapabilityError,
)


def _not_found_error() -> openai.NotFoundError:
    response = httpx.Response(404, request=httpx.Request('GET', 'https://example.test'))
    return openai.NotFoundError('not found', response=response, body=None)



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
    def __init__(self, result_sequence: list[object], continuation: str = 'ct-1', done_sequence: list[bool] | None = None) -> None:
        self._results = list(result_sequence)
        self._continuation = continuation
        self._polling = _PollingMethod()
        self._done_sequence = list(done_sequence or [False, True])
        self.result_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def continuation_token(self) -> str:
        return self._continuation

    def polling_method(self) -> _PollingMethod:
        return self._polling

    def done(self) -> bool:
        return self._done_sequence.pop(0) if self._done_sequence else True

    def result(self, *args: object, **kwargs: object) -> object:
        self.result_calls.append((args, kwargs))
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _Jobs:
    def __init__(self, create_results: list[object] | None = None, list_result: list[object] | None = None, *, versions: dict[tuple[str, str], object] | None = None, fail_delete_version: set[tuple[str, str]] | None = None) -> None:
        self.create_calls: list[tuple[object, str | None, str | None]] = []
        self.create_results = list(create_results or [])
        self.list_result = list_result or []
        self.versions = versions or {}
        self.delete_version_calls: list[tuple[str, str]] = []
        self.fail_delete_version = fail_delete_version or set()

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

    def get_version(self, name: str, version: str, **kwargs: object) -> object:
        del kwargs
        if (name, version) not in self.versions:
            raise ResourceNotFoundError(message='not found')
        return self.versions[(name, version)]

    def delete_version(self, name: str, version: str, **kwargs: object) -> None:
        del kwargs
        if (name, version) in self.fail_delete_version:
            raise RuntimeError('delete_version failed')
        self.delete_version_calls.append((name, version))
        self.versions.pop((name, version), None)


class _Agents:
    def __init__(self, items: list[object], versions: list[object] | None = None, *, fail_delete_version: set[tuple[str, str]] | None = None) -> None:
        self.items = items
        self.versions = versions or []
        self.delete_version_calls: list[tuple[str, str]] = []
        self.fail_delete_version = fail_delete_version or set()

    def list(self, **kwargs: object) -> list[object]:
        del kwargs
        return list(self.items)

    def list_versions(self, agent_name: str, **kwargs: object) -> list[object]:
        del agent_name, kwargs
        return list(self.versions)

    def delete_version(self, agent_name: str, agent_version: str, **kwargs: object) -> None:
        del kwargs
        if (agent_name, agent_version) in self.fail_delete_version:
            raise RuntimeError('delete_version failed')
        self.delete_version_calls.append((agent_name, agent_version))



class _Datasets:
    def __init__(self, items: list[object], versions: list[object] | None = None, gets: dict[tuple[str, str], object] | None = None, *, fail_delete: set[tuple[str, str]] | None = None) -> None:
        self.items = items
        self.versions = versions or []
        self.gets = gets or {}
        self.create_calls: list[tuple[str, str, object]] = []
        self.delete_calls: list[tuple[str, str]] = []
        self.fail_delete = fail_delete or set()

    def list(self, **kwargs: object) -> list[object]:
        del kwargs
        return list(self.items)

    def list_versions(self, name: str, **kwargs: object) -> list[object]:
        del kwargs
        return [item for item in self.versions if _SdkValue(item.as_dict()).as_dict().get('name') == name] if self.versions and hasattr(self.versions[0], 'as_dict') else list(self.versions)

    def get(self, name: str, version: str, **kwargs: object) -> object:
        del kwargs
        if (name, version) not in self.gets:
            raise ResourceNotFoundError(message='not found')
        return self.gets[(name, version)]

    def create_or_update(self, name: str, version: str, dataset_version: object, **kwargs: object) -> object:
        del kwargs
        self.create_calls.append((name, version, dataset_version))
        value = _SdkValue({'name': name, 'version': version, 'id': f'azureai://accounts/a/projects/p/data/{name}/versions/{version}', 'type': getattr(dataset_version, 'type', None), 'dataUri': getattr(dataset_version, 'data_uri', None), 'tags': getattr(dataset_version, 'tags', None) or {}})
        self.gets[(name, version)] = value
        return value

    def delete(self, name: str, version: str, **kwargs: object) -> None:
        del kwargs
        if (name, version) in self.fail_delete:
            raise RuntimeError('delete failed')
        self.delete_calls.append((name, version))
        self.gets.pop((name, version), None)


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


class _EvalObject:
    def __init__(self, eval_id: str, name: str) -> None:
        self.id = eval_id
        self.name = name


class _RunObject:
    def __init__(self, run_id: str) -> None:
        self.id = run_id


class _OpenAIRuns:
    def __init__(self) -> None:
        self.create_calls: list[tuple[str, Mapping[str, object]]] = []
        self.delete_calls: list[tuple[str, str]] = []
        self.items: set[str] = set()
        self._next_id = 0
        self.fail_create = False

    def create(self, eval_id: str, *, data_source: Mapping[str, object], name: str | None = None) -> _RunObject:
        del name
        if self.fail_create:
            raise RuntimeError('run submission failed')
        self.create_calls.append((eval_id, data_source))
        self._next_id += 1
        run_id = f'run-{self._next_id}'
        self.items.add(run_id)
        return _RunObject(run_id)

    def retrieve(self, run_id: str, *, eval_id: str) -> _RunObject:
        del eval_id
        if run_id not in self.items:
            raise _not_found_error()
        return _RunObject(run_id)

    def delete(self, run_id: str, *, eval_id: str) -> None:
        self.delete_calls.append((eval_id, run_id))
        self.items.discard(run_id)


class _OpenAIEvals:
    def __init__(self, existing: list[_EvalObject] | None = None) -> None:
        self.items: dict[str, _EvalObject] = {item.id: item for item in (existing or [])}
        self.create_calls: list[Mapping[str, object]] = []
        self.delete_calls: list[str] = []
        self.runs = _OpenAIRuns()
        self._next_id = 0
        self.fail_create = False

    def list(self) -> list[_EvalObject]:
        return list(self.items.values())

    def create(self, *, data_source_config: Mapping[str, object], testing_criteria: list[Mapping[str, object]], name: str) -> _EvalObject:
        if self.fail_create:
            raise RuntimeError('eval creation failed')
        self.create_calls.append({'data_source_config': data_source_config, 'testing_criteria': testing_criteria, 'name': name})
        self._next_id += 1
        created = _EvalObject(f'eval_{self._next_id}', name)
        self.items[created.id] = created
        return created

    def retrieve(self, eval_id: str) -> _EvalObject:
        if eval_id not in self.items:
            raise _not_found_error()
        return self.items[eval_id]

    def delete(self, eval_id: str) -> None:
        self.delete_calls.append(eval_id)
        self.items.pop(eval_id, None)


class _OpenAIClient:
    def __init__(self, evals: _OpenAIEvals | None = None) -> None:
        self.evals = evals or _OpenAIEvals()


class _Client:
    def __init__(self, *, beta: object | None = None, agents: object | None = None, datasets: object | None = None, connections: object | None = None, deployments: object | None = None, project: object | None = None, openai_client: _OpenAIClient | None = None) -> None:
        self.beta = beta
        self.agents = agents or _Agents([])
        self.datasets = datasets or _Datasets([])
        self.connections = connections or _Connections([])
        self.deployments = deployments or _Deployments([])
        self.project = project
        self._openai_client = openai_client or _OpenAIClient()

    def get_openai_client(self) -> _OpenAIClient:
        return self._openai_client


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
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(create_results=[_Poller([], done_sequence=[True]), _Poller([], done_sequence=[True])]), evaluators=_Jobs(create_results=[]))))
    with pytest.raises(FoundryPrerequisiteError):
        adapter.create_dataset_generation_job({'agent_name': 'flat-only'})


def test_real_poller_continuation_resume_and_deadline() -> None:
    poller = _Poller([_SdkValue({'id': 'job-1', 'status': 'succeeded', 'generated_samples': 15, 'outputs': [], 'created_at': datetime(2026, 1, 1, tzinfo=UTC)})], continuation='resume-1', done_sequence=[False, True])
    jobs = _Jobs(create_results=[poller, poller])
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=jobs, evaluators=_Jobs(create_results=[]))), sleep=lambda _: None, time_source=iter([1.0, 1.5, 2.0]).__next__)
    handle = adapter.create_dataset_generation_job({'sources': [{'type': 'agent', 'agent_name': 'a', 'agent_version': '7'}], 'options': {'type': 'simple_qna', 'max_samples': 30, 'model_options': {'model': 'gpt-4o'}}, 'scenario': 'evaluation'})
    assert handle.continuation_token == 'resume-1'
    persisted: list[FoundryOperationHandle] = []
    resumed = adapter.resume_generation_job(handle, persist_before_poll=persisted.append, deadline_monotonic=3.0)
    assert persisted[0].operation_id == handle.operation_id
    assert resumed['generated_samples'] == 15
    assert jobs.create_calls[1][2] == 'resume-1'
    assert poller.result_calls == [((), {})]


def test_dataset_create_or_update_and_adopt_verification() -> None:
    existing = _SdkValue({'name': 'dataset-a', 'version': '1', 'id': 'azureai://accounts/a/projects/p/data/dataset-a/versions/1', 'type': 'uri_file', 'dataUri': 'https://blob/data.jsonl'})
    datasets = _Datasets([], gets={('dataset-a', '1'): existing})
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=_Jobs(create_results=[])), datasets=datasets))
    adopted = adapter.create_or_adopt_dataset(operation_id='op', action_id='dataset-a:1', dataset_name='dataset-a', dataset_version='1', dataset_content_uri='https://blob/data.jsonl', dataset_type='uri_file')
    assert adopted['adopted'] is True
    created = adapter.create_or_adopt_dataset(operation_id='op', action_id='dataset-b:2', dataset_name='dataset-b', dataset_version='2', dataset_content_uri='https://blob/data.jsonl', dataset_type='uri_file')
    assert created['created'] is True
    assert datasets.create_calls[0][0:2] == ('dataset-b', '2')
    assert getattr(datasets.create_calls[0][2], 'tags') == {'foundry_opt_operation': adapter._ownership_token('op', 'dataset-b:2')}


def test_generated_samples_below_15_rejected_with_no_outputs() -> None:
    poller = _Poller([_SdkValue({'id': 'job-1', 'status': 'succeeded', 'generated_samples': 14, 'outputs': [{'type': 'dataset', 'name': 'gen', 'version': '15'}], 'created_at': datetime(2026, 1, 1, tzinfo=UTC)})], done_sequence=[True])
    jobs = _Jobs(create_results=[poller, poller])
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=jobs, evaluators=_Jobs(create_results=[]))))
    result = adapter.resume_generation_job(adapter.create_dataset_generation_job({'sources': [{'type': 'agent', 'agent_name': 'a'}], 'options': {'type': 'simple_qna', 'max_samples': 30, 'model_options': {'model': 'gpt-4o'}}, 'scenario': 'evaluation'}))
    assert result['outcome'] == 'rejected'
    assert result['output_datasets'] == ()


def test_traces_require_companion_source_and_content_safety_no_impersonation() -> None:
    evaluator_jobs = _Jobs(create_results=[] , list_result=[_SdkValue({'name': 'fake-content-safety', 'version': '1', 'id': 'azureai://accounts/a/projects/p/evaluators/content_safety/versions/1', 'evaluator_type': 'custom'}), _SdkValue({'name': 'content-safety', 'version': '1', 'id': 'azureai://built-in/evaluators/content_safety', 'evaluator_type': 'builtin'})])
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=evaluator_jobs)))
    with pytest.raises(FoundryPrerequisiteError):
        adapter.create_evaluator_generation_job({'sources': [{'type': 'traces', 'agent_name': 'a', 'start_time': datetime(2026,1,1,tzinfo=UTC)}], 'model': 'gpt-4o', 'evaluator_name': 'rubric'})
    resolved = adapter.resolve_builtin_content_safety()
    assert resolved['evaluator_type'] == 'builtin'
    assert resolved['id'] == 'azureai://built-in/evaluators/content_safety'


def test_plan_apply_rollback_lifecycle_and_canonical_operation_ids() -> None:
    datasets = _Datasets([], gets={('dataset-a', '1'): _SdkValue({'name': 'dataset-a', 'version': '1', 'id': 'azureai://accounts/a/projects/p/data/dataset-a/versions/1', 'type': 'uri_file', 'dataUri': 'https://blob/data.jsonl'})})
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=_Jobs(create_results=[])), datasets=datasets))
    first = adapter._operation_id('dataset-generation', {'b': 2, 'a': 1})
    second = adapter._operation_id('dataset-generation', {'a': 1, 'b': 2})
    assert first == second
    receipt = adapter.apply_resources(_plan())
    assert receipt.adopted_actions == ('dataset-a:1',)
    assert receipt.before_fingerprints and receipt.after_fingerprints
    assert receipt.changed_actions == ()
    state = adapter.export_provider_state(receipt)
    assert state['repository_identity'] == 'org/repo'
    assert state['resources'][0]['disposition'] == 'adopted'
    assert len(state['resources'][0]['fingerprint']) == 64
    assert adapter.get_dataset('dataset-a', '1')['content_fingerprint'] == state['resources'][0]['fingerprint']
    assert adapter.verify_resources(receipt) is True
    restored = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=_Jobs(create_results=[])), datasets=datasets))
    restored.restore_provider_state(state)
    assert restored.verify_resources(receipt) is True
    adapter.rollback_resources(receipt)
    assert datasets.delete_calls == []
    assert adapter.verify_rollback(receipt) is True


def test_plan_apply_partial_failure_rolls_back_only_created_assets() -> None:
    datasets = _Datasets([], gets={('dataset-old', '1'): _SdkValue({'name': 'dataset-old', 'version': '1', 'id': 'azureai://accounts/a/projects/p/data/dataset-old/versions/1', 'type': 'uri_file', 'dataUri': 'https://blob/old.jsonl'})})
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=_Jobs(create_results=[])), datasets=datasets))
    receipt = adapter.apply_resources(BootstrapPlan.create(operation_id='e'*64, runtime_repository='https://github.com/example/runtime.git', runtime_commit='a'*40, repository_identity='org/repo', actions=(BootstrapAction(action_id='dataset-new:1', phase='evaluations', stage='planned', kind='dataset', diagnostics=('dataset-new','1','https://blob/new.jsonl','uri_file')),)))
    assert receipt.created_actions == ('dataset-new:1',)
    assert receipt.changed_actions == ()
    state = adapter.export_provider_state(receipt)
    restored = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=_Jobs(create_results=[])), datasets=datasets))
    restored.restore_provider_state(state)
    adapter.rollback_resources(receipt)
    assert datasets.delete_calls == [('dataset-new', '1')]
    assert restored.verify_rollback(receipt) is True
    datasets.gets[('dataset-old', '1')] = _SdkValue({'name': 'dataset-old', 'version': '1', 'id': 'azureai://accounts/a/projects/p/data/dataset-old/versions/1', 'type': 'uri_file', 'dataUri': 'https://blob/changed.jsonl'})
    assert restored.verify_rollback(receipt) is True


def test_restore_provider_state_rejects_tamper_and_repository_mismatch() -> None:
    datasets = _Datasets([], gets={('dataset-a', '1'): _SdkValue({'name': 'dataset-a', 'version': '1', 'id': 'azureai://accounts/a/projects/p/data/dataset-a/versions/1', 'type': 'uri_file', 'dataUri': 'https://blob/data.jsonl'})})
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=_Jobs(create_results=[])), datasets=datasets))
    receipt = adapter.apply_resources(_plan())
    state = dict(adapter.export_provider_state(receipt))
    tampered = dict(state)
    tampered['binding_hash'] = '0' * 64
    restored = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=_Jobs(create_results=[])), datasets=datasets))
    with pytest.raises(FoundryPrerequisiteError):
        restored.restore_provider_state(tampered)
    tampered2 = dict(state)
    tampered2['resources'] = []
    with pytest.raises(FoundryPrerequisiteError):
        restored.restore_provider_state(tampered2)
    mismatch_receipt = receipt.model_copy(update={'repository_identity': 'other/repo'})
    adapter.restore_provider_state(state)
    with pytest.raises(FoundryPrerequisiteError):
        adapter.export_provider_state(mismatch_receipt)


def test_verify_resources_detects_immutable_drift_and_state_excludes_sdk_types() -> None:
    datasets = _Datasets([], gets={('dataset-a', '1'): _SdkValue({'name': 'dataset-a', 'version': '1', 'id': 'azureai://accounts/a/projects/p/data/dataset-a/versions/1', 'type': 'uri_file', 'dataUri': 'https://blob/data.jsonl'})})
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=_Jobs(create_results=[])), datasets=datasets))
    created_plan = BootstrapPlan.create(operation_id='z' * 64, runtime_repository='https://github.com/example/runtime.git', runtime_commit='a' * 40, repository_identity='org/repo', actions=(BootstrapAction(action_id='dataset-z:1', phase='evaluations', stage='planned', kind='dataset', diagnostics=('dataset-z','1','https://blob/data.jsonl','uri_file')),))
    receipt = adapter.apply_resources(created_plan)
    state = adapter.export_provider_state(receipt)
    assert '"_SdkValue"' not in str(state)
    datasets.gets[('dataset-z', '1')] = _SdkValue({'name': 'dataset-z', 'version': '1', 'id': 'azureai://accounts/a/projects/p/data/dataset-z/versions/1', 'type': 'uri_file', 'dataUri': 'https://blob/drift.jsonl', 'tags': {'foundry_opt_operation': adapter._ownership_token(created_plan.operation_id, 'dataset-z:1')}})
    assert adapter.verify_resources(receipt) is False
    datasets.gets[('dataset-z', '1')] = _SdkValue({'name': 'dataset-z', 'version': '1', 'id': 'azureai://accounts/a/projects/p/data/dataset-z/versions/1', 'type': 'uri_file', 'dataUri': 'https://blob/data.jsonl', 'tags': {}})
    assert adapter.verify_resources(receipt) is False


def test_rollback_uses_persisted_reverse_order_not_receipt_order() -> None:
    plan = BootstrapPlan.create(operation_id='r'*64, runtime_repository='https://github.com/example/runtime.git', runtime_commit='a'*40, repository_identity='org/repo', actions=(
        BootstrapAction(action_id='dataset-a:1', phase='evaluations', stage='planned', kind='dataset', diagnostics=('dataset-a','1','https://blob/a.jsonl','uri_file')),
        BootstrapAction(action_id='dataset-b:1', phase='evaluations', stage='planned', kind='dataset', diagnostics=('dataset-b','1','https://blob/b.jsonl','uri_file')),
    ))
    datasets = _Datasets([], gets={})
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=_Jobs(create_results=[])), datasets=datasets))
    receipt = adapter.apply_resources(plan)
    state = dict(adapter.export_provider_state(receipt))
    state['rollback_order'] = [state['resources'][1]['id'], state['resources'][0]['id']]
    state['state_hash'] = adapter._state_hash(state)
    adapter.restore_provider_state(state)
    adapter.rollback_resources(receipt)
    assert datasets.delete_calls == [('dataset-b', '1'), ('dataset-a', '1')]


def test_crash_retry_keeps_owned_resource_compensable() -> None:
    datasets = _Datasets([], gets={})
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=_Jobs(create_results=[])), datasets=datasets))
    plan = BootstrapPlan.create(operation_id='c'*64, runtime_repository='https://github.com/example/runtime.git', runtime_commit='a'*40, repository_identity='org/repo', actions=(BootstrapAction(action_id='dataset-c:1', phase='evaluations', stage='planned', kind='dataset', diagnostics=('dataset-c','1','https://blob/c.jsonl','uri_file')),))
    receipt = adapter.apply_resources(plan)
    state = adapter.export_provider_state(receipt)
    retried = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=_Jobs(create_results=[])), datasets=datasets))
    retried.restore_provider_state(state)
    retry_receipt = retried.apply_resources(plan)
    assert retry_receipt.created_actions == ('dataset-c:1',)
    assert retry_receipt.adopted_actions == ()


def test_partial_apply_rollback_failure_carries_receipt_and_state() -> None:
    datasets = _Datasets([], gets={}, fail_delete={('dataset-fail', '1')})
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=_Jobs(create_results=[])), datasets=datasets))
    plan = BootstrapPlan.create(operation_id='d'*64, runtime_repository='https://github.com/example/runtime.git', runtime_commit='a'*40, repository_identity='org/repo', actions=(BootstrapAction(action_id='dataset-fail:1', phase='evaluations', stage='planned', kind='dataset', diagnostics=('dataset-fail','1','https://blob/fail.jsonl','uri_file')),))
    receipt = adapter.apply_resources(plan)
    with pytest.raises(FoundryRollbackError) as caught:
        adapter.rollback_resources(receipt)
    assert caught.value.compensation_receipt is receipt
    assert caught.value.provider_state['receipt_hash'] == receipt.receipt_hash


def test_rollback_skips_missing_but_rejects_id_or_tag_mismatch() -> None:
    datasets = _Datasets([], gets={})
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=_Jobs(create_results=[])), datasets=datasets))
    plan = BootstrapPlan.create(operation_id='g'*64, runtime_repository='https://github.com/example/runtime.git', runtime_commit='a'*40, repository_identity='org/repo', actions=(BootstrapAction(action_id='dataset-g:1', phase='evaluations', stage='planned', kind='dataset', diagnostics=('dataset-g','1','https://blob/g.jsonl','uri_file')),))
    receipt = adapter.apply_resources(plan)
    datasets.gets.clear()
    adapter.rollback_resources(receipt)
    assert datasets.delete_calls == []
    receipt2 = adapter.apply_resources(plan)
    datasets.gets[('dataset-g', '1')] = _SdkValue({'name': 'dataset-g', 'version': '1', 'id': 'azureai://accounts/a/projects/p/data/other/versions/1', 'type': 'uri_file', 'dataUri': 'https://blob/g.jsonl', 'tags': {'foundry_opt_operation': adapter._ownership_token(plan.operation_id, 'dataset-g:1')}})
    with pytest.raises(FoundryRollbackError):
        adapter.rollback_resources(receipt2)


def test_worst_case_plan_bounds_prevent_first_mutation() -> None:
    datasets = _Datasets([], gets={})
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=_Jobs(create_results=[])), datasets=datasets))
    actions = tuple(BootstrapAction(action_id=f'dataset-{index}:1', phase='evaluations', stage='planned', kind='dataset', diagnostics=(f'dataset-{index}','1','https://blob/data.jsonl','uri_file')) for index in range(129))
    plan = BootstrapPlan.create(operation_id='h'*64, runtime_repository='https://github.com/example/runtime.git', runtime_commit='a'*40, repository_identity='org/repo', actions=actions)
    with pytest.raises(FoundryPrerequisiteError):
        adapter.apply_resources(plan)
    assert datasets.create_calls == []


def test_oversize_state_rejected_before_mutation() -> None:
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=_Jobs(create_results=[])), datasets=_Datasets([], gets={})))
    huge_uri = 'https://blob/' + ('x' * 40000)
    plan = BootstrapPlan.create(operation_id='b'*64, runtime_repository='https://github.com/example/runtime.git', runtime_commit='a'*40, repository_identity='org/repo', actions=(BootstrapAction(action_id='dataset-big:1', phase='evaluations', stage='planned', kind='dataset', diagnostics=('dataset-big','1',huge_uri,'uri_file')),))
    with pytest.raises(FoundryPrerequisiteError):
        adapter.apply_resources(plan)


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


def test_keyword_signatures_and_default_hash_normalization() -> None:
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(create_results=[_Poller([], done_sequence=[True]), _Poller([], done_sequence=[True])]), evaluators=_Jobs(create_results=[]))))
    job = adapter._build_evaluator_generation_job({'sources': [{'type': 'agent', 'agent_name': 'a'}], 'model': 'gpt-4o', 'evaluator_name': 'rubric'})
    assert job.inputs.evaluator_name == 'rubric'
    data_job = adapter._build_dataset_generation_job({'sources': [{'type': 'agent', 'agent_name': 'a'}], 'options': {'type': 'simple_qna', 'max_samples': 30, 'model_options': {'model': 'gpt-4o'}}, 'output_name': 'generated'})
    assert data_job.inputs.output_options.name == 'generated'
    one = adapter.create_dataset_generation_job({'sources': [{'type': 'agent', 'agent_name': 'a'}], 'options': {'type': 'simple_qna', 'max_samples': 30, 'model_options': {'model': 'gpt-4o'}}})
    two = adapter.create_dataset_generation_job({'name': 'foundry-opt-data-generation', 'scenario': 'evaluation', 'sources': [{'type': 'agent', 'agent_name': 'a'}], 'options': {'type': 'simple_qna', 'max_samples': 30, 'model_options': {'model': 'gpt-4o'}}})
    assert one.operation_id == two.operation_id


# --- Evaluation onboarding (issue #10): evaluator / evaluation_definition / activation_run /
# --- activation_cleanup action kinds, and the false-success regression fix. ---

_CS_ID = 'azureai://built-in/evaluators/content_safety'


def _activation_case(phase: str, evaluator_id: str, *, score: float = 1.0, normalization_kind: str = 'pass_fail', source_min: float | None = None, source_max: float | None = None, executable: bool = True) -> dict[str, object]:
    return {'phase': phase, 'evaluator_id': evaluator_id, 'executable': executable, 'normalization_kind': normalization_kind, 'score': score, 'source_min': source_min, 'source_max': source_max}


def _activation_guardrail(phase: str, evaluator_id: str, pass_rate: float = 1.0) -> dict[str, object]:
    return {'phase': phase, 'evaluator_id': evaluator_id, 'pass_rate': pass_rate}


def _default_activation_payload() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    cases = [
        _activation_case('development', _CS_ID, score=1.0),
        _activation_case('development', 'quality-metric', score=0.8, normalization_kind='scalar', source_min=0.0, source_max=1.0),
        _activation_case('validating', _CS_ID, score=1.0),
        _activation_case('validating', 'quality-metric', score=0.8, normalization_kind='scalar', source_min=0.0, source_max=1.0),
    ]
    guardrails = [_activation_guardrail('development', _CS_ID, 1.0), _activation_guardrail('validating', _CS_ID, 1.0)]
    return cases, guardrails


def _activation_diagnostics(
    *,
    development_definition_name: str = 'dev-def',
    validating_definition_name: str = 'val-def',
    draft_agent_name: str = 'draft-agent',
    draft_agent_version: str = '1',
    model_deployment: str = 'gpt-4o',
    bundle_objective_hash: str = 'b' * 64,
    split_lineage_hash: str = 's' * 64,
    cases: list[dict[str, object]] | None = None,
    guardrails: list[dict[str, object]] | None = None,
) -> tuple[str, ...]:
    default_cases, default_guardrails = _default_activation_payload()
    payload = {'cases': cases if cases is not None else default_cases, 'guardrails': guardrails if guardrails is not None else default_guardrails}
    return (development_definition_name, validating_definition_name, draft_agent_name, draft_agent_version, model_deployment, bundle_objective_hash, split_lineage_hash, json.dumps(payload, sort_keys=True))


def _definition_diagnostics(role: str, definition_name: str, *, dataset_name: str = 'dataset-a', dataset_version: str = '1', evaluator_name: str = 'content-safety', evaluator_version: str = '1', evaluator_kind: str = 'builtin', model_deployment: str = 'gpt-4o') -> tuple[str, ...]:
    return (role, definition_name, dataset_name, dataset_version, evaluator_name, evaluator_version, evaluator_kind, model_deployment)


def _evaluator_diagnostics(name: str, version: str, kind: str, provenance: str, job_id: str = '') -> tuple[str, ...]:
    return (name, version, kind, provenance, job_id)


def _cleanup_diagnostics(draft_agent_name: str, draft_agent_version: str) -> tuple[str, ...]:
    return (draft_agent_name, draft_agent_version)


def test_false_success_regression_unsupported_kind_rejected_before_mutation() -> None:
    datasets = _Datasets([], gets={})
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=_Jobs(create_results=[])), datasets=datasets))
    plan = BootstrapPlan.create(
        operation_id='u' * 64,
        runtime_repository='https://github.com/example/runtime.git',
        runtime_commit='a' * 40,
        repository_identity='org/repo',
        actions=(
            BootstrapAction(action_id='dataset-a:1', phase='evaluations', stage='planned', kind='dataset', diagnostics=('dataset-a', '1', 'https://blob/data.jsonl', 'uri_file')),
            BootstrapAction(action_id='mystery:1', phase='evaluations', stage='planned', kind='mystery_resource_kind', diagnostics=('a', 'b')),
        ),
    )
    with pytest.raises(FoundryUnsupportedCapabilityError):
        adapter.apply_resources(plan)
    # No mutation of any kind occurred: an unsupported action must never yield a partial,
    # success-shaped receipt.
    assert datasets.create_calls == []


def test_evaluator_builtin_reuse_and_custom_generated_lineage() -> None:
    evaluators = _Jobs(
        list_result=[_SdkValue({'name': 'content-safety', 'version': '1', 'id': 'azureai://built-in/evaluators/content_safety', 'evaluator_type': 'builtin'})],
        versions={('quality-eval', '2'): _SdkValue({'name': 'quality-eval', 'version': '2', 'id': 'azureai://accounts/a/projects/p/evaluators/quality-eval/versions/2', 'evaluator_type': 'custom', 'generation_job_id': 'job-123'})},
    )
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=evaluators)))
    reused = adapter.adopt_or_verify_evaluator(adapter._evaluator_request_from_action(BootstrapAction(action_id='e1', phase='evaluations', stage='planned', kind='evaluator', diagnostics=_evaluator_diagnostics('content-safety', '1', 'builtin', 'reused_existing'))))
    assert reused['created'] is False
    assert reused['adopted'] is True
    assert reused['resource_id'] == 'azureai://built-in/evaluators/content_safety'
    generated = adapter.adopt_or_verify_evaluator(adapter._evaluator_request_from_action(BootstrapAction(action_id='e2', phase='evaluations', stage='planned', kind='evaluator', diagnostics=_evaluator_diagnostics('quality-eval', '2', 'custom', 'auto_generated_unreviewed', 'job-123'))))
    assert generated['created'] is True
    assert generated['resource_id'] == 'azureai://accounts/a/projects/p/evaluators/quality-eval/versions/2'
    with pytest.raises(FoundryPrerequisiteError):
        adapter.adopt_or_verify_evaluator(adapter._evaluator_request_from_action(BootstrapAction(action_id='e3', phase='evaluations', stage='planned', kind='evaluator', diagnostics=_evaluator_diagnostics('quality-eval', '2', 'custom', 'auto_generated_unreviewed', 'job-999'))))


def test_evaluator_generated_without_job_id_rejected() -> None:
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=_Jobs(create_results=[]))))
    action = BootstrapAction(action_id='evaluator-bad:1', phase='evaluations', stage='planned', kind='evaluator', diagnostics=_evaluator_diagnostics('quality-eval', '2', 'custom', 'auto_generated_unreviewed', ''))
    with pytest.raises(FoundryPrerequisiteError):
        adapter._evaluator_request_from_action(action)


def test_evaluation_definition_create_and_adopt_by_name() -> None:
    evals = _OpenAIEvals(existing=[_EvalObject('eval_existing', 'val-def')])
    client = _Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=_Jobs(create_results=[])), openai_client=_OpenAIClient(evals=evals))
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=client)
    create_request = adapter._definition_request_from_action(BootstrapAction(action_id='d1', phase='evaluations', stage='planned', kind='evaluation_definition', diagnostics=_definition_diagnostics('development', 'dev-def')))
    created = adapter.create_or_adopt_evaluation_definition(create_request)
    assert created['created'] is True
    assert evals.create_calls[0]['name'] == 'dev-def'
    assert evals.create_calls[0]['testing_criteria'][0]['type'] == 'python'
    adopt_request = adapter._definition_request_from_action(BootstrapAction(action_id='d2', phase='evaluations', stage='planned', kind='evaluation_definition', diagnostics=_definition_diagnostics('validating', 'val-def')))
    adopted = adapter.create_or_adopt_evaluation_definition(adopt_request)
    assert adopted == {'created': False, 'adopted': True, 'definition': {'id': 'eval_existing', 'name': 'val-def'}, 'resource_id': 'eval_existing'}


def test_evaluation_definition_invalid_role_rejected() -> None:
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=_Jobs(create_results=[]))))
    action = BootstrapAction(action_id='definition-bad:1', phase='evaluations', stage='planned', kind='evaluation_definition', diagnostics=_definition_diagnostics('staging', 'dev-def'))
    with pytest.raises(FoundryPrerequisiteError):
        adapter._definition_request_from_action(action)


def test_activation_run_missing_content_safety_phase_rejected() -> None:
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=_Jobs(create_results=[]))))
    cases = [
        _activation_case('development', _CS_ID, score=1.0),
        _activation_case('validating', 'quality-metric', score=0.8, normalization_kind='scalar', source_min=0.0, source_max=1.0),
    ]
    guardrails = [_activation_guardrail('development', _CS_ID, 1.0)]
    action = BootstrapAction(action_id='activation-bad:1', phase='evaluations', stage='planned', kind='activation_run', diagnostics=_activation_diagnostics(cases=cases, guardrails=guardrails))
    with pytest.raises(FoundryPrerequisiteError):
        adapter._activation_request_from_action(action)


def test_activation_cleanup_without_preceding_activation_run_rejected() -> None:
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=_Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=_Jobs(create_results=[]))))
    plan = BootstrapPlan.create(operation_id='k' * 64, runtime_repository='https://github.com/example/runtime.git', runtime_commit='a' * 40, repository_identity='org/repo', actions=(BootstrapAction(action_id='cleanup-only:1', phase='evaluations', stage='planned', kind='activation_cleanup', diagnostics=_cleanup_diagnostics('draft-agent', '1')),))
    with pytest.raises(FoundryPrerequisiteError):
        adapter.apply_resources(plan)


def test_activation_content_safety_failure_gates_and_rolls_back_created_resources() -> None:
    evaluators = _Jobs(versions={('quality-eval', '2'): _SdkValue({'name': 'quality-eval', 'version': '2', 'id': 'azureai://accounts/a/projects/p/evaluators/quality-eval/versions/2', 'evaluator_type': 'custom', 'generation_job_id': 'job-123'})})
    evals = _OpenAIEvals()
    client = _Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=evaluators), openai_client=_OpenAIClient(evals=evals))
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=client)
    failing_guardrails = [_activation_guardrail('development', _CS_ID, 0.9), _activation_guardrail('validating', _CS_ID, 1.0)]
    actions = (
        BootstrapAction(action_id='evaluator-quality:2', phase='evaluations', stage='planned', kind='evaluator', diagnostics=_evaluator_diagnostics('quality-eval', '2', 'custom', 'auto_generated_unreviewed', 'job-123')),
        BootstrapAction(action_id='definition-dev:1', phase='evaluations', stage='planned', kind='evaluation_definition', diagnostics=_definition_diagnostics('development', 'dev-def', evaluator_name='quality-eval', evaluator_version='2', evaluator_kind='custom')),
        BootstrapAction(action_id='definition-val:1', phase='evaluations', stage='planned', kind='evaluation_definition', diagnostics=_definition_diagnostics('validating', 'val-def', evaluator_name='quality-eval', evaluator_version='2', evaluator_kind='custom')),
        BootstrapAction(action_id='activation-run:1', phase='evaluations', stage='planned', kind='activation_run', diagnostics=_activation_diagnostics(guardrails=failing_guardrails)),
    )
    plan = BootstrapPlan.create(operation_id='q' * 64, runtime_repository='https://github.com/example/runtime.git', runtime_commit='a' * 40, repository_identity='org/repo', actions=actions)
    with pytest.raises(FoundryPrerequisiteError):
        adapter.apply_resources(plan)
    # Both phase runs were durably submitted as a real, structural-only audit record before the
    # local gate rejected the bundle...
    assert len(evals.runs.create_calls) == 2
    # ...but the evaluator and both evaluation definitions created earlier in this same apply
    # call were rolled back (created-only rollback; nothing pre-existing is ever touched).
    assert evaluators.delete_version_calls == [('quality-eval', '2')]
    assert len(evals.delete_calls) == 2
    assert evals.items == {}


def test_activation_headroom_failure_gates_and_rolls_back_created_resources() -> None:
    saturated_cases = [
        _activation_case('development', _CS_ID, score=1.0),
        _activation_case('development', 'quality-metric', score=1.0, normalization_kind='scalar', source_min=0.0, source_max=1.0),
        _activation_case('validating', _CS_ID, score=1.0),
        _activation_case('validating', 'quality-metric', score=1.0, normalization_kind='scalar', source_min=0.0, source_max=1.0),
    ]
    evaluators = _Jobs(versions={('quality-eval', '2'): _SdkValue({'name': 'quality-eval', 'version': '2', 'id': 'azureai://accounts/a/projects/p/evaluators/quality-eval/versions/2', 'evaluator_type': 'custom', 'generation_job_id': 'job-123'})})
    evals = _OpenAIEvals()
    client = _Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=evaluators), openai_client=_OpenAIClient(evals=evals))
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=client)
    actions = (
        BootstrapAction(action_id='evaluator-quality:2', phase='evaluations', stage='planned', kind='evaluator', diagnostics=_evaluator_diagnostics('quality-eval', '2', 'custom', 'auto_generated_unreviewed', 'job-123')),
        BootstrapAction(action_id='definition-dev:1', phase='evaluations', stage='planned', kind='evaluation_definition', diagnostics=_definition_diagnostics('development', 'dev-def', evaluator_name='quality-eval', evaluator_version='2', evaluator_kind='custom')),
        BootstrapAction(action_id='definition-val:1', phase='evaluations', stage='planned', kind='evaluation_definition', diagnostics=_definition_diagnostics('validating', 'val-def', evaluator_name='quality-eval', evaluator_version='2', evaluator_kind='custom')),
        BootstrapAction(action_id='activation-run:1', phase='evaluations', stage='planned', kind='activation_run', diagnostics=_activation_diagnostics(cases=saturated_cases)),
    )
    plan = BootstrapPlan.create(operation_id='n' * 64, runtime_repository='https://github.com/example/runtime.git', runtime_commit='a' * 40, repository_identity='org/repo', actions=actions)
    with pytest.raises(FoundryPrerequisiteError):
        adapter.apply_resources(plan)
    assert evaluators.delete_version_calls == [('quality-eval', '2')]
    assert len(evals.delete_calls) == 2


def test_full_evaluation_onboarding_lifecycle_apply_verify_rollback_restart() -> None:
    datasets = _Datasets([], gets={('dataset-a', '1'): _SdkValue({'name': 'dataset-a', 'version': '1', 'id': 'azureai://accounts/a/projects/p/data/dataset-a/versions/1', 'type': 'uri_file', 'dataUri': 'https://blob/data.jsonl'})})
    evaluators = _Jobs(
        list_result=[_SdkValue({'name': 'content-safety', 'version': '1', 'id': 'azureai://built-in/evaluators/content_safety', 'evaluator_type': 'builtin'})],
        versions={('quality-eval', '2'): _SdkValue({'name': 'quality-eval', 'version': '2', 'id': 'azureai://accounts/a/projects/p/evaluators/quality-eval/versions/2', 'evaluator_type': 'custom', 'generation_job_id': 'job-123'})},
    )
    agents = _Agents([])
    evals = _OpenAIEvals()
    client = _Client(beta=_Beta(datasets=_Jobs(create_results=[]), evaluators=evaluators), datasets=datasets, agents=agents, openai_client=_OpenAIClient(evals=evals))
    adapter = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=client)
    actions = (
        BootstrapAction(action_id='dataset-a:1', phase='evaluations', stage='planned', kind='dataset', diagnostics=('dataset-a', '1', 'https://blob/data.jsonl', 'uri_file')),
        BootstrapAction(action_id='evaluator-cs:1', phase='evaluations', stage='planned', kind='evaluator', diagnostics=_evaluator_diagnostics('content-safety', '1', 'builtin', 'reused_existing')),
        BootstrapAction(action_id='evaluator-quality:2', phase='evaluations', stage='planned', kind='evaluator', diagnostics=_evaluator_diagnostics('quality-eval', '2', 'custom', 'auto_generated_unreviewed', 'job-123')),
        BootstrapAction(action_id='definition-dev:1', phase='evaluations', stage='planned', kind='evaluation_definition', diagnostics=_definition_diagnostics('development', 'dev-def', evaluator_name='quality-eval', evaluator_version='2', evaluator_kind='custom')),
        BootstrapAction(action_id='definition-val:1', phase='evaluations', stage='planned', kind='evaluation_definition', diagnostics=_definition_diagnostics('validating', 'val-def', evaluator_name='quality-eval', evaluator_version='2', evaluator_kind='custom')),
        BootstrapAction(action_id='activation-run:1', phase='evaluations', stage='planned', kind='activation_run', diagnostics=_activation_diagnostics()),
        BootstrapAction(action_id='activation-cleanup:1', phase='evaluations', stage='planned', kind='activation_cleanup', diagnostics=_cleanup_diagnostics('draft-agent', '1')),
    )
    plan = BootstrapPlan.create(operation_id='l' * 64, runtime_repository='https://github.com/example/runtime.git', runtime_commit='a' * 40, repository_identity='org/repo', actions=actions)
    receipt = adapter.apply_resources(plan)

    assert set(receipt.adopted_actions) == {'dataset-a:1', 'evaluator-cs:1'}
    assert set(receipt.created_actions) == {'evaluator-quality:2', 'definition-dev:1', 'definition-val:1', 'activation-run:1:development', 'activation-run:1:validating'}
    assert receipt.changed_actions == ('activation-run:1', 'activation-cleanup:1')
    assert len(evals.runs.create_calls) == 2
    assert agents.delete_version_calls == [('draft-agent', '1')]

    assert adapter.verify_resources(receipt) is True
    state = adapter.export_provider_state(receipt)
    restored = FoundryAdapter('https://account.services.ai.azure.com/api/projects/demo', _Cred(), client=client)
    restored.restore_provider_state(state)
    assert restored.verify_resources(receipt) is True

    adapter.rollback_resources(receipt)
    # Only resources *created* in this apply are removed: the custom evaluator, both new
    # evaluation definitions, and the two activation-smoke audit runs.
    assert evaluators.delete_version_calls == [('quality-eval', '2')]
    assert len(evals.delete_calls) == 2
    assert len(evals.runs.delete_calls) == 2
    # Adopted/pre-existing assets are never deleted, regardless of rollback.
    assert datasets.delete_calls == []
    assert adapter.verify_rollback(receipt) is True
