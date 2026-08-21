"""Minimal GitHub issue-evidence write layer for the optimize-job POC."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Final, Literal

import httpx
from pydantic import (
    Field,
    StrictBool,
    ValidationError,
    field_validator,
    model_validator,
)

from foundry_opt.models import FrozenModel


GITHUB_API_BASE_URL: Final = "https://api.github.com"
GITHUB_HTML_BASE_URL: Final = "https://github.com"

MAX_HTTP_REQUEST_BYTES: Final = 128 * 1024
MAX_HTTP_RESPONSE_BYTES: Final = 256 * 1024
MAX_SOCKET_FRAME_BYTES: Final = 256 * 1024
MAX_BINDING_FILE_BYTES: Final = 32 * 1024
MAX_COMMENT_BODY_BYTES: Final = 64 * 1024
MAX_MARKDOWN_CHARACTERS: Final = 60_000
MAX_TIMEOUT_SECONDS: Final = 30.0
COMMENTS_PER_PAGE: Final = 100
MAX_COMMENT_PAGES: Final = 20
MAX_COMMENT_ITEMS: Final = COMMENTS_PER_PAGE * MAX_COMMENT_PAGES
READ_CHUNK_BYTES: Final = 64 * 1024
DEFAULT_IDLE_TIMEOUT_SECONDS: Final = 5.0
DEFAULT_USER_AGENT: Final = "foundry-opt-poc-github/0.1"

REQUEST_ID_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
REPOSITORY_OWNER_PATTERN: Final = r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$"
REPOSITORY_NAME_PATTERN: Final = r"^[A-Za-z0-9._-]{1,100}$"
GITHUB_LOGIN_PATTERN: Final = (
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,62})(?:\[bot\])?$"
)
JOB_ID_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$"
LOGICAL_KIND_PATTERN: Final = (
    r"^(?:baseline|final|candidate-[A-Za-z0-9](?:[A-Za-z0-9._-]{0,61}[A-Za-z0-9])?)$"
)
BRANCH_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$"
COMMIT_PATTERN: Final = r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"
ERROR_TYPE_PATTERN: Final = r"^[A-Za-z][A-Za-z0-9_]{0,127}$"
TOKEN_PATTERN: Final = r"^[A-Za-z0-9._-]{8,512}$"
PERMISSION_PATTERN: Final = r"^[A-Za-z][A-Za-z_-]{0,31}$"
TOKEN_SHAPE_PATTERN: Final = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9_]{8,}|ghs-[A-Za-z0-9_-]{8,}|github_pat_[A-Za-z0-9_]{20,})"
)

RepositoryOwner = Annotated[
    str,
    Field(
        strict=True,
        min_length=1,
        max_length=39,
        pattern=REPOSITORY_OWNER_PATTERN,
    ),
]
RepositoryName = Annotated[
    str,
    Field(
        strict=True,
        min_length=1,
        max_length=100,
        pattern=REPOSITORY_NAME_PATTERN,
    ),
]
GitHubLogin = Annotated[
    str,
    Field(
        strict=True,
        min_length=1,
        max_length=72,
        pattern=GITHUB_LOGIN_PATTERN,
    ),
]
JobId = Annotated[
    str,
    Field(strict=True, min_length=1, max_length=63, pattern=JOB_ID_PATTERN),
]
LogicalKind = Annotated[
    str,
    Field(
        strict=True,
        min_length=1,
        max_length=80,
        pattern=LOGICAL_KIND_PATTERN,
    ),
]
BranchName = Annotated[
    str,
    Field(strict=True, min_length=1, max_length=255, pattern=BRANCH_PATTERN),
]
CommitSha = Annotated[
    str,
    Field(strict=True, min_length=40, max_length=64, pattern=COMMIT_PATTERN),
]
RequestId = Annotated[
    str,
    Field(
        strict=True,
        min_length=1,
        max_length=64,
        pattern=REQUEST_ID_PATTERN,
    ),
]
Sha256Hex = Annotated[
    str,
    Field(strict=True, min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$"),
]
UrlText = Annotated[str, Field(strict=True, min_length=1, max_length=2048)]
ErrorTypeText = Annotated[
    str,
    Field(
        strict=True,
        min_length=1,
        max_length=128,
        pattern=ERROR_TYPE_PATTERN,
    ),
]
ErrorMessageText = Annotated[str, Field(strict=True, min_length=1, max_length=4096)]
PositiveInt = Annotated[int, Field(strict=True, gt=0, le=2**31 - 1)]
PositiveId = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
TimeoutSeconds = Annotated[float, Field(gt=0.0, le=MAX_TIMEOUT_SECONDS)]
MarkdownText = Annotated[
    str,
    Field(strict=True, min_length=1, max_length=MAX_MARKDOWN_CHARACTERS),
]
GitHubPermissionText = Annotated[
    str,
    Field(strict=True, min_length=1, max_length=32, pattern=PERMISSION_PATTERN),
]


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _redact_text(text: str, *, secrets: tuple[str, ...] = ()) -> str:
    redacted = str(text)
    for secret in secrets:
        if secret:
            redacted = redacted.replace(secret, "******")
    return TOKEN_SHAPE_PATTERN.sub("******", redacted)


def _validation_message(error: ValidationError) -> str:
    issue = error.errors(include_url=False)[0]
    location = ".".join(str(part) for part in issue.get("loc", ()))
    message = str(issue.get("msg", "validation failed"))
    return f"{location}: {message}" if location else message


def _require_string(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise GitHubContractError(f"{field} must be a non-empty string")
    if len(value) > 4096:
        raise GitHubContractError(f"{field} exceeds its bounded length")
    return value


def _require_mapping(value: object, *, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GitHubContractError(f"{field} must be a JSON object")
    return value


def _require_array(value: object, *, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise GitHubContractError(f"{field} must be a JSON array")
    return value


def _require_positive_int(value: object, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise GitHubContractError(f"{field} must be a positive integer")
    return value


def _require_bool(value: object, *, field: str) -> bool:
    if not isinstance(value, bool):
        raise GitHubContractError(f"{field} must be a boolean")
    return value


def _dump_json_bytes(
    value: object,
    *,
    max_bytes: int,
    subject: str,
    error_type: type[Exception],
) -> bytes:
    try:
        text = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as error:
        raise error_type(f"{subject} cannot be encoded as canonical JSON") from error
    encoded = text.encode("ascii")
    if len(encoded) > max_bytes:
        raise error_type(f"{subject} exceeds its byte budget")
    return encoded


def _load_json_value(
    raw: bytes,
    *,
    max_bytes: int,
    subject: str,
    error_type: type[Exception],
    ascii_only: bool,
) -> Any:
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise error_type(f"{subject} is empty")
    if len(raw) > max_bytes:
        raise error_type(f"{subject} exceeds its byte budget")

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise error_type(f"{subject} contains a duplicate JSON key")
            result[key] = value
        return result

    try:
        text = raw.decode("ascii" if ascii_only else "utf-8")
    except UnicodeDecodeError as error:
        encoding = "ASCII" if ascii_only else "UTF-8"
        raise error_type(f"{subject} is not valid {encoding} JSON") from error
    try:
        return json.loads(text, object_pairs_hook=unique_pairs)
    except json.JSONDecodeError as error:
        raise error_type(f"{subject} is not valid JSON") from error


def _load_json_object(
    raw: bytes,
    *,
    max_bytes: int,
    subject: str,
    error_type: type[Exception],
    ascii_only: bool,
) -> dict[str, Any]:
    value = _load_json_value(
        raw,
        max_bytes=max_bytes,
        subject=subject,
        error_type=error_type,
        ascii_only=ascii_only,
    )
    if not isinstance(value, dict):
        raise error_type(f"{subject} must be a JSON object")
    return value


class BrokerOperation(StrEnum):
    COMMENT_UPSERT = "comment.upsert"
    PULL_REQUEST_ENSURE_BINDING = "pull_request.ensure_binding"
    PULL_REQUEST_CLOSE_NO_WINNER = "pull_request.close_no_winner"


class FinalDecision(StrEnum):
    NO_WINNER = "no_winner"
    WINNER = "winner"


class GitHubWriteLayerError(RuntimeError):
    """Base class for the POC GitHub write layer."""

    def __init__(self, message: str, *, secrets: tuple[str, ...] = ()) -> None:
        super().__init__(_redact_text(message, secrets=secrets))


class GitHubPolicyError(GitHubWriteLayerError):
    """Trusted bindings or closed policy were violated."""


class GitHubContractError(GitHubWriteLayerError):
    """GitHub returned a response outside the trusted contract."""


class GitHubTransportError(GitHubWriteLayerError):
    """The GitHub transport failed or returned an unexpected status."""


class TokenLeakageError(GitHubWriteLayerError):
    """An outbound payload would leak token material."""


class BrokerProtocolError(GitHubWriteLayerError):
    """A broker request or response violated the wire protocol."""


class BrokerOperationError(GitHubWriteLayerError):
    """The broker operation is outside the closed policy."""


class BrokerUnavailableError(GitHubWriteLayerError):
    """The broker socket could not be reached safely."""


class BrokerRemoteError(GitHubWriteLayerError):
    """The broker returned a structured failure response."""



class _BearerToken:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise GitHubPolicyError("bearer token must be a string")
        if re.fullmatch(TOKEN_PATTERN, value) is None:
            raise GitHubPolicyError(
                "bearer token must be 8-512 URL-safe characters"
            )
        self._value = value

    @property
    def value(self) -> str:
        return self._value

    def header_value(self) -> str:
        return f"Bearer {self._value}"

    def appears_in(self, text: str) -> bool:
        return self._value in text

    def __repr__(self) -> str:
        return "BearerToken(***)"

    __str__ = __repr__


class RepositoryIdentity(FrozenModel):
    owner: RepositoryOwner
    name: RepositoryName
    repository_id: PositiveId

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def repository_api_url(self) -> str:
        return f"{GITHUB_API_BASE_URL}/repos/{self.full_name}"

    @property
    def repository_html_url(self) -> str:
        return f"{GITHUB_HTML_BASE_URL}/{self.full_name}"

    def issue_api_url(self, issue_number: int) -> str:
        return f"{self.repository_api_url}/issues/{issue_number}"

    def issue_comments_api_url(self, issue_number: int) -> str:
        return f"{self.issue_api_url(issue_number)}/comments"

    def issue_comments_api_path(self, issue_number: int) -> str:
        return f"/repos/{self.full_name}/issues/{issue_number}/comments"

    def issue_comment_api_url(self, comment_id: int) -> str:
        return f"{self.repository_api_url}/issues/comments/{comment_id}"

    def issue_comment_api_path(self, comment_id: int) -> str:
        return f"/repos/{self.full_name}/issues/comments/{comment_id}"

    def issue_comment_html_url(self, issue_number: int, comment_id: int) -> str:
        return (
            f"{self.repository_html_url}/issues/{issue_number}"
            f"#issuecomment-{comment_id}"
        )

    def pull_request_api_url(self, pull_request_number: int) -> str:
        return f"{self.repository_api_url}/pulls/{pull_request_number}"

    @property
    def pull_requests_api_path(self) -> str:
        return f"/repos/{self.full_name}/pulls"

    def pull_request_api_path(self, pull_request_number: int) -> str:
        return f"/repos/{self.full_name}/pulls/{pull_request_number}"

    def pull_request_html_url(self, pull_request_number: int) -> str:
        return f"{self.repository_html_url}/pull/{pull_request_number}"

    def issue_timeline_api_path(self, issue_number: int) -> str:
        return f"/repos/{self.full_name}/issues/{issue_number}/timeline"


class IssueBinding(FrozenModel):
    repository: RepositoryIdentity
    issue_number: PositiveInt
    job_id: JobId
    comment_author_login: GitHubLogin
    issue_author_login: GitHubLogin | None = None
    issue_author_permission: GitHubPermissionText | None = None

    @model_validator(mode="after")
    def validate_issue_author_binding(self) -> "IssueBinding":
        if (self.issue_author_login is None) != (
            self.issue_author_permission is None
        ):
            raise ValueError(
                "issue_author_login and issue_author_permission must both be set or both be omitted"
            )
        return self


class PullRequestBinding(FrozenModel):
    repository: RepositoryIdentity
    issue_number: PositiveInt
    pull_request_number: PositiveInt
    base_branch: BranchName
    head_branch: BranchName
    head_sha: CommitSha | None = None
    expected_author_login: GitHubLogin
    expected_author_type: Literal["Bot", "User", "Organization"] | None = None


class CommentReceipt(FrozenModel):
    repository_id: PositiveId
    issue_number: PositiveInt
    logical_kind: LogicalKind
    marker: Annotated[str, Field(strict=True, min_length=1, max_length=256)]
    comment_id: PositiveId
    api_url: UrlText
    html_url: UrlText
    body_sha256: Sha256Hex
    action: Literal["created", "updated", "unchanged"]


class PullRequestReceipt(FrozenModel):
    repository_id: PositiveId
    issue_number: PositiveInt
    pull_request_number: PositiveInt
    api_url: UrlText
    html_url: UrlText
    state: Literal["closed"]
    merged: Literal[False]
    action: Literal["closed", "unchanged"]


class PullRequestBindingReceipt(FrozenModel):
    repository_id: PositiveId
    issue_number: PositiveInt
    pull_request_number: PositiveInt
    api_url: UrlText
    html_url: UrlText
    base_branch: BranchName
    head_branch: BranchName
    head_sha: CommitSha
    expected_author_login: GitHubLogin
    expected_author_type: Literal["Bot", "User", "Organization"]
    action: Literal["created", "updated", "unchanged"]


class FinalDecisionReceipt(FrozenModel):
    logical_kind: Literal["final"] = "final"
    decision: FinalDecision
    comment_receipt: CommentReceipt

    @model_validator(mode="after")
    def validate_comment_receipt(self) -> FinalDecisionReceipt:
        if self.comment_receipt.logical_kind != "final":
            raise ValueError("final decision receipts require a final comment receipt")
        return self


class BrokerRequest(FrozenModel):
    request_id: RequestId
    operation: BrokerOperation
    timeout_seconds: TimeoutSeconds
    logical_kind: LogicalKind | None = None
    markdown: MarkdownText | None = None
    head_branch: BranchName | None = None
    final_decision_receipt: FinalDecisionReceipt | None = None

    @field_validator("timeout_seconds", mode="before")
    @classmethod
    def validate_timeout_type(cls, value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError("timeout_seconds must be numeric")
        return float(value)

    @model_validator(mode="after")
    def validate_operation_payload(self) -> BrokerRequest:
        if self.operation == BrokerOperation.COMMENT_UPSERT:
            if self.logical_kind is None or self.markdown is None:
                raise ValueError(
                    "comment.upsert requires logical_kind and markdown"
                )
            if self.head_branch is not None:
                raise ValueError("comment.upsert does not accept head_branch")
            if self.final_decision_receipt is not None:
                raise ValueError(
                    "comment.upsert does not accept a final decision receipt"
                )
            return self
        if self.operation == BrokerOperation.PULL_REQUEST_ENSURE_BINDING:
            if self.head_branch is None:
                raise ValueError(
                    "pull_request.ensure_binding requires head_branch"
                )
            if self.logical_kind is not None or self.markdown is not None:
                raise ValueError(
                    "pull_request.ensure_binding accepts only head_branch"
                )
            if self.final_decision_receipt is not None:
                raise ValueError(
                    "pull_request.ensure_binding does not accept a final decision receipt"
                )
            return self
        if (
            self.logical_kind is not None
            or self.markdown is not None
            or self.head_branch is not None
        ):
            raise ValueError(
                "pull_request.close_no_winner accepts only a final decision receipt"
            )
        if self.final_decision_receipt is None:
            raise ValueError(
                "pull_request.close_no_winner requires a final decision receipt"
            )
        return self


class BrokerResponse(FrozenModel):
    request_id: RequestId | None = None
    ok: StrictBool
    comment_receipt: CommentReceipt | None = None
    pull_request_binding_receipt: PullRequestBindingReceipt | None = None
    pull_request_receipt: PullRequestReceipt | None = None
    error_type: ErrorTypeText | None = None
    error_message: ErrorMessageText | None = None

    @model_validator(mode="after")
    def validate_result(self) -> BrokerResponse:
        if self.ok:
            receipts = (
                self.comment_receipt,
                self.pull_request_binding_receipt,
                self.pull_request_receipt,
            )
            if sum(receipt is not None for receipt in receipts) != 1:
                raise ValueError(
                    "successful responses must carry exactly one receipt"
                )
            if self.error_type is not None or self.error_message is not None:
                raise ValueError("successful responses cannot carry error fields")
            return self
        if (
            self.comment_receipt is not None
            or self.pull_request_binding_receipt is not None
            or self.pull_request_receipt is not None
        ):
            raise ValueError("error responses cannot carry receipts")
        if self.error_type is None or self.error_message is None:
            raise ValueError("error responses require error_type and error_message")
        return self


class _BindingDocument(FrozenModel):
    issue: IssueBinding
    pull_request: PullRequestBinding | None = None

    @model_validator(mode="after")
    def validate_consistency(self) -> _BindingDocument:
        if self.pull_request is None:
            return self
        if self.issue.repository != self.pull_request.repository:
            raise ValueError("issue and pull_request bindings must share a repository")
        if self.issue.issue_number != self.pull_request.issue_number:
            raise ValueError("issue and pull_request bindings must share an issue")
        return self


@dataclass(frozen=True, slots=True)
class _VerifiedPullRequest:
    number: int
    api_url: str
    html_url: str
    state: str
    merged: bool
    base_branch: str
    head_branch: str
    head_sha: str
    expected_author_login: str
    expected_author_type: str


def stable_comment_marker(job_id: str, logical_kind: str) -> str:
    if re.fullmatch(JOB_ID_PATTERN, job_id) is None:
        raise GitHubPolicyError("job_id must be a bounded identifier")
    if re.fullmatch(LOGICAL_KIND_PATTERN, logical_kind) is None:
        raise GitHubPolicyError("logical_kind must be baseline, final, or candidate-<id>")
    return f"<!-- foundry-optimize-job:{job_id}:{logical_kind} -->"


def _render_comment_body(*, marker: str, markdown: str) -> str:
    body = f"{marker}\n{markdown}"
    if len(body.encode("utf-8")) > MAX_COMMENT_BODY_BYTES:
        raise GitHubPolicyError("comment body exceeds its byte budget")
    return body


def _read_bounded_file(path: Path, *, max_bytes: int, subject: str) -> bytes:
    if path.is_symlink():
        raise BrokerProtocolError(f"{subject} must not be a symlink")
    try:
        with path.open("rb") as handle:
            data = handle.read(max_bytes + 1)
    except OSError as error:
        raise BrokerProtocolError(f"{subject} could not be read") from error
    if not data:
        raise BrokerProtocolError(f"{subject} is empty")
    if len(data) > max_bytes:
        raise BrokerProtocolError(f"{subject} exceeds its byte budget")
    return data


def _load_binding_document(path: Path) -> _BindingDocument:
    raw = _read_bounded_file(
        path,
        max_bytes=MAX_BINDING_FILE_BYTES,
        subject="trusted binding file",
    )
    payload = _load_json_object(
        raw,
        max_bytes=MAX_BINDING_FILE_BYTES,
        subject="trusted binding file",
        error_type=BrokerProtocolError,
        ascii_only=False,
    )
    try:
        return _BindingDocument.model_validate(payload)
    except ValidationError as error:
        raise BrokerProtocolError(_validation_message(error)) from error


def _write_binding_document(
    path: Path,
    *,
    issue: IssueBinding,
    pull_request: PullRequestBinding | None,
) -> None:
    payload = _dump_json_bytes(
        {
            "issue": issue.model_dump(mode="json"),
            "pull_request": (
                None if pull_request is None else pull_request.model_dump(mode="json")
            ),
        },
        max_bytes=MAX_BINDING_FILE_BYTES,
        subject="trusted binding file",
        error_type=BrokerProtocolError,
    )
    resolved = path.resolve(strict=False)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    temporary = resolved.with_name(
        f".{resolved.name}.{hashlib.sha256(payload).hexdigest()[:12]}.tmp"
    )
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, resolved)
    except OSError as error:
        raise BrokerProtocolError("trusted binding file could not be updated") from error


class GitHubRestBackend:
    """Closed-policy GitHub REST backend for issue comments and PR closure."""

    def __init__(
        self,
        *,
        issue_binding: IssueBinding,
        pull_request_binding: PullRequestBinding | None = None,
        token: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        document = _BindingDocument(
            issue=issue_binding,
            pull_request=pull_request_binding,
        )
        self._issue_binding = document.issue
        self._pull_request_binding = document.pull_request
        self._token = _BearerToken(token)
        self._http = httpx.Client(
            transport=transport,
            base_url=GITHUB_API_BASE_URL,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": self._token.header_value(),
                "User-Agent": user_agent,
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        self._comments_path = self._issue_binding.repository.issue_comments_api_path(
            self._issue_binding.issue_number
        )
        self._pulls_path = self._issue_binding.repository.pull_requests_api_path
        self._issue_timeline_path = (
            self._issue_binding.repository.issue_timeline_api_path(
                self._issue_binding.issue_number
            )
        )
        self._pull_request_path = (
            None
            if self._pull_request_binding is None
            else self._pull_request_binding.repository.pull_request_api_path(
                self._pull_request_binding.pull_request_number
            )
        )
        self._comment_path_pattern = re.compile(
            re.escape(
                f"/repos/{self._issue_binding.repository.full_name}/issues/comments/"
            )
            + r"[1-9][0-9]*$"
        )

    def __enter__(self) -> GitHubRestBackend:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def __repr__(self) -> str:
        pull_request_number = (
            None
            if self._pull_request_binding is None
            else self._pull_request_binding.pull_request_number
        )
        return (
            "GitHubRestBackend("
            f"repository={self._issue_binding.repository.full_name!r},"
            f" issue_number={self._issue_binding.issue_number},"
            f" pull_request_number={pull_request_number})"
        )

    def _assert_no_token_leak(self, text: str) -> None:
        if self._token.appears_in(text):
            raise TokenLeakageError(
                "outbound GitHub content contains bearer token material",
                secrets=(self._token.value,),
            )

    def _allow_request(self, method: str, path: str) -> None:
        normalized_method = method.upper()
        if (
            normalized_method in {"GET", "POST"}
            and path == self._comments_path
        ):
            return
        if normalized_method == "PATCH" and self._comment_path_pattern.fullmatch(path):
            return
        if (
            self._pull_request_path is not None
            and normalized_method in {"GET", "PATCH"}
            and path == self._pull_request_path
        ):
            return
        if normalized_method == "GET" and path in {
            self._pulls_path,
            self._issue_timeline_path,
        }:
            return
        raise GitHubPolicyError(
            "request method or path is outside the closed GitHub policy"
        )

    def _require_pull_request_binding(self) -> PullRequestBinding:
        if self._pull_request_binding is None or self._pull_request_path is None:
            raise GitHubPolicyError(
                "trusted binding file does not yet contain a pull request binding"
            )
        return self._pull_request_binding

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        expected_status: tuple[int, ...],
        timeout_seconds: float,
        params: Mapping[str, object] | None = None,
        json_body: Mapping[str, object] | None = None,
    ) -> Any:
        self._allow_request(method, path)
        payload_bytes: bytes | None = None
        if json_body is not None:
            payload_bytes = _dump_json_bytes(
                json_body,
                max_bytes=MAX_HTTP_REQUEST_BYTES,
                subject="GitHub request body",
                error_type=GitHubPolicyError,
            )
            self._assert_no_token_leak(payload_bytes.decode("ascii"))
        try:
            response = self._http.request(
                method.upper(),
                path,
                params=params,
                content=payload_bytes,
                timeout=timeout_seconds,
            )
        except httpx.TimeoutException as error:
            raise GitHubTransportError("GitHub request timed out") from error
        except httpx.HTTPError as error:
            raise GitHubTransportError(
                f"GitHub transport failed: {error}",
                secrets=(self._token.value,),
            ) from error
        if 300 <= response.status_code <= 399 or response.is_redirect:
            raise GitHubContractError("redirect responses are not allowed")
        if response.status_code not in expected_status:
            raise GitHubTransportError(
                f"GitHub returned unexpected status {response.status_code}"
            )
        body = response.content
        return _load_json_value(
            body,
            max_bytes=MAX_HTTP_RESPONSE_BYTES,
            subject="GitHub response",
            error_type=GitHubContractError,
            ascii_only=False,
        )

    def _verify_repository_payload(
        self,
        payload: object,
        *,
        field: str,
    ) -> None:
        repository = _require_mapping(payload, field=field)
        repository_id = _require_positive_int(
            repository.get("id"),
            field=f"{field}.id",
        )
        if repository_id != self._issue_binding.repository.repository_id:
            raise GitHubPolicyError(
                "repository ID does not match the trusted binding"
            )
        full_name = _require_string(
            repository.get("full_name"),
            field=f"{field}.full_name",
        )
        if full_name != self._issue_binding.repository.full_name:
            raise GitHubPolicyError(
                "repository full_name does not match the trusted binding"
            )
        api_url = _require_string(repository.get("url"), field=f"{field}.url")
        if api_url != self._issue_binding.repository.repository_api_url:
            raise GitHubPolicyError("repository API URL is not the trusted origin")
        html_url = _require_string(
            repository.get("html_url"),
            field=f"{field}.html_url",
        )
        if html_url != self._issue_binding.repository.repository_html_url:
            raise GitHubPolicyError("repository HTML URL is not the trusted origin")

    def _parse_pull_request(
        self,
        payload: object,
        *,
        field: str,
        reject_merged: bool,
        require_open: bool,
        expected_head_branch: str | None = None,
    ) -> _VerifiedPullRequest:
        pull_request = _require_mapping(payload, field=field)
        number = _require_positive_int(
            pull_request.get("number"),
            field=f"{field}.number",
        )
        api_url = _require_string(
            pull_request.get("url"),
            field=f"{field}.url",
        )
        if api_url != self._issue_binding.repository.pull_request_api_url(number):
            raise GitHubPolicyError("pull request API URL is not the trusted origin")
        html_url = _require_string(
            pull_request.get("html_url"),
            field=f"{field}.html_url",
        )
        if html_url != self._issue_binding.repository.pull_request_html_url(number):
            raise GitHubPolicyError("pull request HTML URL is not the trusted origin")
        user = _require_mapping(pull_request.get("user"), field=f"{field}.user")
        login = _require_string(user.get("login"), field=f"{field}.user.login")
        author_type = _require_string(
            user.get("type"),
            field=f"{field}.user.type",
        )
        if author_type not in {"Bot", "User", "Organization"}:
            raise GitHubContractError(
                "pull request.user.type must be Bot, User, or Organization"
            )
        base = _require_mapping(pull_request.get("base"), field=f"{field}.base")
        head = _require_mapping(pull_request.get("head"), field=f"{field}.head")
        self._verify_repository_payload(
            base.get("repo"),
            field=f"{field}.base.repo",
        )
        self._verify_repository_payload(
            head.get("repo"),
            field=f"{field}.head.repo",
        )
        base_ref = _require_string(base.get("ref"), field=f"{field}.base.ref")
        head_ref = _require_string(head.get("ref"), field=f"{field}.head.ref")
        if expected_head_branch is not None and head_ref != expected_head_branch:
            raise GitHubPolicyError(
                "pull request head branch does not match the current checkout branch"
            )
        head_sha = _require_string(head.get("sha"), field=f"{field}.head.sha")
        if re.fullmatch(COMMIT_PATTERN, head_sha) is None:
            raise GitHubContractError("pull request.head.sha must be a commit hash")
        state = _require_string(pull_request.get("state"), field=f"{field}.state")
        merged_raw = pull_request.get("merged")
        if merged_raw is None:
            merged = False
        else:
            merged = _require_bool(merged_raw, field=f"{field}.merged")
        merged_at = pull_request.get("merged_at")
        if merged_at is not None and not isinstance(merged_at, str):
            raise GitHubContractError("pull request.merged_at must be a string or null")
        merged = merged or merged_at is not None
        if require_open and state != "open":
            raise GitHubPolicyError("the exact early pull request is not open")
        if reject_merged and merged:
            raise GitHubPolicyError("the bound pull request is already merged")
        return _VerifiedPullRequest(
            number=number,
            api_url=api_url,
            html_url=html_url,
            state=state,
            merged=merged,
            base_branch=base_ref,
            head_branch=head_ref,
            head_sha=head_sha,
            expected_author_login=login,
            expected_author_type=author_type,
        )

    def _verify_pull_request(
        self,
        *,
        timeout_seconds: float,
        reject_merged: bool,
        require_exact_head: bool = False,
        require_open: bool = False,
    ) -> _VerifiedPullRequest:
        pull_request_binding = self._require_pull_request_binding()
        assert self._pull_request_path is not None
        verified = self._parse_pull_request(
            self._request_json(
                "GET",
                self._pull_request_path,
                expected_status=(200,),
                timeout_seconds=timeout_seconds,
            ),
            field="pull request",
            reject_merged=reject_merged,
            require_open=require_open,
        )
        if verified.number != pull_request_binding.pull_request_number:
            raise GitHubPolicyError(
                "pull request number does not match the trusted binding"
            )
        if (
            verified.expected_author_login.casefold()
            != pull_request_binding.expected_author_login.casefold()
        ):
            raise GitHubPolicyError(
                "pull request author is not the trusted discovered identity"
            )
        if (
            pull_request_binding.expected_author_type is not None
            and verified.expected_author_type != pull_request_binding.expected_author_type
        ):
            raise GitHubPolicyError(
                "pull request author type is not the trusted discovered identity"
            )
        if verified.base_branch != pull_request_binding.base_branch:
            raise GitHubPolicyError(
                "pull request base branch changed from the trusted binding"
            )
        if verified.head_branch != pull_request_binding.head_branch:
            raise GitHubPolicyError(
                "pull request head branch changed from the trusted binding"
            )
        if (
            require_exact_head
            and pull_request_binding.head_sha is not None
            and verified.head_sha != pull_request_binding.head_sha
        ):
            raise GitHubPolicyError(
                "pull request head changed from the trusted binding"
            )
        return verified

    def _list_pull_requests_by_head_branch(
        self,
        *,
        head_branch: str,
        timeout_seconds: float,
    ) -> list[dict[str, Any]]:
        payload = self._request_json(
            "GET",
            self._pulls_path,
            expected_status=(200,),
            timeout_seconds=timeout_seconds,
            params={
                "direction": "asc",
                "head": f"{self._issue_binding.repository.owner}:{head_branch}",
                "per_page": 20,
                "sort": "created",
                "state": "open",
            },
        )
        items = _require_array(payload, field="pull requests")
        if len(items) > 20:
            raise GitHubContractError("pull request discovery exceeded its page budget")
        return [
            _require_mapping(item, field="pull request")
            for item in items
        ]

    def _issue_referenced_pull_request_numbers(
        self,
        *,
        timeout_seconds: float,
    ) -> set[int]:
        payload = self._request_json(
            "GET",
            self._issue_timeline_path,
            expected_status=(200,),
            timeout_seconds=timeout_seconds,
            params={"per_page": 100},
        )
        items = _require_array(payload, field="issue timeline")
        if len(items) > 100:
            raise GitHubContractError("issue timeline exceeded its page budget")
        referenced: set[int] = set()
        for item in items:
            entry = _require_mapping(item, field="issue timeline item")
            source = entry.get("source")
            if source is None:
                continue
            source_mapping = _require_mapping(source, field="issue timeline item.source")
            source_issue = source_mapping.get("issue")
            if source_issue is None:
                continue
            issue = _require_mapping(
                source_issue,
                field="issue timeline item.source.issue",
            )
            pull_request = issue.get("pull_request")
            if pull_request is None:
                continue
            pull_request_mapping = _require_mapping(
                pull_request,
                field="issue timeline item.source.issue.pull_request",
            )
            number = _require_positive_int(
                issue.get("number"),
                field="issue timeline item.source.issue.number",
            )
            api_url = pull_request_mapping.get("url")
            if isinstance(api_url, str):
                if api_url == self._issue_binding.repository.pull_request_api_url(number):
                    referenced.add(number)
                continue
            html_url = pull_request_mapping.get("html_url")
            if (
                isinstance(html_url, str)
                and html_url
                == self._issue_binding.repository.pull_request_html_url(number)
            ):
                referenced.add(number)
        return referenced

    def ensure_pull_request_binding(
        self,
        *,
        head_branch: str,
        timeout_seconds: float,
    ) -> tuple[PullRequestBinding, PullRequestBindingReceipt]:
        if re.fullmatch(BRANCH_PATTERN, head_branch) is None:
            raise GitHubPolicyError("head_branch must be a bounded branch name")
        existing = self._pull_request_binding
        if (
            existing is not None
            and existing.head_sha is not None
            and existing.expected_author_type is not None
        ):
            if existing.head_branch != head_branch:
                raise GitHubPolicyError(
                    "current head branch does not match the trusted pull request binding"
                )
            verified = self._verify_pull_request(
                timeout_seconds=timeout_seconds,
                reject_merged=True,
                require_open=True,
            )
            binding = existing.model_copy(update={"head_sha": verified.head_sha})
        else:
            referenced_numbers = self._issue_referenced_pull_request_numbers(
                timeout_seconds=timeout_seconds,
            )
            candidates = [
                self._parse_pull_request(
                    payload,
                    field="pull request",
                    reject_merged=True,
                    require_open=True,
                    expected_head_branch=head_branch,
                )
                for payload in self._list_pull_requests_by_head_branch(
                    head_branch=head_branch,
                    timeout_seconds=timeout_seconds,
                )
            ]
            matches = [
                candidate
                for candidate in candidates
                if candidate.number in referenced_numbers
            ]
            if not matches:
                raise GitHubPolicyError(
                    "no exact early same-repository pull request matches the trusted issue and current head branch"
                )
            if len(matches) > 1:
                raise GitHubPolicyError(
                    "more than one same-repository pull request matches the trusted issue and current head branch"
                )
            verified = matches[0]
            binding = PullRequestBinding(
                repository=self._issue_binding.repository,
                issue_number=self._issue_binding.issue_number,
                pull_request_number=verified.number,
                base_branch=verified.base_branch,
                head_branch=verified.head_branch,
                head_sha=verified.head_sha,
                expected_author_login=verified.expected_author_login,
                expected_author_type=verified.expected_author_type,
            )
            if existing is not None and existing.head_sha is not None:
                raise GitHubPolicyError(
                    "discovered pull request does not match the trusted binding"
                )
        action: Literal["created", "updated", "unchanged"]
        if existing is None:
            action = "created"
        elif existing == binding:
            action = "unchanged"
        else:
            action = "updated"
        return (
            binding,
            PullRequestBindingReceipt(
                repository_id=self._issue_binding.repository.repository_id,
                issue_number=self._issue_binding.issue_number,
                pull_request_number=binding.pull_request_number,
                api_url=self._issue_binding.repository.pull_request_api_url(
                    binding.pull_request_number
                ),
                html_url=self._issue_binding.repository.pull_request_html_url(
                    binding.pull_request_number
                ),
                base_branch=binding.base_branch,
                head_branch=binding.head_branch,
                head_sha=binding.head_sha,
                expected_author_login=binding.expected_author_login,
                expected_author_type=binding.expected_author_type,
                action=action,
            ),
        )

    def _list_issue_comments(self, *, timeout_seconds: float) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        page = 1
        while page <= MAX_COMMENT_PAGES:
            payload = self._request_json(
                "GET",
                self._comments_path,
                expected_status=(200,),
                timeout_seconds=timeout_seconds,
                params={"per_page": COMMENTS_PER_PAGE, "page": page},
            )
            page_items = _require_array(payload, field="issue comments")
            if len(page_items) > COMMENTS_PER_PAGE:
                raise GitHubContractError(
                    "GitHub returned too many issue comments in one page"
                )
            for item in page_items:
                comment = _require_mapping(item, field="issue comment")
                comments.append(comment)
                if len(comments) > MAX_COMMENT_ITEMS:
                    raise GitHubContractError("issue comments exceed the page budget")
            if len(page_items) < COMMENTS_PER_PAGE:
                return comments
            page += 1
        raise GitHubContractError("issue comments exceed the page budget")

    def _comment_receipt(
        self,
        comment: Mapping[str, Any],
        *,
        logical_kind: str,
        marker: str,
        action: Literal["created", "updated", "unchanged"],
        expected_body: str,
    ) -> CommentReceipt:
        comment_id = _require_positive_int(comment.get("id"), field="comment.id")
        api_url = _require_string(comment.get("url"), field="comment.url")
        expected_api_url = self._issue_binding.repository.issue_comment_api_url(comment_id)
        if api_url != expected_api_url:
            raise GitHubPolicyError("comment API URL is not the trusted origin")
        html_url = _require_string(comment.get("html_url"), field="comment.html_url")
        expected_html_url = self._issue_binding.repository.issue_comment_html_url(
            self._issue_binding.issue_number,
            comment_id,
        )
        if html_url != expected_html_url:
            raise GitHubPolicyError("comment HTML URL is not the trusted origin")
        issue_url = _require_string(comment.get("issue_url"), field="comment.issue_url")
        expected_issue_url = self._issue_binding.repository.issue_api_url(
            self._issue_binding.issue_number
        )
        if issue_url != expected_issue_url:
            raise GitHubPolicyError("comment issue URL is not the trusted issue")
        body = _require_string(comment.get("body"), field="comment.body")
        if body != expected_body:
            raise GitHubPolicyError("GitHub did not persist the exact owned comment")
        if marker not in body:
            raise GitHubPolicyError("owned comment marker is missing from the body")
        return CommentReceipt(
            repository_id=self._issue_binding.repository.repository_id,
            issue_number=self._issue_binding.issue_number,
            logical_kind=logical_kind,
            marker=marker,
            comment_id=comment_id,
            api_url=api_url,
            html_url=html_url,
            body_sha256=_sha256_text(body),
            action=action,
        )

    def upsert_issue_comment(
        self,
        *,
        logical_kind: str,
        markdown: str,
        timeout_seconds: float,
    ) -> CommentReceipt:
        try:
            request = BrokerRequest(
                request_id="local-comment-upsert",
                operation=BrokerOperation.COMMENT_UPSERT,
                timeout_seconds=timeout_seconds,
                logical_kind=logical_kind,
                markdown=markdown,
            )
        except ValidationError as error:
            for issue in error.errors(include_url=False):
                if tuple(issue.get("loc", ())) == ("markdown",):
                    raise GitHubPolicyError(
                        "comment body exceeds its byte budget"
                    ) from error
            raise GitHubPolicyError(_validation_message(error)) from error
        assert request.logical_kind is not None
        assert request.markdown is not None
        marker = stable_comment_marker(
            self._issue_binding.job_id,
            request.logical_kind,
        )
        desired_body = _render_comment_body(marker=marker, markdown=request.markdown)
        self._assert_no_token_leak(desired_body)
        if self._pull_request_binding is not None:
            self._verify_pull_request(
                timeout_seconds=request.timeout_seconds,
                reject_merged=True,
            )
        comments = self._list_issue_comments(timeout_seconds=request.timeout_seconds)
        owned_comment: dict[str, Any] | None = None
        for comment in comments:
            body = comment.get("body")
            if not isinstance(body, str) or marker not in body:
                continue
            user = _require_mapping(comment.get("user"), field="comment.user")
            login = _require_string(user.get("login"), field="comment.user.login")
            if login.casefold() != self._issue_binding.comment_author_login.casefold():
                raise GitHubPolicyError(
                    "matching marker found on a foreign issue comment"
                )
            if owned_comment is not None:
                raise GitHubPolicyError(
                    "more than one owned issue comment carries the same marker"
                )
            owned_comment = comment
        if owned_comment is None:
            created = self._request_json(
                "POST",
                self._comments_path,
                expected_status=(200, 201),
                timeout_seconds=request.timeout_seconds,
                json_body={"body": desired_body},
            )
            return self._comment_receipt(
                _require_mapping(created, field="created comment"),
                logical_kind=request.logical_kind,
                marker=marker,
                action="created",
                expected_body=desired_body,
            )
        existing_body = _require_string(owned_comment.get("body"), field="comment.body")
        if existing_body == desired_body:
            return self._comment_receipt(
                owned_comment,
                logical_kind=request.logical_kind,
                marker=marker,
                action="unchanged",
                expected_body=desired_body,
            )
        comment_id = _require_positive_int(owned_comment.get("id"), field="comment.id")
        updated = self._request_json(
            "PATCH",
            self._issue_binding.repository.issue_comment_api_path(comment_id),
            expected_status=(200,),
            timeout_seconds=request.timeout_seconds,
            json_body={"body": desired_body},
        )
        return self._comment_receipt(
            _require_mapping(updated, field="updated comment"),
            logical_kind=request.logical_kind,
            marker=marker,
            action="updated",
            expected_body=desired_body,
        )

    def close_pull_request_no_winner(
        self,
        *,
        final_decision_receipt: FinalDecisionReceipt,
        timeout_seconds: float,
    ) -> PullRequestReceipt:
        try:
            receipt = FinalDecisionReceipt.model_validate(final_decision_receipt)
            request = BrokerRequest(
                request_id="local-pr-close",
                operation=BrokerOperation.PULL_REQUEST_CLOSE_NO_WINNER,
                timeout_seconds=timeout_seconds,
                final_decision_receipt=receipt,
            )
        except ValidationError as error:
            raise GitHubPolicyError(_validation_message(error)) from error
        pull_request_binding = self._require_pull_request_binding()
        assert request.final_decision_receipt is not None
        receipt = request.final_decision_receipt
        if receipt.decision != FinalDecision.NO_WINNER:
            raise GitHubPolicyError("winner closure requests are not allowed")
        expected_marker = stable_comment_marker(self._issue_binding.job_id, "final")
        if receipt.comment_receipt.repository_id != self._issue_binding.repository.repository_id:
            raise GitHubPolicyError(
                "final decision receipt repository_id does not match the binding"
            )
        if receipt.comment_receipt.issue_number != self._issue_binding.issue_number:
            raise GitHubPolicyError(
                "final decision receipt issue_number does not match the binding"
            )
        if receipt.comment_receipt.marker != expected_marker:
            raise GitHubPolicyError(
                "final decision receipt marker does not match the binding"
            )
        verified = self._verify_pull_request(
            timeout_seconds=timeout_seconds,
            reject_merged=True,
        )
        if verified.state == "closed":
            return PullRequestReceipt(
                repository_id=self._issue_binding.repository.repository_id,
                issue_number=self._issue_binding.issue_number,
                pull_request_number=pull_request_binding.pull_request_number,
                api_url=verified.api_url,
                html_url=verified.html_url,
                state="closed",
                merged=False,
                action="unchanged",
            )
        updated = self._request_json(
            "PATCH",
            self._pull_request_path,
            expected_status=(200,),
            timeout_seconds=timeout_seconds,
            json_body={"state": "closed"},
        )
        pull_request = _require_mapping(updated, field="updated pull request")
        closed = self._verify_pull_request(
            timeout_seconds=timeout_seconds,
            reject_merged=True,
        )
        state = _require_string(pull_request.get("state"), field="pull request.state")
        if state != "closed" or closed.state != "closed":
            raise GitHubPolicyError("GitHub did not close the exact bound pull request")
        return PullRequestReceipt(
            repository_id=self._issue_binding.repository.repository_id,
            issue_number=self._issue_binding.issue_number,
            pull_request_number=pull_request_binding.pull_request_number,
            api_url=closed.api_url,
            html_url=closed.html_url,
            state="closed",
            merged=False,
            action="closed",
        )


def _require_private_socket_parent(parent: Path) -> None:
    if parent.is_symlink():
        raise BrokerUnavailableError("broker socket parent must not be a symlink")
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as error:
        raise BrokerUnavailableError(
            "broker socket parent could not be prepared"
        ) from error
    if os.name != "nt":
        mode = parent.stat().st_mode & 0o777
        if mode & 0o077:
            raise BrokerUnavailableError("broker socket parent must be private")


def _write_socket_frame(
    connection: socket.socket,
    payload: bytes,
    *,
    timeout_seconds: float,
) -> None:
    if len(payload) > MAX_SOCKET_FRAME_BYTES:
        raise BrokerProtocolError("socket frame exceeds its byte budget")
    connection.settimeout(timeout_seconds)
    try:
        connection.sendall(payload + b"\n")
    except OSError as error:
        raise BrokerUnavailableError("broker socket write failed") from error


def _read_socket_frame(
    connection: socket.socket,
    *,
    timeout_seconds: float,
) -> bytes:
    buffer = bytearray()
    connection.settimeout(timeout_seconds)
    while True:
        try:
            chunk = connection.recv(READ_CHUNK_BYTES)
        except OSError as error:
            raise BrokerUnavailableError("broker socket read failed") from error
        if not chunk:
            raise BrokerProtocolError(
                "broker socket closed before a complete JSON frame arrived"
            )
        buffer.extend(chunk)
        if len(buffer) > MAX_SOCKET_FRAME_BYTES:
            raise BrokerProtocolError("socket frame exceeds its byte budget")
        newline_index = buffer.find(b"\n")
        if newline_index < 0:
            continue
        if newline_index != len(buffer) - 1:
            raise BrokerProtocolError("socket frame contains trailing data")
        return bytes(buffer[:newline_index])


class UnixSocketBrokerServer:
    """Unix-domain-socket broker that keeps the GitHub token in-process only."""

    def __init__(
        self,
        *,
        socket_path: Path,
        binding_path: Path,
        token: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if os.name == "nt" or not hasattr(socket, "AF_UNIX"):
            raise BrokerUnavailableError("Unix socket brokers require Linux")
        self._socket_path = socket_path
        self._binding_path = binding_path
        self._token = _BearerToken(token)
        self._transport = transport
        _require_private_socket_parent(socket_path.parent)
        if socket_path.exists() or socket_path.is_symlink():
            raise BrokerUnavailableError("broker socket path is already occupied")
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        previous_umask = os.umask(0o177)
        try:
            self._listener.bind(str(socket_path))
        except OSError as error:
            self._listener.close()
            raise BrokerUnavailableError("broker socket could not be bound") from error
        finally:
            os.umask(previous_umask)
        os.chmod(socket_path, 0o600)
        self._listener.listen(8)

    @property
    def socket_path(self) -> Path:
        return self._socket_path

    def close(self) -> None:
        try:
            self._listener.close()
        finally:
            try:
                self._socket_path.unlink()
            except FileNotFoundError:
                pass

    def __repr__(self) -> str:
        return (
            "UnixSocketBrokerServer("
            f"socket_path={str(self._socket_path)!r},"
            f" binding_path={str(self._binding_path)!r})"
        )

    def _error_response(
        self,
        *,
        request_id: str | None,
        error: Exception,
    ) -> BrokerResponse:
        return BrokerResponse(
            request_id=request_id,
            ok=False,
            error_type=type(error).__name__,
            error_message=_redact_text(str(error), secrets=(self._token.value,)),
        )

    def _dispatch_bytes(self, raw: bytes) -> bytes:
        request_id: str | None = None
        try:
            payload = _load_json_object(
                raw,
                max_bytes=MAX_SOCKET_FRAME_BYTES,
                subject="broker request",
                error_type=BrokerProtocolError,
                ascii_only=True,
            )
            candidate_request_id = payload.get("request_id")
            if (
                isinstance(candidate_request_id, str)
                and re.fullmatch(REQUEST_ID_PATTERN, candidate_request_id) is not None
            ):
                request_id = candidate_request_id
            operation = payload.get("operation")
            if not isinstance(operation, str):
                raise BrokerProtocolError("operation must be a string")
            try:
                BrokerOperation(operation)
            except ValueError as error:
                raise BrokerOperationError(
                    f"unsupported broker operation: {operation}"
                ) from error
            try:
                request = BrokerRequest.model_validate(payload)
            except ValidationError as error:
                raise BrokerProtocolError(_validation_message(error)) from error
            binding = _load_binding_document(self._binding_path)
            with GitHubRestBackend(
                issue_binding=binding.issue,
                pull_request_binding=binding.pull_request,
                token=self._token.value,
                transport=self._transport,
            ) as backend:
                if request.operation == BrokerOperation.COMMENT_UPSERT:
                    assert request.logical_kind is not None
                    assert request.markdown is not None
                    comment_receipt = backend.upsert_issue_comment(
                        logical_kind=request.logical_kind,
                        markdown=request.markdown,
                        timeout_seconds=request.timeout_seconds,
                    )
                    response = BrokerResponse(
                        request_id=request.request_id,
                        ok=True,
                        comment_receipt=comment_receipt,
                    )
                elif request.operation == BrokerOperation.PULL_REQUEST_ENSURE_BINDING:
                    assert request.head_branch is not None
                    pull_request_binding, binding_receipt = (
                        backend.ensure_pull_request_binding(
                            head_branch=request.head_branch,
                            timeout_seconds=request.timeout_seconds,
                        )
                    )
                    _write_binding_document(
                        self._binding_path,
                        issue=binding.issue,
                        pull_request=pull_request_binding,
                    )
                    response = BrokerResponse(
                        request_id=request.request_id,
                        ok=True,
                        pull_request_binding_receipt=binding_receipt,
                    )
                elif request.operation == BrokerOperation.PULL_REQUEST_CLOSE_NO_WINNER:
                    assert request.final_decision_receipt is not None
                    pull_request_receipt = backend.close_pull_request_no_winner(
                        final_decision_receipt=request.final_decision_receipt,
                        timeout_seconds=request.timeout_seconds,
                    )
                    response = BrokerResponse(
                        request_id=request.request_id,
                        ok=True,
                        pull_request_receipt=pull_request_receipt,
                    )
                else:
                    raise BrokerOperationError(
                        f"unsupported broker operation: {request.operation}"
                    )
        except (
            BrokerProtocolError,
            BrokerOperationError,
            GitHubWriteLayerError,
        ) as error:
            response = self._error_response(request_id=request_id, error=error)
        return _dump_json_bytes(
            response.model_dump(mode="json", exclude_none=True),
            max_bytes=MAX_SOCKET_FRAME_BYTES,
            subject="broker response",
            error_type=BrokerProtocolError,
        )

    def serve_once(self, *, timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS) -> None:
        self._listener.settimeout(timeout_seconds)
        connection, _ = self._listener.accept()
        try:
            raw = _read_socket_frame(connection, timeout_seconds=timeout_seconds)
            response = self._dispatch_bytes(raw)
            _write_socket_frame(
                connection,
                response,
                timeout_seconds=timeout_seconds,
            )
        finally:
            connection.close()

    def serve_forever(
        self,
        *,
        idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
        max_requests: int | None = None,
    ) -> None:
        served = 0
        while max_requests is None or served < max_requests:
            self._listener.settimeout(idle_timeout_seconds)
            try:
                connection, _ = self._listener.accept()
            except socket.timeout:
                return
            try:
                raw = _read_socket_frame(
                    connection,
                    timeout_seconds=idle_timeout_seconds,
                )
                response = self._dispatch_bytes(raw)
                _write_socket_frame(
                    connection,
                    response,
                    timeout_seconds=idle_timeout_seconds,
                )
            finally:
                connection.close()
            served += 1


class UnixSocketBrokerClient:
    """Client for the Linux Unix-domain-socket GitHub broker."""

    def __init__(self, *, socket_path: Path) -> None:
        self._socket_path = socket_path

    def __repr__(self) -> str:
        return f"UnixSocketBrokerClient(socket_path={str(self._socket_path)!r})"

    def _connect(self, *, timeout_seconds: float) -> socket.socket:
        if os.name == "nt" or not hasattr(socket, "AF_UNIX"):
            raise BrokerUnavailableError("Unix socket brokers require Linux")
        try:
            info = self._socket_path.lstat()
        except OSError as error:
            raise BrokerUnavailableError("broker socket is not available") from error
        if stat.S_ISLNK(info.st_mode):
            raise BrokerUnavailableError("broker socket path must not be a symlink")
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(timeout_seconds)
        try:
            connection.connect(str(self._socket_path))
        except OSError as error:
            connection.close()
            raise BrokerUnavailableError("broker socket is not reachable") from error
        return connection

    def send(self, request: BrokerRequest) -> BrokerResponse:
        payload = _dump_json_bytes(
            request.model_dump(mode="json", exclude_none=True),
            max_bytes=MAX_SOCKET_FRAME_BYTES,
            subject="broker request",
            error_type=BrokerProtocolError,
        )
        connection = self._connect(timeout_seconds=request.timeout_seconds)
        try:
            _write_socket_frame(
                connection,
                payload,
                timeout_seconds=request.timeout_seconds,
            )
            raw = _read_socket_frame(
                connection,
                timeout_seconds=request.timeout_seconds,
            )
        finally:
            connection.close()
        response_payload = _load_json_object(
            raw,
            max_bytes=MAX_SOCKET_FRAME_BYTES,
            subject="broker response",
            error_type=BrokerProtocolError,
            ascii_only=True,
        )
        try:
            response = BrokerResponse.model_validate(response_payload)
        except ValidationError as error:
            raise BrokerProtocolError(_validation_message(error)) from error
        if response.request_id != request.request_id:
            raise BrokerProtocolError(
                "broker response request_id does not match the request"
            )
        return response

    def upsert_comment(
        self,
        *,
        request_id: str,
        logical_kind: str,
        markdown: str,
        timeout_seconds: float,
    ) -> CommentReceipt:
        response = self.send(
            BrokerRequest(
                request_id=request_id,
                operation=BrokerOperation.COMMENT_UPSERT,
                timeout_seconds=timeout_seconds,
                logical_kind=logical_kind,
                markdown=markdown,
            )
        )
        if not response.ok or response.comment_receipt is None:
            raise BrokerRemoteError(
                f"{response.error_type}: {response.error_message}"
            )
        return response.comment_receipt

    def ensure_pull_request_binding(
        self,
        *,
        request_id: str,
        head_branch: str,
        timeout_seconds: float,
    ) -> PullRequestBindingReceipt:
        response = self.send(
            BrokerRequest(
                request_id=request_id,
                operation=BrokerOperation.PULL_REQUEST_ENSURE_BINDING,
                timeout_seconds=timeout_seconds,
                head_branch=head_branch,
            )
        )
        if not response.ok or response.pull_request_binding_receipt is None:
            raise BrokerRemoteError(
                f"{response.error_type}: {response.error_message}"
            )
        return response.pull_request_binding_receipt

    def close_no_winner(
        self,
        *,
        request_id: str,
        final_decision_receipt: FinalDecisionReceipt,
        timeout_seconds: float,
    ) -> PullRequestReceipt:
        response = self.send(
            BrokerRequest(
                request_id=request_id,
                operation=BrokerOperation.PULL_REQUEST_CLOSE_NO_WINNER,
                timeout_seconds=timeout_seconds,
                final_decision_receipt=final_decision_receipt,
            )
        )
        if not response.ok or response.pull_request_receipt is None:
            raise BrokerRemoteError(
                f"{response.error_type}: {response.error_message}"
            )
        return response.pull_request_receipt


__all__ = [
    "BrokerOperation",
    "BrokerOperationError",
    "BrokerProtocolError",
    "BrokerRemoteError",
    "BrokerRequest",
    "BrokerResponse",
    "BrokerUnavailableError",
    "CommentReceipt",
    "FinalDecision",
    "FinalDecisionReceipt",
    "GitHubContractError",
    "GitHubPolicyError",
    "GitHubRestBackend",
    "GitHubTransportError",
    "GitHubWriteLayerError",
    "IssueBinding",
    "PullRequestBinding",
    "PullRequestBindingReceipt",
    "PullRequestReceipt",
    "RepositoryIdentity",
    "TokenLeakageError",
    "UnixSocketBrokerClient",
    "UnixSocketBrokerServer",
    "stable_comment_marker",
]
