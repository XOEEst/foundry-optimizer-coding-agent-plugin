from __future__ import annotations

import base64
import json
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import parse_qs

import httpx
import pytest
from azure.core.exceptions import ClientAuthenticationError

from foundry_opt.poc.auth import (
    ACTIONS_ID_TOKEN_REQUEST_TOKEN_ENV,
    ACTIONS_ID_TOKEN_REQUEST_URL_ENV,
    GITHUB_ACTIONS_OIDC_AUDIENCE,
    AuthError,
    GitHubActionsOidcAssertionProvider,
    GitHubActionsOidcConfig,
    build_client_assertion_credential,
    detect_github_actions_oidc,
)


TENANT_ID = "11111111-2222-3333-4444-555555555555"
CLIENT_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
SUBJECT = "repo:octo-org/example:environment:copilot"
REPOSITORY_ID = "123456789"
NOW = 2_000_000_000.0


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _jwt(**updates: object) -> str:
    header = {"alg": "RS256", "kid": "test", "typ": "JWT"}
    payload: dict[str, object] = {
        "iss": "https://token.actions.githubusercontent.com",
        "aud": GITHUB_ACTIONS_OIDC_AUDIENCE,
        "sub": SUBJECT,
        "repository_id": REPOSITORY_ID,
        "nbf": int(NOW) - 30,
        "exp": int(NOW) + 600,
    }
    payload.update(updates)
    return ".".join(
        (
            _b64url(json.dumps(header, separators=(",", ":")).encode("ascii")),
            _b64url(json.dumps(payload, separators=(",", ":")).encode("ascii")),
            _b64url(b"sig"),
        )
    )


def _config() -> GitHubActionsOidcConfig:
    return GitHubActionsOidcConfig(
        tenant_id=TENANT_ID,
        client_id=CLIENT_ID,
        expected_subject=SUBJECT,
        expected_repository_id=REPOSITORY_ID,
    )


def test_detect_github_actions_oidc_requires_both_environment_variables() -> None:
    assert not detect_github_actions_oidc({})
    assert detect_github_actions_oidc(
        {
            ACTIONS_ID_TOKEN_REQUEST_URL_ENV: "https://vstoken.actions.githubusercontent.com/oidc",
            ACTIONS_ID_TOKEN_REQUEST_TOKEN_ENV: "request-token",
        }
    )
    with pytest.raises(AuthError, match="partially configured"):
        detect_github_actions_oidc(
            {ACTIONS_ID_TOKEN_REQUEST_URL_ENV: "https://vstoken.actions.githubusercontent.com/oidc"}
        )
    with pytest.raises(AuthError, match="partially configured"):
        detect_github_actions_oidc(
            {ACTIONS_ID_TOKEN_REQUEST_TOKEN_ENV: "request-token"}
        )


@dataclass
class _CapturedCredential:
    tenant_id: str
    client_id: str
    callback: Callable[[], str]

    def get_token(self, *scopes: str, **kwargs: object) -> object:
        del kwargs
        return {"scopes": scopes, "assertion": self.callback()}


def test_build_client_assertion_credential_requests_exact_audience() -> None:
    request_token = "github-request-token-secret"
    assertion = _jwt()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.url.host == "vstoken.actions.githubusercontent.com"
        assert parse_qs(request.url.query.decode()) == {
            "job": ["job-123"],
            "audience": [GITHUB_ACTIONS_OIDC_AUDIENCE],
        }
        assert request.headers["authorization"] == f"bearer {request_token}"
        assert request.headers["accept"] == "application/json"
        assert request.headers["accept-encoding"] == "identity"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={"value": assertion},
        )

    captured: list[_CapturedCredential] = []

    def factory(
        tenant_id: str,
        client_id: str,
        callback: Callable[[], str],
    ) -> _CapturedCredential:
        credential = _CapturedCredential(tenant_id, client_id, callback)
        captured.append(credential)
        return credential

    credential = build_client_assertion_credential(
        _config(),
        environment={
            ACTIONS_ID_TOKEN_REQUEST_URL_ENV: (
                "https://vstoken.actions.githubusercontent.com/oidc?job=job-123"
            ),
            ACTIONS_ID_TOKEN_REQUEST_TOKEN_ENV: request_token,
        },
        transport=httpx.MockTransport(handler),
        now=lambda: NOW,
        credential_factory=factory,
    )

    assert credential.get_token("https://ai.azure.com/.default") == {
        "scopes": ("https://ai.azure.com/.default",),
        "assertion": assertion,
    }
    assert len(requests) == 1
    assert captured[0].tenant_id == TENANT_ID
    assert captured[0].client_id == CLIENT_ID
    diagnostics = repr(credential) + repr(credential.assertion_provider)
    assert request_token not in diagnostics
    assert assertion not in diagnostics


