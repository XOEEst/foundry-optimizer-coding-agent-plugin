from __future__ import annotations

import base64
import binascii
import json
import math
import os
import re
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from foundry_opt._tls import system_ssl_context


GITHUB_ACTIONS_OIDC_AUDIENCE = "api://AzureADTokenExchange"
GITHUB_ACTIONS_OIDC_ISSUER = "https://token.actions.githubusercontent.com"
ACTIONS_ID_TOKEN_REQUEST_URL_ENV = "ACTIONS_ID_TOKEN_REQUEST_URL"
ACTIONS_ID_TOKEN_REQUEST_TOKEN_ENV = "ACTIONS_ID_TOKEN_REQUEST_TOKEN"
USER_AGENT = "foundry-opt-poc/0.1"
_JWT_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


class AuthError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        stage: str,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.status_code = status_code

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(stage={self.stage!r}, "
            f"status_code={self.status_code!r})"
        )


@dataclass(frozen=True, slots=True)
class GitHubActionsOidcConfig:
    tenant_id: str
    client_id: str
    expected_subject: str
    expected_repository_id: str
    audience: str = GITHUB_ACTIONS_OIDC_AUDIENCE
    issuer: str = GITHUB_ACTIONS_OIDC_ISSUER
    request_url_env: str = ACTIONS_ID_TOKEN_REQUEST_URL_ENV
    request_token_env: str = ACTIONS_ID_TOKEN_REQUEST_TOKEN_ENV

    def __post_init__(self) -> None:
        object.__setattr__(self, "tenant_id", _validated_uuid(self.tenant_id, "tenant_id"))
        object.__setattr__(self, "client_id", _validated_uuid(self.client_id, "client_id"))
        object.__setattr__(
            self,
            "expected_subject",
            _validated_string(self.expected_subject, "expected_subject", 2048),
        )
        object.__setattr__(
            self,
            "expected_repository_id",
            _validated_repository_id(self.expected_repository_id),
        )
        object.__setattr__(
            self,
            "audience",
            _validated_string(self.audience, "audience", 2048),
        )
        object.__setattr__(
            self,
            "issuer",
            _validated_string(self.issuer, "issuer", 2048),
        )
        object.__setattr__(
            self,
            "request_url_env",
            _validated_environment_name(self.request_url_env, "request_url_env"),
        )
        object.__setattr__(
            self,
            "request_token_env",
            _validated_environment_name(
                self.request_token_env,
                "request_token_env",
            ),
        )
        if self.request_url_env == self.request_token_env:
            raise ValueError("request_url_env and request_token_env must differ")


def detect_github_actions_oidc(
    environment: Mapping[str, str] | None = None,
    *,
    request_url_env: str = ACTIONS_ID_TOKEN_REQUEST_URL_ENV,
    request_token_env: str = ACTIONS_ID_TOKEN_REQUEST_TOKEN_ENV,
) -> bool:
    source = os.environ if environment is None else environment
    request_url = source.get(request_url_env, "")
    request_token = source.get(request_token_env, "")
    has_url = isinstance(request_url, str) and bool(request_url)
    has_token = isinstance(request_token, str) and bool(request_token)
    if has_url != has_token:
        raise AuthError(
            "GitHub Actions OIDC is partially configured",
            stage="detect",
        )
    return has_url and has_token


class _CredentialFactory(Protocol):
    def __call__(
        self,
        tenant_id: str,
        client_id: str,
        assertion_callback: Callable[[], str],
    ) -> object: ...


def _default_credential_factory(
    tenant_id: str,
    client_id: str,
    assertion_callback: Callable[[], str],
) -> object:
    from azure.identity import ClientAssertionCredential

    return ClientAssertionCredential(tenant_id, client_id, assertion_callback)


