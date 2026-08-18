from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, MutableMapping, Sequence
import csv
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import io
import json
import math
import os
import queue
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlencode, urlparse, urlunparse
import xml.etree.ElementTree as ET
import zipfile

import httpx

from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import (
    AgentDataGenerationJobSource,
    AgentEvaluatorGenerationJobSource,
    AzureAIAgentTargetParam,
    AzureAIDataSourceConfig,
    CodeConfiguration,
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
    HostedAgentDefinition,
    PromptDataGenerationJobSource,
    PromptEvaluatorGenerationJobSource,
    ProtocolVersionRecord,
    SimpleQnADataGenerationJobOptions,
    TargetCompletionEvalRunDataSource,
    TaskGenerationDataGenerationJobOptions,
    TestingCriterionAzureAIEvaluator,
    TracesDataGenerationJobOptions,
    TracesDataGenerationJobSource,
    TracesEvaluatorGenerationJobSource,
)
from azure.core.credentials import AccessToken, TokenCredential
from azure.core.exceptions import ClientAuthenticationError, HttpResponseError, ResourceNotFoundError, ServiceRequestError
import openai

from foundry_opt.bootstrap.canonical import safe_persisted_document
from foundry_opt.bootstrap.contracts import (
    BootstrapAction,
    BootstrapPlan,
    BootstrapReceipt,
    EvaluatorNormalization,
    EvaluatorReference,
    FingerprintRecord,
    RedactedStatusInfo,
    ResolvedEvaluator,
    ResolvedWeightedObjective,
)
from foundry_opt.bootstrap.errors import BootstrapConfigError, BootstrapProviderError
from foundry_opt.bootstrap.discovery import fingerprint_files, is_fingerprintable_path
from foundry_opt.bootstrap.evaluation.core import (
    ActivationCleanup,
    ActivationRun,
    LEGACY_AGGREGATE_SAFETY_ID,
    LEGACY_AGGREGATE_SAFETY_NAME,
    REQUIRED_SAFETY_EVALUATORS,
    assert_required_safety_coverage,
    canonical_safety_name,
    compute_split_lineage_hash,
    split_dataset_rows,
    validate_activation,
    validate_generated_rubric,
)
from foundry_opt.bootstrap.evaluation.execution import (
    ActivationCaseFinalization,
    ActivationFinalization,
    DatasetFinalization,
    DefinitionFinalization,
    EvaluationFinalization,
    EvaluationOnboardingRequest,
    EvaluatorFinalization,
    SplitFinalization,
)
from foundry_opt.models import FrozenModel
from foundry_opt.optimize_job.safety import UnsafeCheckpointContentError

_CONTENT_SAFETY_ID = LEGACY_AGGREGATE_SAFETY_ID  # legacy aggregate, honored only when a project returns it
_IMMUTABLE_DATASET_URI_PREFIX = 'azureai://accounts/'
_DATASET_URI_RE = re.compile(r'^azureai://accounts/[^/]+/projects/[^/]+/data/(?P<name>[^/]+)/versions/(?P<version>[^/]+)$')
_EVALUATOR_URI_RE = re.compile(r'^azureai://accounts/[^/]+/projects/[^/]+/evaluators/(?P<name>[^/]+)/versions/(?P<version>[^/]+)$')
_PROVIDER_STATE_SCHEMA_VERSION = 1
_MAX_PROVIDER_STATE_BYTES = 32768
_MAX_PROVIDER_STATE_RESOURCES = 128
_OWNERSHIP_TAG = 'foundry_opt_operation'
# Real cloud-evaluation surface: `azure_ai_source` definitions with `azure_ai_evaluator`
# graders, `azure_ai_target_completions` runs over immutable split datasets, and
# `azure_ai_synthetic_data_gen_preview` runs for synthetic dataset generation.
_EVAL_SCENARIO = 'synthetic_data_gen_preview'
_SYNTHETIC_ITEM_GENERATION_TYPE = 'synthetic_data_gen_preview'
_ITEM_QUERY_REFERENCE = 'item.query'
_EVALUATOR_DATA_MAPPING = {'query': '{{item.query}}', 'response': '{{sample.output_text}}'}
_OBJECTIVE_DATA_MAPPING = {'query': '{{item.query}}', 'response': '{{sample.output_items}}'}
_MAX_RUN_OUTPUT_ITEMS = 5000
_MAX_AGENT_CODE_BYTES = 32 * 1024 * 1024
_MAX_AGENT_CODE_ENTRIES = 2000
_MAX_DATASET_BYTES = 32 * 1024 * 1024
_MAX_DATASET_FILES = 32
_MAX_DATASET_ROWS = 5000
_DATASET_ROW_ID_FIELDS = ('row_id', 'rowId', 'id', 'case_id', 'caseId', 'sample_id', 'sampleId', 'item_id', 'itemId')
_SUPPORTED_DATASET_SUFFIXES = ('.jsonl', '.ndjson', '.csv')

# Evaluation-phase BootstrapAction.kind values and their fixed positional `diagnostics`
# tuple[str, ...] layouts. Every element is a plain identifier/enum/number-as-string; no raw
# prompts, responses, traces, or dataset rows are ever encoded in a diagnostics tuple.
#
#   "dataset"               -> (dataset_name, dataset_version, dataset_content_uri, dataset_type)
#   "evaluator"             -> (evaluator_name, evaluator_version, evaluator_kind, provenance, expected_generation_job_id)
#   "evaluation_definition" -> (role, definition_name, dataset_name, dataset_version, evaluator_name, evaluator_version, evaluator_kind, model_deployment)
#   "activation_run"        -> (development_definition_name, validating_definition_name, draft_agent_name, draft_agent_version, model_deployment, bundle_objective_hash, split_lineage_hash, cases_and_guardrails_json)
#   "activation_cleanup"    -> (draft_agent_name, draft_agent_version)
#   "evaluation_onboarding" -> (repo_agent_id, contract_hash, approved_onboarding_contract_json)
#
# `evaluation_onboarding` is the composite, approval-bound action the plan factory emits: one
# per agent. It authorizes the staged provider state machine below (inventory -> generation ->
# split -> evaluator -> definitions -> activation -> cleanup). The other kinds remain supported
# as the primitive operations that machine executes.
_SUPPORTED_EVALUATION_ACTION_KINDS = ('dataset', 'evaluator', 'evaluation_definition', 'activation_run', 'activation_cleanup', 'evaluation_onboarding')
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


@dataclass(frozen=True, slots=True)
class AgentPackage:
    """A deterministically packaged agent source tree awaiting draft creation.

    Only the temporary archive path and its digests travel with this record; package bytes are
    transient and never reach provider state, receipts, or logs.
    """

    repo_agent_id: str
    zip_path: str
    zip_sha256: str
    tree_sha256: str
    file_count: int
    size_bytes: int


@dataclass(frozen=True, slots=True)
class _ResourceDraft:
    """A resource touched by one onboarding stage, before rollback ordering is assigned."""

    suffix: str
    resource_id: str
    name: str
    version: str
    kind: str
    disposition: str
    fingerprint: str | None = None
    resource_type: str | None = None
    ownership_token: str | None = None
    ownership_tag: str | None = None


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


def _sdk_mapping(value: object) -> Mapping[str, object]:
    """Normalize any SDK response value (TypedDict, azure model, pydantic model) to a mapping."""

    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    for accessor in ('model_dump', 'as_dict', 'to_dict', 'dict'):
        method = getattr(value, accessor, None)
        if callable(method):
            try:
                data = method(mode='json') if accessor == 'model_dump' else method()
            except TypeError:
                data = method()
            if isinstance(data, Mapping):
                return {str(key): _plain(item) for key, item in data.items()}
    raise FoundryPrerequisiteError('evaluation definition response is not a readable mapping', kind='prerequisite')


def _definition_signature(data_source_config: Mapping[str, object], testing_criteria: Sequence[Mapping[str, object]]) -> str:
    """Canonical signature of a definition: data source config plus every grader binding.

    Only the fields that decide what is measured participate, so a definition may only be
    adopted when it measures exactly the approved evaluators the same way.
    """

    criteria = [
        {
            'type': str(item.get('type') or ''),
            'name': str(item.get('name') or ''),
            'evaluator_name': str(item.get('evaluator_name') or ''),
            'evaluator_version': str(item.get('evaluator_version') or ''),
            'data_mapping': {str(key): str(value) for key, value in (item.get('data_mapping') or {}).items()} if isinstance(item.get('data_mapping'), Mapping) else {},
            'initialization_parameters': {str(key): _plain(value) for key, value in item.get('initialization_parameters').items()} if isinstance(item.get('initialization_parameters'), Mapping) else {},
        }
        for item in testing_criteria
    ]
    criteria.sort(key=lambda item: (item['name'], item['evaluator_name'], item['evaluator_version']))
    payload = {
        'data_source_config': {str(key): _plain(value) for key, value in data_source_config.items()},
        'testing_criteria': criteria,
    }
    return hashlib.sha256(_canonical_json(payload).encode('utf-8')).hexdigest()


def _sdk_attribute(value: object, *names: str) -> object:
    """Read one attribute from an SDK model, mapping, or serialized payload."""

    if value is None:
        return None
    for name in names:
        found = getattr(value, name, None)
        if found is not None:
            return found
    if isinstance(value, Mapping):
        for name in names:
            if value.get(name) is not None:
                return value[name]
        return None
    for accessor in ('as_dict', 'model_dump', 'to_dict'):
        method = getattr(value, accessor, None)
        if callable(method):
            try:
                data = method(mode='json') if accessor == 'model_dump' else method()
            except TypeError:
                data = method()
            if isinstance(data, Mapping):
                for name in names:
                    if data.get(name) is not None:
                        return data[name]
    return None


def _is_supported_blob_uri(uri: str) -> bool:
    """Accept TLS blob endpoints, plus plain HTTP only against a loopback emulator."""

    parsed = urlparse(uri)
    if parsed.scheme == 'https':
        return True
    if parsed.scheme != 'http':
        return False
    return (parsed.hostname or '') in {'127.0.0.1', '::1', 'localhost'}


def _stable_row_id(row: Mapping[str, object]) -> str:
    """Prefer an existing safe identifier field, else a canonical digest of the row."""

    for field in _DATASET_ROW_ID_FIELDS:
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, int) and not isinstance(value, bool):
            return str(value)
    return hashlib.sha256(_canonical_json(row).encode('utf-8')).hexdigest()


def _split_content_fingerprint(*, source_dataset: Mapping[str, object], role: str, case_ids: Sequence[str]) -> str:
    """Exact, content-free fingerprint of one materialized split."""

    payload = {
        'source_dataset_id': str(source_dataset.get('id') or ''),
        'role': role,
        'case_ids': [str(item) for item in case_ids],
    }
    return hashlib.sha256(_canonical_json(payload).encode('utf-8')).hexdigest()


def _repo_relative_archive_path(name: str, *, root: str) -> str | None:
    """Map an agent code archive entry onto its repository-relative path under `root`.

    Returns `None` for entries that cannot be trusted (absolute, traversing, or empty), so a
    malicious or malformed archive can never inject paths outside the reviewed root.
    """

    normalized = name.replace('\\', '/').strip()
    if not normalized or normalized.endswith('/'):
        return None
    if normalized.startswith('/') or ':' in normalized.split('/')[0]:
        return None
    parts = [part for part in normalized.split('/') if part not in ('', '.')]
    if not parts or any(part == '..' for part in parts):
        return None
    if root == '.':
        return '/'.join(parts)
    return '/'.join([*root.split('/'), *parts])


def _short_reason(exc: BaseException) -> str:
    """Return a bounded, redacted reason for a contract/validation failure."""

    text = str(exc).replace('\n', ' ').strip()
    return text[:200] if text else type(exc).__name__


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
    ownership_tag: str | None = None


class FoundryCapabilityProbe(FrozenModel):
    mode: str
    supported: bool
    preview_required: bool
    app_insights_available: bool
    reasons: tuple[str, ...] = ()
    region: str | None = None
    model_deployments: tuple[str, ...] = ()


class _CachingTokenCredential:
    """Thread-safe process-local token cache for the many short-lived live clients."""

    def __init__(self, source: TokenCredential) -> None:
        self._source = source
        self._tokens: dict[tuple[tuple[str, ...], tuple[tuple[str, str], ...]], AccessToken] = {}
        self._lock = threading.Lock()

    def get_token(self, *scopes: str, **kwargs: object) -> AccessToken:
        options = tuple(sorted((str(key), str(value)) for key, value in kwargs.items()))
        key = (tuple(scopes), options)
        with self._lock:
            cached = self._tokens.get(key)
            if cached is not None and cached.expires_on > time.time() + 300:
                return cached
            token = self._source.get_token(*scopes, **kwargs)
            self._tokens[key] = token
            return token


