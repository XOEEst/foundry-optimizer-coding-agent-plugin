from __future__ import annotations

from dataclasses import asdict
import json
import hashlib
import time
from urllib.parse import parse_qs

import httpx
import pytest

from foundry_opt.poc.auth import AuthError
from foundry_opt.poc.foundry import (
    API_VERSION,
    DRAFT_FEATURE,
    ContractError,
    DraftReference,
    DraftUnavailableError,
    EvaluationContract,
    FoundryPocClient,
    HostedDefinition,
    RegularVersionReference,
    RouteDriftError,
    RouteFingerprint,
    RouteModeError,
    ServiceError,
    AzureProjectsEvaluationBackend,
    CleanupError,
    DeadlineError,
)


class _TokenProvider:
    def get_token(self, *scopes: str, **kwargs: object) -> object:
        del kwargs
        assert scopes == ("https://ai.azure.com/.default",)
        return "foundry-token"


def _deadline(seconds: float) -> float:
    return time.monotonic() + seconds


def _route_payload(version: str = "7") -> dict[str, object]:
    return {
        "name": "travel-agent",
        "state": "enabled",
        "versions": {"latest": {"version": version}},
        "agent_endpoint": {
            "version_selector": {
                "version_selection_rules": [{"type": "static", "version": version}]
            }
        },
    }


def _latest_route_payload(version: str = "7") -> dict[str, object]:
    return {
        "name": "travel-agent",
        "state": "enabled",
        "versions": {"latest": {"version": version}},
        "agent_endpoint": None,
    }


def _route_fingerprint(version: str = "7", sha256: str = "a" * 64) -> RouteFingerprint:
    return RouteFingerprint(
        agent_name="travel-agent",
        latest_version=version,
        selector={"version_selection_rules": ({"type": "static", "version": version},)},
        endpoint_configuration={"version_selector": {"version_selection_rules": ({"type": "static", "version": version},)}},
        sha256=sha256,
    )


def _draft_reference(
    *,
    version: str = "draft-abc",
    code_sha256: str = "b" * 64,
    route_sha256: str = "a" * 64,
    ownership_token: str = "owned-token",
) -> DraftReference:
    return DraftReference(
        agent_name="travel-agent",
        version=version,
        ownership_token=ownership_token,
        code_sha256=code_sha256,
        route=_route_fingerprint(sha256=route_sha256),
        definition=HostedDefinition(),
        service_id=f"travel-agent:{version}",
    )