class GitHubActionsOidcAssertionProvider:
    __slots__ = (
        "_client",
        "_config",
        "_environment",
        "_max_jwt_bytes",
        "_max_request_token_bytes",
        "_max_response_bytes",
        "_max_url_bytes",
        "_now",
        "_timeout",
    )

    def __init__(
        self,
        config: GitHubActionsOidcConfig,
        *,
        environment: Mapping[str, str] | None = None,
        transport: httpx.BaseTransport | None = None,
        now: Callable[[], float] = time.time,
        timeout: float = 10.0,
        max_url_bytes: int = 16 * 1024,
        max_request_token_bytes: int = 32 * 1024,
        max_response_bytes: int = 64 * 1024,
        max_jwt_bytes: int = 32 * 1024,
    ) -> None:
        self._config = config
        self._environment = os.environ if environment is None else environment
        self._client = httpx.Client(
            transport=transport,
            follow_redirects=False,
            trust_env=False,
            timeout=timeout,
            verify=system_ssl_context(),
        )
        self._now = now
        self._timeout = float(timeout)
        self._max_url_bytes = _positive_int(max_url_bytes, "max_url_bytes")
        self._max_request_token_bytes = _positive_int(
            max_request_token_bytes,
            "max_request_token_bytes",
        )
        self._max_response_bytes = _positive_int(
            max_response_bytes,
            "max_response_bytes",
        )
        self._max_jwt_bytes = _positive_int(max_jwt_bytes, "max_jwt_bytes")

    def __call__(self) -> str:
        return self.get_assertion()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"tenant_id={self._config.tenant_id!r}, "
            f"client_id={self._config.client_id!r}, "
            "environment=<redacted>)"
        )

    def close(self) -> None:
        self._client.close()

    def get_assertion(self) -> str:
        detect_github_actions_oidc(
            self._environment,
            request_url_env=self._config.request_url_env,
            request_token_env=self._config.request_token_env,
        )
        request_url = self._environment[self._config.request_url_env]
        request_token = self._environment[self._config.request_token_env]
        if len(request_token.encode("utf-8")) > self._max_request_token_bytes:
            raise AuthError(
                "GitHub Actions OIDC request token exceeds the configured limit",
                stage="github_oidc",
            )
        if not _safe_bearer_value(request_token):
            raise AuthError(
                "GitHub Actions OIDC request token has an invalid bearer format",
                stage="github_oidc",
            )
        oidc_url = _build_oidc_request_url(
            request_url,
            audience=self._config.audience,
            max_bytes=self._max_url_bytes,
        )
        try:
            with self._client.stream(
                "GET",
                oidc_url,
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "Authorization": f"bearer {request_token}",
                    "User-Agent": USER_AGENT,
                },
            ) as response:
                if 300 <= response.status_code < 400:
                    raise AuthError(
                        "GitHub Actions OIDC refused an HTTP redirect",
                        stage="github_oidc",
                        status_code=response.status_code,
                    )
                if response.status_code != 200:
                    raise AuthError(
                        f"GitHub Actions OIDC failed with HTTP {response.status_code}",
                        stage="github_oidc",
                        status_code=response.status_code,
                    )
                content_type = response.headers.get("content-type", "")
                if content_type.split(";", 1)[0].strip().lower() != "application/json":
                    raise AuthError(
                        "GitHub Actions OIDC returned an unexpected content type",
                        stage="github_oidc",
                        status_code=response.status_code,
                    )
                content_encoding = response.headers.get("content-encoding", "")
                if content_encoding.strip().lower() not in ("", "identity"):
                    raise AuthError(
                        "GitHub Actions OIDC returned an unexpected content encoding",
                        stage="github_oidc",
                        status_code=response.status_code,
                    )
                content_length = response.headers.get("content-length")
                if content_length is not None:
                    declared = _declared_length(
                        content_length,
                        maximum=self._max_response_bytes,
                    )
                    if declared > self._max_response_bytes:
                        raise AuthError(
                            "GitHub Actions OIDC response exceeds the configured limit",
                            stage="github_oidc",
                            status_code=response.status_code,
                        )
                body = _bounded_bytes(
                    response.iter_bytes(),
                    maximum=self._max_response_bytes,
                    stage="github_oidc",
                )
        except httpx.TimeoutException as exc:
            raise AuthError("GitHub Actions OIDC timed out", stage="github_oidc") from exc
        except httpx.TransportError as exc:
            raise AuthError(
                "GitHub Actions OIDC transport failed",
                stage="github_oidc",
            ) from exc
        payload = _strict_json_object(body, stage="github_oidc")
        assertion = payload.get("value")
        if not isinstance(assertion, str) or not assertion:
            raise AuthError(
                "GitHub Actions OIDC response omitted the assertion",
                stage="github_oidc",
            )
        self._validate_assertion(assertion)
        return assertion

    def _validate_assertion(self, assertion: str) -> None:
        try:
            encoded = assertion.encode("ascii")
        except UnicodeEncodeError as exc:
            raise AuthError(
                "GitHub Actions OIDC assertion is not ASCII",
                stage="github_oidc",
            ) from exc
        if len(encoded) > self._max_jwt_bytes:
            raise AuthError(
                "GitHub Actions OIDC assertion exceeds the configured limit",
                stage="github_oidc",
            )
        segments = assertion.split(".")
        if len(segments) != 3:
            raise AuthError(
                "GitHub Actions OIDC assertion is not a compact JWT",
                stage="github_oidc",
            )
        for segment in segments:
            if not segment or _JWT_SEGMENT_PATTERN.fullmatch(segment) is None:
                raise AuthError(
                    "GitHub Actions OIDC assertion contains an invalid segment",
                    stage="github_oidc",
                )
        header = _strict_json_object(_decode_segment(segments[0]), stage="github_oidc")
        claims = _strict_json_object(_decode_segment(segments[1]), stage="github_oidc")
        if not _decode_segment(segments[2]):
            raise AuthError(
                "GitHub Actions OIDC assertion contains an empty signature",
                stage="github_oidc",
            )
        if header.get("alg") != "RS256":
            raise AuthError(
                "GitHub Actions OIDC assertion uses an unexpected algorithm",
                stage="github_oidc",
            )
        if header.get("typ") not in (None, "JWT"):
            raise AuthError(
                "GitHub Actions OIDC assertion uses an unexpected type",
                stage="github_oidc",
            )
        expected_claims = {
            "iss": self._config.issuer,
            "aud": self._config.audience,
            "sub": self._config.expected_subject,
            "repository_id": self._config.expected_repository_id,
        }
        for claim_name, expected_value in expected_claims.items():
            if claims.get(claim_name) != expected_value:
                raise AuthError(
                    f"GitHub Actions OIDC claim {claim_name!r} did not match",
                    stage="github_oidc",
                )
        expires_at = _numeric_date(claims.get("exp"), "exp")
        if expires_at <= self._now():
            raise AuthError(
                "GitHub Actions OIDC assertion is expired",
                stage="github_oidc",
            )
        if "nbf" in claims and _numeric_date(claims.get("nbf"), "nbf") > self._now():
            raise AuthError(
                "GitHub Actions OIDC assertion is not yet valid",
                stage="github_oidc",
            )