class FoundryAdapter:
    def __init__(self, project_endpoint: str, credential: TokenCredential, *, client: object | None = None, time_source: Callable[[], float] | None = None, sleep: Callable[[float], None] | None = None, default_poll_interval: float = 1.0, split_writer: Callable[..., str] | None = None, checkpoint: Callable[[Mapping[str, object]], None] | None = None, download_timeout: float = 60.0, request_timeout: float = 120.0, operation_timeout: float = 1800.0) -> None:
        self._project_endpoint = project_endpoint
        self._credential = credential if client is not None else _CachingTokenCredential(credential)
        self._injected_client = client is not None
        self._client = client if client is not None else AIProjectClient(project_endpoint, self._credential)
        # A synchronous hosted-code upload can keep its client pipeline occupied after the
        # version is already active. Live observation uses an independent pipeline so the
        # bounded owner can see that exact version and continue.
        self._agent_observer_client = (
            client
            if client is not None
            else AIProjectClient(project_endpoint, self._credential)
        )
        self._time = time_source or time.monotonic
        self._sleep = sleep or time.sleep
        self._default_poll_interval = default_poll_interval
        self._provider_state: dict[str, object] | None = None
        self._openai: object | None = None
        self._openai_observer: object | None = None
        self._split_writer = split_writer
        self._onboarding: dict[str, dict[str, object]] = {}
        self._checkpoint = checkpoint
        # Raw dataset rows are cached in memory for this operation only and never persisted.
        self._dataset_row_cache: dict[tuple[str, str], tuple[Mapping[str, object], ...]] = {}
        self._published_splits: dict[tuple[str, str], str] = {}
        self._download_timeout = download_timeout
        self._request_timeout = request_timeout
        self._operation_timeout = operation_timeout
        if self._request_timeout <= 0 or self._operation_timeout <= 0:
            raise FoundryPrerequisiteError('Foundry timeouts must be positive', kind='prerequisite')
        self._agent_packages: Mapping[str, AgentPackage] = {}
        self._created_drafts: dict[tuple[str, str], str] = {}

    @property
    def project_endpoint(self) -> str:
        return self._project_endpoint

    def set_checkpoint(self, checkpoint: Callable[[Mapping[str, object]], None] | None) -> None:
        """Install the durable sink used to persist in-flight generation handles."""

        self._checkpoint = checkpoint

    def onboarding_ledger_snapshot(self) -> Mapping[str, object]:
        """Restart-relevant ledger of stages and in-flight generation handles."""

        return json.loads(_canonical_json({'schema_version': _PROVIDER_STATE_SCHEMA_VERSION, 'onboarding': self._onboarding}))

    def _publish_checkpoint(self) -> None:
        if self._checkpoint is None:
            return
        self._checkpoint(self.onboarding_ledger_snapshot())

    def _record_pending_split(self, ledger: Mapping[str, object], role: str, pending: Mapping[str, object]) -> None:
        """Checkpoint an about-to-be-uploaded split so a restart adopts it instead of re-uploading."""

        stages = ledger.get('stages')
        if not isinstance(stages, dict):
            stages = {}
            ledger['stages'] = stages  # type: ignore[index]
        entry = stages.get('split')
        detail = dict(entry) if isinstance(entry, Mapping) else {}
        pending_splits = dict(detail.get('pending_splits') or {}) if isinstance(detail.get('pending_splits'), Mapping) else {}
        pending_splits[role] = {str(key): value for key, value in pending.items()}
        detail['status'] = detail.get('status') if detail.get('status') == 'completed' else 'in_flight'
        detail['pending_splits'] = pending_splits
        stages['split'] = detail
        self._published_splits[(str(pending.get('dataset_name') or ''), str(pending.get('dataset_version') or ''))] = str(pending.get('split_fingerprint') or '')
        self._publish_checkpoint()

    def _record_pending_handle(self, ledger: Mapping[str, object], stage: str, handle: 'FoundryOperationHandle') -> None:
        """Persist an in-flight generation handle before the first poll.

        A crash between job submission and completion must resume the recorded continuation
        instead of resubmitting, so the handle is durably checkpointed first.
        """

        stages = ledger.get('stages')
        if not isinstance(stages, dict):
            stages = {}
            ledger['stages'] = stages  # type: ignore[index]
        entry = stages.get(stage)
        detail = dict(entry) if isinstance(entry, Mapping) else {}
        detail['status'] = 'in_flight'
        detail['handle'] = {
            'operation_id': handle.operation_id,
            'job_kind': handle.job_kind,
            'continuation_token': handle.continuation_token,
            'polling_url': handle.polling_url,
        }
        stages[stage] = detail
        self._publish_checkpoint()

    def _pending_handle(self, ledger: Mapping[str, object], stage: str, *, job_kind: str) -> 'FoundryOperationHandle | None':
        stages = ledger.get('stages')
        if not isinstance(stages, Mapping):
            return None
        entry = stages.get(stage)
        if not isinstance(entry, Mapping) or entry.get('status') == 'completed':
            return None
        handle = entry.get('handle')
        if not isinstance(handle, Mapping):
            return None
        token = handle.get('continuation_token')
        operation_id = handle.get('operation_id')
        if not isinstance(token, str) or not token or not isinstance(operation_id, str) or not operation_id:
            return None
        if str(handle.get('job_kind') or '') != job_kind:
            return None
        return FoundryOperationHandle(
            operation_id=operation_id,
            job_kind=job_kind,
            continuation_token=token,
            polling_url=str(handle.get('polling_url')) if isinstance(handle.get('polling_url'), str) else None,
            created=False,
        )

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
                'ownership_tag': item.ownership_tag,
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
            'onboarding': state.get('onboarding'),
        }
        return hashlib.sha256(_canonical_json(payload).encode('utf-8')).hexdigest()

    def _validate_provider_state_bounds(self, resources: Sequence[_ResourceRecord]) -> None:
        if len(resources) > _MAX_PROVIDER_STATE_RESOURCES:
            raise FoundryPrerequisiteError('provider state resource count exceeds safe bound', kind='prerequisite')
        for resource in resources:
            if len(_canonical_json({'action_id': resource.action_id, 'id': resource.resource_id, 'name': resource.name, 'version': resource.version, 'kind': resource.kind, 'disposition': resource.disposition, 'resource_type': resource.resource_type, 'fingerprint': resource.fingerprint, 'rollback_order': resource.rollback_order, 'ownership_token': resource.ownership_token, 'ownership_tag': resource.ownership_tag}).encode('utf-8')) > (_MAX_PROVIDER_STATE_BYTES // 4):
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
            if action.kind == 'evaluation_onboarding':
                contract = self._onboarding_request_from_action(action)
                assert contract.dataset_plan is not None and contract.evaluator_plan is not None
                assert contract.definition_plan is not None and contract.activation_plan is not None
                for role in _DEFINITION_ROLES:
                    order += 1
                    worst_case_resources.append(_ResourceRecord(action_id=f'{action.action_id}:dataset:{role}', resource_id=f'{_IMMUTABLE_DATASET_URI_PREFIX}accounts-max/projects/max/data/max-dataset/versions/{contract.dataset_plan.requested_version}', name='max-dataset', version=contract.dataset_plan.requested_version, kind='dataset', disposition='created', fingerprint=_fingerprint_dataset_content('https://max', contract.dataset_plan.dataset_type), rollback_order=order, resource_type=contract.dataset_plan.dataset_type, ownership_token=self._ownership_token(plan.operation_id, action.action_id)))
                for suffix in ('objective', 'guardrail'):
                    order += 1
                    worst_case_resources.append(_ResourceRecord(action_id=f'{action.action_id}:evaluator:{suffix}', resource_id=f'azureai://accounts-max/projects/max/evaluators/{contract.evaluator_plan.requested_name}/versions/{contract.evaluator_plan.requested_version}', name=contract.evaluator_plan.requested_name, version=contract.evaluator_plan.requested_version, kind='evaluator', disposition='created', fingerprint=contract.evaluator_plan.generation_job_id, rollback_order=order, resource_type='custom', ownership_token=None))
                for role in _DEFINITION_ROLES:
                    order += 1
                    worst_case_resources.append(_ResourceRecord(action_id=f'{action.action_id}:definition:{role}', resource_id=f'eval-max-{role}', name=f'max-definition-{role}', version=role, kind='evaluation_definition', disposition='created', fingerprint=None, rollback_order=order, resource_type=None, ownership_token=None))
                    order += 1
                    worst_case_resources.append(_ResourceRecord(action_id=f'{action.action_id}:activation-run:{role}', resource_id=f'run-max-{role}', name=f'max-definition-{role}', version=role, kind='activation_run', disposition='created', fingerprint=None, rollback_order=order, resource_type=None, ownership_token=None))
                continue
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
            if not resource.ownership_token:
                return False
            tags = live.get('tags')
            ownership_tag = resource.ownership_tag or _OWNERSHIP_TAG
            if not isinstance(tags, Mapping) or str(tags.get(ownership_tag) or '') != resource.ownership_token:
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
        with_options = getattr(client, 'with_options', None)
        if callable(with_options):
            client = with_options(timeout=self._request_timeout, max_retries=2)
        self._openai = client
        return client

    def _openai_observer_client(self) -> object:
        if self._openai_observer is not None:
            return self._openai_observer
        getter = getattr(self._agent_observer_client, 'get_openai_client', None)
        if not callable(getter):
            raise FoundryUnsupportedCapabilityError('OpenAI-compatible evals observer unavailable', kind='unsupported_preview')
        try:
            client = getter()
        except Exception as exc:
            raise self._classify_error(exc) from exc
        with_options = getattr(client, 'with_options', None)
        if callable(with_options):
            client = with_options(timeout=self._request_timeout, max_retries=2)
        self._openai_observer = client
        return client

    def _new_openai_submission_client(self) -> object:
        if self._injected_client:
            return self._openai_client()
        project = AIProjectClient(self._project_endpoint, self._credential)
        getter = getattr(project, 'get_openai_client', None)
        if not callable(getter):
            raise FoundryUnsupportedCapabilityError('OpenAI-compatible evals submission unavailable', kind='unsupported_preview')
        try:
            client = getter()
        except Exception as exc:
            raise self._classify_error(exc) from exc
        with_options = getattr(client, 'with_options', None)
        return with_options(timeout=self._request_timeout, max_retries=2) if callable(with_options) else client

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

    @staticmethod
    def _agent_code_content_hash(version: object) -> str:
        details = _sdk_mapping(version)
        definition = _sdk_mapping(details.get('definition'))
        configuration = _sdk_mapping(
            definition.get('code_configuration')
            or definition.get('codeConfiguration')
            or details.get('code_configuration')
            or details.get('codeConfiguration')
        )
        return str(
            configuration.get('content_hash')
            or configuration.get('contentHash')
            or ''
        )

    def observe_agent_binding(self, *, agent_name: str, agent_version: str, source_root: str, package_root: str) -> Mapping[str, object]:
        """Observe a deployed immutable agent version and derive content fingerprints.

        The version's code zip is downloaded and hashed entry by entry, so the resulting
        fingerprints are comparable to the repository fingerprints discovery computes locally.
        Service metadata is never trusted on its own: when the version publishes a
        `content_hash`, the downloaded bytes must reproduce it exactly or the observation
        fails closed. Only paths and digests are returned; file content never leaves here.
        """

        getter = getattr(self._client.agents, 'get_version', None)
        downloader = getattr(self._client.agents, 'download_code', None)
        if not callable(getter) or not callable(downloader):
            raise FoundryUnsupportedCapabilityError('agent version code download is unavailable', kind='unsupported_preview')
        try:
            details = _as_mapping(getter(agent_name, agent_version))
        except ResourceNotFoundError as exc:
            raise FoundryPrerequisiteError(f'agent version {agent_name}:{agent_version} was not found', kind='prerequisite') from exc
        except Exception as exc:
            raise self._classify_error(exc) from exc
        observed_version = details.get('version')
        if isinstance(observed_version, str) and observed_version and observed_version != agent_version:
            raise FoundryPrerequisiteError('agent version response does not match the requested version', kind='prerequisite')
        definition = _sdk_mapping(details.get('definition'))
        code_configuration = (
            definition.get('code_configuration')
            or definition.get('codeConfiguration')
            or details.get('code_configuration')
            or details.get('codeConfiguration')
            or {}
        )
        normalized_code_configuration = _sdk_mapping(code_configuration)
        declared_hash = (
            normalized_code_configuration.get('content_hash')
            or normalized_code_configuration.get('contentHash')
        )
        try:
            chunks = downloader(agent_name, agent_version=agent_version)
        except ResourceNotFoundError as exc:
            raise FoundryPrerequisiteError(f'agent version {agent_name}:{agent_version} publishes no code archive', kind='prerequisite') from exc
        except Exception as exc:
            raise self._classify_error(exc) from exc
        payload = bytearray()
        try:
            for chunk in chunks:
                if not isinstance(chunk, (bytes, bytearray)):
                    raise FoundryPrerequisiteError('agent code download returned a non-binary chunk', kind='prerequisite')
                payload.extend(chunk)
                if len(payload) > _MAX_AGENT_CODE_BYTES:
                    raise FoundryPrerequisiteError('agent code archive exceeds the supported size budget', kind='prerequisite')
        except FoundryAdapterError:
            raise
        except Exception as exc:
            raise self._classify_error(exc) from exc
        if not payload:
            raise FoundryPrerequisiteError('agent code download returned no content', kind='prerequisite')
        content_hash = hashlib.sha256(bytes(payload)).hexdigest()
        verified = False
        if isinstance(declared_hash, str) and declared_hash:
            if declared_hash.split(':')[-1].casefold() != content_hash:
                raise FoundryPrerequisiteError('downloaded agent code does not match the published content hash', kind='prerequisite')
            verified = True
        digests = self._agent_code_digests(bytes(payload), source_root=source_root, package_root=package_root)
        return {
            'agent_name': str(details.get('name') or agent_name),
            'agent_version': agent_version,
            'code_content_hash': content_hash,
            'code_content_hash_verified': verified,
            'source_fingerprint': digests[0],
            'package_fingerprint': digests[1],
            'observed_file_count': digests[2],
        }

    def _agent_code_digests(self, archive: bytes, *, source_root: str, package_root: str) -> tuple[str, str, int]:
        source_files: dict[str, str] = {}
        package_files: dict[str, str] = {}
        try:
            with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                entries = bundle.infolist()
                if len(entries) > _MAX_AGENT_CODE_ENTRIES:
                    raise FoundryPrerequisiteError('agent code archive exceeds the supported entry count', kind='prerequisite')
                total = 0
                normalized_source_root = source_root.rstrip('/')
                for entry in entries:
                    if entry.is_dir():
                        continue
                    total += entry.file_size
                    if total > _MAX_AGENT_CODE_BYTES:
                        raise FoundryPrerequisiteError('agent code archive expands beyond the supported size budget', kind='prerequisite')
                    relative = _repo_relative_archive_path(
                        entry.filename,
                        root=package_root,
                    )
                    if relative is None or not is_fingerprintable_path(relative):
                        continue
                    digest = hashlib.sha256(bundle.read(entry)).hexdigest()
                    package_files[relative] = digest
                    if (
                        normalized_source_root == '.'
                        or relative == normalized_source_root
                        or relative.startswith(f'{normalized_source_root}/')
                    ):
                        source_files[relative] = digest
        except zipfile.BadZipFile as exc:
            raise FoundryPrerequisiteError('agent code download is not a readable zip archive', kind='prerequisite') from exc
        if not source_files or not package_files:
            raise FoundryPrerequisiteError('agent code archive contains no fingerprintable files', kind='prerequisite')
        try:
            return fingerprint_files(source_files), fingerprint_files(package_files), len(source_files)
        except BootstrapConfigError as exc:
            raise FoundryPrerequisiteError(f'agent code fingerprint failed: {exc}', kind='prerequisite') from exc

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

    def resolve_safety_bundle(self, required: Sequence[str] | None = None) -> tuple[Mapping[str, object], ...]:
        """Resolve the built-in safety evaluators this project actually publishes.

        Live projects expose individual registry evaluators such as
        `azureml://registries/azureml/evaluators/builtin.violence/versions/3`; there is no
        aggregate `content_safety` built-in in the catalogs observed so far. The legacy
        aggregate is honored only when a project really returns it. A project that cannot
        supply the required bundle fails closed, which blocks activation.
        """

        required_names = tuple(required or REQUIRED_SAFETY_EVALUATORS)
        resolved: dict[str, Mapping[str, object]] = {}
        for item in self.inventory_evaluators(include_builtin=True):
            if item.get('evaluator_type') != 'builtin':
                continue
            evaluator_id = item.get('id')
            if not isinstance(evaluator_id, str) or not evaluator_id:
                continue
            name = canonical_safety_name(evaluator_id, item.get('name') if isinstance(item.get('name'), str) else None)
            if name is None or name in resolved:
                continue
            resolved[name] = item
        if LEGACY_AGGREGATE_SAFETY_NAME in resolved:
            aggregate = resolved[LEGACY_AGGREGATE_SAFETY_NAME]
            return ({**aggregate, 'safety_name': LEGACY_AGGREGATE_SAFETY_NAME},)
        missing = [name for name in required_names if name not in resolved]
        if missing:
            raise FoundryUnsupportedCapabilityError(
                'required built-in safety evaluators are unavailable in this project: ' + ', '.join(sorted(missing)),
                kind='unsupported_preview',
            )
        ordered = [*required_names, *(name for name in sorted(resolved) if name not in required_names)]
        return tuple({**resolved[name], 'safety_name': name} for name in ordered)

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
        metadata = raw.get('metadata')
        generation_operation_id = (
            metadata.get('operation_id')
            if isinstance(metadata, Mapping)
            else None
        )
        return {
            'name': raw.get('name'),
            'version': raw.get('version'),
            'id': raw.get('id'),
            'evaluator_type': raw.get('evaluator_type'),
            'generation_job_id': generation_operation_id or raw.get('generation_job_id'),
            'service_generation_job_id': raw.get('generation_job_id'),
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
        return {
            'id': getattr(item, 'id', None),
            'name': getattr(item, 'name', None),
            'data_source_config': _sdk_mapping(getattr(item, 'data_source_config', None)),
            'testing_criteria': [_sdk_mapping(entry) for entry in (getattr(item, 'testing_criteria', None) or ())],
        }

    def create_or_adopt_evaluation_definition(self, request: _DefinitionActionRequest) -> Mapping[str, object]:
        """Create or adopt an immutable evaluation definition for the legacy granular action.

        DEPRECATED SURFACE. The plan factory emits a single composite `evaluation_onboarding`
        action whose definitions are created by `create_or_adopt_onboarding_definition`, which
        binds real `azure_ai_evaluator` (`TestingCriterionAzureAIEvaluator`) graders against an
        `azure_ai_source` data source config. This granular kind is retained only so plans
        created before contract v3 can still be resumed/rolled back; its `python` grader is a
        structurally valid, executable container that echoes the precomputed structural result
        supplied by `run_activation_smoke` and performs no independent scoring. Do not use it
        for new plans.
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
        client = self._openai_observer_client()
        try:
            item = client.evals.runs.retrieve(run_id=run_id, eval_id=definition_id)
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
        """Submit the legacy structural activation audit run and gate on the same fail-closed rules.

        DEPRECATED SURFACE, retained for pre-v3 plan resume. The composite onboarding machine
        submits a real `azure_ai_target_completions` run (see `_target_completion_data_source`)
        against the immutable split dataset with an `azure_ai_agent` target, then reads the
        service-produced per-criterion result counts via `activation_measurements`.

        Gating uses `evaluation.core.validate_activation` over the caller-supplied structural
        `cases`/`guardrails` (plain numbers/booleans only -- no raw prompts, responses, traces,
        or dataset rows are ever transmitted or persisted by this method). The corresponding
        run submission is a durable audit record (JSONL `file_content` data source) of those
        same structural results; it does not itself perform independent scoring.

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
                run = client.evals.runs.create(eval_id=definition_id, data_source=data_source, name=f'{phase}-activation-smoke')
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

    def cleanup_activation_draft(self, *, draft_agent_name: str, draft_agent_version: str, require_operation_created: bool = True) -> Mapping[str, object]:
        """Delete the temporary draft.

        On the composite onboarding path only a draft this operation created is ever deleted:
        a pre-existing agent version with the same requested name/version is refused earlier,
        and deleting it here would otherwise destroy a retained baseline. The deprecated
        pre-v3 `activation_cleanup` action opts out, because there the caller owned the draft
        lifecycle before this adapter existed.
        """

        if require_operation_created and (draft_agent_name, draft_agent_version) not in self._created_drafts:
            return {'draft_agent_name': draft_agent_name, 'draft_agent_version': draft_agent_version, 'completed': False, 'skipped': 'draft was not created by this operation'}
        deleter = getattr(self._client.agents, 'delete_version', None)
        if not callable(deleter):
            raise FoundryUnsupportedCapabilityError('agent draft version deletion unavailable', kind='unsupported_preview')
        try:
            deleter(draft_agent_name, draft_agent_version, force=True)
        except ResourceNotFoundError:
            pass
        except Exception as exc:
            raise self._classify_error(exc) from exc
        self._created_drafts.pop((draft_agent_name, draft_agent_version), None)
        return {'draft_agent_name': draft_agent_name, 'draft_agent_version': draft_agent_version, 'completed': True}

    def set_agent_packages(self, packages: Mapping[str, AgentPackage]) -> None:
        """Install the deterministically packaged source trees for this apply.

        The mapping is stored by reference and only ever read, so callers keep control of how
        packages are resolved.
        """

        self._agent_packages = packages

    def create_activation_draft(
        self,
        *,
        contract: EvaluationOnboardingRequest,
        package: AgentPackage,
        operation_id: str,
        action_id: str,
        on_pending: Callable[[Mapping[str, object]], None] | None = None,
    ) -> Mapping[str, object]:
        """Create the temporary hosted draft the activation smoke run targets.

        The reviewed repository source is uploaded as a new code-based agent version using the
        approved runtime, entry point, protocol, cpu/memory, and model deployment. A version
        that already exists under the requested name/version is a conflict, never an adoption:
        the draft must be provably operation-created before any evaluation run targets it and
        before cleanup is allowed to delete it.
        """

        assert contract.activation_plan is not None
        policy = contract.sidecar_policy
        if policy is None:
            raise FoundryPrerequisiteError('activation draft creation requires the reviewed sidecar policy', kind='prerequisite')
        plan = contract.activation_plan
        name, version = plan.draft_agent_name, plan.draft_agent_version
        existing = self._get_agent_version(name, version)
        if existing is not None:
            recorded = self._created_drafts.get((name, version))
            content_hash = self._agent_code_content_hash(existing)
            if recorded is None or recorded != package.zip_sha256 or content_hash.split(':')[-1].casefold() != package.zip_sha256:
                raise FoundryPrerequisiteError(
                    f'agent version {name}:{version} already exists and was not created by this operation',
                    kind='conflict',
                )
            return {'created': False, 'replayed': True, 'draft_agent_name': name, 'draft_agent_version': version, 'code_digest': package.zip_sha256}
        creator = getattr(self._client.agents, 'create_version_from_code', None)
        if not callable(creator):
            raise FoundryUnsupportedCapabilityError('code-based agent version creation is unavailable', kind='unsupported_preview')
        if on_pending is not None:
            on_pending({'draft_agent_name': name, 'draft_agent_version': version, 'zip_sha256': package.zip_sha256, 'tree_sha256': package.tree_sha256})
        definition = self._hosted_draft_definition(policy=policy, model_deployment=plan.model_deployment)
        archive = Path(package.zip_path)
        if not archive.is_file():
            raise FoundryPrerequisiteError('packaged agent source archive is missing', kind='prerequisite')
        archive_bytes = archive.read_bytes()
        digest = hashlib.sha256(archive_bytes).hexdigest()
        if digest != package.zip_sha256:
            raise FoundryPrerequisiteError('packaged agent source archive does not match its recorded digest', kind='prerequisite')
        ownership = self._with_ownership_tags(None, operation_id=operation_id, action_id=action_id)

        def _owned_created_version() -> object | None:
            observed = self._get_agent_version(name, version)
            if observed is None:
                return None
            observed_metadata = _sdk_attribute(observed, 'metadata')
            observed_hash = self._agent_code_content_hash(observed)
            if not isinstance(observed_metadata, Mapping) or observed_metadata.get(_OWNERSHIP_TAG) != ownership[_OWNERSHIP_TAG]:
                raise FoundryPrerequisiteError(
                    f'agent version {name}:{version} appeared with foreign ownership during upload',
                    kind='conflict',
                )
            if observed_hash and observed_hash.split(':')[-1].casefold() != package.zip_sha256:
                raise FoundryPrerequisiteError(
                    f'agent version {name}:{version} appeared with different uploaded content',
                    kind='conflict',
                )
            return observed if observed_hash else None

        outcome: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)

        def _submit() -> None:
            stream = io.BytesIO(archive_bytes)
            stream.name = str(archive)
            try:
                created_version = creator(
                    name,
                    definition=definition,
                    code=stream,
                    code_zip_sha256=package.zip_sha256,
                    metadata=ownership,
                    connection_timeout=self._request_timeout,
                    read_timeout=self._request_timeout,
                )
            except BaseException as exc:  # daemon submission reports back to the bounded owner
                outcome.put(('error', exc))
            else:
                outcome.put(('created', created_version))

        upload = threading.Thread(
            target=_submit,
            name=f'foundry-opt-upload-{name}-{version}',
            daemon=True,
        )
        upload.start()
        deadline = self._time() + self._operation_timeout
        while True:
            try:
                status, value = outcome.get_nowait()
            except queue.Empty:
                observed = _owned_created_version()
                if observed is not None:
                    created = observed
                    break
                if self._time() >= deadline:
                    raise FoundryOperationDeadlineError(
                        'agent draft upload deadline exceeded',
                        kind='deadline',
                        retryable=True,
                    )
                self._sleep(self._default_poll_interval)
                continue
            if status == 'created':
                created = value
                break
            observed = _owned_created_version()
            if observed is None:
                assert isinstance(value, BaseException)
                raise self._classify_error(value) from value
            created = observed
            break
        created_name = str(_sdk_attribute(created, 'name') or '')
        created_version = str(_sdk_attribute(created, 'version') or '')
        if created_name != name or created_version != version:
            raise FoundryPrerequisiteError('created agent draft identity does not match the approved activation plan', kind='prerequisite')
        content_hash = self._agent_code_content_hash(created)
        if content_hash and content_hash.split(':')[-1].casefold() != package.zip_sha256:
            raise FoundryPrerequisiteError('created agent draft content hash does not match the uploaded package', kind='prerequisite')
        self._created_drafts[(name, version)] = package.zip_sha256
        self._await_agent_version_active(name, version)
        return {'created': True, 'replayed': False, 'draft_agent_name': name, 'draft_agent_version': version, 'code_digest': content_hash or package.zip_sha256}

    def _hosted_draft_definition(self, *, policy: object, model_deployment: str) -> HostedAgentDefinition:
        runtime = getattr(policy, 'runtime', None)
        if runtime is None:
            raise FoundryPrerequisiteError('sidecar policy carries no runtime settings', kind='prerequisite')
        environment: dict[str, str] = {}
        variable = getattr(runtime, 'model_environment_variable', None)
        if isinstance(variable, str) and variable:
            environment[variable] = model_deployment
        project = getattr(policy, 'foundry_project', None)
        endpoint = getattr(project, 'project_endpoint', None) if project is not None else None
        if isinstance(endpoint, str) and endpoint:
            environment['AZURE_AI_PROJECT_ENDPOINT'] = endpoint
        return HostedAgentDefinition(
            cpu=str(getattr(runtime, 'cpu', None) or '1'),
            memory=str(getattr(runtime, 'memory', None) or '2Gi'),
            environment_variables=environment,
            protocol_versions=[ProtocolVersionRecord(protocol=str(runtime.protocol_name), version=str(runtime.protocol_version))],
            code_configuration=CodeConfiguration(
                runtime=str(runtime.runtime),
                entry_point=[str(item) for item in runtime.entrypoint],
                dependency_resolution=str(runtime.dependency_resolution),
            ),
        )

    def _get_agent_version(self, agent_name: str, agent_version: str) -> object | None:
        if self._injected_client:
            getter = getattr(self._agent_observer_client.agents, 'get_version', None)
            if not callable(getter):
                return None
            try:
                return getter(agent_name, agent_version)
            except ResourceNotFoundError:
                return None
            except Exception as exc:
                raise self._classify_error(exc) from exc
        token = self._credential.get_token('https://ai.azure.com/.default')
        url = (
            f"{self._project_endpoint.rstrip('/')}/agents/"
            f"{quote(agent_name, safe='')}/versions/{quote(agent_version, safe='')}"
        )
        try:
            response = httpx.get(
                url,
                params={'api-version': 'v1'},
                headers={
                    'Authorization': f'Bearer {token.token}',
                    'Accept': 'application/json',
                    'Accept-Encoding': 'identity',
                },
                timeout=self._request_timeout,
            )
        except httpx.HTTPError:
            raise FoundryNetworkError('agent version status request failed', kind='network', retryable=True) from None
        if response.status_code == 404:
            return None
        if response.status_code in {401, 403}:
            raise FoundryPermissionError('agent version status request was rejected', kind='permission', status_code=response.status_code)
        if response.status_code >= 400:
            raise FoundryPlatformError(
                'agent version status request failed',
                kind='platform',
                retryable=response.status_code >= 500,
                status_code=response.status_code,
            )
        try:
            payload = response.json()
        except ValueError:
            raise FoundryPlatformError('agent version status response was invalid', kind='platform') from None
        if not isinstance(payload, Mapping):
            raise FoundryPlatformError('agent version status response was invalid', kind='platform')
        return payload

    def _await_agent_version_active(self, agent_name: str, agent_version: str, *, deadline_monotonic: float | None = None) -> None:
        """Wait until the created draft is servable, failing closed on terminal failures."""

        if deadline_monotonic is None:
            deadline_monotonic = self._time() + self._operation_timeout
        while True:
            version = self._get_agent_version(agent_name, agent_version)
            if version is None:
                raise FoundryPrerequisiteError('created agent draft disappeared before activation', kind='prerequisite')
            status = str(_sdk_attribute(version, 'status', 'state', 'provisioning_state', 'provisioningState') or '').casefold()
            if status in {'', 'active', 'succeeded', 'ready', 'running'}:
                return
            if status in {'failed', 'canceled', 'cancelled', 'error', 'deleting'}:
                raise FoundryPrerequisiteError(f'created agent draft entered {status!r} instead of becoming active', kind='prerequisite')
            if deadline_monotonic is not None and self._time() >= deadline_monotonic:
                raise FoundryOperationDeadlineError('agent draft activation deadline exceeded', kind='deadline', retryable=True)
            self._sleep(self._default_poll_interval)

    def _record_pending_draft(self, ledger: Mapping[str, object], pending: Mapping[str, object]) -> None:
        stages = ledger.get('stages')
        if not isinstance(stages, dict):
            stages = {}
            ledger['stages'] = stages  # type: ignore[index]
        entry = stages.get('activation')
        detail = dict(entry) if isinstance(entry, Mapping) else {}
        detail['status'] = detail.get('status') if detail.get('status') == 'completed' else 'in_flight'
        detail['pending_draft'] = {str(key): value for key, value in pending.items()}
        stages['activation'] = detail
        self._created_drafts[(str(pending.get('draft_agent_name') or ''), str(pending.get('draft_agent_version') or ''))] = str(pending.get('zip_sha256') or '')
        self._publish_checkpoint()

    def _restore_created_drafts(self) -> None:
        for ledger in self._onboarding.values():
            stages = ledger.get('stages')
            entry = stages.get('activation') if isinstance(stages, Mapping) else None
            pending = entry.get('pending_draft') if isinstance(entry, Mapping) else None
            if not isinstance(pending, Mapping):
                continue
            name = str(pending.get('draft_agent_name') or '')
            version = str(pending.get('draft_agent_version') or '')
            digest = str(pending.get('zip_sha256') or '')
            if name and version and digest:
                self._created_drafts[(name, version)] = digest

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
        if deadline_monotonic is None:
            deadline_monotonic = self._time() + self._operation_timeout
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
                    recovered = self._reconcile_generation_job(handle)
                    if recovered is not None:
                        return self._normalize_job_result(handle.job_kind, recovered)
                    classified = self._classify_error(exc)
                    if isinstance(classified, FoundryPlatformError):
                        if self._time() >= deadline_monotonic:
                            raise FoundryOperationDeadlineError('polling deadline exceeded', kind='deadline', retryable=True) from None
                        self._sleep(poll_interval)
                        while True:
                            try:
                                poller = beta_group.begin_create_generation_job(
                                    None,
                                    continuation_token=handle.continuation_token,
                                    operation_id=handle.operation_id,
                                )
                                break
                            except Exception as resume_exc:
                                resume_error = self._classify_error(resume_exc)
                                if not isinstance(resume_error, FoundryPlatformError):
                                    raise resume_error from None
                                if self._time() >= deadline_monotonic:
                                    raise FoundryOperationDeadlineError('polling deadline exceeded', kind='deadline', retryable=True) from None
                                self._sleep(poll_interval)
                        continue
                    raise classified from None
                return self._normalize_job_result(handle.job_kind, _as_mapping(job))
            if deadline_monotonic is not None and self._time() >= deadline_monotonic:
                raise FoundryOperationDeadlineError('polling deadline exceeded', kind='deadline', retryable=True)
            self._sleep(poll_interval)

    def resume_generation_job(self, handle: FoundryOperationHandle, *, persist_before_poll: Callable[[FoundryOperationHandle], None] | None = None, deadline_monotonic: float | None = None, poll_interval: float | None = None) -> Mapping[str, object]:
        return self.poll_generation_job(handle, persist_before_poll=persist_before_poll, deadline_monotonic=deadline_monotonic, poll_interval=poll_interval)

    def _reconcile_generation_job(self, handle: FoundryOperationHandle) -> Mapping[str, object] | None:
        """Recover a succeeded evaluator job when the resumed LRO endpoint fails."""

        if handle.job_kind != 'evaluator_generation':
            return None
        lister = getattr(self._beta('evaluators'), 'list_generation_jobs', None)
        if not callable(lister):
            return None
        try:
            jobs = lister(limit=100)
            matches = []
            for item in jobs:
                job = _as_mapping(item)
                result = job.get('result')
                metadata = result.get('metadata') if isinstance(result, Mapping) else None
                if isinstance(metadata, Mapping) and metadata.get('operation_id') == handle.operation_id:
                    matches.append(job)
        except Exception:
            return None
        if len(matches) != 1 or str(matches[0].get('status') or '') not in {'completed', 'succeeded'}:
            return None
        return matches[0]

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
            saved = result if isinstance(result, Mapping) else job
            normalized['saved_evaluator'] = {
                'id': saved.get('id'),
                'name': saved.get('name'),
                'version': saved.get('version'),
                'display_name': saved.get('display_name'),
            }
        return normalized

    def plan_resources(self, plan: BootstrapPlan) -> tuple[BootstrapAction, ...]:
        return tuple(action for action in plan.actions if action.phase == 'evaluations')

    # ------------------------------------------------------------------
    # Composite, approval-bound onboarding: staged provider state machine
    # ------------------------------------------------------------------

    def _onboarding_request_from_action(self, action: BootstrapAction) -> EvaluationOnboardingRequest:
        payload = action.diagnostics
        if len(payload) != 3:
            raise FoundryPrerequisiteError('evaluation_onboarding action diagnostics are incomplete', kind='prerequisite')
        repo_agent_id, contract_hash, contract_json = (str(item) for item in payload)
        if not repo_agent_id or not contract_hash:
            raise FoundryPrerequisiteError('evaluation_onboarding action identity fields must be non-empty', kind='prerequisite')
        if len(contract_json.encode('utf-8')) > _MAX_PROVIDER_STATE_BYTES:
            raise FoundryPrerequisiteError('evaluation_onboarding contract exceeds safe persisted bound', kind='prerequisite')
        try:
            decoded = json.loads(contract_json)
        except (TypeError, ValueError) as exc:
            raise FoundryPrerequisiteError('evaluation_onboarding contract is not valid JSON', kind='prerequisite') from exc
        if not isinstance(decoded, Mapping):
            raise FoundryPrerequisiteError('evaluation_onboarding contract must be a mapping', kind='prerequisite')
        try:
            safe_persisted_document(decoded)
        except UnsafeCheckpointContentError as exc:
            raise FoundryPrerequisiteError('evaluation_onboarding contract contains prohibited content', kind='prerequisite') from exc
        try:
            contract = EvaluationOnboardingRequest.model_validate(dict(decoded))
        except (BootstrapConfigError, Exception) as exc:  # noqa: B014 - pydantic wraps config errors
            raise FoundryPrerequisiteError(f'evaluation_onboarding contract is invalid: {_short_reason(exc)}', kind='prerequisite') from None
        if contract.contract_hash != contract_hash or contract.repo_agent_id != repo_agent_id:
            raise FoundryPrerequisiteError('evaluation_onboarding contract does not match its approved identity', kind='prerequisite')
        if contract.stopped:
            raise FoundryPrerequisiteError('stopped agents must not carry an onboarding action', kind='prerequisite')
        return contract

    def _parse_dataset_uri(self, dataset_id: str) -> tuple[str, str]:
        match = _DATASET_URI_RE.fullmatch(dataset_id)
        if match is None:
            raise FoundryPrerequisiteError('reviewed dataset identifier is not an immutable dataset version', kind='prerequisite')
        return match.group('name'), match.group('version')

    def _parse_evaluator_uri(self, evaluator_id: str) -> tuple[str, str]:
        match = _EVALUATOR_URI_RE.fullmatch(evaluator_id)
        if match is None:
            raise FoundryPrerequisiteError('reviewed evaluator identifier is not an immutable evaluator version', kind='prerequisite')
        return match.group('name'), match.group('version')

    def dataset_case_index(self, dataset_name: str, dataset_version: str) -> tuple[Mapping[str, object], ...]:
        """Return stable case identifiers for a dataset version.

        Only `row_id`, optional `group_id`, and optional `category` are ever returned; no
        prompts, responses, or row content cross this boundary. The default live path reads
        the registered blob through a short-lived dataset SAS credential, derives stable row
        identifiers, and keeps the raw rows in memory for this operation only. A test/preview
        `get_case_index` seam is honored when a client provides one.
        """
        getter = getattr(self._client.datasets, 'get_case_index', None)
        if callable(getter):
            try:
                rows = getter(dataset_name, dataset_version)
            except Exception as exc:
                raise self._classify_error(exc) from exc
            index = [self._normalized_index_entry(_as_mapping(item)) for item in rows or ()]
        else:
            index = [self._normalized_index_entry(row) for row in self._dataset_rows(dataset_name, dataset_version)]
        if not index:
            raise FoundryPrerequisiteError('dataset case index is empty', kind='prerequisite')
        return tuple(index)

    @staticmethod
    def _normalized_index_entry(entry: Mapping[str, object]) -> Mapping[str, object]:
        row_id = entry.get('row_id') or entry.get('id')
        if not isinstance(row_id, str) or not row_id:
            raise FoundryPrerequisiteError('dataset case index entries require a stable row identifier', kind='prerequisite')
        normalized: dict[str, object] = {'row_id': row_id}
        for optional in ('group_id', 'category'):
            value = entry.get(optional)
            if isinstance(value, str) and value:
                normalized[optional] = value
        return normalized

    def _dataset_rows(self, dataset_name: str, dataset_version: str) -> tuple[Mapping[str, object], ...]:
        """Download and parse a registered dataset version, caching rows in memory only.

        Raw rows are never persisted: they live in this adapter instance for the duration of
        the operation, are used solely to materialize the deterministic split, and are dropped
        by `clear_dataset_cache`.
        """

        cached = self._dataset_row_cache.get((dataset_name, dataset_version))
        if cached is not None:
            return cached
        sas_uri, blob_uri = self._dataset_blob_credential(dataset_name, dataset_version)
        suffix = self._dataset_suffix(blob_uri or sas_uri)
        payloads = (
            ((suffix, self._download_bounded(sas_uri)),)
            if suffix is not None
            else self._download_folder_payloads(sas_uri, blob_uri)
        )
        rows: list[Mapping[str, object]] = []
        seen: set[str] = set()
        for payload_suffix, payload in payloads:
            for row in self._parse_dataset_payload(payload, suffix=payload_suffix):
                row_id = str(row['row_id'])
                if row_id in seen:
                    raise FoundryPrerequisiteError('dataset contains duplicate stable row identifiers', kind='prerequisite')
                seen.add(row_id)
                rows.append(row)
                if len(rows) > _MAX_DATASET_ROWS:
                    raise FoundryPrerequisiteError('dataset row count exceeds the supported budget', kind='prerequisite')
        if not rows:
            raise FoundryPrerequisiteError('dataset version contains no rows', kind='prerequisite')
        result = tuple(rows)
        self._dataset_row_cache[(dataset_name, dataset_version)] = result
        return result

    def _dataset_blob_credential(self, dataset_name: str, dataset_version: str) -> tuple[str, str]:
        getter = getattr(self._client.datasets, 'get_credentials', None)
        if not callable(getter):
            raise FoundryUnsupportedCapabilityError('dataset credentials are unavailable', kind='unsupported_preview')
        try:
            credential = getter(dataset_name, dataset_version)
        except ResourceNotFoundError as exc:
            raise FoundryPrerequisiteError('dataset version does not exist', kind='prerequisite') from exc
        except Exception as exc:
            raise self._classify_error(exc) from exc
        blob_reference = _sdk_attribute(credential, 'blob_reference', 'blobReference')
        if blob_reference is None:
            raise FoundryPrerequisiteError('dataset credential carries no blob reference', kind='prerequisite')
        sas = _sdk_attribute(blob_reference, 'credential')
        sas_uri = _sdk_attribute(sas, 'sas_uri', 'sasUri') if sas is not None else None
        blob_uri = _sdk_attribute(blob_reference, 'blob_uri', 'blobUri')
        if not isinstance(sas_uri, str) or not _is_supported_blob_uri(sas_uri):
            raise FoundryPrerequisiteError('dataset credential carries no usable SAS uri', kind='prerequisite')
        return sas_uri, blob_uri if isinstance(blob_uri, str) else ''

    @staticmethod
    def _dataset_suffix(uri: str) -> str | None:
        path = urlparse(uri).path
        name = path.rsplit('/', 1)[-1]
        if not name or '.' not in name:
            return None
        suffix = f'.{name.rsplit(".", 1)[-1]}'.casefold()
        if suffix not in _SUPPORTED_DATASET_SUFFIXES:
            raise FoundryUnsupportedCapabilityError(f'unsupported dataset file type {suffix!r}', kind='unsupported_preview')
        return suffix

    def _download_folder_payloads(self, sas_uri: str, blob_uri: str) -> tuple[tuple[str, bytes], ...]:
        """List and download a bounded container-scoped dataset deterministically."""

        sas = urlparse(sas_uri)
        blob = urlparse(blob_uri or sas_uri)
        sas_parts = [unquote(part) for part in sas.path.split('/') if part]
        blob_parts = [unquote(part) for part in blob.path.split('/') if part]
        if not sas_parts or not blob_parts or sas_parts[0].casefold() != blob_parts[0].casefold():
            raise FoundryPrerequisiteError('folder dataset credential does not identify one blob container', kind='prerequisite')
        prefix = '/'.join(blob_parts[1:]).strip('/')
        if prefix:
            prefix += '/'
        list_path = f'/{quote(sas_parts[0], safe="")}'
        query_parts = [('restype', 'container'), ('comp', 'list'), ('maxresults', str(_MAX_DATASET_FILES + 1))]
        if prefix:
            query_parts.append(('prefix', prefix))
        list_query = '&'.join(part for part in (sas.query, urlencode(query_parts)) if part)
        list_uri = urlunparse((sas.scheme, sas.netloc, list_path, '', list_query, ''))
        listing = self._download_bounded(list_uri)
        try:
            root = ET.fromstring(listing)
        except ET.ParseError:
            raise FoundryPrerequisiteError('folder dataset blob listing is invalid', kind='prerequisite') from None
        marker = root.findtext('./NextMarker')
        names = [
            str(item.text)
            for item in root.findall('./Blobs/Blob/Name')
            if isinstance(item.text, str) and item.text
        ]
        if marker or len(names) > _MAX_DATASET_FILES:
            raise FoundryPrerequisiteError('folder dataset file count exceeds the supported budget', kind='prerequisite')
        selected = sorted(
            (name for name in names if not prefix or name.startswith(prefix)),
            key=lambda value: (value.casefold(), value),
        )
        if not selected:
            raise FoundryPrerequisiteError('folder dataset contains no supported data files', kind='prerequisite')
        payloads: list[tuple[str, bytes]] = []
        total_bytes = 0
        for name in selected:
            suffix = self._dataset_suffix(name)
            if suffix is None:
                raise FoundryUnsupportedCapabilityError('nested folder entries are not supported', kind='unsupported_preview')
            blob_path = f'{list_path}/{quote(name, safe="/")}'
            download_uri = urlunparse((sas.scheme, sas.netloc, blob_path, '', sas.query, ''))
            payload = self._download_bounded(download_uri, maximum_bytes=_MAX_DATASET_BYTES - total_bytes)
            total_bytes += len(payload)
            payloads.append((suffix, payload))
        return tuple(payloads)

    def _download_bounded(self, sas_uri: str, *, maximum_bytes: int | None = None) -> bytes:
        maximum_bytes = _MAX_DATASET_BYTES if maximum_bytes is None else maximum_bytes
        if maximum_bytes <= 0:
            raise FoundryPrerequisiteError('dataset content exceeds the supported size budget', kind='prerequisite')
        payload = bytearray()
        try:
            with httpx.stream('GET', sas_uri, timeout=self._download_timeout) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    payload.extend(chunk)
                    if len(payload) > maximum_bytes:
                        raise FoundryPrerequisiteError('dataset content exceeds the supported size budget', kind='prerequisite')
        except FoundryAdapterError:
            raise
        except httpx.HTTPStatusError as exc:
            raise FoundryPermissionError('dataset content download was rejected', kind='permission', status_code=exc.response.status_code) from None
        except httpx.HTTPError:
            raise FoundryNetworkError('dataset content download failed', kind='network', retryable=True) from None
        return bytes(payload)

    @staticmethod
    def _parse_dataset_payload(payload: bytes, *, suffix: str) -> tuple[Mapping[str, object], ...]:
        try:
            text = payload.decode('utf-8')
        except UnicodeDecodeError:
            raise FoundryPrerequisiteError('dataset content is not UTF-8', kind='prerequisite') from None
        rows: list[Mapping[str, object]] = []
        if suffix == '.csv':
            reader = csv.DictReader(io.StringIO(text))
            for entry in reader:
                rows.append({str(key): value for key, value in entry.items() if key is not None})
                if len(rows) > _MAX_DATASET_ROWS:
                    raise FoundryPrerequisiteError('dataset row count exceeds the supported budget', kind='prerequisite')
        else:
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    raise FoundryPrerequisiteError('dataset content is not valid JSONL', kind='prerequisite') from None
                if not isinstance(entry, Mapping):
                    raise FoundryPrerequisiteError('dataset rows must be JSON objects', kind='prerequisite')
                rows.append({str(key): value for key, value in entry.items()})
                if len(rows) > _MAX_DATASET_ROWS:
                    raise FoundryPrerequisiteError('dataset row count exceeds the supported budget', kind='prerequisite')
        identified: list[Mapping[str, object]] = []
        seen: set[str] = set()
        for entry in rows:
            row_id = _stable_row_id(entry)
            if row_id in seen:
                raise FoundryPrerequisiteError('dataset contains duplicate stable row identifiers', kind='prerequisite')
            seen.add(row_id)
            identified.append({**entry, 'row_id': row_id})
        return tuple(identified)

    def clear_dataset_cache(self) -> None:
        """Drop every cached raw row; called as soon as an onboarding run finishes."""

        self._dataset_row_cache.clear()

    def publish_split_dataset(
        self,
        *,
        source_dataset: Mapping[str, object],
        role: str,
        case_ids: Sequence[str],
        dataset_name: str,
        dataset_version: str,
        dataset_type: str,
        connection_name: str | None,
        operation_id: str,
        action_id: str,
        on_pending: Callable[[Mapping[str, object]], None] | None = None,
    ) -> Mapping[str, object]:
        """Publish one deterministic split as its own immutable dataset version.

        The default live path writes the selected rows to a restrictive temporary JSONL file,
        uploads it with `datasets.upload_file`, validates the returned immutable identity, and
        deletes the temporary file immediately. An already published version is adopted when
        its recorded split fingerprint matches, so a restart never uploads or registers twice;
        a version that exists with a different fingerprint fails closed. An injected
        `split_writer` seam keeps the previous URI-based behaviour for fakes and tests.
        """

        fingerprint = _split_content_fingerprint(source_dataset=source_dataset, role=role, case_ids=case_ids)
        if self._split_writer is not None:
            uri = self.materialize_split_dataset(
                source_dataset=source_dataset,
                role=role,
                case_ids=case_ids,
                dataset_name=dataset_name,
                dataset_version=dataset_version,
            )
            result = self.create_or_adopt_dataset(
                operation_id=operation_id,
                action_id=action_id,
                dataset_name=dataset_name,
                dataset_version=dataset_version,
                dataset_content_uri=uri,
                dataset_type=dataset_type,
                connection_name=connection_name,
            )
            return {**result, 'split_fingerprint': fingerprint}
        existing = self.get_dataset(dataset_name, dataset_version)
        if existing is not None:
            recorded = self._published_splits.get((dataset_name, dataset_version))
            if recorded != fingerprint:
                raise FoundryPrerequisiteError(
                    f'dataset version {dataset_name}:{dataset_version} already exists with different content',
                    kind='prerequisite',
                )
            return {'created': False, 'adopted': True, 'replayed': True, 'dataset': existing, 'resource_id': str(existing['id']), 'split_fingerprint': fingerprint}
        if on_pending is not None:
            on_pending({'dataset_name': dataset_name, 'dataset_version': dataset_version, 'split_fingerprint': fingerprint, 'role': role})
        rows = self._selected_rows(source_dataset, case_ids)
        uploader = getattr(self._client.datasets, 'upload_file', None)
        if not callable(uploader):
            raise FoundryUnsupportedCapabilityError('dataset upload is unavailable', kind='unsupported_preview')
        handle, temp_path = tempfile.mkstemp(prefix='foundry-opt-split-', suffix='.jsonl')
        try:
            with os.fdopen(handle, 'w', encoding='utf-8', newline='\n') as stream:
                for row in rows:
                    stream.write(json.dumps({key: value for key, value in row.items()}, ensure_ascii=True, sort_keys=True))
                    stream.write('\n')
            os.chmod(temp_path, 0o600)
            try:
                created = uploader(name=dataset_name, version=dataset_version, file_path=temp_path, connection_name=connection_name)
            except Exception as exc:
                raise self._classify_error(exc) from exc
        finally:
            # Raw split content never outlives the upload.
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        dataset = self._normalize_dataset(created)
        if str(dataset.get('name') or '') != dataset_name or str(dataset.get('version') or '') != dataset_version:
            raise FoundryPrerequisiteError('uploaded dataset identity does not match the requested split version', kind='prerequisite')
        uploaded_type = str(dataset.get('type') or '')
        if dataset_type and uploaded_type and uploaded_type != dataset_type:
            raise FoundryPrerequisiteError('uploaded dataset type does not match the approved dataset type', kind='prerequisite')
        data_uri = str(dataset.get('data_uri') or '')
        if not data_uri:
            raise FoundryPrerequisiteError('uploaded dataset version exposes no content uri', kind='prerequisite')
        ownership_token = self._ownership_token(operation_id, action_id)
        owned = self._stamp_split_ownership(
            dataset_name=dataset_name,
            dataset_version=dataset_version,
            data_uri=data_uri,
            dataset_type=uploaded_type or dataset_type,
            connection_name=connection_name,
            operation_id=operation_id,
            action_id=action_id,
        )
        self._published_splits[(dataset_name, dataset_version)] = fingerprint
        return {
            'created': True,
            'adopted': False,
            'replayed': False,
            'dataset': owned,
            'resource_id': str(owned['id']),
            'ownership_token': ownership_token,
            'split_fingerprint': fingerprint,
        }

    def _stamp_split_ownership(self, *, dataset_name: str, dataset_version: str, data_uri: str, dataset_type: str, connection_name: str | None, operation_id: str, action_id: str) -> Mapping[str, object]:
        """Tag the just-uploaded version so created-only rollback can prove ownership.

        This updates the metadata of the same immutable version (identical name, version, and
        content uri); the blob itself is uploaded exactly once. Without a provable ownership
        tag the adapter could never safely delete the version again, so a tagging failure
        fails closed instead of leaving an uncompensatable resource behind.
        """

        payload_cls = FileDatasetVersion if dataset_type == 'uri_file' else FolderDatasetVersion
        payload = payload_cls(
            data_uri=data_uri,
            type=dataset_type,
            connection_name=connection_name,
            tags=self._with_ownership_tags(None, operation_id=operation_id, action_id=action_id),
        )
        try:
            tagged = self._client.datasets.create_or_update(dataset_name, dataset_version, payload)
        except Exception as exc:
            raise FoundryPrerequisiteError(
                f'uploaded split {dataset_name}:{dataset_version} could not be tagged for ownership: {_short_reason(exc)}',
                kind='prerequisite',
            ) from None
        return self._normalize_dataset(tagged, fingerprint=_fingerprint_dataset_content(data_uri, dataset_type))

    def _selected_rows(self, source_dataset: Mapping[str, object], case_ids: Sequence[str]) -> tuple[Mapping[str, object], ...]:
        rows = self._dataset_rows(str(source_dataset.get('name') or ''), str(source_dataset.get('version') or ''))
        by_id = {str(row['row_id']): row for row in rows}
        selected: list[Mapping[str, object]] = []
        for case_id in case_ids:
            row = by_id.get(str(case_id))
            if row is None:
                raise FoundryPrerequisiteError('split references a case that is not present in the source dataset', kind='prerequisite')
            selected.append(row)
        if not selected:
            raise FoundryPrerequisiteError('split selection is empty', kind='prerequisite')
        return tuple(selected)

    def materialize_split_dataset(self, *, source_dataset: Mapping[str, object], role: str, case_ids: Sequence[str], dataset_name: str, dataset_version: str) -> str:
        """Return the content URI of a materialized split of `source_dataset`.

        NOTE (narrow adapter seam): materialization is delegated to an injected writer that
        streams only the selected stable case identifiers from the source dataset into a new
        blob under the project connection and returns its URI. The writer never returns row
        content to this adapter. When no writer is configured the operation fails closed
        instead of registering two dataset versions that point at unsplit content.
        """
        if self._split_writer is None:
            raise FoundryUnsupportedCapabilityError('dataset split materialization is unavailable', kind='unsupported_preview')
        try:
            uri = self._split_writer(
                source_data_uri=str(source_dataset.get('data_uri') or ''),
                role=role,
                case_ids=tuple(case_ids),
                dataset_name=dataset_name,
                dataset_version=dataset_version,
            )
        except Exception as exc:
            raise self._classify_error(exc) from exc
        if not isinstance(uri, str) or not uri.startswith('https://'):
            raise FoundryPrerequisiteError('split materialization returned an invalid content URI', kind='prerequisite')
        return uri

    def _generated_rubric_document(self, evaluator_name: str, evaluator_version: str) -> Mapping[str, object]:
        version = self.get_evaluator_version(evaluator_name, evaluator_version)
        if version is None:
            raise FoundryPrerequisiteError('generated evaluator version does not exist', kind='prerequisite')
        raw = version.get('raw')
        rubric = (
            raw.get('rubric') or raw.get('definition')
            if isinstance(raw, Mapping)
            else None
        )
        if not isinstance(rubric, Mapping):
            raise FoundryPrerequisiteError('generated rubric structure is unavailable for validation', kind='prerequisite')
        return rubric

    def _await_activation_run(self, run_id: str, definition_id: str, *, deadline_monotonic: float | None = None) -> Mapping[str, object]:
        client = self._openai_observer_client()
        if deadline_monotonic is None:
            deadline_monotonic = self._time() + self._operation_timeout
        while True:
            try:
                run = client.evals.runs.retrieve(run_id=run_id, eval_id=definition_id)
            except Exception as exc:
                raise self._classify_error(exc) from exc
            status = str(getattr(run, 'status', '') or '')
            if status in {'completed', 'succeeded'}:
                return _as_mapping(run) if isinstance(run, Mapping) else {'id': run_id, 'status': status, 'run': run}
            if status in {'failed', 'canceled', 'cancelled', 'errored'}:
                raise FoundryPrerequisiteError('activation run did not complete successfully', kind='prerequisite')
            if deadline_monotonic is not None and self._time() >= deadline_monotonic:
                raise FoundryOperationDeadlineError('activation run polling deadline exceeded', kind='deadline', retryable=True)
            self._sleep(self._default_poll_interval)

    def activation_measurements(self, *, run_id: str, definition_id: str, phase: str, criteria: Mapping[str, Mapping[str, object]]) -> tuple[ActivationCaseFinalization, ...]:
        """Normalize one completed activation run into structural per-evaluator measurements.

        Only booleans and finite numbers are extracted; no sample content is read or persisted.
        A criterion that the platform did not execute, or that reports a non-finite/absent
        score for a scalar evaluator, fails closed.
        """
        payload = self._await_activation_run(run_id, definition_id)
        run = payload.get('run') if isinstance(payload.get('run'), object) and 'run' in payload else payload
        per_criterion = getattr(run, 'per_testing_criteria_results', None)
        if per_criterion is None and isinstance(payload, Mapping):
            per_criterion = payload.get('per_testing_criteria_results')
        if not isinstance(per_criterion, Sequence) or isinstance(per_criterion, (str, bytes, bytearray)) or not per_criterion:
            raise FoundryPrerequisiteError('activation run reported no testing-criteria measurements', kind='prerequisite')
        measurements: list[ActivationCaseFinalization] = []
        seen: set[str] = set()
        for item in per_criterion:
            entry = _as_mapping(item)
            name = entry.get('testing_criteria') or entry.get('name')
            if not isinstance(name, str) or name not in criteria:
                continue
            seen.add(name)
            criterion = criteria[name]
            passed = entry.get('passed')
            failed = entry.get('failed')
            errored = entry.get('errored', 0)
            if not isinstance(passed, int) or not isinstance(failed, int) or isinstance(passed, bool) or isinstance(failed, bool):
                raise FoundryPrerequisiteError('activation criterion counts are invalid', kind='prerequisite')
            if not isinstance(errored, int) or isinstance(errored, bool) or errored > 0:
                raise FoundryPrerequisiteError('activation criterion reported execution errors', kind='prerequisite')
            total = passed + failed
            if total <= 0:
                raise FoundryPrerequisiteError('activation criterion executed no cases', kind='prerequisite')
            pass_rate = passed / total
            normalization_kind = str(criterion['normalization_kind'])
            if normalization_kind == 'scalar':
                score = entry.get('score')
                if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(float(score)):
                    raise FoundryPrerequisiteError('scalar activation criterion reported no finite score', kind='prerequisite')
                measurement = ActivationCaseFinalization(
                    phase=phase,
                    evaluator_id=str(criterion['evaluator_id']),
                    executable=True,
                    normalization_kind='scalar',
                    score=float(score),
                    pass_rate=pass_rate,
                    source_min=float(criterion['source_min']),
                    source_max=float(criterion['source_max']),
                )
            else:
                # A pass/fail evaluator is recorded as a binary score plus its measured pass
                # rate: the score keeps the structural gate meaningful while the pass rate
                # drives the 100% Content Safety guardrail.
                measurement = ActivationCaseFinalization(
                    phase=phase,
                    evaluator_id=str(criterion['evaluator_id']),
                    executable=True,
                    normalization_kind='pass_fail',
                    score=1.0 if pass_rate == 1.0 else 0.0,
                    pass_rate=pass_rate,
                )
            measurements.append(measurement)
        missing = set(criteria) - seen
        if missing:
            raise FoundryPrerequisiteError('activation run did not measure every approved evaluator', kind='prerequisite')
        return tuple(measurements)

    def create_or_adopt_onboarding_definition(self, *, role: str, definition_name: str, dataset: Mapping[str, object], criteria: Mapping[str, Mapping[str, object]]) -> Mapping[str, object]:
        """Create or adopt the immutable definition that measures every approved evaluator.

        Uses the real cloud-evaluation surface: an `azure_ai_source` data source config plus
        one `TestingCriterionAzureAIEvaluator` (`azure_ai_evaluator`) grader per approved
        evaluator, binding the immutable Foundry evaluator name/version directly instead of
        echoing precomputed numbers through a Python grader.

        Adoption is strict: an existing definition with the requested name is retrieved and
        may only be adopted when its canonical signature (data source config plus every
        criterion's type/name/evaluator name/version/data mapping/initialization parameters)
        equals the requested one. A name collision with different measurement semantics fails
        closed instead of silently evaluating against the wrong definition.
        """
        if role not in _DEFINITION_ROLES:
            raise FoundryPrerequisiteError('evaluation definition role is invalid', kind='prerequisite')
        client = self._openai_client()
        data_source_config: AzureAIDataSourceConfig = {'type': 'azure_ai_source', 'scenario': _EVAL_SCENARIO}
        testing_criteria: list[TestingCriterionAzureAIEvaluator] = []
        for name in sorted(criteria):
            criterion = criteria[name]
            entry: TestingCriterionAzureAIEvaluator = {
                'type': 'azure_ai_evaluator',
                'name': name,
                'evaluator_name': str(criterion['evaluator_name']),
                'data_mapping': dict(
                    criterion.get('data_mapping')
                    if isinstance(criterion.get('data_mapping'), Mapping)
                    else _EVALUATOR_DATA_MAPPING
                ),
            }
            initialization = criterion.get('initialization_parameters')
            if isinstance(initialization, Mapping) and initialization:
                entry['initialization_parameters'] = {str(key): value for key, value in initialization.items()}
            version = criterion.get('evaluator_version')
            if isinstance(version, str) and version:
                entry['evaluator_version'] = version
            testing_criteria.append(entry)
        requested_signature = _definition_signature(data_source_config, testing_criteria)
        existing = next((item for item in self.list_evaluation_definitions() if item.get('name') == definition_name), None)
        if existing is not None:
            resource_id = existing.get('id')
            if not isinstance(resource_id, str) or not resource_id:
                raise FoundryPrerequisiteError('existing evaluation definition has no immutable identifier', kind='prerequisite')
            live = self.get_evaluation_definition(resource_id)
            if live is None:
                raise FoundryPrerequisiteError('existing evaluation definition could not be retrieved for adoption', kind='prerequisite')
            live_criteria = live.get('testing_criteria')
            if not isinstance(live_criteria, Sequence) or isinstance(live_criteria, (str, bytes, bytearray)) or not live_criteria:
                raise FoundryPrerequisiteError('existing evaluation definition exposes no testing criteria', kind='prerequisite')
            live_config = live.get('data_source_config')
            existing_signature = _definition_signature(
                live_config if isinstance(live_config, Mapping) else {},
                [item for item in live_criteria if isinstance(item, Mapping)],
            )
            if existing_signature != requested_signature:
                raise FoundryPrerequisiteError(
                    f'existing evaluation definition {definition_name!r} does not match the approved evaluator bindings',
                    kind='prerequisite',
                )
            return {'created': False, 'adopted': True, 'definition': {'id': resource_id, 'name': definition_name, 'signature': existing_signature}, 'resource_id': resource_id}
        try:
            created = client.evals.create(data_source_config=data_source_config, testing_criteria=testing_criteria, name=definition_name)
        except Exception as exc:
            raise self._classify_error(exc) from exc
        created_id = getattr(created, 'id', None)
        if not isinstance(created_id, str) or not created_id:
            raise FoundryPrerequisiteError('evaluation definition creation returned no id', kind='prerequisite')
        del dataset
        return {'created': True, 'adopted': False, 'definition': {'id': created_id, 'name': definition_name, 'signature': requested_signature}, 'resource_id': created_id}

    def get_dataset_by_id(self, dataset_id: str) -> Mapping[str, object] | None:
        """Resolve a dataset version from an immutable `azureai://.../data/<name>/versions/<v>` id."""

        name, version = self._parse_dataset_uri(dataset_id)
        return self.get_dataset(name, version)

    def _synthetic_generation_prompt(self, contract: EvaluationOnboardingRequest) -> str:
        """Build the reviewed, bounded generation instruction (never raw customer content)."""

        assert contract.dataset_plan is not None
        return (
            f'Generate representative evaluation cases for the {contract.dataset_plan.agent_name} '
            f'agent (version {contract.dataset_plan.agent_version}) covering its documented task '
            'scope, including realistic edge cases, using only the reviewed agent definition.'
        )

    def _target_completion_data_source(self, *, dataset_file_id: str, draft_agent_name: str, draft_agent_version: str) -> TargetCompletionEvalRunDataSource:
        """Evaluate an existing immutable split dataset against the owned draft agent."""

        target: AzureAIAgentTargetParam = {
            'type': 'azure_ai_agent',
            'name': draft_agent_name,
            'version': draft_agent_version,
        }
        return {
            'type': 'azure_ai_target_completions',
            'source': {'type': 'file_id', 'id': dataset_file_id},
            'target': target,
            'input_messages': {
                'type': 'template',
                'template': [
                    {
                        'type': 'message',
                        'role': 'user',
                        'content': {
                            'type': 'input_text',
                            'text': '{{item.query}}',
                        },
                    }
                ],
            },
        }

    def run_synthetic_generation(
        self,
        *,
        definition_id: str,
        samples_count: int,
        prompt: str,
        model_deployment_name: str,
        output_dataset_name: str,
        agent_name: str,
        agent_version: str,
        run_name: str,
        on_submitted: Callable[[str], None] | None = None,
        deadline_monotonic: float | None = None,
    ) -> Mapping[str, object]:
        """Generate a synthetic dataset by running the agent through the cloud-eval API.

        Mirrors the official `sample_synthetic_data_agent_evaluation.py` flow: an
        `azure_ai_synthetic_data_gen_preview` run data source with
        `item_generation_params` (`synthetic_data_gen_preview`) and an `azure_ai_agent`
        target, polled through `evals.runs.retrieve`. The immutable output dataset id is read
        back from `run.data_source.item_generation_params.output_dataset_id`, and the accepted
        sample count from the run's output items.
        """
        if samples_count <= 0:
            raise FoundryPrerequisiteError('synthetic generation requires a positive sample count', kind='prerequisite')
        data_source = {
            'type': 'azure_ai_synthetic_data_gen_preview',
            'item_generation_params': {
                'type': _SYNTHETIC_ITEM_GENERATION_TYPE,
                'samples_count': int(samples_count),
                'prompt': prompt,
                'model_deployment_name': model_deployment_name,
                'output_dataset_name': output_dataset_name,
            },
            'target': {'type': 'azure_ai_agent', 'name': agent_name, 'version': agent_version},
        }
        run_id = self._submit_eval_run(
            definition_id=definition_id,
            data_source=data_source,
            run_name=run_name,
            on_submitted=on_submitted,
        )
        payload = self._await_activation_run(run_id, definition_id, deadline_monotonic=deadline_monotonic)
        completed = payload.get('run') if 'run' in payload else payload
        output_dataset_id = self._synthetic_output_dataset_id(completed, payload)
        if not isinstance(output_dataset_id, str) or not output_dataset_id:
            raise FoundryPrerequisiteError('synthetic generation run returned no output dataset id', kind='prerequisite')
        return {'run_id': run_id, 'output_dataset_id': output_dataset_id, 'generated_samples': self.run_output_item_count(run_id=run_id, definition_id=definition_id)}

    @staticmethod
    def _operation_run_name(prefix: str, operation_id: str) -> str:
        safe_prefix = re.sub(r'[^A-Za-z0-9._~-]+', '-', prefix).strip('-') or 'foundry-opt'
        suffix = hashlib.sha256(operation_id.encode('utf-8')).hexdigest()[:12]
        return f'{safe_prefix[:48]}-{suffix}'

    def _submit_eval_run(
        self,
        *,
        definition_id: str,
        data_source: Mapping[str, object],
        run_name: str,
        on_submitted: Callable[[str], None] | None = None,
    ) -> str:
        """Submit without waiting for the service-held response, reconciling by unique name."""

        observer = self._openai_observer_client()

        def _matching_runs() -> list[object]:
            try:
                return [
                    item
                    for item in observer.evals.runs.list(eval_id=definition_id)
                    if getattr(item, 'name', None) == run_name
                ]
            except Exception as exc:
                raise self._classify_error(exc) from exc

        existing = _matching_runs()
        if len(existing) > 1:
            raise FoundryPrerequisiteError('evaluation run name is ambiguous', kind='prerequisite')
        if existing:
            run_id = getattr(existing[0], 'id', None)
            if not isinstance(run_id, str) or not run_id:
                raise FoundryPrerequisiteError('existing evaluation run has no id', kind='prerequisite')
            if on_submitted is not None:
                on_submitted(run_id)
            return run_id

        outcome: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)
        submission = self._new_openai_submission_client()

        def _submit() -> None:
            try:
                created = submission.evals.runs.create(
                    eval_id=definition_id,
                    data_source=data_source,
                    name=run_name,
                )
            except BaseException as exc:  # daemon submission reports to the bounded owner
                outcome.put(('error', exc))
            else:
                outcome.put(('created', created))

        threading.Thread(
            target=_submit,
            name=f'foundry-opt-eval-run-{run_name}',
            daemon=True,
        ).start()
        deadline = self._time() + self._operation_timeout
        while True:
            try:
                status, value = outcome.get_nowait()
            except queue.Empty:
                matches = _matching_runs()
                if len(matches) > 1:
                    raise FoundryPrerequisiteError('evaluation run name is ambiguous', kind='prerequisite')
                if matches:
                    run_id = getattr(matches[0], 'id', None)
                    if isinstance(run_id, str) and run_id:
                        if on_submitted is not None:
                            on_submitted(run_id)
                        return run_id
                if self._time() >= deadline:
                    raise FoundryOperationDeadlineError('evaluation run submission deadline exceeded', kind='deadline', retryable=True)
                self._sleep(self._default_poll_interval)
                continue
            if status == 'created':
                run_id = getattr(value, 'id', None)
                if not isinstance(run_id, str) or not run_id:
                    raise FoundryPrerequisiteError('evaluation run submission returned no id', kind='prerequisite')
                if on_submitted is not None:
                    on_submitted(run_id)
                return run_id
            matches = _matching_runs()
            if len(matches) == 1:
                run_id = getattr(matches[0], 'id', None)
                if isinstance(run_id, str) and run_id:
                    if on_submitted is not None:
                        on_submitted(run_id)
                    return run_id
            assert isinstance(value, BaseException)
            raise self._classify_error(value) from value

    def _synthetic_output_dataset_id(self, completed: object, payload: Mapping[str, object]) -> str | None:
        """Read `data_source.item_generation_params.output_dataset_id` from a completed run."""

        candidates: list[object] = [getattr(completed, 'data_source', None)]
        if isinstance(payload, Mapping):
            candidates.append(payload.get('data_source'))
        for source in candidates:
            if source is None:
                continue
            params = getattr(source, 'item_generation_params', None)
            if params is None and isinstance(source, Mapping):
                params = source.get('item_generation_params')
            if params is None and callable(getattr(source, 'as_dict', None)):
                params = _as_mapping(source).get('item_generation_params')
            if isinstance(params, Mapping):
                value = params.get('output_dataset_id')
            else:
                value = getattr(params, 'output_dataset_id', None)
            if isinstance(value, str) and value:
                return value
        return None

    def run_output_item_count(self, *, run_id: str, definition_id: str) -> int | None:
        """Count a run's output items, the real per-generated-sample records.

        Returns `None` when the project's client does not expose the output-items API, so the
        caller can fall back to the resolved dataset's case index.
        """

        client = self._openai_observer_client()
        runs = getattr(client.evals, 'runs', None)
        output_items = getattr(runs, 'output_items', None)
        lister = getattr(output_items, 'list', None)
        if not callable(lister):
            return None
        try:
            items = lister(run_id=run_id, eval_id=definition_id)
            count = 0
            for _item in items:
                count += 1
                if count > _MAX_RUN_OUTPUT_ITEMS:
                    raise FoundryPrerequisiteError('evaluation run reported more output items than the supported budget', kind='prerequisite')
        except FoundryAdapterError:
            raise
        except Exception as exc:
            raise self._classify_error(exc) from exc
        return count

    def _stage_ledger(self, action_id: str) -> dict[str, object]:
        ledger = self._onboarding.get(action_id)
        if ledger is None:
            ledger = {'stages': {}, 'finalization': None}
            self._onboarding[action_id] = ledger
        return ledger

    def _record_stage(self, ledger: Mapping[str, object], stage: str, detail: Mapping[str, object]) -> None:
        stages = ledger.get('stages')
        if isinstance(stages, dict):
            # A completed stage drops any in-flight handle: there is nothing left to resume.
            existing = stages.get(stage)
            recovery = {}
            if stage == 'split' and isinstance(existing, Mapping) and isinstance(existing.get('pending_splits'), Mapping):
                recovery['pending_splits'] = dict(existing['pending_splits'])
            stages[stage] = {'status': 'completed', **recovery, **{key: value for key, value in detail.items()}}
            self._publish_checkpoint()

    def _completed_stage(self, ledger: Mapping[str, object], stage: str) -> Mapping[str, object] | None:
        stages = ledger.get('stages')
        if not isinstance(stages, Mapping):
            return None
        entry = stages.get(stage)
        if isinstance(entry, Mapping) and entry.get('status') == 'completed':
            return entry
        return None

    def _assert_activation_gates(self, bounds: object, measurements: Sequence[ActivationCaseFinalization]) -> None:
        """Fail closed on structural, execution, headroom, or safety-bundle gate violations."""

        if not measurements:
            raise FoundryPrerequisiteError('activation produced no measurements', kind='prerequisite')
        cases = [
            {
                'executable': case.executable,
                'normalization': {'kind': case.normalization_kind, 'source_min': case.source_min, 'source_max': case.source_max},
                'score': case.score,
            }
            for case in measurements
        ]
        safety_measurements = [
            (case, canonical_safety_name(case.evaluator_id))
            for case in measurements
            if canonical_safety_name(case.evaluator_id) is not None
        ]
        guardrails = [
            {'evaluator_id': case.evaluator_id, 'safety_name': name, 'pass_rate': case.pass_rate}
            for case, name in safety_measurements
        ]
        try:
            validate_activation(cases=cases, guardrails=guardrails)
        except BootstrapConfigError as exc:
            raise FoundryPrerequisiteError(f'activation smoke gate failed: {exc}', kind='prerequisite') from None
        required = float(getattr(bounds, 'required_safety_pass_rate', 1.0))
        required_names = tuple(getattr(bounds, 'required_safety_evaluators', REQUIRED_SAFETY_EVALUATORS))
        for name in required_names:
            phases = {case.phase for case, resolved in safety_measurements if resolved == name}
            aggregate_phases = {
                case.phase for case, resolved in safety_measurements if resolved == LEGACY_AGGREGATE_SAFETY_NAME
            }
            if phases != set(_DEFINITION_ROLES) and aggregate_phases != set(_DEFINITION_ROLES):
                raise FoundryPrerequisiteError(
                    f'safety evaluator {name} must be measured in both activation phases', kind='prerequisite'
                )
        if any(case.pass_rate != required for case, _ in safety_measurements):
            raise FoundryPrerequisiteError(
                'every configured safety evaluator must pass at 100% in both activation phases', kind='prerequisite'
            )

    def run_evaluation_onboarding(self, *, plan: BootstrapPlan, action: BootstrapAction, contract: EvaluationOnboardingRequest, on_resource: Callable[[_ResourceDraft], None] | None = None) -> Mapping[str, object]:
        """Execute the approval-bound onboarding stages and return the receipt finalization.

        Stages run in order and are recorded in provider state so a restarted operation
        resumes instead of repeating a completed stage. Every dynamic output is checked
        against the approved contract bounds before the finalization is sealed; any gate
        failure raises, which keeps the previously active sidecar and bundle in place.
        """
        assert contract.dataset_plan is not None and contract.evaluator_plan is not None
        assert contract.definition_plan is not None and contract.activation_plan is not None
        assert contract.telemetry_probe is not None
        ledger = self._stage_ledger(action.action_id)
        drafts: list[_ResourceDraft] = []

        def record(draft: _ResourceDraft) -> None:
            """Report a touched resource immediately so a mid-stage failure still rolls back."""

            drafts.append(draft)
            if on_resource is not None:
                on_resource(draft)

        bounds = contract.bounds
        dataset_plan = contract.dataset_plan
        evaluator_plan = contract.evaluator_plan

        # --- stage 1: inventory -------------------------------------------------
        reuse_candidates = dataset_plan.reuse_candidates
        reuse_decision = 'reuse_existing_assets' if reuse_candidates and evaluator_plan.reuse_evaluator_id else 'generate_new_assets'
        inventory_detail = {
            'reuse_decision': reuse_decision,
            'dataset_versions': len(self.inventory_datasets()),
            'evaluator_versions': len(self.inventory_evaluators(include_builtin=True)),
            'definitions': len(self.list_evaluation_definitions()),
        }
        self._record_stage(ledger, 'inventory', inventory_detail)

        # --- stage 2: generation ------------------------------------------------
        dataset_strategy = 'trace' if dataset_plan.generation_kind == 'dataset_trace' else 'synthetic_only'
        generated_sample_count = 0
        source_dataset: Mapping[str, object] | None = None
        if reuse_decision == 'generate_new_assets':
            completed = self._completed_stage(ledger, 'generation')
            if completed is not None and isinstance(completed.get('dataset_name'), str):
                source_dataset = self.get_dataset(str(completed['dataset_name']), str(completed['dataset_version']))
                generated_sample_count = int(completed.get('generated_sample_count') or 0)
                if source_dataset is None:
                    stages = ledger.get('stages')
                    if isinstance(stages, dict):
                        stages.pop('generation', None)
                        self._publish_checkpoint()
                    completed = None
            if completed is None:
                if dataset_strategy == 'synthetic_only':
                    # Real synthetic agent run: the service generates the dataset and returns
                    # its immutable `output_dataset_id`.
                    generation_definition = self.create_or_adopt_onboarding_definition(
                        role='development',
                        definition_name=f'{contract.repo_agent_id}-synthetic-generation',
                        dataset={},
                        criteria={
                            'coherence': {
                                'evaluator_name': 'builtin.coherence',
                                'initialization_parameters': {
                                    'deployment_name': dataset_plan.generation_model_deployment,
                                },
                            }
                        },
                    )
                    generation_definition_id = str(generation_definition['resource_id'])
                    if generation_definition['created']:
                        record(
                            _ResourceDraft(
                                suffix='definition:generation',
                                resource_id=generation_definition_id,
                                name=f'{contract.repo_agent_id}-synthetic-generation',
                                version='generation',
                                kind='evaluation_definition',
                                disposition='created',
                            )
                        )
                    outcome = self.run_synthetic_generation(
                        definition_id=generation_definition_id,
                        samples_count=bounds.target_sample_count,
                        prompt=self._synthetic_generation_prompt(contract),
                        model_deployment_name=dataset_plan.generation_model_deployment,
                        output_dataset_name=f'{dataset_plan.requested_development_name}-source',
                        agent_name=dataset_plan.agent_name,
                        agent_version=dataset_plan.agent_version,
                        run_name=self._operation_run_name(
                            f'{contract.repo_agent_id}-synthetic',
                            plan.operation_id,
                        ),
                        on_submitted=lambda run_id: record(
                            _ResourceDraft(
                                suffix='activation-run:generation',
                                resource_id=run_id,
                                name=generation_definition_id,
                                version='generation',
                                kind='activation_run',
                                disposition='created',
                            )
                        ),
                    )
                    source_dataset = self.get_dataset_by_id(str(outcome['output_dataset_id']))
                    if source_dataset is None:
                        raise FoundryPrerequisiteError('synthetic generation output dataset is not resolvable', kind='prerequisite')
                    source_tags = source_dataset.get('tags')
                    generation_resource_id = (
                        str(source_tags.get('data_generation_job_id') or '')
                        if isinstance(source_tags, Mapping)
                        else ''
                    )
                    if not generation_resource_id:
                        raise FoundryPrerequisiteError(
                            'synthetic generation output dataset has no service ownership tag',
                            kind='prerequisite',
                        )
                    record(
                        _ResourceDraft(
                            suffix='dataset:generation-source',
                            resource_id=str(source_dataset['id']),
                            name=str(source_dataset.get('name') or ''),
                            version=str(source_dataset.get('version') or ''),
                            kind='dataset',
                            disposition='created',
                            fingerprint=(
                                _fingerprint_dataset_content(
                                    str(source_dataset.get('data_uri') or ''),
                                    str(source_dataset.get('type') or ''),
                                )
                            ),
                            resource_type=str(source_dataset.get('type') or ''),
                            ownership_token=generation_resource_id,
                            ownership_tag='data_generation_job_id',
                        )
                    )
                    index = self.dataset_case_index(str(source_dataset.get('name') or ''), str(source_dataset.get('version') or ''))
                    generated_sample_count = len(index)
                else:
                    handle = self._pending_handle(ledger, 'generation', job_kind='dataset_generation')
                    if handle is None:
                        handle = self.create_dataset_generation_job(self._dataset_generation_request(contract))
                    job = self.poll_generation_job(
                        handle,
                        persist_before_poll=lambda pending: self._record_pending_handle(ledger, 'generation', pending),
                    )
                    generated = job.get('generated_samples')
                    if not isinstance(generated, int) or isinstance(generated, bool):
                        raise FoundryPrerequisiteError('dataset generation reported no sample count', kind='prerequisite')
                    generated_sample_count = generated
                    if generated_sample_count < bounds.telemetry_minimum_samples:
                        # 14 or fewer useful trace samples must never be configured as a partial
                        # trace dataset; the reviewed contract must switch to synthetic-only.
                        raise FoundryPrerequisiteError(
                            f'trace generation produced {generated_sample_count} useful samples; '
                            f'{bounds.telemetry_minimum_samples}+ are required before a trace dataset may be configured',
                            kind='prerequisite',
                        )
                    outputs = job.get('output_datasets')
                    if not isinstance(outputs, Sequence) or isinstance(outputs, (str, bytes, bytearray)) or len(outputs) != 1:
                        raise FoundryPrerequisiteError('dataset generation produced no single accepted output dataset', kind='prerequisite')
                    source_dataset = _as_mapping(outputs[0])
                if generated_sample_count > bounds.maximum_generated_sample_count:
                    raise FoundryPrerequisiteError('generated sample count exceeds the approved bound', kind='prerequisite')
                assert source_dataset is not None
                self._record_stage(
                    ledger,
                    'generation',
                    {
                        'dataset_name': str(source_dataset.get('name') or ''),
                        'dataset_version': str(source_dataset.get('version') or ''),
                        'generated_sample_count': generated_sample_count,
                        'generation_job_id': dataset_plan.generation_job_id,
                        'dataset_strategy': dataset_strategy,
                    },
                )
        else:
            self._record_stage(ledger, 'generation', {'skipped': 'reused_existing_assets'})

        # --- stage 3: split -----------------------------------------------------
        datasets: dict[str, DatasetFinalization] = {}
        if reuse_decision == 'reuse_existing_assets':
            assert reuse_candidates is not None
            rows: list[Mapping[str, object]] = []
            for role, dataset_id in zip(_DEFINITION_ROLES, reuse_candidates, strict=True):
                name, version = self._parse_dataset_uri(dataset_id)
                live = self.get_dataset(name, version)
                if live is None:
                    raise FoundryPrerequisiteError('reviewed reuse dataset version does not exist', kind='prerequisite')
                index = self.dataset_case_index(name, version)
                rows.extend({**dict(item), 'split_role': role} for item in index)
                datasets[role] = DatasetFinalization(
                    role=role,
                    dataset_name=name,
                    dataset_version=version,
                    dataset_id=str(live['id']),
                    dataset_type=str(live.get('type') or dataset_plan.dataset_type),
                    case_count=len(index),
                    disposition='adopted',
                )
                record(
                    _ResourceDraft(
                        suffix=f'dataset:{role}',
                        resource_id=str(live['id']),
                        name=name,
                        version=version,
                        kind='dataset',
                        disposition='adopted',
                        fingerprint=str(live.get('content_fingerprint')) if live.get('content_fingerprint') else None,
                        resource_type=str(live.get('type') or ''),
                    )
                )
            development_rows = tuple(item['row_id'] for item in rows if item.get('split_role') == 'development')
            validating_rows = tuple(item['row_id'] for item in rows if item.get('split_role') == 'validating')
            if set(development_rows) & set(validating_rows):
                raise FoundryPrerequisiteError('reused development and validating datasets overlap', kind='prerequisite')
            split_result = split_dataset_rows(
                [
                    {'row_id': str(item['row_id']), 'group_id': str(item.get('group_id') or item['row_id']), 'category': str(item.get('category') or '')}
                    for item in rows
                ]
            )
            split = SplitFinalization(
                algorithm_version=split_result.algorithm_version,
                split_hash=split_result.split_hash,
                split_lineage_hash=compute_split_lineage_hash(split_result),
                development_case_count=len(development_rows),
                validating_case_count=len(validating_rows),
            )
        else:
            assert source_dataset is not None
            index = self.dataset_case_index(str(source_dataset.get('name') or ''), str(source_dataset.get('version') or ''))
            split_result = split_dataset_rows(
                [
                    {'row_id': str(item['row_id']), 'group_id': str(item.get('group_id') or item['row_id']), 'category': str(item.get('category') or '')}
                    for item in index
                ]
            )
            split = SplitFinalization(
                algorithm_version=split_result.algorithm_version,
                split_hash=split_result.split_hash,
                split_lineage_hash=compute_split_lineage_hash(split_result),
                development_case_count=len(split_result.development),
                validating_case_count=len(split_result.validating),
            )
            requested = {
                'development': (dataset_plan.requested_development_name, split_result.development),
                'validating': (dataset_plan.requested_validating_name, split_result.validating),
            }
            for role, (dataset_name, case_ids) in requested.items():
                result = self.publish_split_dataset(
                    source_dataset=source_dataset,
                    role=role,
                    case_ids=case_ids,
                    dataset_name=dataset_name,
                    dataset_version=dataset_plan.requested_version,
                    dataset_type=dataset_plan.dataset_type,
                    connection_name=dataset_plan.connection_name,
                    operation_id=plan.operation_id,
                    action_id=f'{action.action_id}:dataset:{role}',
                    on_pending=lambda pending, stage_role=role: self._record_pending_split(ledger, stage_role, pending),
                )
                dataset = _as_mapping(result['dataset'])
                datasets[role] = DatasetFinalization(
                    role=role,
                    dataset_name=dataset_name,
                    dataset_version=dataset_plan.requested_version,
                    dataset_id=str(result['resource_id']),
                    dataset_type=dataset_plan.dataset_type,
                    case_count=len(case_ids),
                    disposition='created' if result['created'] else 'adopted',
                )
                record(
                    _ResourceDraft(
                        suffix=f'dataset:{role}',
                        resource_id=str(result['resource_id']),
                        name=dataset_name,
                        version=dataset_plan.requested_version,
                        kind='dataset',
                        disposition='created' if result['created'] else 'adopted',
                        fingerprint=str(dataset.get('content_fingerprint')) if dataset.get('content_fingerprint') else None,
                        resource_type=dataset_plan.dataset_type,
                        ownership_token=str(result.get('ownership_token')) if result.get('ownership_token') else None,
                    )
                )
        if split.development_case_count < bounds.minimum_development_cases or split.validating_case_count < bounds.minimum_validating_cases:
            raise FoundryPrerequisiteError('deterministic split violates the approved 10/5 minimums', kind='prerequisite')
        self._record_stage(
            ledger,
            'split',
            {
                'split_lineage_hash': split.split_lineage_hash,
                'development_case_count': split.development_case_count,
                'validating_case_count': split.validating_case_count,
            },
        )

        # --- stage 4: evaluator -------------------------------------------------
        evaluators: list[EvaluatorFinalization] = []
        if evaluator_plan.reuse_evaluator_id is not None:
            name, version = self._parse_evaluator_uri(evaluator_plan.reuse_evaluator_id)
            existing = self.get_evaluator_version(name, version)
            if existing is None or not existing.get('id'):
                raise FoundryPrerequisiteError('reviewed reuse evaluator version does not exist', kind='prerequisite')
            objective = EvaluatorFinalization(
                role='objective',
                evaluator_name=name,
                evaluator_version=version,
                evaluator_id=str(existing['id']),
                evaluator_kind='custom',
                provenance='reused_existing',
                normalization=evaluator_plan.objective_normalization,
                weight=evaluator_plan.objective_weight,
                disposition='adopted',
            )
            record(_ResourceDraft(suffix='evaluator:objective', resource_id=str(existing['id']), name=name, version=version, kind='evaluator', disposition='adopted', resource_type='custom'))
        else:
            completed = self._completed_stage(ledger, 'evaluator')
            existing = self.get_evaluator_version(evaluator_plan.requested_name, evaluator_plan.requested_version)
            if existing is None:
                handle = self._pending_handle(ledger, 'evaluator', job_kind='evaluator_generation')
                if handle is None:
                    handle = self.create_evaluator_generation_job(self._evaluator_generation_request(contract, datasets['development']))
                job = self.poll_generation_job(
                    handle,
                    persist_before_poll=lambda pending: self._record_pending_handle(ledger, 'evaluator', pending),
                )
                saved = job.get('saved_evaluator')
                if not isinstance(saved, Mapping) or not saved.get('name') or not saved.get('version'):
                    raise FoundryPrerequisiteError('rubric generation produced no saved evaluator version', kind='prerequisite')
                existing = self.get_evaluator_version(str(saved['name']), str(saved['version']))
                if existing is None or not existing.get('id'):
                    raise FoundryPrerequisiteError('generated evaluator version is not resolvable', kind='prerequisite')
            if str(existing.get('generation_job_id') or '') != evaluator_plan.generation_job_id:
                raise FoundryPrerequisiteError('generated evaluator lineage does not match the approved generation job', kind='prerequisite')
            try:
                validate_generated_rubric(self._generated_rubric_document(str(existing['name']), str(existing['version'])))
            except BootstrapConfigError as exc:
                raise FoundryPrerequisiteError(f'generated rubric failed structural validation: {exc}', kind='prerequisite') from None
            objective = EvaluatorFinalization(
                role='objective',
                evaluator_name=str(existing['name']),
                evaluator_version=str(existing['version']),
                evaluator_id=str(existing['id']),
                evaluator_kind='custom',
                provenance='auto_generated_unreviewed',
                generation_operation_id=evaluator_plan.generation_job_id,
                normalization=evaluator_plan.objective_normalization,
                weight=evaluator_plan.objective_weight,
                disposition='created',
            )
            record(
                _ResourceDraft(
                    suffix='evaluator:objective',
                    resource_id=str(existing['id']),
                    name=str(existing['name']),
                    version=str(existing['version']),
                    kind='evaluator',
                    disposition='created' if completed is None else 'adopted',
                    fingerprint=evaluator_plan.generation_job_id,
                    resource_type='custom',
                )
            )
        safety_bundle = self.resolve_safety_bundle(evaluator_plan.required_safety_evaluators)
        guardrails: list[EvaluatorFinalization] = []
        for entry in safety_bundle:
            safety_name = str(entry.get('safety_name') or '')
            evaluator_id = str(entry.get('id') or '')
            evaluator_name = str(entry.get('name') or safety_name)
            if canonical_safety_name(evaluator_id, evaluator_name) != safety_name:
                raise FoundryPrerequisiteError('resolved safety evaluator identity is inconsistent', kind='prerequisite')
            guardrail = EvaluatorFinalization(
                role='guardrail',
                evaluator_name=evaluator_name,
                evaluator_version=str(entry.get('version') or '1'),
                evaluator_id=evaluator_id,
                evaluator_kind='builtin',
                provenance='reused_existing',
                normalization=EvaluatorNormalization(kind='pass_fail'),
                weight=1.0,
                disposition='adopted',
                safety_name=safety_name,
            )
            guardrails.append(guardrail)
            record(
                _ResourceDraft(
                    suffix=f'evaluator:safety:{safety_name}',
                    resource_id=guardrail.evaluator_id,
                    name=guardrail.evaluator_name,
                    version=guardrail.evaluator_version,
                    kind='evaluator',
                    disposition='adopted',
                    resource_type='builtin',
                )
            )
        try:
            assert_required_safety_coverage(
                [item.safety_name or '' for item in guardrails],
                required=evaluator_plan.required_safety_evaluators,
                field='resolved safety bundle',
            )
        except BootstrapConfigError as exc:
            raise FoundryPrerequisiteError(str(exc), kind='prerequisite') from None
        evaluators.append(objective)
        evaluators.extend(guardrails)
        self._record_stage(
            ledger,
            'evaluator',
            {
                'provenance': objective.provenance,
                'evaluator_version': objective.evaluator_version,
                'safety_bundle': ','.join(sorted(item.safety_name or '' for item in guardrails)),
            },
        )

        # --- stage 5: definitions -----------------------------------------------
        # Criterion names are the evaluator names the service knows: `builtin.violence` for
        # safety, the custom evaluator name for the objective.
        criteria: dict[str, dict[str, object]] = {
            objective.evaluator_name: {
                'evaluator_name': objective.evaluator_name,
                'evaluator_version': objective.evaluator_version,
                'evaluator_id': objective.evaluator_id,
                'normalization_kind': objective.normalization.kind,
                'source_min': objective.normalization.source_min,
                'source_max': objective.normalization.source_max,
                # AI-assisted evaluators are initialized with the judge deployment; built-in
                # safety evaluators take no initialization parameters.
                'initialization_parameters': {'deployment_name': contract.activation_plan.model_deployment},
                'data_mapping': dict(_OBJECTIVE_DATA_MAPPING),
            },
        }
        for guardrail in guardrails:
            criteria[guardrail.evaluator_name] = {
                'evaluator_name': guardrail.evaluator_name,
                'evaluator_version': guardrail.evaluator_version,
                'evaluator_id': guardrail.evaluator_id,
                'normalization_kind': 'pass_fail',
                'source_min': None,
                'source_max': None,
            }
        definitions: list[DefinitionFinalization] = []
        definition_ids: dict[str, str] = {}
        requested_definitions = {
            'development': contract.definition_plan.requested_development_name,
            'validating': contract.definition_plan.requested_validating_name,
        }
        for role, definition_name in requested_definitions.items():
            result = self.create_or_adopt_onboarding_definition(role=role, definition_name=definition_name, dataset=datasets[role].model_dump(mode='json'), criteria=criteria)
            resource_id = str(result['resource_id'])
            definition_ids[role] = resource_id
            definitions.append(
                DefinitionFinalization(
                    role=role,
                    definition_name=definition_name,
                    definition_id=resource_id,
                    disposition='created' if result['created'] else 'adopted',
                )
            )
            record(
                _ResourceDraft(
                    suffix=f'definition:{role}',
                    resource_id=resource_id,
                    name=definition_name,
                    version=role,
                    kind='evaluation_definition',
                    disposition='created' if result['created'] else 'adopted',
                )
            )
        self._record_stage(ledger, 'definitions', dict(definition_ids))

        # --- stage 6: activation -------------------------------------------------
        run_ids: dict[str, str] = {}
        measurements: list[ActivationCaseFinalization] = []
        package = self._agent_packages.get(contract.repo_agent_id)
        if package is None:
            raise FoundryPrerequisiteError(
                'activation requires the reviewed repository source packaged as an owned draft',
                kind='prerequisite',
            )
        draft = self.create_activation_draft(
            contract=contract,
            package=package,
            operation_id=plan.operation_id,
            action_id=f'{action.action_id}:agent-draft',
            on_pending=lambda pending: self._record_pending_draft(ledger, pending),
        )
        record(
            _ResourceDraft(
                suffix='agent-draft',
                resource_id=f'{contract.activation_plan.draft_agent_name}:{contract.activation_plan.draft_agent_version}',
                name=contract.activation_plan.draft_agent_name,
                version=contract.activation_plan.draft_agent_version,
                kind='agent_draft',
                disposition='created',
                fingerprint=package.zip_sha256,
            )
        )
        try:
            for role in _DEFINITION_ROLES:
                dataset = datasets[role]
                live_dataset = self.get_dataset(dataset.dataset_name, dataset.dataset_version)
                if live_dataset is None:
                    raise FoundryPrerequisiteError('activation dataset version is not resolvable', kind='prerequisite')
                # Evaluate the immutable split dataset against the owned draft agent.
                data_source = self._target_completion_data_source(
                    dataset_file_id=str(live_dataset.get('id') or dataset.dataset_id),
                    draft_agent_name=contract.activation_plan.draft_agent_name,
                    draft_agent_version=contract.activation_plan.draft_agent_version,
                )
                run_id = self._submit_eval_run(
                    definition_id=definition_ids[role],
                    data_source=data_source,
                    run_name=self._operation_run_name(
                        f'{role}-activation',
                        plan.operation_id,
                    ),
                )
                run_ids[role] = run_id
                record(_ResourceDraft(suffix=f'activation-run:{role}', resource_id=run_id, name=definition_ids[role], version=role, kind='activation_run', disposition='created'))
                measurements.extend(self.activation_measurements(run_id=run_id, definition_id=definition_ids[role], phase=role, criteria=criteria))
            self._assert_activation_gates(bounds, measurements)
        finally:
            # The owned draft is always cleaned up, whether or not the gates passed. Only a
            # draft this operation created is ever deleted.
            self.cleanup_activation_draft(
                draft_agent_name=contract.activation_plan.draft_agent_name,
                draft_agent_version=contract.activation_plan.draft_agent_version,
            )
        self._record_stage(ledger, 'activation', {'development_run_id': run_ids['development'], 'validating_run_id': run_ids['validating'], 'draft_code_digest': str(draft['code_digest'])})
        self._record_stage(ledger, 'cleanup', {'completed': True})

        activation = ActivationFinalization(
            status='succeeded',
            development_run_id=run_ids['development'],
            validating_run_id=run_ids['validating'],
            draft_agent_name=contract.activation_plan.draft_agent_name,
            draft_agent_version=contract.activation_plan.draft_agent_version,
            cases=tuple(measurements),
            cleanup_completed=True,
            package_tree_sha256=package.tree_sha256,
            package_zip_sha256=package.zip_sha256,
            draft_code_digest=str(draft['code_digest']) if str(draft['code_digest']) else None,
        )
        objective_reference = ResolvedEvaluator(
            reference=EvaluatorReference(evaluator_id=objective.evaluator_id, provenance=objective.provenance),
            normalization=objective.normalization,
            weight=objective.weight,
        )
        bundle_objective_hash = ResolvedWeightedObjective.create([objective_reference]).objective_hash
        try:
            finalization = EvaluationFinalization.create(
                repo_agent_id=contract.repo_agent_id,
                contract_hash=contract.contract_hash,
                reuse_decision=reuse_decision,
                dataset_strategy=dataset_strategy,
                generated_sample_count=generated_sample_count,
                generation_context_fingerprint=dataset_plan.source_fingerprint,
                datasets=tuple(datasets[role] for role in _DEFINITION_ROLES),
                split=split,
                evaluators=tuple(evaluators),
                definitions=tuple(definitions),
                activation=activation,
                bundle_objective_hash=bundle_objective_hash,
            )
            finalization.verify_against_contract(contract)
        except BootstrapConfigError as exc:
            raise FoundryPrerequisiteError(f'onboarding finalization failed the approved bounds: {exc}', kind='prerequisite') from None
        except Exception as exc:
            raise FoundryPrerequisiteError(f'onboarding finalization is invalid: {_short_reason(exc)}', kind='prerequisite') from None
        ledger['finalization'] = finalization.model_dump(mode='json')
        # Raw rows exist only for the duration of the run.
        self.clear_dataset_cache()
        return {'finalization': finalization, 'resources': tuple(drafts), 'stages': dict(ledger.get('stages') or {})}

    def _dataset_generation_request(self, contract: EvaluationOnboardingRequest) -> Mapping[str, object]:
        assert contract.dataset_plan is not None and contract.telemetry_probe is not None
        plan = contract.dataset_plan
        if plan.generation_kind == 'dataset_trace':
            sources = [{'type': 'traces', 'agent_name': plan.agent_name}]
            options = {'type': 'traces', 'max_samples': contract.bounds.target_sample_count}
        else:
            sources = [{'type': 'agent', 'agent_name': plan.agent_name, 'agent_version': plan.agent_version}]
            options = {'type': 'simple_qna', 'max_samples': contract.bounds.target_sample_count, 'model_options': {'model': plan.generation_model_deployment}}
        return {
            'operation_id': plan.generation_job_id,
            'name': f'{contract.repo_agent_id}-dataset-generation',
            'scenario': 'evaluation',
            'sources': sources,
            'options': options,
        }

    def _evaluator_generation_request(self, contract: EvaluationOnboardingRequest, development_dataset: DatasetFinalization) -> Mapping[str, object]:
        assert contract.dataset_plan is not None and contract.evaluator_plan is not None
        plan = contract.evaluator_plan
        dataset_plan = contract.dataset_plan
        # Traces are never the only rubric source: the agent and the prepared development
        # dataset are always supplied as companion sources.
        return {
            'operation_id': plan.generation_job_id,
            'name': f'{contract.repo_agent_id}-rubric-generation',
            'sources': [
                {'type': 'agent', 'agent_name': dataset_plan.agent_name, 'agent_version': dataset_plan.agent_version},
                {'type': 'dataset', 'name': development_dataset.dataset_name, 'version': development_dataset.dataset_version},
            ],
            'model': dataset_plan.generation_model_deployment,
            'evaluator_name': plan.requested_name,
        }

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
        safety_phases: dict[str, set[str]] = defaultdict(set)
        for case in cases:
            safety_name = canonical_safety_name(case.evaluator_id)
            if safety_name is not None:
                safety_phases[safety_name].add(case.phase)
        covered = {name for name, phases in safety_phases.items() if phases == set(_DEFINITION_ROLES)}
        try:
            assert_required_safety_coverage(sorted(covered), field='activation_run safety cases')
        except BootstrapConfigError as exc:
            raise FoundryPrerequisiteError(
                f'activation_run cases must include both-phase safety results: {exc}', kind='prerequisite'
            ) from None
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
            'onboarding': {action_id: _plain(ledger) for action_id, ledger in sorted(self._onboarding.items())},
        }
        state['state_hash'] = self._state_hash(state)
        self._validate_state_document_bounds(state)
        return json.loads(_canonical_json(state))

    def export_provider_state(self, receipt: BootstrapReceipt) -> Mapping[str, object]:
        resources = self._current_resource_records(receipt)
        state = self._provider_state_from_receipt(receipt, resources)
        self._provider_state = state
        return state

    def onboarding_finalizations(self) -> Mapping[str, Mapping[str, object]]:
        """Return the receipt-derived onboarding finalizations recorded by this apply."""

        return {
            action_id: dict(ledger['finalization'])
            for action_id, ledger in self._onboarding.items()
            if isinstance(ledger.get('finalization'), Mapping)
        }

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
            records.append(_ResourceRecord(action_id=str(item.get('action_id') or ''), resource_id=str(item.get('id') or ''), name=str(item.get('name') or ''), version=str(item.get('version') or ''), kind=str(item.get('kind') or ''), disposition=str(item.get('disposition') or ''), fingerprint=str(item.get('fingerprint')) if item.get('fingerprint') is not None else None, rollback_order=int(item['rollback_order']) if isinstance(item.get('rollback_order'), int) else None, resource_type=str(item.get('resource_type')) if item.get('resource_type') is not None else None, ownership_token=str(item.get('ownership_token')) if item.get('ownership_token') is not None else None, ownership_tag=str(item.get('ownership_tag')) if item.get('ownership_tag') is not None else None))
        self._validate_provider_state_bounds(records)
        return tuple(records)

    def _current_resource_records(self, receipt: BootstrapReceipt) -> tuple[_ResourceRecord, ...]:
        if self._provider_state is None:
            raise FoundryPrerequisiteError('provider state unavailable for receipt export', kind='prerequisite')
        self._validate_provider_state_binding(receipt, self._provider_state)
        return self._resource_records_from_state(self._provider_state)

    def restore_checkpoint(self, mapping: Mapping[str, object]) -> None:
        """Restore an in-flight onboarding ledger recorded before any receipt exists.

        Checkpoints are written while a phase is still `applying`, so they cannot be receipt
        bound. Only the stage ledger is accepted, and it is re-validated for shape and size.
        """

        state = json.loads(_canonical_json(mapping))
        if state.get('schema_version') != _PROVIDER_STATE_SCHEMA_VERSION:
            raise FoundryPrerequisiteError('checkpoint schema_version mismatch', kind='prerequisite')
        onboarding = state.get('onboarding')
        if not isinstance(onboarding, Mapping):
            raise FoundryPrerequisiteError('checkpoint onboarding ledger is invalid', kind='prerequisite')
        self._validate_state_document_bounds(state)
        restored: dict[str, dict[str, object]] = {}
        for action_id, ledger in onboarding.items():
            if not isinstance(ledger, Mapping):
                raise FoundryPrerequisiteError('checkpoint onboarding ledger entry is invalid', kind='prerequisite')
            stages = ledger.get('stages')
            restored[str(action_id)] = {
                'stages': dict(stages) if isinstance(stages, Mapping) else {},
                'finalization': ledger.get('finalization'),
            }
        self._onboarding = restored
        self._restore_published_splits()
        self._restore_created_drafts()

    def _restore_published_splits(self) -> None:
        """Rebuild the uploaded-split fingerprints recorded before a crash."""

        for ledger in self._onboarding.values():
            stages = ledger.get('stages')
            entry = stages.get('split') if isinstance(stages, Mapping) else None
            pending = entry.get('pending_splits') if isinstance(entry, Mapping) else None
            if not isinstance(pending, Mapping):
                continue
            for record in pending.values():
                if not isinstance(record, Mapping):
                    continue
                name = str(record.get('dataset_name') or '')
                version = str(record.get('dataset_version') or '')
                fingerprint = str(record.get('split_fingerprint') or '')
                if name and version and fingerprint:
                    self._published_splits[(name, version)] = fingerprint

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
        onboarding = state.get('onboarding')
        if onboarding is not None and not isinstance(onboarding, Mapping):
            raise FoundryPrerequisiteError('provider state onboarding ledger is invalid', kind='prerequisite')
        self._onboarding = {str(key): dict(value) for key, value in (onboarding or {}).items() if isinstance(value, Mapping)}
        self._restore_published_splits()
        self._restore_created_drafts()
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
        if resource.kind == 'agent_draft':
            version = self._get_agent_version(resource.name, resource.version)
            if version is None:
                return None
            return {'id': resource.resource_id, 'name': resource.name, 'version': resource.version}
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
                self._openai_observer_client().evals.delete(eval_id=resource.resource_id)
                return True
            if resource.kind == 'activation_run':
                self._openai_observer_client().evals.runs.delete(run_id=resource.resource_id, eval_id=resource.name)
                return True
            if resource.kind == 'agent_draft':
                result = self.cleanup_activation_draft(draft_agent_name=resource.name, draft_agent_version=resource.version)
                return bool(result.get('completed'))
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
                if action.kind == 'evaluation_onboarding':
                    contract = self._onboarding_request_from_action(action)

                    def _record_onboarding(draft: _ResourceDraft, action_id: str = action.action_id) -> None:
                        record_action_id = f'{action_id}:{draft.suffix}'
                        if draft.disposition == 'created':
                            created.append(record_action_id)
                            created_resource_ids.append(draft.resource_id)
                            rollback_order = len(created_resource_ids)
                        else:
                            adopted.append(record_action_id)
                            rollback_order = None
                        resource_records.append(
                            _ResourceRecord(
                                action_id=record_action_id,
                                resource_id=draft.resource_id,
                                name=draft.name,
                                version=draft.version,
                                kind=draft.kind,
                                disposition=draft.disposition,
                                fingerprint=draft.fingerprint,
                                rollback_order=rollback_order,
                                resource_type=draft.resource_type,
                                ownership_token=draft.ownership_token,
                                ownership_tag=draft.ownership_tag,
                            )
                        )
                        if draft.kind == 'dataset':
                            digest = draft.fingerprint or hashlib.sha256(draft.resource_id.encode('utf-8')).hexdigest()
                            before_fingerprints.append(FingerprintRecord(label=f'{record_action_id}:before', sha256=digest))
                            after_fingerprints.append(FingerprintRecord(label=f'{record_action_id}:after', sha256=digest))

                    self.run_evaluation_onboarding(plan=plan, action=action, contract=contract, on_resource=_record_onboarding)
                    changed.append(action.action_id)
                elif action.kind == 'dataset':
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
                    # Deprecated pre-v3 surface: the caller owned the draft lifecycle there.
                    self.cleanup_activation_draft(draft_agent_name=cleanup_request.draft_agent_name, draft_agent_version=cleanup_request.draft_agent_version, require_operation_created=False)
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
            if resource.kind == 'agent_draft':
                continue  # the owned draft is intentionally ephemeral: cleanup removes it
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
                if resource.kind == 'agent_draft' and self._get_agent_version(resource.name, resource.version) is None:
                    continue
                live = self._get_live_resource(resource)
                if live is not None:
                    return False
        return True


__all__ = [name for name in globals() if name.startswith('Foundry') or name in {'rollback_failure_details', 'FoundryAdapter'}]