def test_create_source_code_draft_posts_preview_multipart_and_rejects_numeric_version() -> None:
    zip_bytes = b"deterministic-zip"
    expected_sha = hashlib.sha256(zip_bytes).hexdigest()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer foundry-token"
        query = parse_qs(request.url.query.decode())
        assert query == {"api-version": [API_VERSION]}
        if request.method == "GET" and request.url.path.endswith("/agents/travel-agent"):
            return httpx.Response(200, json=_route_payload())
        if request.method == "POST" and request.url.path.endswith("/versions"):
            assert request.headers["foundry-features"] == DRAFT_FEATURE
            assert request.headers["x-ms-code-zip-sha256"] == expected_sha
            body = request.read()
            assert b'name="metadata"' in body
            assert b'"draft":true' in body
            assert b'"kind":"hosted"' in body
            assert b'name="code"' in body
            assert zip_bytes in body
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"id": "travel-agent:7", "version": "7", "status": "active"},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = FoundryPocClient(
        "https://foundry.example/project",
        _TokenProvider(),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(DraftUnavailableError) as caught:
        client.create_source_code_draft(
            "travel-agent",
            HostedDefinition(),
            zip_bytes,
            deadline_monotonic=_deadline(30.0),
            ownership_token="owned-token",
        )

    assert caught.value.owned_version.version == "7"
    assert caught.value.owned_version.code_sha256 == expected_sha
    assert [request.method for request in requests] == ["GET", "POST"]


def test_create_regular_version_posts_numeric_source_version_without_route_mutation() -> None:
    zip_bytes = b"regular-version-zip"
    expected_sha = hashlib.sha256(zip_bytes).hexdigest()
    operation_id = "deploy-abc123"
    metadata = {
        "foundry_opt_release_commit": "a" * 40,
        "foundry_opt_run_id": operation_id,
        "foundry_opt_release_operation": operation_id,
        "foundry_opt_source_zip_sha256": expected_sha,
    }
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith(
            "/agents/travel-agent"
        ):
            return httpx.Response(200, json=_latest_route_payload())
        if request.method == "GET" and request.url.path.endswith("/versions"):
            return httpx.Response(
                200,
                json={"data": [], "has_more": False},
            )
        if request.method == "POST" and request.url.path.endswith("/versions"):
            assert "foundry-features" not in request.headers
            assert request.headers["idempotency-key"] == operation_id
            assert request.headers["x-ms-code-zip-sha256"] == expected_sha
            body = request.read()
            assert b'"draft"' not in body
            assert b'"kind":"hosted"' in body
            assert b'"foundry_opt_release_commit":"' + (b"a" * 40) + b'"' in body
            assert zip_bytes in body
            return httpx.Response(
                200,
                json={
                    "id": "travel-agent:15",
                    "version": "15",
                    "status": "creating",
                    "metadata": metadata,
                },
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = FoundryPocClient(
        "https://foundry.example/project",
        _TokenProvider(),
        transport=httpx.MockTransport(handler),
    )

    reference = client.create_regular_version(
        "travel-agent",
        HostedDefinition(),
        zip_bytes,
        operation_id=operation_id,
        provenance={"foundry_opt_release_commit": "a" * 40},
        description="Deploy merge a",
        deadline_monotonic=_deadline(30.0),
    )

    assert reference.version == "15"
    assert reference.code_sha256 == expected_sha
    assert [request.method for request in requests] == ["GET", "GET", "POST"]
    assert all(request.method in {"GET", "POST"} for request in requests)


def test_create_regular_version_reconciles_existing_operation() -> None:
    zip_bytes = b"regular-version-zip"
    expected_sha = hashlib.sha256(zip_bytes).hexdigest()
    operation_id = "deploy-replay"
    record = {
        "id": "travel-agent:15",
        "version": "15",
        "status": "active",
        "metadata": {
            "foundry_opt_release_commit": "a" * 40,
            "foundry_opt_run_id": operation_id,
            "foundry_opt_release_operation": operation_id,
            "foundry_opt_source_zip_sha256": expected_sha,
        },
    }
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.url.path.endswith("/agents/travel-agent"):
            return httpx.Response(200, json=_latest_route_payload("15"))
        if request.url.path.endswith("/versions"):
            return httpx.Response(
                200,
                json={"data": [record], "has_more": False},
            )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = FoundryPocClient(
        "https://foundry.example/project",
        _TokenProvider(),
        transport=httpx.MockTransport(handler),
    )

    reference = client.create_regular_version(
        "travel-agent",
        HostedDefinition(),
        zip_bytes,
        operation_id=operation_id,
        provenance={"foundry_opt_release_commit": "a" * 40},
        description="Deploy merge a",
        deadline_monotonic=_deadline(30.0),
    )

    assert reference.version == "15"
    assert methods == ["GET", "GET"]


def test_create_regular_version_reconciles_ambiguous_publish() -> None:
    zip_bytes = b"regular-version-zip"
    expected_sha = hashlib.sha256(zip_bytes).hexdigest()
    operation_id = "deploy-ambiguous"
    state = {"lists": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agents/travel-agent"):
            return httpx.Response(200, json=_latest_route_payload())
        if request.method == "GET" and request.url.path.endswith("/versions"):
            state["lists"] += 1
            records = []
            if state["lists"] == 2:
                records = [
                    {
                        "id": "travel-agent:15",
                        "version": "15",
                        "status": "creating",
                        "metadata": {
                            "foundry_opt_release_commit": "a" * 40,
                            "foundry_opt_run_id": operation_id,
                            "foundry_opt_release_operation": operation_id,
                            "foundry_opt_source_zip_sha256": expected_sha,
                        },
                    }
                ]
            return httpx.Response(
                200,
                json={"data": records, "has_more": False},
            )
        if request.method == "POST":
            return httpx.Response(503, json={"error": {"message": "retry"}})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = FoundryPocClient(
        "https://foundry.example/project",
        _TokenProvider(),
        transport=httpx.MockTransport(handler),
    )

    reference = client.create_regular_version(
        "travel-agent",
        HostedDefinition(),
        zip_bytes,
        operation_id=operation_id,
        provenance={"foundry_opt_release_commit": "a" * 40},
        description="Deploy merge a",
        deadline_monotonic=_deadline(30.0),
    )

    assert reference.version == "15"
    assert state["lists"] == 2


def test_regular_version_requires_service_managed_latest_route() -> None:
    client = FoundryPocClient(
        "https://foundry.example/project",
        _TokenProvider(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=_route_payload())
        ),
    )

    with pytest.raises(RouteModeError, match="explicit version selector"):
        client.create_regular_version(
            "travel-agent",
            HostedDefinition(),
            b"zip",
            operation_id="deploy-pinned",
            provenance={"foundry_opt_release_commit": "a" * 40},
            description="Deploy merge a",
            deadline_monotonic=_deadline(30.0),
        )


def test_wait_download_and_latest_verify_regular_version() -> None:
    zip_bytes = b"regular-version-zip"
    expected_sha = hashlib.sha256(zip_bytes).hexdigest()
    operation_id = "deploy-verified"
    metadata = {
        "foundry_opt_run_id": operation_id,
        "foundry_opt_release_operation": operation_id,
        "foundry_opt_source_zip_sha256": expected_sha,
    }
    reference = RegularVersionReference(
        agent_name="travel-agent",
        version="15",
        operation_id=operation_id,
        code_sha256=expected_sha,
        metadata=metadata,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/versions/15"):
            return httpx.Response(
                200,
                json={
                    "id": "travel-agent:15",
                    "version": "15",
                    "status": "active",
                    "metadata": metadata,
                },
            )
        if request.url.path.endswith("/code:download"):
            return httpx.Response(
                200,
                content=zip_bytes,
                headers={
                    "content-type": "application/zip",
                    "x-ms-agent-version": "15",
                    "x-ms-code-zip-sha256": expected_sha,
                },
            )
        if request.url.path.endswith("/agents/travel-agent"):
            return httpx.Response(200, json=_latest_route_payload("15"))
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    client = FoundryPocClient(
        "https://foundry.example/project",
        _TokenProvider(),
        transport=httpx.MockTransport(handler),
    )

    active = client.wait_for_regular_version_active(
        reference,
        deadline_monotonic=_deadline(30.0),
        poll_interval_seconds=0,
    )
    downloaded = client.download_regular_version_code(
        active,
        deadline_monotonic=_deadline(30.0),
    )
    latest = client.assert_regular_version_is_latest(
        active,
        deadline_monotonic=_deadline(30.0),
    )

    assert downloaded == zip_bytes
    assert latest.latest_version == "15"


def test_latest_verification_polls_service_managed_route() -> None:
    operation_id = "deploy-latest"
    metadata = {
        "foundry_opt_run_id": operation_id,
        "foundry_opt_release_operation": operation_id,
        "foundry_opt_source_zip_sha256": "b" * 64,
    }
    reference = RegularVersionReference(
        agent_name="travel-agent",
        version="15",
        operation_id=operation_id,
        code_sha256="b" * 64,
        metadata=metadata,
    )
    calls = {"count": 0}
    monotonic_state = {"now": 0.0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        version = "14" if calls["count"] == 1 else "15"
        return httpx.Response(200, json=_latest_route_payload(version))

    def monotonic() -> float:
        return monotonic_state["now"]

    def sleep(seconds: float) -> None:
        monotonic_state["now"] += seconds

    client = FoundryPocClient(
        "https://foundry.example/project",
        _TokenProvider(),
        transport=httpx.MockTransport(handler),
        monotonic=monotonic,
        sleep=sleep,
    )

    route = client.assert_regular_version_is_latest(
        reference,
        deadline_monotonic=10.0,
        poll_interval_seconds=1.0,
    )

    assert route.latest_version == "15"
    assert calls["count"] == 2
    assert monotonic_state["now"] == 1.0


def test_foundry_service_error_redacts_headers_and_body() -> None:
    secret_bytes = b"zip-secret"
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json=_route_payload())
        return httpx.Response(
            503,
            headers={"content-type": "application/json"},
            json={"error": {"message": f"echoed {request.headers['authorization']}"}},
        )

    client = FoundryPocClient(
        "https://foundry.example/project",
        _TokenProvider(),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ServiceError) as caught:
        client.create_source_code_draft(
            "travel-agent",
            HostedDefinition(),
            secret_bytes,
            deadline_monotonic=_deadline(30.0),
            ownership_token="owned-secret",
        )

    assert caught.value.status_code == 503
    assert caught.value.request is not None
    assert [request.headers["authorization"] for request in requests] == [
        "Bearer foundry-token",
        "Bearer foundry-token",
    ]
    headers = {key.lower(): value for key, value in caught.value.request.headers}
    assert headers["authorization"] == "******"
    diagnostics = " ".join(
        (
            str(caught.value),
            repr(caught.value),
            str(caught.value.request),
            repr(caught.value.request),
            repr(client),
        )
    )
    assert "foundry-token" not in diagnostics
    assert secret_bytes.decode("ascii") not in diagnostics
    assert "owned-secret" not in diagnostics


def test_foundry_redirect_error_does_not_echo_authorization() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302,
            headers={
                "location": (
                    "https://attacker.example/redirect"
                    f"?authorization={request.headers['authorization']}"
                )
            },
        )

    client = FoundryPocClient(
        "https://foundry.example/project",
        _TokenProvider(),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(ServiceError, match="redirect") as caught:
        client.route_fingerprint("travel-agent", deadline_monotonic=_deadline(20.0))

    assert caught.value.status_code == 302
    assert caught.value.request is not None
    assert [request.headers["authorization"] for request in requests] == ["Bearer foundry-token"]
    headers = {key.lower(): value for key, value in caught.value.request.headers}
    assert headers["authorization"] == "******"
    diagnostics = " ".join(
        (
            str(caught.value),
            repr(caught.value),
            str(caught.value.request),
            repr(caught.value.request),
        )
    )
    assert "foundry-token" not in diagnostics
    assert "attacker.example" not in diagnostics


def test_foundry_auth_error_does_not_echo_authorization() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            403,
            headers={"content-type": "application/json"},
            json={"error": {"message": f"echoed {request.headers['authorization']}"}},
        )

    client = FoundryPocClient(
        "https://foundry.example/project",
        _TokenProvider(),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(AuthError, match="authentication failed") as caught:
        client.route_fingerprint("travel-agent", deadline_monotonic=_deadline(20.0))

    assert [request.headers["authorization"] for request in requests] == ["Bearer foundry-token"]
    diagnostics = " ".join((str(caught.value), repr(caught.value), repr(client)))
    assert "foundry-token" not in diagnostics
    assert "echoed" not in diagnostics


def test_wait_for_version_active_polls_exact_version() -> None:
    calls: list[str] = []
    state = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        state["count"] += 1
        status = "creating" if state["count"] == 1 else "active"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "id": "travel-agent:draft-abc",
                "version": "draft-abc",
                "status": status,
                "metadata": {
                    "foundry_opt_run_id": "owned-token",
                    "foundry_opt_source_zip_sha256": "b" * 64,
                    "foundry_opt_route_sha256": "a" * 64,
                },
            },
        )

    slept: list[float] = []
    monotonic_state = {"now": 0.0}

    def monotonic() -> float:
        return monotonic_state["now"]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        monotonic_state["now"] += seconds

    client = FoundryPocClient(
        "https://foundry.example/project",
        _TokenProvider(),
        transport=httpx.MockTransport(handler),
        monotonic=monotonic,
        sleep=sleep,
    )
    reference = _draft_reference()

    active = client.wait_for_version_active(
        reference,
        deadline_monotonic=5.0,
        poll_interval_seconds=1.25,
    )

    assert active.status == "active"
    assert calls == [
        "/project/agents/travel-agent/versions/draft-abc",
        "/project/agents/travel-agent/versions/draft-abc",
    ]
    assert slept == [1.25]


def test_wait_for_version_active_allows_missing_metadata_when_not_proving_ownership() -> None:
    reference = _draft_reference()
    client = FoundryPocClient(
        "https://foundry.example/project",
        _TokenProvider(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "id": "travel-agent:draft-abc",
                    "version": "draft-abc",
                    "status": "active",
                },
            )
        ),
    )

    active = client.wait_for_version_active(
        reference,
        deadline_monotonic=_deadline(5.0),
    )

    assert active.status == "active"
    assert active.ownership_token == reference.ownership_token
    assert active.code_sha256 == reference.code_sha256
    assert active.route_sha256 == reference.route_sha256