class GitHubActionsClientAssertionCredential:
    __slots__ = ("_credential", "assertion_provider", "client_id", "tenant_id")

    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        assertion_provider: GitHubActionsOidcAssertionProvider,
        *,
        credential_factory: _CredentialFactory = _default_credential_factory,
    ) -> None:
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.assertion_provider = assertion_provider
        self._credential = credential_factory(tenant_id, client_id, assertion_provider)

    @property
    def credential(self) -> object:
        return self._credential

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}("
            f"tenant_id={self.tenant_id!r}, client_id={self.client_id!r}, "
            "credential=<redacted>)"
        )

    def get_token(self, *scopes: str, **kwargs: object) -> object:
        getter = getattr(self._credential, "get_token", None)
        if not callable(getter):
            raise TypeError("wrapped credential does not define get_token")
        return getter(*scopes, **kwargs)

    def close(self) -> None:
        self.assertion_provider.close()
        closer = getattr(self._credential, "close", None)
        if callable(closer):
            closer()


def build_client_assertion_credential(
    config: GitHubActionsOidcConfig,
    *,
    environment: Mapping[str, str] | None = None,
    transport: httpx.BaseTransport | None = None,
    now: Callable[[], float] = time.time,
    timeout: float = 10.0,
    credential_factory: _CredentialFactory = _default_credential_factory,
) -> GitHubActionsClientAssertionCredential:
    provider = GitHubActionsOidcAssertionProvider(
        config,
        environment=environment,
        transport=transport,
        now=now,
        timeout=timeout,
    )
    return GitHubActionsClientAssertionCredential(
        config.tenant_id,
        config.client_id,
        provider,
        credential_factory=credential_factory,
    )


create_client_assertion_credential = build_client_assertion_credential
GitHubActionsAssertionProvider = GitHubActionsOidcAssertionProvider
ClientAssertionCredentialWrapper = GitHubActionsClientAssertionCredential