def test_client_assertion_credential_sanitizes_entra_rejection() -> None:
    class RejectedCredential:
        def get_token(self, *scopes: str, **kwargs: object) -> object:
            del scopes, kwargs
            raise ClientAuthenticationError(
                message="sensitive tenant rejection details"
            )

    credential = build_client_assertion_credential(
        _config(),
        environment={},
        credential_factory=lambda *_args: RejectedCredential(),
    )

    with pytest.raises(AuthError, match="Microsoft Entra rejected") as captured:
        credential.get_token("https://ai.azure.com/.default")

    assert captured.value.stage == "entra_token"
    assert "sensitive tenant rejection details" not in str(captured.value)


@pytest.mark.parametrize(
    ("environment", "handler", "match"),
    [
        pytest.param(
            {
                ACTIONS_ID_TOKEN_REQUEST_URL_ENV: "http://vstoken.actions.githubusercontent.com/oidc",
                ACTIONS_ID_TOKEN_REQUEST_TOKEN_ENV: "request-token",
            },
            None,
            "violates the GitHub Actions contract",
            id="non-https",
        ),
        pytest.param(
            {
                ACTIONS_ID_TOKEN_REQUEST_URL_ENV: "https://vstoken.actions.githubusercontent.com/oidc",
                ACTIONS_ID_TOKEN_REQUEST_TOKEN_ENV: "request-token",
            },
            lambda request: httpx.Response(
                302,
                headers={"location": "https://attacker.example/redirect"},
            ),
            "redirect",
            id="redirect",
        ),
    ],
)
def test_assertion_provider_refuses_unsafe_request_urls(
    environment: dict[str, str],
    handler: Callable[[httpx.Request], httpx.Response] | None,
    match: str,
) -> None:
    provider = GitHubActionsOidcAssertionProvider(
        _config(),
        environment=environment,
        transport=None if handler is None else httpx.MockTransport(handler),
        now=lambda: NOW,
    )
    with pytest.raises(AuthError, match=match) as caught:
        provider.get_assertion()
    diagnostics = repr(caught.value) + str(caught.value) + repr(provider)
    assert "request-token" not in diagnostics


def test_assertion_provider_rejects_oversized_bodies_without_echoing_them() -> None:
    secret = "oversized-secret-should-not-leak"
    provider = GitHubActionsOidcAssertionProvider(
        _config(),
        environment={
            ACTIONS_ID_TOKEN_REQUEST_URL_ENV: "https://vstoken.actions.githubusercontent.com/oidc",
            ACTIONS_ID_TOKEN_REQUEST_TOKEN_ENV: "request-token",
        },
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps({"value": secret}).encode("ascii"),
            )
        ),
        now=lambda: NOW,
        max_response_bytes=8,
    )
    with pytest.raises(AuthError, match="configured limit") as caught:
        provider.get_assertion()
    diagnostics = repr(caught.value) + str(caught.value)
    assert secret not in diagnostics


@pytest.mark.parametrize(
    "claims",
    [
        pytest.param({"sub": "repo:octo-org/other:environment:copilot"}, id="subject"),
        pytest.param({"repository_id": "42"}, id="repository"),
        pytest.param({"aud": "api://wrong"}, id="audience"),
        pytest.param({"exp": int(NOW) - 1}, id="expired"),
        pytest.param({"nbf": int(NOW) + 60}, id="not-yet-valid"),
    ],
)
def test_assertion_provider_validates_expected_claims(claims: dict[str, object]) -> None:
    assertion = _jwt(**claims)
    provider = GitHubActionsOidcAssertionProvider(
        _config(),
        environment={
            ACTIONS_ID_TOKEN_REQUEST_URL_ENV: "https://vstoken.actions.githubusercontent.com/oidc",
            ACTIONS_ID_TOKEN_REQUEST_TOKEN_ENV: "request-token",
        },
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json={"value": assertion},
            )
        ),
        now=lambda: NOW,
    )
    with pytest.raises(AuthError):
        provider.get_assertion()