def test_wait_for_version_active_raises_on_failed_status() -> None:
    client = FoundryPocClient(
        "https://foundry.example/project",
        _TokenProvider(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "id": "travel-agent:draft-abc",
                    "version": "draft-abc",
                    "status": "failed",
                    "metadata": {
                        "foundry_opt_run_id": "owned-token",
                        "foundry_opt_source_zip_sha256": "b" * 64,
                        "foundry_opt_route_sha256": "a" * 64,
                    },
                },
            )
        ),
    )

    with pytest.raises(ServiceError, match="terminal status"):
        client.wait_for_version_active(
            _draft_reference(),
            deadline_monotonic=_deadline(5.0),
        )


def test_wait_for_version_active_raises_deadline_error() -> None:
    monotonic_state = {"now": 0.0}

    def monotonic() -> float:
        return monotonic_state["now"]

    def sleep(seconds: float) -> None:
        monotonic_state["now"] += seconds

    client = FoundryPocClient(
        "https://foundry.example/project",
        _TokenProvider(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "id": "travel-agent:draft-abc",
                    "version": "draft-abc",
                    "status": "creating",
                    "metadata": {
                        "foundry_opt_run_id": "owned-token",
                        "foundry_opt_source_zip_sha256": "b" * 64,
                        "foundry_opt_route_sha256": "a" * 64,
                    },
                },
            )
        ),
        monotonic=monotonic,
        sleep=sleep,
    )

    with pytest.raises(DeadlineError):
        client.wait_for_version_active(
            _draft_reference(),
            deadline_monotonic=1.0,
            poll_interval_seconds=0.6,
        )