def _build_oidc_request_url(
    value: object,
    *,
    audience: str,
    max_bytes: int,
) -> str:
    if not isinstance(value, str) or not value:
        raise AuthError(
            "GitHub Actions OIDC request URL is unavailable",
            stage="github_oidc",
        )
    if len(value.encode("utf-8")) > max_bytes:
        raise AuthError(
            "GitHub Actions OIDC request URL exceeds the configured limit",
            stage="github_oidc",
        )
    if not value.isascii() or any(ord(character) <= 0x20 for character in value):
        raise AuthError(
            "GitHub Actions OIDC request URL is malformed",
            stage="github_oidc",
        )
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise AuthError(
            "GitHub Actions OIDC request URL is malformed",
            stage="github_oidc",
        ) from exc
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not hostname.endswith(".actions.githubusercontent.com")
        or hostname == "actions.githubusercontent.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in (None, 443)
        or not parsed.path
    ):
        raise AuthError(
            "GitHub Actions OIDC request URL violates the GitHub Actions contract",
            stage="github_oidc",
        )
    try:
        query_pairs = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
        )
    except ValueError as exc:
        raise AuthError(
            "GitHub Actions OIDC request URL contains malformed query data",
            stage="github_oidc",
        ) from exc
    seen_audience = [value for name, value in query_pairs if name.casefold() == "audience"]
    if seen_audience and seen_audience != [audience]:
        raise AuthError(
            "GitHub Actions OIDC request URL already targets a different audience",
            stage="github_oidc",
        )
    if not seen_audience:
        query_pairs.append(("audience", audience))
    merged_url = urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query_pairs),
            "",
        )
    )
    if len(merged_url.encode("utf-8")) > max_bytes:
        raise AuthError(
            "GitHub Actions OIDC request URL exceeds the configured limit",
            stage="github_oidc",
        )
    return merged_url


def _decode_segment(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.b64decode(segment + padding, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AuthError(
            "GitHub Actions OIDC assertion contains invalid base64url data",
            stage="github_oidc",
        ) from exc


def _strict_json_object(value: bytes, *, stage: str) -> dict[str, Any]:
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
        raise AuthError(f"{stage} returned malformed JSON", stage=stage) from exc
    if not isinstance(payload, dict):
        raise AuthError(f"{stage} returned a non-object JSON document", stage=stage)
    return payload


def _bounded_bytes(
    chunks: Any,
    *,
    maximum: int,
    stage: str,
) -> bytes:
    total = 0
    parts: list[bytes] = []
    for chunk in chunks:
        total += len(chunk)
        if total > maximum:
            raise AuthError(
                f"{stage} response exceeds the configured limit",
                stage=stage,
            )
        parts.append(chunk)
    return b"".join(parts)


def _declared_length(value: str, *, maximum: int) -> int:
    try:
        result = int(value)
    except ValueError as exc:
        raise AuthError(
            "GitHub Actions OIDC returned an invalid content length",
            stage="github_oidc",
        ) from exc
    if result < 0 or result > maximum:
        raise AuthError(
            "GitHub Actions OIDC response exceeds the configured limit",
            stage="github_oidc",
        )
    return result


def _numeric_date(value: object, claim_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AuthError(
            f"GitHub Actions OIDC claim {claim_name!r} is invalid",
            stage="github_oidc",
        )
    result = float(value)
    if not math.isfinite(result):
        raise AuthError(
            f"GitHub Actions OIDC claim {claim_name!r} is invalid",
            stage="github_oidc",
        )
    return result


def _validated_uuid(value: object, subject: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{subject} must be a UUID string")
    try:
        parsed = uuid.UUID(value)
    except ValueError as exc:
        raise ValueError(f"{subject} must be a valid UUID") from exc
    if parsed.int == 0:
        raise ValueError(f"{subject} must not be the nil UUID")
    return str(parsed)


def _validated_environment_name(value: object, subject: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", value):
        raise ValueError(f"{subject} must be an uppercase environment variable name")
    return value


def _validated_string(value: object, subject: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 0x20 for character in value)
    ):
        raise ValueError(f"{subject} must be a bounded nonempty string")
    return value


def _validated_repository_id(value: object) -> str:
    result = _validated_string(value, "expected_repository_id", 128)
    if not result.isascii() or not result.isdecimal():
        raise ValueError("expected_repository_id must be an ASCII decimal string")
    return result


def _positive_int(value: object, subject: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{subject} must be a positive integer")
    return value


def _safe_bearer_value(value: str) -> bool:
    return value.isascii() and not any(
        character.isspace() or ord(character) < 0x21 or ord(character) == 0x7F
        for character in value
    )
