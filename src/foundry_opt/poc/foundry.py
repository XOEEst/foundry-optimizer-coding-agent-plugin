from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from foundry_opt._tls import system_ssl_context
from foundry_opt.poc.auth import AuthError


FOUNDRY_SCOPE = "https://ai.azure.com/.default"
API_VERSION = "v1"
DRAFT_FEATURE = "DraftAgents=V1Preview"
USER_AGENT = "foundry-opt-poc/0.1"
OWNERSHIP_METADATA_KEY = "foundry_opt_run_id"
SOURCE_ZIP_METADATA_KEY = "foundry_opt_source_zip_sha256"
ROUTE_FINGERPRINT_METADATA_KEY = "foundry_opt_route_sha256"
RELEASE_OPERATION_METADATA_KEY = "foundry_opt_release_operation"

_REGULAR_VERSION_PATTERN = re.compile(r"^[1-9][0-9]*$")


JsonScalar = str | int | float | bool | None
JsonValue = JsonScalar | tuple["JsonValue", ...] | dict[str, "JsonValue"]


@dataclass(frozen=True, slots=True)
class RedactedRequest:
    method: str
    url: str
    headers: tuple[tuple[str, str], ...]
    body: str | None


class FoundryError(RuntimeError):
    pass


class ServiceError(FoundryError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        request: RedactedRequest | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.request = request

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(status_code={self.status_code!r}, "
            f"request={self.request!r})"
        )


class ContractError(FoundryError):
    def __init__(
        self,
        message: str,
        *,
        request: RedactedRequest | None = None,
    ) -> None:
        super().__init__(message)
        self.request = request

    def __repr__(self) -> str:
        return f"{type(self).__name__}(request={self.request!r})"


class DeadlineError(TimeoutError, FoundryError):
    pass


class DraftUnavailableError(FoundryError):
    def __init__(self, message: str, *, owned_version: DraftReference) -> None:
        super().__init__(message)
        self.owned_version = owned_version
        self.draft = owned_version

    def __repr__(self) -> str:
        return f"{type(self).__name__}(owned_version={self.owned_version!r})"


class RouteDriftError(FoundryError):
    def __init__(
        self,
        message: str,
        *,
        expected: RouteFingerprint,
        actual: RouteFingerprint,
    ) -> None:
        super().__init__(message)
        self.expected = expected
        self.actual = actual


class CleanupError(FoundryError):
    def __init__(self, message: str, *, reference: DraftReference) -> None:
        super().__init__(message)
        self.reference = reference

    def __repr__(self) -> str:
        return f"{type(self).__name__}(reference={self.reference!r})"


class RouteModeError(FoundryError):
    def __init__(self, message: str, *, route: RouteFingerprint) -> None:
        super().__init__(message)
        self.route = route


@dataclass(frozen=True, slots=True)
class HostedDefinition:
    kind: str = "hosted"
    payload: Mapping[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind != "hosted":
            raise ValueError("HostedDefinition.kind must be 'hosted'")
        if not isinstance(self.payload, Mapping):
            raise TypeError("HostedDefinition.payload must be a mapping")
        plain = _plain_json_object(self.payload)
        if "kind" in plain and plain["kind"] != "hosted":
            raise ValueError("HostedDefinition payload kind must remain 'hosted'")
        object.__setattr__(self, "payload", plain)

    def as_payload(self) -> dict[str, JsonValue]:
        payload = dict(self.payload)
        payload["kind"] = "hosted"
        return payload

    @classmethod
    def coerce(cls, value: HostedDefinition | Mapping[str, object]) -> HostedDefinition:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("hosted definition must be a mapping")
        payload = dict(value)
        kind = payload.pop("kind", None)
        if kind != "hosted":
            raise ValueError("hosted definition kind must be 'hosted'")
        return cls(payload=payload)


@dataclass(frozen=True, slots=True)
class RouteFingerprint:
    agent_name: str
    latest_version: str | None
    selector: JsonValue | None
    endpoint_configuration: JsonValue | None
    sha256: str

    def __post_init__(self) -> None:
        _validate_agent_name(self.agent_name)
        if self.latest_version is not None:
            _validate_nonempty(self.latest_version, "latest_version")
        _validate_sha256(self.sha256, "sha256")

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"agent_name={self.agent_name!r}, "
            f"latest_version={self.latest_version!r}, "
            f"sha256={self.sha256!r})"
        )


@dataclass(frozen=True, slots=True)
class DraftReference:
    agent_name: str
    version: str
    ownership_token: str
    code_sha256: str
    route: RouteFingerprint
    definition: HostedDefinition
    service_id: str | None = None
    status: str | None = None

    def __post_init__(self) -> None:
        _validate_agent_name(self.agent_name)
        _validate_nonempty(self.version, "version")
        _validate_nonempty(self.ownership_token, "ownership_token")
        _validate_sha256(self.code_sha256, "code_sha256")
        if self.agent_name != self.route.agent_name:
            raise ValueError("route fingerprint agent_name must match draft agent_name")

    @property
    def is_draft(self) -> bool:
        return self.version.startswith("draft-")

    @property
    def route_sha256(self) -> str:
        return self.route.sha256

    @property
    def ownership_token_sha256(self) -> str:
        return hashlib.sha256(self.ownership_token.encode("utf-8")).hexdigest()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"agent_name={self.agent_name!r}, "
            f"version={self.version!r}, "
            f"ownership_token_sha256={self.ownership_token_sha256!r}, "
            f"code_sha256={self.code_sha256!r})"
        )


@dataclass(frozen=True, slots=True)
class RegularVersionReference:
    agent_name: str
    version: str
    operation_id: str
    code_sha256: str
    metadata: Mapping[str, str]
    service_id: str | None = None
    status: str | None = None
    reconciled: bool = False

    def __post_init__(self) -> None:
        _validate_agent_name(self.agent_name)
        if _REGULAR_VERSION_PATTERN.fullmatch(self.version) is None:
            raise ValueError("regular version must be a positive numeric identifier")
        _validate_ownership_token(self.operation_id)
        _validate_sha256(self.code_sha256, "code_sha256")
        metadata = _metadata_string_object(self.metadata)
        if metadata.get(OWNERSHIP_METADATA_KEY) != self.operation_id:
            raise ValueError("regular version metadata must prove operation ownership")
        if metadata.get(RELEASE_OPERATION_METADATA_KEY) != self.operation_id:
            raise ValueError("regular version metadata must contain its release operation")
        if metadata.get(SOURCE_ZIP_METADATA_KEY) != self.code_sha256:
            raise ValueError("regular version metadata must contain its source ZIP hash")
        object.__setattr__(self, "metadata", metadata)

    @property
    def is_regular(self) -> bool:
        return True

    @property
    def operation_id_sha256(self) -> str:
        return hashlib.sha256(self.operation_id.encode("utf-8")).hexdigest()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"agent_name={self.agent_name!r}, "
            f"version={self.version!r}, "
            f"operation_id_sha256={self.operation_id_sha256!r}, "
            f"code_sha256={self.code_sha256!r})"
        )