def test_download_exact_deployed_code_detects_sha_mismatch() -> None:
    code = b"downloaded-zip"
    client = FoundryPocClient(
        "https://foundry.example/project",
        _TokenProvider(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=code,
                headers={
                    "content-type": "application/zip",
                    "x-ms-agent-version": "draft-abc",
                    "x-ms-code-zip-sha256": "f" * 64,
                },
            )
        ),
    )

    with pytest.raises(ContractError, match="expected SHA-256"):
        client.download_exact_deployed_code(
            _draft_reference(code_sha256=hashlib.sha256(code).hexdigest()),
            deadline_monotonic=_deadline(10.0),
        )


def test_delete_owned_version_only_deletes_exact_owned_version_and_verifies_absence() -> None:
    reference = _draft_reference()
    calls: list[tuple[str, str]] = []
    deleted = {"value": False}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            if deleted["value"]:
                return httpx.Response(404, json={"error": {"code": "not_found"}})
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "id": "travel-agent:draft-abc",
                    "version": "draft-abc",
                    "status": "active",
                    "metadata": {
                        "foundry_opt_run_id": "owned-token",
                        "foundry_opt_source_zip_sha256": "b" * 64,
                        "foundry_opt_route_sha256": "a" * 64,
                    },
                },
            )
        assert request.url.params.get("force") == "true"
        deleted["value"] = True
        return httpx.Response(204)

    client = FoundryPocClient(
        "https://foundry.example/project",
        _TokenProvider(),
        transport=httpx.MockTransport(handler),
    )
    client.delete_owned_version(reference, deadline_monotonic=_deadline(20.0))

    assert calls == [
        ("GET", "/project/agents/travel-agent/versions/draft-abc"),
        ("DELETE", "/project/agents/travel-agent/versions/draft-abc"),
        ("GET", "/project/agents/travel-agent/versions/draft-abc"),
    ]


def test_delete_owned_version_retries_conflicts_until_absent() -> None:
    reference = _draft_reference()
    calls: list[tuple[str, str]] = []
    delete_attempts = 0
    deleted = {"value": False}
    now = {"value": 0.0}
    sleep_calls: list[float] = []

    def sleep(seconds: float) -> None:
        sleep_calls.append(seconds)
        now["value"] += seconds

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal delete_attempts
        calls.append((request.method, request.url.path))
        if request.method == "GET":
            if deleted["value"]:
                return httpx.Response(404, json={"error": {"code": "not_found"}})
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "id": "travel-agent:draft-abc",
                    "version": "draft-abc",
                    "status": "active",
                    "metadata": {
                        "foundry_opt_run_id": "owned-token",
                        "foundry_opt_source_zip_sha256": "b" * 64,
                        "foundry_opt_route_sha256": "a" * 64,
                    },
                },
            )
        assert request.url.params.get("force") == "true"
        delete_attempts += 1
        if delete_attempts == 1:
            return httpx.Response(
                409,
                headers={"content-type": "application/json"},
                json={"error": {"code": "conflict"}},
            )
        deleted["value"] = True
        return httpx.Response(204)

    client = FoundryPocClient(
        "https://foundry.example/project",
        _TokenProvider(),
        transport=httpx.MockTransport(handler),
        monotonic=lambda: now["value"],
        sleep=sleep,
    )
    client.delete_owned_version(reference, deadline_monotonic=10.0)

    assert sleep_calls == [2.0]
    assert calls == [
        ("GET", "/project/agents/travel-agent/versions/draft-abc"),
        ("DELETE", "/project/agents/travel-agent/versions/draft-abc"),
        ("GET", "/project/agents/travel-agent/versions/draft-abc"),
        ("DELETE", "/project/agents/travel-agent/versions/draft-abc"),
        ("GET", "/project/agents/travel-agent/versions/draft-abc"),
    ]