@dataclass(frozen=True, slots=True)
class EvaluationReference:
    evaluation_id: str
    run_id: str
    dataset_id: str
    agent_name: str
    agent_version: str
    evaluator_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _validate_nonempty(self.evaluation_id, "evaluation_id")
        _validate_nonempty(self.run_id, "run_id")
        _validate_nonempty(self.dataset_id, "dataset_id")
        _validate_agent_name(self.agent_name)
        _validate_nonempty(self.agent_version, "agent_version")
        _validate_unique_nonempty(self.evaluator_ids, "evaluator_ids")


@dataclass(frozen=True, slots=True)
class Metric:
    name: str
    score: float | None
    passed: bool
    focused_cases: int
    passed_cases: int
    failed_cases: int

    def __post_init__(self) -> None:
        _validate_nonempty(self.name, "name")
        if self.score is not None and not math.isfinite(self.score):
            raise ValueError("score must be finite")
        for field_name in ("focused_cases", "passed_cases", "failed_cases"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a nonnegative integer")
        if self.passed_cases + self.failed_cases != self.focused_cases:
            raise ValueError("passed_cases plus failed_cases must equal focused_cases")


@dataclass(frozen=True, slots=True)
class EvaluationContract:
    evaluation_id: str
    dataset_id: str
    evaluator_ids: tuple[str, ...]
    run_name: str | None = None
    poll_interval_seconds: float = 5.0

    def __post_init__(self) -> None:
        _validate_nonempty(self.evaluation_id, "evaluation_id")
        _validate_nonempty(self.dataset_id, "dataset_id")
        _validate_unique_nonempty(self.evaluator_ids, "evaluator_ids")
        if self.run_name is not None:
            _validate_nonempty(self.run_name, "run_name")
        if (
            not isinstance(self.poll_interval_seconds, (int, float))
            or isinstance(self.poll_interval_seconds, bool)
            or self.poll_interval_seconds < 0
            or not math.isfinite(float(self.poll_interval_seconds))
        ):
            raise ValueError("poll_interval_seconds must be finite and nonnegative")


@dataclass(frozen=True, slots=True)
class EvaluationEvidence:
    reference: EvaluationReference
    metrics: tuple[Metric, ...]
    total_cases: int
    passed_cases: int
    failed_cases: int
    report_url: str

    def __post_init__(self) -> None:
        _validate_report_url(self.report_url)
        _validate_unique_nonempty(tuple(metric.name for metric in self.metrics), "metric names")
        for field_name in ("total_cases", "passed_cases", "failed_cases"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{field_name} must be a nonnegative integer")
        if self.passed_cases + self.failed_cases > self.total_cases:
            raise ValueError("passed_cases plus failed_cases cannot exceed total_cases")

    @property
    def focused_case_count(self) -> int:
        return sum(metric.focused_cases for metric in self.metrics)


@dataclass(frozen=True, slots=True)
class _CriterionBinding:
    contract_id: str
    metric_name: str
    aliases: tuple[str, ...]


class TokenProvider(Protocol):
    def get_token(self, *scopes: str, **kwargs: object) -> object: ...


class EvaluationBackend(Protocol):
    def run(
        self,
        draft: DraftReference,
        contract: EvaluationContract,
        *,
        deadline_monotonic: float,
        monotonic: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> EvaluationEvidence: ...


class AzureProjectsEvaluationBackend:
    __slots__ = ("_openai_client", "_openai_client_factory", "_project_endpoint", "_credential")

    def __init__(
        self,
        *,
        openai_client: object | None = None,
        openai_client_factory: Callable[[], object] | None = None,
        project_endpoint: str | None = None,
        credential: object | None = None,
    ) -> None:
        self._openai_client = openai_client
        self._openai_client_factory = openai_client_factory
        self._project_endpoint = project_endpoint
        self._credential = credential

    def run(
        self,
        draft: DraftReference,
        contract: EvaluationContract,
        *,
        deadline_monotonic: float,
        monotonic: Callable[[], float],
        sleep: Callable[[float], None],
    ) -> EvaluationEvidence:
        if not draft.is_draft:
            raise DraftUnavailableError(
                "regular numeric versions must not be evaluated as drafts",
                owned_version=draft,
            )
        client = self._resolve_openai_client()
        evals = getattr(client, "evals", None)
        if evals is None:
            raise ContractError("OpenAI evals client is unavailable")
        definition = evals.retrieve(contract.evaluation_id)
        criterion_aliases = _validate_evaluator_contract(definition, contract)
        run_name = contract.run_name or f"{draft.agent_name}-{draft.version}"
        created = evals.runs.create(
            contract.evaluation_id,
            name=run_name,
            metadata={
                "foundry_opt_agent_name": draft.agent_name,
                "foundry_opt_agent_version": draft.version,
                "foundry_opt_dataset_id": contract.dataset_id,
            },
            data_source={
                "type": "azure_ai_target_completions",
                "source": {"type": "file_id", "id": contract.dataset_id},
                "input_messages": {
                    "type": "template",
                    "template": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": {"type": "input_text", "text": "{{item.query}}"},
                        }
                    ],
                },
                "target": {
                    "type": "azure_ai_agent",
                    "name": draft.agent_name,
                    "version": draft.version,
                },
            },
        )
        run_id = _required_text(created, "id", subject="run.id")
        while True:
            current = evals.runs.retrieve(run_id, eval_id=contract.evaluation_id)
            status = _required_text(current, "status", subject="run.status").lower()
            if status == "completed":
                return _normalize_evidence(
                    current,
                    draft=draft,
                    contract=contract,
                    criterion_aliases=criterion_aliases,
                )
            if status in {"failed", "canceled", "cancelled"}:
                raise ServiceError(
                    f"evaluation run ended with terminal status {status!r}"
                )
            remaining = deadline_monotonic - monotonic()
            if remaining <= 0:
                raise DeadlineError("evaluation polling deadline exhausted")
            sleep(min(contract.poll_interval_seconds, remaining))

    run_evaluation = run

    def _resolve_openai_client(self) -> object:
        if self._openai_client is not None:
            return self._openai_client
        if self._openai_client_factory is not None:
            self._openai_client = self._openai_client_factory()
            return self._openai_client
        if self._project_endpoint is None or self._credential is None:
            raise ContractError("AzureProjectsEvaluationBackend is not configured")
        from azure.ai.projects import AIProjectClient

        project_client = AIProjectClient(self._project_endpoint, self._credential)
        self._openai_client = project_client.get_openai_client()
        return self._openai_client


class FoundryPocClient:
    __slots__ = (
        "_download_max_bytes",
        "_endpoint",
        "_evaluation_backend",
        "_http",
        "_json_max_bytes",
        "_monotonic",
        "_owns_http_client",
        "_sleep",
        "_timeout",
        "_token_provider",
        "_token_scope",
        "_ownership_token_factory",
    )

    def __init__(
        self,
        project_endpoint: str,
        token_provider: TokenProvider,
        *,
        evaluation_backend: EvaluationBackend | None = None,
        http_client: httpx.Client | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        json_max_bytes: int = 64 * 1024,
        download_max_bytes: int = 32 * 1024 * 1024,
        ownership_token_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        token_scope: str = FOUNDRY_SCOPE,
    ) -> None:
        if http_client is not None and transport is not None:
            raise ValueError("provide either http_client or transport, not both")
        self._endpoint = _validate_project_endpoint(project_endpoint)
        self._token_provider = token_provider
        self._evaluation_backend = evaluation_backend
        self._owns_http_client = http_client is None
        self._http = http_client or httpx.Client(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            timeout=timeout,
            verify=system_ssl_context(),
        )
        self._timeout = float(timeout)
        self._monotonic = monotonic
        self._sleep = sleep
        self._json_max_bytes = _positive_int(json_max_bytes, "json_max_bytes")
        self._download_max_bytes = _positive_int(download_max_bytes, "download_max_bytes")
        self._ownership_token_factory = ownership_token_factory
        self._token_scope = token_scope

    def __repr__(self) -> str:
        return f"{type(self).__name__}(project_endpoint={self._endpoint!r}, token_provider=<redacted>)"

    def close(self) -> None:
        if self._owns_http_client:
            self._http.close()

    def route_fingerprint(
        self,
        agent_name: str,
        *,
        deadline_monotonic: float,
    ) -> RouteFingerprint:
        _validate_agent_name(agent_name)
        response = self._request(
            "GET",
            f"/agents/{_safe_segment(agent_name, 'agent_name')}",
            params={"api-version": API_VERSION},
            deadline_monotonic=deadline_monotonic,
        )
        payload = self._json_object(response)
        latest_version: str | None = None
        versions = payload.get("versions")
        if isinstance(versions, Mapping):
            latest = versions.get("latest")
            if isinstance(latest, Mapping):
                raw_latest = latest.get("version")
                if isinstance(raw_latest, str) and raw_latest:
                    latest_version = raw_latest
        endpoint = payload.get("agent_endpoint", payload.get("agentEndpoint"))
        endpoint_json = _plain_json_value(endpoint) if endpoint is not None else None
        selector: JsonValue | None = None
        if isinstance(endpoint_json, dict):
            selector = endpoint_json.get("version_selector", endpoint_json.get("versionSelector"))
        route_payload: dict[str, JsonValue] = {
            "agent_endpoint": endpoint_json,
            "state": _plain_json_value(payload.get("state")),
        }
        if selector is None:
            route_payload["latest_version"] = latest_version
        digest = hashlib.sha256(_canonical_json_bytes(route_payload)).hexdigest()
        return RouteFingerprint(
            agent_name=agent_name,
            latest_version=latest_version,
            selector=selector,
            endpoint_configuration=endpoint_json,
            sha256=digest,
        )

    fingerprint_route = route_fingerprint

    def require_service_managed_latest(
        self,
        agent_name: str,
        *,
        deadline_monotonic: float,
    ) -> RouteFingerprint:
        route = self.route_fingerprint(
            agent_name,
            deadline_monotonic=deadline_monotonic,
        )
        if route.selector is not None:
            raise RouteModeError(
                "Foundry agent has an explicit version selector; publishing a "
                "new regular version would not reliably make it live",
                route=route,
            )
        return route

    def assert_route_unchanged(
        self,
        expected: RouteFingerprint,
        actual: RouteFingerprint | None = None,
        *,
        deadline_monotonic: float | None = None,
    ) -> RouteFingerprint:
        if actual is None:
            if deadline_monotonic is None:
                raise ValueError("deadline_monotonic is required when actual is not supplied")
            current = self.route_fingerprint(
                expected.agent_name,
                deadline_monotonic=deadline_monotonic,
            )
        else:
            current = actual
        if current.agent_name != expected.agent_name or current.sha256 != expected.sha256:
            raise RouteDriftError(
                "Foundry route changed while the POC was operating on a draft",
                expected=expected,
                actual=current,
            )
        return current

    def create_source_code_draft(
        self,
        agent_name: str,
        hosted_definition: HostedDefinition | Mapping[str, object],
        code_zip: bytes | bytearray | memoryview | str | Path,
        *,
        deadline_monotonic: float,
        ownership_token: str | None = None,
    ) -> DraftReference:
        definition = HostedDefinition.coerce(hosted_definition)
        route = self.route_fingerprint(agent_name, deadline_monotonic=deadline_monotonic)
        archive_bytes = _read_code_bytes(code_zip)
        code_sha256 = hashlib.sha256(archive_bytes).hexdigest()
        owner = _validate_ownership_token(
            ownership_token or self._ownership_token_factory()
        )
        metadata_bytes = _canonical_json_bytes(
            {
                "draft": True,
                "definition": definition.as_payload(),
                "metadata": {
                    OWNERSHIP_METADATA_KEY: owner,
                    SOURCE_ZIP_METADATA_KEY: code_sha256,
                    ROUTE_FINGERPRINT_METADATA_KEY: route.sha256,
                },
            }
        )
        response = self._request(
            "POST",
            f"/agents/{_safe_segment(agent_name, 'agent_name')}/versions",
            params={"api-version": API_VERSION},
            headers={
                "Foundry-Features": DRAFT_FEATURE,
                "x-ms-code-zip-sha256": code_sha256,
            },
            files={
                "metadata": ("metadata.json", metadata_bytes, "application/json"),
                "code": (f"{agent_name}.zip", archive_bytes, "application/zip"),
            },
            deadline_monotonic=deadline_monotonic,
        )
        payload = self._json_object(response)
        version = _required_text(payload, "version", subject="draft.version")
        service_id = _optional_text(payload, "id")
        status = _optional_text(payload, "status")
        reference = DraftReference(
            agent_name=agent_name,
            version=version,
            ownership_token=owner,
            code_sha256=code_sha256,
            route=route,
            definition=definition,
            service_id=service_id,
            status=status,
        )
        if not version.startswith("draft-"):
            raise DraftUnavailableError(
                "Foundry returned a regular numeric version instead of a draft",
                owned_version=reference,
            )
        return reference

    create_draft = create_source_code_draft

    def create_regular_version(
        self,
        agent_name: str,
        hosted_definition: HostedDefinition | Mapping[str, object],
        code_zip: bytes | bytearray | memoryview | str | Path,
        *,
        operation_id: str,
        provenance: Mapping[str, str],
        description: str,
        deadline_monotonic: float,
    ) -> RegularVersionReference:
        definition = HostedDefinition.coerce(hosted_definition)
        owner = _validate_ownership_token(operation_id)
        archive_bytes = _read_code_bytes(code_zip)
        code_sha256 = hashlib.sha256(archive_bytes).hexdigest()
        custom_provenance = _metadata_string_object(provenance)
        reserved = {
            OWNERSHIP_METADATA_KEY,
            RELEASE_OPERATION_METADATA_KEY,
            SOURCE_ZIP_METADATA_KEY,
        }
        overlap = sorted(reserved.intersection(custom_provenance))
        if overlap:
            raise ValueError(
                "regular version provenance cannot override reserved metadata: "
                + ", ".join(overlap)
            )
        if not isinstance(description, str) or not description.strip():
            raise ValueError("description must be a nonempty string")
        if len(description) > 512:
            raise ValueError("description must not exceed 512 characters")

        self.require_service_managed_latest(
            agent_name,
            deadline_monotonic=deadline_monotonic,
        )
        existing = self.find_regular_version_by_operation(
            agent_name,
            owner,
            deadline_monotonic=deadline_monotonic,
        )
        metadata = {
            **custom_provenance,
            OWNERSHIP_METADATA_KEY: owner,
            RELEASE_OPERATION_METADATA_KEY: owner,
            SOURCE_ZIP_METADATA_KEY: code_sha256,
        }
        if existing is not None:
            self._assert_regular_version_matches(
                existing,
                code_sha256=code_sha256,
                metadata=metadata,
            )
            return replace(existing, reconciled=True)

        metadata_bytes = _canonical_json_bytes(
            {
                "description": description.strip(),
                "definition": definition.as_payload(),
                "metadata": metadata,
            }
        )
        try:
            response = self._request(
                "POST",
                f"/agents/{_safe_segment(agent_name, 'agent_name')}/versions",
                params={"api-version": API_VERSION},
                headers={
                    "Idempotency-Key": owner,
                    "x-ms-code-zip-sha256": code_sha256,
                },
                files={
                    "metadata": (
                        "metadata.json",
                        metadata_bytes,
                        "application/json",
                    ),
                    "code": (
                        f"{agent_name}.zip",
                        archive_bytes,
                        "application/zip",
                    ),
                },
                deadline_monotonic=deadline_monotonic,
            )
            payload = self._json_object(response)
            reference = _regular_reference_from_payload(
                payload,
                agent_name=agent_name,
                fallback_metadata=metadata,
            )
        except (ContractError, ServiceError):
            reconciled = self.find_regular_version_by_operation(
                agent_name,
                owner,
                deadline_monotonic=deadline_monotonic,
            )
            if reconciled is None:
                raise
            reference = replace(reconciled, reconciled=True)
        self._assert_regular_version_matches(
            reference,
            code_sha256=code_sha256,
            metadata=metadata,
        )
        return reference

    publish_regular_version = create_regular_version

    def wait_for_version_active(
        self,
        reference: DraftReference,
        *,
        deadline_monotonic: float,
        poll_interval_seconds: float = 5.0,
    ) -> DraftReference:
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be nonnegative")
        while True:
            record = self._get_version(reference, deadline_monotonic=deadline_monotonic)
            status = (record.status or "").lower()
            if status == "active":
                return record
            if status in {"failed", "deleted", "deleting", "canceled", "cancelled"}:
                raise ServiceError(
                    f"Foundry version entered terminal status {status!r}"
                )
            remaining = deadline_monotonic - self._monotonic()
            if remaining <= 0:
                raise DeadlineError(
                    f"timed out waiting for Foundry version {reference.version!r}"
                )
            self._sleep(min(float(poll_interval_seconds), remaining))

    poll_version_active = wait_for_version_active

    def wait_for_regular_version_active(
        self,
        reference: RegularVersionReference,
        *,
        deadline_monotonic: float,
        poll_interval_seconds: float = 5.0,
    ) -> RegularVersionReference:
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be nonnegative")
        while True:
            record = self.get_regular_version(
                reference.agent_name,
                reference.version,
                deadline_monotonic=deadline_monotonic,
            )
            self._assert_regular_version_matches(
                record,
                code_sha256=reference.code_sha256,
                metadata=reference.metadata,
            )
            status = (record.status or "").lower()
            if status == "active":
                return record
            if status in {"failed", "deleted", "deleting", "canceled", "cancelled"}:
                raise ServiceError(
                    f"Foundry version entered terminal status {status!r}"
                )
            remaining = deadline_monotonic - self._monotonic()
            if remaining <= 0:
                raise DeadlineError(
                    f"timed out waiting for Foundry version {reference.version!r}"
                )
            self._sleep(min(float(poll_interval_seconds), remaining))

    def download_exact_deployed_code(
        self,
        reference: DraftReference,
        *,
        deadline_monotonic: float,
    ) -> bytes:
        response = self._request(
            "GET",
            f"/agents/{_safe_segment(reference.agent_name, 'agent_name')}/code:download",
            params={
                "api-version": API_VERSION,
                "agent_version": reference.version,
            },
            headers={
                "Accept": "application/zip",
                "Foundry-Features": DRAFT_FEATURE,
            },
            deadline_monotonic=deadline_monotonic,
        )
        content = response.content
        if len(content) > self._download_max_bytes:
            raise ContractError("Foundry code download exceeded the configured limit")
        served_version = response.headers.get("x-ms-agent-version")
        if served_version is not None and served_version != reference.version:
            raise ContractError("Foundry downloaded a different version than requested")
        header_sha256 = response.headers.get("x-ms-code-zip-sha256")
        if not header_sha256:
            raise ContractError("Foundry code download omitted x-ms-code-zip-sha256")
        _validate_sha256(header_sha256, "x-ms-code-zip-sha256")
        content_sha256 = hashlib.sha256(content).hexdigest()
        if header_sha256 != content_sha256 or content_sha256 != reference.code_sha256:
            raise ContractError("Foundry code download did not match the expected SHA-256")
        return content

    download_code = download_exact_deployed_code

    def download_regular_version_code(
        self,
        reference: RegularVersionReference,
        *,
        deadline_monotonic: float,
    ) -> bytes:
        response = self._request(
            "GET",
            f"/agents/{_safe_segment(reference.agent_name, 'agent_name')}/code:download",
            params={
                "api-version": API_VERSION,
                "agent_version": reference.version,
            },
            headers={
                "Accept": "application/zip",
                "Foundry-Features": DRAFT_FEATURE,
            },
            deadline_monotonic=deadline_monotonic,
        )
        content = response.content
        if len(content) > self._download_max_bytes:
            raise ContractError("Foundry code download exceeded the configured limit")
        served_version = response.headers.get("x-ms-agent-version")
        if served_version is not None and served_version != reference.version:
            raise ContractError("Foundry downloaded a different version than requested")
        header_sha256 = response.headers.get("x-ms-code-zip-sha256")
        if not header_sha256:
            raise ContractError("Foundry code download omitted x-ms-code-zip-sha256")
        _validate_sha256(header_sha256, "x-ms-code-zip-sha256")
        content_sha256 = hashlib.sha256(content).hexdigest()
        if header_sha256 != content_sha256 or content_sha256 != reference.code_sha256:
            raise ContractError("Foundry code download did not match the expected SHA-256")
        return content

    def list_regular_versions(
        self,
        agent_name: str,
        *,
        deadline_monotonic: float,
        limit: int = 100,
    ) -> tuple[RegularVersionReference, ...]:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        safe_name = _safe_segment(agent_name, "agent_name")
        after: str | None = None
        seen_cursors: set[str] = set()
        records: list[RegularVersionReference] = []
        while True:
            params: dict[str, object] = {
                "api-version": API_VERSION,
                "include_drafts": "false",
                "limit": limit,
            }
            if after is not None:
                params["after"] = after
            response = self._request(
                "GET",
                f"/agents/{safe_name}/versions",
                params=params,
                deadline_monotonic=deadline_monotonic,
            )
            payload = self._json_object(response)
            data = payload.get("data")
            if not isinstance(data, list):
                raise ContractError("Foundry version list omitted its data array")
            for item in data:
                if not isinstance(item, Mapping):
                    raise ContractError(
                        "Foundry version list contained a non-object item"
                    )
                version = _optional_text(item, "version")
                if version is None or _REGULAR_VERSION_PATTERN.fullmatch(version) is None:
                    continue
                metadata = _metadata_object(item.get("metadata"))
                if not _has_regular_ownership_metadata(metadata):
                    continue
                records.append(
                    _regular_reference_from_payload(
                        item,
                        agent_name=agent_name,
                    )
                )
            if payload.get("has_more") is not True:
                break
            cursor = payload.get("last_id")
            if not isinstance(cursor, str) or not cursor:
                if not data:
                    raise ContractError(
                        "Foundry version list has_more without a continuation cursor"
                    )
                last = data[-1]
                if not isinstance(last, Mapping):
                    raise ContractError(
                        "Foundry version list has_more without a continuation cursor"
                    )
                cursor = _required_text(last, "id", subject="version.id")
            if cursor in seen_cursors:
                raise ContractError(
                    "Foundry version pagination repeated a continuation cursor"
                )
            seen_cursors.add(cursor)
            after = cursor
        return tuple(records)

    def find_regular_version_by_operation(
        self,
        agent_name: str,
        operation_id: str,
        *,
        deadline_monotonic: float,
    ) -> RegularVersionReference | None:
        owner = _validate_ownership_token(operation_id)
        matches = tuple(
            record
            for record in self.list_regular_versions(
                agent_name,
                deadline_monotonic=deadline_monotonic,
            )
            if record.operation_id == owner
        )
        if len(matches) > 1:
            raise ContractError(
                "multiple regular versions share the deployment operation identifier"
            )
        return None if not matches else matches[0]

    def get_regular_version(
        self,
        agent_name: str,
        version: str,
        *,
        deadline_monotonic: float,
    ) -> RegularVersionReference:
        if _REGULAR_VERSION_PATTERN.fullmatch(version) is None:
            raise ValueError("regular version must be a positive numeric identifier")
        response = self._request(
            "GET",
            f"/agents/{_safe_segment(agent_name, 'agent_name')}/versions/"
            f"{_safe_segment(version, 'agent_version')}",
            params={"api-version": API_VERSION},
            deadline_monotonic=deadline_monotonic,
        )
        return _regular_reference_from_payload(
            self._json_object(response),
            agent_name=agent_name,
        )

    def assert_regular_version_is_latest(
        self,
        reference: RegularVersionReference,
        *,
        deadline_monotonic: float,
        poll_interval_seconds: float = 5.0,
    ) -> RouteFingerprint:
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be nonnegative")
        while True:
            route = self.require_service_managed_latest(
                reference.agent_name,
                deadline_monotonic=deadline_monotonic,
            )
            if route.latest_version == reference.version:
                return route
            remaining = deadline_monotonic - self._monotonic()
            if remaining <= 0:
                raise DeadlineError(
                    "timed out waiting for the published regular version to "
                    "become Foundry latest"
                )
            self._sleep(min(float(poll_interval_seconds), remaining))

    def delete_owned_version(
        self,
        reference: DraftReference,
        *,
        deadline_monotonic: float,
    ) -> None:
        try:
            existing = self._try_get_version(
                reference,
                deadline_monotonic=deadline_monotonic,
                require_ownership_proof=True,
            )
        except ContractError as exc:
            raise CleanupError(
                "Foundry version ownership could not be proven for cleanup",
                reference=reference,
            ) from exc
        if existing is None:
            return
        if (
            existing.ownership_token != reference.ownership_token
            or existing.code_sha256 != reference.code_sha256
            or existing.route_sha256 != reference.route_sha256
        ):
            raise CleanupError(
                "Foundry version ownership could not be proven for cleanup",
                reference=reference,
            )
        path = (
            f"/agents/{_safe_segment(reference.agent_name, 'agent_name')}/versions/"
            f"{_safe_segment(reference.version, 'agent_version')}"
        )
        while True:
            try:
                self._request(
                    "DELETE",
                    path,
                    params={"api-version": API_VERSION, "force": "true"},
                    deadline_monotonic=deadline_monotonic,
                )
            except ServiceError as exc:
                if exc.status_code not in {404, 409}:
                    raise
            if self._try_get_version(reference, deadline_monotonic=deadline_monotonic) is None:
                return
            remaining = deadline_monotonic - self._monotonic()
            if remaining <= 0:
                raise CleanupError(
                    "Foundry version still existed after delete",
                    reference=reference,
                )
            self._sleep(min(2.0, remaining))

    delete_exact_owned_version = delete_owned_version

    def evaluate_draft(
        self,
        reference: DraftReference,
        contract: EvaluationContract,
        *,
        deadline_monotonic: float,
    ) -> EvaluationEvidence:
        if not reference.is_draft:
            raise DraftUnavailableError(
                "regular numeric versions must not be evaluated as drafts",
                owned_version=reference,
            )
        if self._evaluation_backend is None:
            raise ContractError("evaluation backend is not configured")
        return self._evaluation_backend.run(
            reference,
            contract,
            deadline_monotonic=deadline_monotonic,
            monotonic=self._monotonic,
            sleep=self._sleep,
        )

    run_evaluation = evaluate_draft

    def _try_get_version(
        self,
        reference: DraftReference,
        *,
        deadline_monotonic: float,
        require_ownership_proof: bool = False,
    ) -> DraftReference | None:
        try:
            return self._get_version(
                reference,
                deadline_monotonic=deadline_monotonic,
                require_ownership_proof=require_ownership_proof,
            )
        except ServiceError as exc:
            if exc.status_code == 404:
                return None
            raise

    def _get_version(
        self,
        reference: DraftReference,
        *,
        deadline_monotonic: float,
        require_ownership_proof: bool = False,
    ) -> DraftReference:
        response = self._request(
            "GET",
            f"/agents/{_safe_segment(reference.agent_name, 'agent_name')}/versions/{_safe_segment(reference.version, 'agent_version')}",
            params={"api-version": API_VERSION},
            deadline_monotonic=deadline_monotonic,
        )
        payload = self._json_object(response)
        version = _required_text(payload, "version", subject="version")
        if version != reference.version:
            raise ContractError("Foundry returned a different version than requested")
        metadata = _metadata_object(payload.get("metadata"))
        owner = _validated_metadata_ownership_token(
            metadata,
            OWNERSHIP_METADATA_KEY,
            required=require_ownership_proof,
        )
        if owner is None:
            owner = reference.ownership_token
        code_sha256 = _validated_metadata_sha256(
            metadata,
            SOURCE_ZIP_METADATA_KEY,
            required=require_ownership_proof,
        )
        if code_sha256 is None:
            code_sha256 = reference.code_sha256
        route_sha256 = _validated_metadata_sha256(
            metadata,
            ROUTE_FINGERPRINT_METADATA_KEY,
            required=require_ownership_proof,
        )
        if route_sha256 is None:
            route_sha256 = reference.route_sha256
        return DraftReference(
            agent_name=reference.agent_name,
            version=version,
            ownership_token=owner,
            code_sha256=code_sha256,
            route=replace(reference.route, sha256=route_sha256),
            definition=reference.definition,
            service_id=_optional_text(payload, "id") or reference.service_id,
            status=_optional_text(payload, "status"),
        )

    @staticmethod
    def _assert_regular_version_matches(
        reference: RegularVersionReference,
        *,
        code_sha256: str,
        metadata: Mapping[str, str],
    ) -> None:
        if reference.code_sha256 != code_sha256:
            raise ContractError(
                "regular version source ZIP hash does not match the deployment"
            )
        expected = _metadata_string_object(metadata)
        for key, value in expected.items():
            if reference.metadata.get(key) != value:
                raise ContractError(
                    f"regular version metadata does not match deployment field {key}"
                )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        headers: Mapping[str, str] | None = None,
        files: Mapping[str, object] | None = None,
        deadline_monotonic: float,
    ) -> httpx.Response:
        timeout = _remaining_seconds(self._monotonic, deadline_monotonic)
        token = _normalize_access_token(self._token_provider.get_token(self._token_scope))
        request_headers = {
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if headers is not None:
            request_headers.update(headers)
        request_headers["Authorization"] = f"Bearer {token}"
        url = f"{self._endpoint}{path}"
        preview_headers = dict(request_headers)
        preview_headers["Authorization"] = "******"
        preview_request = self._http.build_request(
            method,
            url,
            params=params,
            headers=preview_headers,
            files=files,
        )
        redacted = _redact_request(preview_request)
        try:
            response = self._http.request(
                method,
                url,
                params=params,
                headers=request_headers,
                files=files,
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise DeadlineError("Foundry request deadline exhausted") from exc
        except httpx.TransportError as exc:
            raise ServiceError("Foundry transport failed", request=redacted) from exc
        if 300 <= response.status_code < 400:
            raise ServiceError(
                "Foundry refused an HTTP redirect",
                status_code=response.status_code,
                request=redacted,
            )
        if response.status_code in {401, 403}:
            raise AuthError(
                "Foundry authentication failed",
                stage="foundry",
                status_code=response.status_code,
            )
        if not 200 <= response.status_code < 300:
            raise ServiceError(
                f"Foundry request failed with HTTP {response.status_code}",
                status_code=response.status_code,
                request=redacted,
            )
        return response

    def _json_object(self, response: httpx.Response) -> dict[str, Any]:
        if len(response.content) > self._json_max_bytes:
            raise ContractError("Foundry JSON response exceeded the configured limit")
        content_type = response.headers.get("content-type", "")
        if content_type and content_type.split(";", 1)[0].strip().lower() != "application/json":
            raise ContractError("Foundry returned a non-JSON response")
        return _strict_json_object(response.content)


FoundryClient = FoundryPocClient
Client = FoundryPocClient


def _validate_evaluator_contract(
    definition: object,
    contract: EvaluationContract,
) -> dict[str, _CriterionBinding]:
    criteria = _field(definition, "testing_criteria")
    if not isinstance(criteria, Sequence):
        raise ContractError("evaluation definition omitted testing_criteria")
    if len(criteria) != len(contract.evaluator_ids):
        raise ContractError("evaluation definition did not match the exact evaluator contract")
    remaining = list(contract.evaluator_ids)
    aliases: dict[str, _CriterionBinding] = {}
    metric_names: dict[str, str] = {}
    for item in criteria:
        matches = [candidate for candidate in remaining if _criterion_matches_contract(item, candidate)]
        if len(matches) != 1:
            raise ContractError("evaluation definition did not match the exact evaluator contract")
        contract_id = matches[0]
        remaining.remove(contract_id)
        binding = _CriterionBinding(
            contract_id=contract_id,
            metric_name=_criterion_name(item),
            aliases=_criterion_aliases(item),
        )
        metric_key = binding.metric_name.casefold()
        previous_metric = metric_names.get(metric_key)
        if previous_metric is not None and previous_metric != contract_id:
            raise ContractError("evaluation definition did not match the exact evaluator contract")
        metric_names[metric_key] = contract_id
        for alias in (contract_id, *binding.aliases):
            previous = aliases.get(alias)
            if previous is not None and previous.contract_id != contract_id:
                raise ContractError("evaluation definition did not match the exact evaluator contract")
            aliases[alias] = binding
    if remaining:
        raise ContractError("evaluation definition did not match the exact evaluator contract")
    return aliases


def _normalize_evidence(
    run: object,
    *,
    draft: DraftReference,
    contract: EvaluationContract,
    criterion_aliases: Mapping[str, _CriterionBinding],
) -> EvaluationEvidence:
    run_id = _required_text(run, "id", subject="run.id")
    evaluation_id = _required_text(run, "eval_id", subject="run.eval_id")
    if evaluation_id != contract.evaluation_id:
        raise ContractError("evaluation run did not match the exact evaluation contract")
    report_url = _sanitize_report_url(
        _required_text(run, "report_url", subject="run.report_url")
    )
    result_counts = _field(run, "result_counts")
    total_cases = _required_int(result_counts, "total", subject="result_counts.total")
    passed_cases = _required_int(result_counts, "passed", subject="result_counts.passed")
    failed_cases = _required_int(result_counts, "failed", subject="result_counts.failed")
    if _required_int(result_counts, "errored", subject="result_counts.errored") != 0:
        raise ServiceError("evaluation result_counts reported errored cases")
    criteria_results = _field(run, "per_testing_criteria_results")
    if not isinstance(criteria_results, Sequence):
        raise ContractError("evaluation run omitted per_testing_criteria_results")
    by_name: dict[str, object] = {}
    for item in criteria_results:
        binding = _resolve_contract_evaluator_id(
            item,
            criterion_aliases=criterion_aliases,
        )
        if binding.contract_id in by_name:
            raise ContractError("evaluation run contained duplicate criteria results")
        by_name[binding.contract_id] = item
    metrics: list[Metric] = []
    for evaluator_id in contract.evaluator_ids:
        binding = criterion_aliases.get(evaluator_id)
        if binding is None:
            raise ContractError("evaluation definition did not match the exact evaluator contract")
        item = by_name.get(evaluator_id)
        if item is None:
            raise ContractError("evaluation run omitted a required criteria result")
        passed = _required_int(item, "passed", subject=f"{evaluator_id}.passed")
        failed = _required_int(item, "failed", subject=f"{evaluator_id}.failed")
        focused_cases = passed + failed
        raw_score = _field(item, "score")
        score = None
        if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool):
            if not math.isfinite(float(raw_score)):
                raise ContractError("evaluation score must be finite")
            score = float(raw_score)
        elif focused_cases > 0:
            score = passed / focused_cases
        metrics.append(
            Metric(
                name=binding.metric_name,
                score=score,
                passed=failed == 0 and focused_cases > 0,
                focused_cases=focused_cases,
                passed_cases=passed,
                failed_cases=failed,
            )
        )
    reference = EvaluationReference(
        evaluation_id=evaluation_id,
        run_id=run_id,
        dataset_id=contract.dataset_id,
        agent_name=draft.agent_name,
        agent_version=draft.version,
        evaluator_ids=contract.evaluator_ids,
    )
    return EvaluationEvidence(
        reference=reference,
        metrics=tuple(metrics),
        total_cases=total_cases,
        passed_cases=passed_cases,
        failed_cases=failed_cases,
        report_url=report_url,
    )


def _sanitize_report_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ContractError("evaluation report_url must be an HTTPS URL")
    if parsed.username is not None or parsed.password is not None:
        raise ContractError("evaluation report_url must not contain credentials")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _redact_request(request: httpx.Request) -> RedactedRequest:
    body = request.read()
    headers: list[tuple[str, str]] = []
    for key, value in request.headers.items():
        if key.lower() == "authorization":
            headers.append((key, "******"))
        else:
            headers.append((key, value))
    body_marker = None
    if body:
        body_marker = (
            f"<body bytes={len(body)} "
            f"sha256={hashlib.sha256(body).hexdigest()}>"
        )
    return RedactedRequest(
        method=request.method,
        url=str(request.url),
        headers=tuple(sorted(headers)),
        body=body_marker,
    )


def _canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        _plain_json_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def _strict_json_object(value: bytes) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = item
        return result

    def reject_constant(constant: str) -> object:
        raise ValueError(constant)

    try:
        payload = json.loads(
            value.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ContractError("Foundry returned malformed JSON") from exc
    if not isinstance(payload, dict):
        raise ContractError("Foundry returned a non-object JSON document")
    return payload


def _plain_json_object(value: Mapping[str, object]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise TypeError("JSON object keys must be nonempty strings")
        result[key] = _plain_json_value(item)
    return result


def _plain_json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return _plain_json_object(value)
    if isinstance(value, (list, tuple)):
        return tuple(_plain_json_value(item) for item in value)
    raise TypeError("value is not JSON compatible")


def _metadata_object(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ContractError("Foundry version metadata must be a JSON object")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise ContractError("Foundry version metadata must be string pairs")
        result[key] = item
    return result


def _metadata_string_object(value: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError("metadata must be a mapping")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise TypeError("metadata keys must be nonempty strings")
        if not isinstance(item, str) or not item:
            raise TypeError("metadata values must be nonempty strings")
        result[key] = item
    return result


def _has_regular_ownership_metadata(metadata: Mapping[str, str]) -> bool:
    owner = metadata.get(OWNERSHIP_METADATA_KEY)
    operation = metadata.get(RELEASE_OPERATION_METADATA_KEY)
    source_sha256 = metadata.get(SOURCE_ZIP_METADATA_KEY)
    return (
        owner is not None
        and owner == operation
        and source_sha256 is not None
    )


def _regular_reference_from_payload(
    payload: Mapping[str, object],
    *,
    agent_name: str,
    fallback_metadata: Mapping[str, str] | None = None,
) -> RegularVersionReference:
    version = _required_text(payload, "version", subject="version")
    if _REGULAR_VERSION_PATTERN.fullmatch(version) is None:
        raise ContractError(
            "Foundry returned a non-numeric version for regular publication"
        )
    metadata = _metadata_object(payload.get("metadata"))
    if not metadata and fallback_metadata is not None:
        metadata = _metadata_string_object(fallback_metadata)
    if not _has_regular_ownership_metadata(metadata):
        raise ContractError(
            "Foundry regular version omitted required deployment provenance"
        )
    operation_id = _required_metadata_text(
        metadata,
        RELEASE_OPERATION_METADATA_KEY,
    )
    code_sha256 = _required_metadata_sha256(
        metadata,
        SOURCE_ZIP_METADATA_KEY,
    )
    return RegularVersionReference(
        agent_name=agent_name,
        version=version,
        operation_id=operation_id,
        code_sha256=code_sha256,
        metadata=metadata,
        service_id=_optional_text(payload, "id"),
        status=_optional_text(payload, "status"),
    )


def _required_metadata_text(metadata: Mapping[str, str], name: str) -> str:
    value = _optional_metadata_text(metadata, name)
    if value is None:
        raise ContractError(f"Foundry version omitted required metadata {name}")
    return value


def _required_metadata_sha256(metadata: Mapping[str, str], name: str) -> str:
    value = _validated_metadata_sha256(metadata, name, required=True)
    assert value is not None
    return value


def _optional_metadata_text(metadata: Mapping[str, str], name: str) -> str | None:
    value = metadata.get(name)
    if value is None:
        return None
    if not value:
        raise ContractError(f"Foundry version metadata {name} must be a nonempty string")
    return value


def _validated_metadata_sha256(
    metadata: Mapping[str, str],
    name: str,
    *,
    required: bool,
) -> str | None:
    value = _optional_metadata_text(metadata, name)
    if value is None:
        if required:
            raise ContractError(f"Foundry version omitted required metadata {name}")
        return None
    try:
        return _validate_sha256(value, name)
    except ValueError as exc:
        raise ContractError(
            f"Foundry version metadata {name} must be a lowercase SHA-256 hex digest"
        ) from exc


def _validated_metadata_ownership_token(
    metadata: Mapping[str, str],
    name: str,
    *,
    required: bool,
) -> str | None:
    value = _optional_metadata_text(metadata, name)
    if value is None:
        if required:
            raise ContractError(f"Foundry version omitted required metadata {name}")
        return None
    try:
        return _validate_ownership_token(value)
    except ValueError as exc:
        raise ContractError(
            f"Foundry version metadata {name} must be a bounded ASCII token"
        ) from exc


def _field(value: object, name: str) -> object:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _criterion_name(value: object) -> str:
    return _criterion_aliases(value)[0]


def _criterion_aliases(value: object) -> tuple[str, ...]:
    aliases: list[str] = []
    for name in ("name", "testing_criteria", "id"):
        candidate = _field(value, name)
        if isinstance(candidate, str) and candidate:
            aliases.append(candidate)
    if not aliases:
        raise ContractError("evaluation criteria omitted its identifier")
    return tuple(dict.fromkeys(aliases))


_PROJECT_EVALUATOR_RESOURCE_ID_RE = re.compile(
    r"^azureai://accounts/[^/]+/projects/[^/]+/evaluators/(?P<name>[^/]+)/versions/(?P<version>[^/]+)$"
)
_REGISTRY_EVALUATOR_RESOURCE_ID_RE = re.compile(
    r"^azureml://registries/[^/]+/evaluators/(?P<name>[^/]+)/versions/(?P<version>[^/]+)$"
)


def _criterion_resource_identity(value: object) -> tuple[str, str] | None:
    evaluator_name = _field(value, "evaluator_name")
    evaluator_version = _field(value, "evaluator_version")
    if (
        isinstance(evaluator_name, str)
        and evaluator_name
        and isinstance(evaluator_version, str)
        and evaluator_version
    ):
        return (evaluator_name, evaluator_version)
    return None


def _contract_evaluator_identity(value: str) -> tuple[str, str] | None:
    for pattern in (
        _PROJECT_EVALUATOR_RESOURCE_ID_RE,
        _REGISTRY_EVALUATOR_RESOURCE_ID_RE,
    ):
        match = pattern.fullmatch(value)
        if match is not None:
            return (match.group("name"), match.group("version"))
    return None


def _criterion_matches_contract(value: object, contract_id: str) -> bool:
    if contract_id in _criterion_aliases(value):
        return True
    contract_identity = _contract_evaluator_identity(contract_id)
    if contract_identity is None:
        return False
    return _criterion_resource_identity(value) == contract_identity


def _resolve_contract_evaluator_id(
    value: object,
    *,
    criterion_aliases: Mapping[str, _CriterionBinding],
) -> _CriterionBinding:
    for alias in _criterion_aliases(value):
        binding = criterion_aliases.get(alias)
        if binding is not None:
            return binding
    raise ContractError("evaluation run reported an unexpected criteria result")


def _required_text(value: object, name: str, *, subject: str) -> str:
    candidate = _field(value, name)
    if not isinstance(candidate, str) or not candidate:
        raise ContractError(f"{subject} must be a nonempty string")
    return candidate


def _optional_text(value: object, name: str) -> str | None:
    candidate = _field(value, name)
    if candidate is None:
        return None
    if not isinstance(candidate, str) or not candidate:
        raise ContractError(f"{name} must be a nonempty string when present")
    return candidate


def _required_int(value: object, name: str, *, subject: str) -> int:
    candidate = _field(value, name)
    if isinstance(candidate, bool) or not isinstance(candidate, int) or candidate < 0:
        raise ContractError(f"{subject} must be a nonnegative integer")
    return candidate


def _normalize_access_token(value: object) -> str:
    if isinstance(value, str):
        if value:
            return value
        raise AuthError("token provider returned an empty token", stage="foundry")
    token = getattr(value, "token", None)
    if isinstance(token, str) and token:
        return token
    raise AuthError("token provider did not return a bearer token", stage="foundry")


def _remaining_seconds(
    monotonic: Callable[[], float],
    deadline_monotonic: float,
) -> float:
    if isinstance(deadline_monotonic, bool):
        raise ValueError("deadline_monotonic must be numeric")
    remaining = float(deadline_monotonic) - monotonic()
    if not math.isfinite(remaining) or remaining <= 0:
        raise DeadlineError("deadline exhausted")
    return remaining


def _validate_project_endpoint(value: str) -> str:
    parsed = urlsplit(value.rstrip("/"))
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("project_endpoint must be an HTTPS Foundry project endpoint")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _validate_report_url(value: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("report_url must be an HTTPS URL")


def _read_code_bytes(value: bytes | bytearray | memoryview | str | Path) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    if isinstance(value, memoryview):
        return value.tobytes()
    path = Path(value)
    return path.read_bytes()


def _validate_agent_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(character in value for character in ("/", "\\", "?", "#"))
    ):
        raise ValueError("agent_name must be a safe nonempty identifier")
    return value


def _validate_nonempty(value: object, subject: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{subject} must be a nonempty string")
    return value


def _validate_unique_nonempty(values: Sequence[str], subject: str) -> tuple[str, ...]:
    normalized: list[str] = []
    for item in values:
        normalized.append(_validate_nonempty(item, subject))
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{subject} must contain unique values")
    return tuple(normalized)


def _validate_sha256(value: object, subject: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"{subject} must be a lowercase SHA-256 hex digest")
    return value


def _validate_ownership_token(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 256
        or not value.isascii()
        or any(character.isspace() or ord(character) < 0x21 for character in value)
    ):
        raise ValueError("ownership_token must be a bounded ASCII token")
    return value


def _positive_int(value: object, subject: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{subject} must be a positive integer")
    return value


def _safe_segment(value: str, subject: str) -> str:
    _validate_nonempty(value, subject)
    return quote(value, safe="-._~")