def test_delete_owned_version_refuses_missing_metadata_and_skips_delete() -> None:
    reference = _draft_reference()
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "id": "travel-agent:draft-abc",
                "version": "draft-abc",
                "status": "active",
                "metadata": {
                    "foundry_opt_run_id": "owned-token",
                    "foundry_opt_source_zip_sha256": "b" * 64,
                },
            },
        )

    client = FoundryPocClient(
        "https://foundry.example/project",
        _TokenProvider(),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CleanupError, match="could not be proven"):
        client.delete_owned_version(reference, deadline_monotonic=_deadline(20.0))

    assert calls == [("GET", "/project/agents/travel-agent/versions/draft-abc")]


def test_delete_owned_version_refuses_changed_metadata_and_skips_delete() -> None:
    reference = _draft_reference()
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "id": "travel-agent:draft-abc",
                "version": "draft-abc",
                "status": "active",
                "metadata": {
                    "foundry_opt_run_id": "owned-token",
                    "foundry_opt_source_zip_sha256": "c" * 64,
                    "foundry_opt_route_sha256": "a" * 64,
                },
            },
        )

    client = FoundryPocClient(
        "https://foundry.example/project",
        _TokenProvider(),
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(CleanupError, match="could not be proven"):
        client.delete_owned_version(reference, deadline_monotonic=_deadline(20.0))

    assert calls == [("GET", "/project/agents/travel-agent/versions/draft-abc")]


def test_delete_owned_version_refuses_mismatched_metadata() -> None:
    client = FoundryPocClient(
        "https://foundry.example/project",
        _TokenProvider(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={
                    "id": "travel-agent:draft-abc",
                    "version": "draft-abc",
                    "status": "active",
                    "metadata": {
                        "foundry_opt_run_id": "someone-else",
                        "foundry_opt_source_zip_sha256": "b" * 64,
                        "foundry_opt_route_sha256": "a" * 64,
                    },
                },
            )
        ),
    )

    with pytest.raises(CleanupError, match="could not be proven"):
        client.delete_owned_version(_draft_reference(), deadline_monotonic=_deadline(20.0))


def test_route_fingerprint_and_assert_route_unchanged_detect_drift() -> None:
    payloads = [_route_payload("7"), _route_payload("8")]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/json"}, json=payloads.pop(0))

    client = FoundryPocClient(
        "https://foundry.example/project",
        _TokenProvider(),
        transport=httpx.MockTransport(handler),
    )
    before = client.route_fingerprint("travel-agent", deadline_monotonic=_deadline(20.0))
    after = client.route_fingerprint("travel-agent", deadline_monotonic=_deadline(20.0))

    with pytest.raises(RouteDriftError):
        client.assert_route_unchanged(before, after, deadline_monotonic=_deadline(20.0))


def test_azure_projects_evaluation_backend_normalizes_metrics_and_redacts_raw_details() -> None:
    class _Runs:
        def __init__(self) -> None:
            self.created: list[tuple[str, object]] = []
            self.retrieve_count = 0

        def create(self, eval_id: str, **kwargs: object) -> object:
            self.created.append((eval_id, kwargs))
            return {"id": "run-123"}

        def retrieve(self, run_id: str, *, eval_id: str) -> object:
            self.retrieve_count += 1
            if self.retrieve_count == 1:
                return {"id": run_id, "eval_id": eval_id, "status": "in_progress"}
            return {
                "id": run_id,
                "eval_id": eval_id,
                "status": "completed",
                "report_url": "https://ai.azure.com/reports/run-123?token=secret#fragment",
                "result_counts": {
                    "total": 5,
                    "passed": 4,
                    "failed": 1,
                    "errored": 0,
                },
                "per_testing_criteria_results": [
                    {
                        "testing_criteria": "quality",
                        "passed": 3,
                        "failed": 1,
                        "prompt": "raw prompt",
                        "tool_args": {"secret": "value"},
                    },
                    {
                        "testing_criteria": "safety",
                        "passed": 4,
                        "failed": 0,
                        "trace": "raw trace",
                        "output": "raw output",
                    },
                ],
            }

    class _Evals:
        def __init__(self) -> None:
            self.runs = _Runs()

        def retrieve(self, evaluation_id: str) -> object:
            return {
                "id": evaluation_id,
                "testing_criteria": [
                    {"name": "quality"},
                    {"name": "safety"},
                ],
            }

    class _OpenAIClient:
        def __init__(self) -> None:
            self.evals = _Evals()

    backend = AzureProjectsEvaluationBackend(openai_client=_OpenAIClient())
    sleep_calls: list[float] = []

    evidence = backend.run(
        _draft_reference(),
        EvaluationContract(
            evaluation_id="eval-1",
            dataset_id="dataset-1",
            evaluator_ids=("quality", "safety"),
            poll_interval_seconds=1.0,
        ),
        deadline_monotonic=10.0,
        monotonic=lambda: float(len(sleep_calls)),
        sleep=lambda seconds: sleep_calls.append(seconds),
    )

    created_eval_id, kwargs = backend._openai_client.evals.runs.created[0]  # type: ignore[attr-defined]
    assert created_eval_id == "eval-1"
    assert kwargs["data_source"]["source"] == {"type": "file_id", "id": "dataset-1"}
    assert kwargs["data_source"]["target"] == {
        "type": "azure_ai_agent",
        "name": "travel-agent",
        "version": "draft-abc",
    }
    assert evidence.reference.run_id == "run-123"
    assert evidence.reference.evaluation_id == "eval-1"
    assert evidence.report_url == "https://ai.azure.com/reports/run-123"
    assert evidence.total_cases == 5
    assert evidence.focused_case_count == 8
    assert [metric.name for metric in evidence.metrics] == ["quality", "safety"]
    assert evidence.metrics[0].failed_cases == 1
    assert evidence.metrics[1].passed is True
    rendered = json.dumps(asdict(evidence), sort_keys=True)
    assert "raw prompt" not in rendered
    assert "raw trace" not in rendered
    assert "raw output" not in rendered
    assert "secret" not in rendered


def test_azure_projects_evaluation_backend_matches_exact_evaluator_resources_without_order_dependence() -> None:
    policy_evaluator_id = (
        "azureai://accounts/foundry-account/projects/project-one/evaluators/"
        "evaluator-policy-coverage-2851c64532f9/versions/1"
    )
    safety_evaluator_id = (
        "azureai://accounts/foundry-account/projects/project-one/evaluators/"
        "evaluator-advisory-safety-e58266b2a28f/versions/1"
    )

    class _Runs:
        def __init__(self) -> None:
            self.created: list[tuple[str, object]] = []
            self.retrieve_count = 0

        def create(self, eval_id: str, **kwargs: object) -> object:
            self.created.append((eval_id, kwargs))
            return {"id": "run-456"}

        def retrieve(self, run_id: str, *, eval_id: str) -> object:
            self.retrieve_count += 1
            if self.retrieve_count == 1:
                return {"id": run_id, "eval_id": eval_id, "status": "in_progress"}
            return {
                "id": run_id,
                "eval_id": eval_id,
                "status": "completed",
                "report_url": "https://ai.azure.com/reports/run-456?token=secret",
                "result_counts": {
                    "total": 6,
                    "passed": 6,
                    "failed": 0,
                    "errored": 0,
                },
                "per_testing_criteria_results": [
                    {
                        "testing_criteria": "advisory_safety",
                        "passed": 6,
                        "failed": 0,
                    },
                    {
                        "testing_criteria": "policy_coverage",
                        "passed": 5,
                        "failed": 1,
                    },
                ],
            }

    class _Evals:
        def __init__(self) -> None:
            self.runs = _Runs()

        def retrieve(self, evaluation_id: str) -> object:
            return {
                "id": evaluation_id,
                "testing_criteria": [
                    {
                        "name": "advisory_safety",
                        "evaluator_name": "evaluator-advisory-safety-e58266b2a28f",
                        "evaluator_version": "1",
                    },
                    {
                        "name": "policy_coverage",
                        "evaluator_name": "evaluator-policy-coverage-2851c64532f9",
                        "evaluator_version": "1",
                    },
                ],
            }

    class _OpenAIClient:
        def __init__(self) -> None:
            self.evals = _Evals()

    backend = AzureProjectsEvaluationBackend(openai_client=_OpenAIClient())

    evidence = backend.run(
        _draft_reference(),
        EvaluationContract(
            evaluation_id="eval-1",
            dataset_id="dataset-1",
            evaluator_ids=(policy_evaluator_id, safety_evaluator_id),
        ),
        deadline_monotonic=10.0,
        monotonic=lambda: 0.0,
        sleep=lambda seconds: None,
    )

    created_eval_id, kwargs = backend._openai_client.evals.runs.created[0]  # type: ignore[attr-defined]
    assert created_eval_id == "eval-1"
    assert kwargs["data_source"]["source"] == {"type": "file_id", "id": "dataset-1"}
    assert [metric.name for metric in evidence.metrics] == [
        "policy_coverage",
        "advisory_safety",
    ]
    assert evidence.reference.evaluator_ids == (policy_evaluator_id, safety_evaluator_id)
    assert evidence.metrics[0].failed_cases == 1
    assert evidence.metrics[1].passed is True
    assert evidence.report_url == "https://ai.azure.com/reports/run-456"


def test_azure_projects_evaluation_backend_matches_registry_evaluator_resources() -> None:
    task_completion_evaluator_id = (
        "azureml://registries/azureml/evaluators/builtin.task_completion/versions/19"
    )

    class _Runs:
        def __init__(self) -> None:
            self.retrieve_count = 0

        def create(self, eval_id: str, **kwargs: object) -> object:
            return {"id": "run-task-completion"}

        def retrieve(self, run_id: str, *, eval_id: str) -> object:
            self.retrieve_count += 1
            if self.retrieve_count == 1:
                return {"id": run_id, "eval_id": eval_id, "status": "in_progress"}
            return {
                "id": run_id,
                "eval_id": eval_id,
                "status": "completed",
                "report_url": "https://ai.azure.com/reports/run-task-completion",
                "result_counts": {
                    "total": 5,
                    "passed": 4,
                    "failed": 1,
                    "errored": 0,
                },
                "per_testing_criteria_results": [
                    {
                        "testing_criteria": "task_completion",
                        "passed": 4,
                        "failed": 1,
                    },
                ],
            }

    class _Evals:
        def __init__(self) -> None:
            self.runs = _Runs()

        def retrieve(self, evaluation_id: str) -> object:
            return {
                "id": evaluation_id,
                "testing_criteria": [
                    {
                        "name": "task_completion",
                        "evaluator_name": "builtin.task_completion",
                        "evaluator_version": "19",
                    },
                ],
            }

    class _OpenAIClient:
        def __init__(self) -> None:
            self.evals = _Evals()

    backend = AzureProjectsEvaluationBackend(openai_client=_OpenAIClient())

    evidence = backend.run(
        _draft_reference(),
        EvaluationContract(
            evaluation_id="eval-task-completion",
            dataset_id="dataset-1",
            evaluator_ids=(task_completion_evaluator_id,),
        ),
        deadline_monotonic=10.0,
        monotonic=lambda: 0.0,
        sleep=lambda seconds: None,
    )

    assert evidence.reference.evaluator_ids == (task_completion_evaluator_id,)
    assert [metric.name for metric in evidence.metrics] == ["task_completion"]
    assert evidence.metrics[0].failed_cases == 1
