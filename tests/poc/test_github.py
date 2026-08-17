from __future__ import annotations

import copy
import hashlib
import json
import os
import socket
import stat
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from foundry_opt.poc import github


TOKEN = "ghs-test-token-1234567890"
PULL_REQUEST_HEAD_SHA = "c" * 40
REPOSITORY = github.RepositoryIdentity(
    owner="contoso",
    name="travel-agent",
    repository_id=918273645,
)
ISSUE_BINDING = github.IssueBinding(
    repository=REPOSITORY,
    issue_number=41,
    job_id="optimize-alpha",
    comment_author_login="github-actions[bot]",
)
PULL_REQUEST_BINDING = github.PullRequestBinding(
    repository=REPOSITORY,
    issue_number=41,
    pull_request_number=7,
    base_branch="main",
    head_branch="copilot/optimize-alpha",
    head_sha=PULL_REQUEST_HEAD_SHA,
    expected_author_login="copilot-swe-agent[bot]",
    expected_author_type="Bot",
)
COMMENTS_PATH = REPOSITORY.issue_comments_api_path(ISSUE_BINDING.issue_number)
PULL_REQUESTS_PATH = REPOSITORY.pull_requests_api_path
ISSUE_TIMELINE_PATH = REPOSITORY.issue_timeline_api_path(ISSUE_BINDING.issue_number)
PULL_REQUEST_PATH = REPOSITORY.pull_request_api_path(
    PULL_REQUEST_BINDING.pull_request_number
)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _final_comment_receipt(
    *,
    action: str = "unchanged",
    marker: str | None = None,
    repository_id: int = REPOSITORY.repository_id,
    issue_number: int = ISSUE_BINDING.issue_number,
) -> github.CommentReceipt:
    final_marker = marker or github.stable_comment_marker(ISSUE_BINDING.job_id, "final")
    body = f"{final_marker}\nno winner"
    return github.CommentReceipt(
        repository_id=repository_id,
        issue_number=issue_number,
        logical_kind="final",
        marker=final_marker,
        comment_id=999,
        api_url=REPOSITORY.issue_comment_api_url(999),
        html_url=REPOSITORY.issue_comment_html_url(issue_number, 999),
        body_sha256=_sha256_text(body),
        action=action,
    )


def _binding_path(tmp_path: Path) -> Path:
    path = tmp_path / "binding.json"
    _write_binding(path)
    return path


def _write_binding(path: Path, *, pull_request_mode: str = "full") -> None:
    payload: dict[str, Any] = {"issue": ISSUE_BINDING.model_dump(mode="json")}
    if pull_request_mode == "full":
        payload["pull_request"] = PULL_REQUEST_BINDING.model_dump(mode="json")
    elif pull_request_mode == "null":
        payload["pull_request"] = None
    elif pull_request_mode != "omitted":
        raise AssertionError(f"unexpected pull_request_mode: {pull_request_mode}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True),
        encoding="ascii",
    )


def _replace_binding(path: Path, *, pull_request_mode: str) -> None:
    temp_path = path.with_name(f"{path.name}.next")
    _write_binding(temp_path, pull_request_mode=pull_request_mode)
    temp_path.replace(path)


class FakeGitHubAPI:
    def __init__(self) -> None:
        self.comments: list[dict[str, Any]] = []
        self.next_comment_id = 1000
        self.pr_state = "open"
        self.pr_merged = False
        self.pr_head_sha = PULL_REQUEST_HEAD_SHA
        self.requests: list[tuple[str, str]] = []
        self.authorization_headers: list[str | None] = []
        self.pr_payload_transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None
        self.redirect_target: dict[tuple[str, str], str] = {}
        self.timeline_pull_request_numbers: list[int] = [
            PULL_REQUEST_BINDING.pull_request_number
        ]

    def comment(self, *, body: str, author_login: str) -> dict[str, Any]:
        comment_id = self.next_comment_id
        self.next_comment_id += 1
        return {
            "id": comment_id,
            "url": REPOSITORY.issue_comment_api_url(comment_id),
            "html_url": REPOSITORY.issue_comment_html_url(
                ISSUE_BINDING.issue_number,
                comment_id,
            ),
            "issue_url": REPOSITORY.issue_api_url(ISSUE_BINDING.issue_number),
            "body": body,
            "user": {"login": author_login},
        }

    def _pull_request_payload(self) -> dict[str, Any]:
        payload = {
            "number": PULL_REQUEST_BINDING.pull_request_number,
            "url": REPOSITORY.pull_request_api_url(
                PULL_REQUEST_BINDING.pull_request_number
            ),
            "html_url": REPOSITORY.pull_request_html_url(
                PULL_REQUEST_BINDING.pull_request_number
            ),
            "state": self.pr_state,
            "merged": self.pr_merged,
            "merged_at": "2026-08-13T00:00:00Z" if self.pr_merged else None,
            "user": {
                "login": PULL_REQUEST_BINDING.expected_author_login,
                "type": PULL_REQUEST_BINDING.expected_author_type,
            },
            "base": {
                "ref": PULL_REQUEST_BINDING.base_branch,
                "repo": {
                    "id": REPOSITORY.repository_id,
                    "full_name": REPOSITORY.full_name,
                    "url": REPOSITORY.repository_api_url,
                    "html_url": REPOSITORY.repository_html_url,
                },
            },
            "head": {
                "ref": PULL_REQUEST_BINDING.head_branch,
                "sha": self.pr_head_sha,
                "repo": {
                    "id": REPOSITORY.repository_id,
                    "full_name": REPOSITORY.full_name,
                    "url": REPOSITORY.repository_api_url,
                    "html_url": REPOSITORY.repository_html_url,
                },
            },
        }
        if self.pr_payload_transform is not None:
            payload = self.pr_payload_transform(copy.deepcopy(payload))
        return payload

    def transport(self) -> httpx.MockTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            key = (request.method, request.url.path)
            self.requests.append(key)
            self.authorization_headers.append(request.headers.get("Authorization"))
            if key in self.redirect_target:
                return httpx.Response(
                    302,
                    headers={"Location": self.redirect_target[key]},
                    request=request,
                )
            comments_path = REPOSITORY.issue_comments_api_path(ISSUE_BINDING.issue_number)
            pull_requests_path = REPOSITORY.pull_requests_api_path
            issue_timeline_path = REPOSITORY.issue_timeline_api_path(
                ISSUE_BINDING.issue_number
            )
            pull_request_path = REPOSITORY.pull_request_api_path(
                PULL_REQUEST_BINDING.pull_request_number
            )
            if request.method == "GET" and request.url.path == comments_path:
                page = int(request.url.params.get("page", "1"))
                per_page = int(request.url.params.get("per_page", str(github.COMMENTS_PER_PAGE)))
                start = (page - 1) * per_page
                stop = start + per_page
                return httpx.Response(
                    200,
                    json=self.comments[start:stop],
                    request=request,
                )
            if request.method == "POST" and request.url.path == comments_path:
                payload = json.loads(request.content.decode("utf-8"))
                comment = self.comment(
                    body=payload["body"],
                    author_login=ISSUE_BINDING.comment_author_login,
                )
                self.comments.append(comment)
                return httpx.Response(201, json=comment, request=request)
            if request.method == "PATCH" and request.url.path.startswith(
                f"/repos/{REPOSITORY.full_name}/issues/comments/"
            ):
                payload = json.loads(request.content.decode("utf-8"))
                comment_id = int(request.url.path.rsplit("/", 1)[1])
                for index, comment in enumerate(self.comments):
                    if comment["id"] == comment_id:
                        updated = dict(comment)
                        updated["body"] = payload["body"]
                        self.comments[index] = updated
                        return httpx.Response(200, json=updated, request=request)
                return httpx.Response(404, json={"message": "not found"}, request=request)
            if request.method == "GET" and request.url.path == pull_request_path:
                return httpx.Response(
                    200,
                    json=self._pull_request_payload(),
                    request=request,
                )
            if request.method == "GET" and request.url.path == pull_requests_path:
                head = request.url.params.get("head")
                state = request.url.params.get("state")
                if (
                    head == f"{REPOSITORY.owner}:{PULL_REQUEST_BINDING.head_branch}"
                    and state == "open"
                    and self.pr_state == "open"
                    and not self.pr_merged
                ):
                    return httpx.Response(
                        200,
                        json=[self._pull_request_payload()],
                        request=request,
                    )
                return httpx.Response(200, json=[], request=request)
            if request.method == "GET" and request.url.path == issue_timeline_path:
                timeline = [
                    {
                        "event": "cross-referenced",
                        "source": {
                            "issue": {
                                "number": number,
                                "pull_request": {
                                    "url": REPOSITORY.pull_request_api_url(number),
                                },
                            }
                        },
                    }
                    for number in self.timeline_pull_request_numbers
                ]
                return httpx.Response(200, json=timeline, request=request)
            if request.method == "PATCH" and request.url.path == pull_request_path:
                payload = json.loads(request.content.decode("utf-8"))
                if payload != {"state": "closed"}:
                    return httpx.Response(422, json={"message": "bad state"}, request=request)
                self.pr_state = "closed"
                return httpx.Response(
                    200,
                    json=self._pull_request_payload(),
                    request=request,
                )
            return httpx.Response(404, json={"message": "unexpected"}, request=request)

        return httpx.MockTransport(handler)


def _backend(
    fake: FakeGitHubAPI,
    *,
    pull_request_binding: github.PullRequestBinding | None = PULL_REQUEST_BINDING,
) -> github.GitHubRestBackend:
    return github.GitHubRestBackend(
        issue_binding=ISSUE_BINDING,
        pull_request_binding=pull_request_binding,
        token=TOKEN,
        transport=fake.transport(),
    )


def test_comment_upsert_create_update_and_idempotent_behavior() -> None:
    fake = FakeGitHubAPI()
    with _backend(fake) as backend:
        created = backend.upsert_issue_comment(
            logical_kind="baseline",
            markdown="baseline evidence",
            timeout_seconds=5.0,
        )
        unchanged = backend.upsert_issue_comment(
            logical_kind="baseline",
            markdown="baseline evidence",
            timeout_seconds=5.0,
        )
        updated = backend.upsert_issue_comment(
            logical_kind="baseline",
            markdown="updated evidence",
            timeout_seconds=5.0,
        )

    marker = github.stable_comment_marker(ISSUE_BINDING.job_id, "baseline")
    assert created.action == "created"
    assert unchanged.action == "unchanged"
    assert updated.action == "updated"
    assert created.marker == marker
    assert len(fake.comments) == 1
    assert fake.comments[0]["body"] == f"{marker}\nupdated evidence"
    assert created.comment_id == unchanged.comment_id == updated.comment_id
    assert [entry[0] for entry in fake.requests] == [
        "GET",
        "GET",
        "POST",
        "GET",
        "GET",
        "GET",
        "GET",
        "PATCH",
    ]


def test_comment_upsert_rejects_duplicate_owned_marker() -> None:
    fake = FakeGitHubAPI()
    marker = github.stable_comment_marker(ISSUE_BINDING.job_id, "baseline")
    fake.comments = [
        fake.comment(body=f"{marker}\none", author_login=ISSUE_BINDING.comment_author_login),
        fake.comment(body=f"{marker}\ntwo", author_login=ISSUE_BINDING.comment_author_login),
    ]
    with _backend(fake) as backend:
        with pytest.raises(
            github.GitHubPolicyError,
            match="more than one owned issue comment carries the same marker",
        ):
            backend.upsert_issue_comment(
                logical_kind="baseline",
                markdown="baseline evidence",
                timeout_seconds=5.0,
            )


def test_comment_upsert_rejects_foreign_marker() -> None:
    fake = FakeGitHubAPI()
    marker = github.stable_comment_marker(ISSUE_BINDING.job_id, "baseline")
    fake.comments = [
        fake.comment(body=f"{marker}\nforeign", author_login="evil-user"),
    ]
    with _backend(fake) as backend:
        with pytest.raises(
            github.GitHubPolicyError,
            match="foreign issue comment",
        ):
            backend.upsert_issue_comment(
                logical_kind="baseline",
                markdown="baseline evidence",
                timeout_seconds=5.0,
            )


def test_issue_only_backend_allows_comment_upsert_and_rejects_close() -> None:
    fake = FakeGitHubAPI()
    with _backend(fake, pull_request_binding=None) as backend:
        created = backend.upsert_issue_comment(
            logical_kind="baseline",
            markdown="baseline evidence",
            timeout_seconds=5.0,
        )
        with pytest.raises(
            github.GitHubPolicyError,
            match="does not yet contain a pull request binding",
        ):
            backend.close_pull_request_no_winner(
                final_decision_receipt=github.FinalDecisionReceipt(
                    decision=github.FinalDecision.NO_WINNER,
                    comment_receipt=_final_comment_receipt(),
                ),
                timeout_seconds=5.0,
            )

    assert created.action == "created"
    assert fake.requests == [("GET", COMMENTS_PATH), ("POST", COMMENTS_PATH)]


def test_ensure_pull_request_binding_discovers_exact_issue_pull_request() -> None:
    fake = FakeGitHubAPI()
    with _backend(fake, pull_request_binding=None) as backend:
        binding, receipt = backend.ensure_pull_request_binding(
            head_branch=PULL_REQUEST_BINDING.head_branch,
            timeout_seconds=5.0,
        )

    assert binding.pull_request_number == PULL_REQUEST_BINDING.pull_request_number
    assert binding.base_branch == PULL_REQUEST_BINDING.base_branch
    assert binding.head_branch == PULL_REQUEST_BINDING.head_branch
    assert binding.head_sha == PULL_REQUEST_HEAD_SHA
    assert binding.expected_author_login == PULL_REQUEST_BINDING.expected_author_login
    assert binding.expected_author_type == PULL_REQUEST_BINDING.expected_author_type
    assert receipt.action == "created"
    assert fake.requests == [
        ("GET", ISSUE_TIMELINE_PATH),
        ("GET", PULL_REQUESTS_PATH),
    ]
    assert fake.authorization_headers == [
        f"Bearer {TOKEN}",
        f"Bearer {TOKEN}",
    ]


def test_ensure_pull_request_binding_accepts_list_payload_without_merged_flag() -> None:
    fake = FakeGitHubAPI()

    def transform(payload: dict[str, Any]) -> dict[str, Any]:
        payload.pop("merged", None)
        return payload

    fake.pr_payload_transform = transform
    with _backend(fake, pull_request_binding=None) as backend:
        binding, receipt = backend.ensure_pull_request_binding(
            head_branch=PULL_REQUEST_BINDING.head_branch,
            timeout_seconds=5.0,
        )

    assert binding.pull_request_number == PULL_REQUEST_BINDING.pull_request_number
    assert receipt.action == "created"


def test_ensure_pull_request_binding_requires_issue_reference() -> None:
    fake = FakeGitHubAPI()
    fake.timeline_pull_request_numbers = []
    with _backend(fake, pull_request_binding=None) as backend:
        with pytest.raises(
            github.GitHubPolicyError,
            match="no exact early same-repository pull request",
        ):
            backend.ensure_pull_request_binding(
                head_branch=PULL_REQUEST_BINDING.head_branch,
                timeout_seconds=5.0,
            )


def test_close_no_winner_closes_exact_pull_request_and_is_idempotent() -> None:
    fake = FakeGitHubAPI()
    final_receipt = github.FinalDecisionReceipt(
        decision=github.FinalDecision.NO_WINNER,
        comment_receipt=_final_comment_receipt(),
    )
    with _backend(fake) as backend:
        closed = backend.close_pull_request_no_winner(
            final_decision_receipt=final_receipt,
            timeout_seconds=5.0,
        )
        unchanged = backend.close_pull_request_no_winner(
            final_decision_receipt=final_receipt,
            timeout_seconds=5.0,
        )

    assert closed.action == "closed"
    assert unchanged.action == "unchanged"
    assert fake.pr_state == "closed"
    assert closed.pull_request_number == PULL_REQUEST_BINDING.pull_request_number
    assert unchanged.state == "closed"


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("number", "pull request number"),
        ("base_repo_id", "repository ID"),
        ("author", "trusted discovered identity"),
        ("base", "base branch"),
        ("head_repo_id", "repository ID"),
    ],
)
def test_close_no_winner_rejects_wrong_binding_author_base_or_head(
    case: str,
    expected: str,
) -> None:
    fake = FakeGitHubAPI()

    def transform(payload: dict[str, Any]) -> dict[str, Any]:
        if case == "number":
            number = PULL_REQUEST_BINDING.pull_request_number + 1
            payload["number"] = number
            payload["url"] = REPOSITORY.pull_request_api_url(number)
            payload["html_url"] = REPOSITORY.pull_request_html_url(number)
        elif case == "base_repo_id":
            payload["base"]["repo"]["id"] = REPOSITORY.repository_id + 1
        elif case == "author":
            payload["user"]["login"] = "other-bot[bot]"
        elif case == "base":
            payload["base"]["ref"] = "release"
        elif case == "head_repo_id":
            payload["head"]["repo"]["id"] = REPOSITORY.repository_id + 1
        return payload

    fake.pr_payload_transform = transform
    final_receipt = github.FinalDecisionReceipt(
        decision=github.FinalDecision.NO_WINNER,
        comment_receipt=_final_comment_receipt(),
    )
    with _backend(fake) as backend:
        with pytest.raises(github.GitHubPolicyError, match=expected):
            backend.close_pull_request_no_winner(
                final_decision_receipt=final_receipt,
                timeout_seconds=5.0,
            )


def test_backend_rejects_forbidden_method_and_path() -> None:
    fake = FakeGitHubAPI()
    with _backend(fake) as backend:
        with pytest.raises(
            github.GitHubPolicyError,
            match="outside the closed GitHub policy",
        ):
            backend._request_json(
                "DELETE",
                REPOSITORY.issue_comments_api_path(ISSUE_BINDING.issue_number),
                expected_status=(204,),
                timeout_seconds=5.0,
            )
        with pytest.raises(
            github.GitHubPolicyError,
            match="outside the closed GitHub policy",
        ):
            backend._request_json(
                "GET",
                "/repos/contoso/other/issues/41/comments",
                expected_status=(200,),
                timeout_seconds=5.0,
            )
    assert fake.requests == []


def test_backend_rejects_redirects_and_cross_origin_urls() -> None:
    fake = FakeGitHubAPI()
    fake.redirect_target[
        ("GET", REPOSITORY.pull_request_api_path(PULL_REQUEST_BINDING.pull_request_number))
    ] = "https://evil.example/redirect"
    with _backend(fake) as backend:
        with pytest.raises(github.GitHubContractError, match="redirect responses"):
            backend.upsert_issue_comment(
                logical_kind="baseline",
                markdown="baseline evidence",
                timeout_seconds=5.0,
            )

    fake = FakeGitHubAPI()

    def transform(payload: dict[str, Any]) -> dict[str, Any]:
        payload["base"]["repo"]["url"] = "https://evil.example/repos/contoso/travel-agent"
        return payload

    fake.pr_payload_transform = transform
    with _backend(fake) as backend:
        with pytest.raises(github.GitHubPolicyError, match="trusted origin"):
            backend.close_pull_request_no_winner(
                final_decision_receipt=github.FinalDecisionReceipt(
                    decision=github.FinalDecision.NO_WINNER,
                    comment_receipt=_final_comment_receipt(),
                ),
                timeout_seconds=5.0,
            )


def test_backend_rejects_oversized_comment_bodies_before_any_http_request() -> None:
    fake = FakeGitHubAPI()
    with _backend(fake) as backend:
        with pytest.raises(github.GitHubPolicyError, match="byte budget"):
            backend.upsert_issue_comment(
                logical_kind="baseline",
                markdown="x" * github.MAX_COMMENT_BODY_BYTES,
                timeout_seconds=5.0,
            )
    assert fake.requests == []


def test_backend_rejects_merged_pull_requests_and_winner_requests() -> None:
    fake = FakeGitHubAPI()
    fake.pr_merged = True
    with _backend(fake) as backend:
        with pytest.raises(github.GitHubPolicyError, match="already merged"):
            backend.upsert_issue_comment(
                logical_kind="baseline",
                markdown="baseline evidence",
                timeout_seconds=5.0,
            )
        with pytest.raises(github.GitHubPolicyError, match="already merged"):
            backend.close_pull_request_no_winner(
                final_decision_receipt=github.FinalDecisionReceipt(
                    decision=github.FinalDecision.NO_WINNER,
                    comment_receipt=_final_comment_receipt(),
                ),
                timeout_seconds=5.0,
            )

    fake = FakeGitHubAPI()
    with _backend(fake) as backend:
        with pytest.raises(github.GitHubPolicyError, match="winner closure requests"):
            backend.close_pull_request_no_winner(
                final_decision_receipt=github.FinalDecisionReceipt(
                    decision=github.FinalDecision.WINNER,
                    comment_receipt=_final_comment_receipt(),
                ),
                timeout_seconds=5.0,
            )


def test_transport_uses_actual_bearer_token_and_redacts_transport_errors() -> None:
    headers: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        headers.append(request.headers.get("Authorization"))
        raise httpx.ConnectError(
            f"transport failed for {request.headers.get('Authorization')}",
            request=request,
        )

    backend = github.GitHubRestBackend(
        issue_binding=ISSUE_BINDING,
        pull_request_binding=PULL_REQUEST_BINDING,
        token=TOKEN,
        transport=httpx.MockTransport(handler),
    )
    with backend:
        with pytest.raises(github.GitHubTransportError) as captured:
            backend.upsert_issue_comment(
                logical_kind="baseline",
                markdown="baseline evidence",
                timeout_seconds=5.0,
            )

    assert headers == [f"Bearer {TOKEN}"]
    assert TOKEN not in str(captured.value)
    assert "******" in str(captured.value)


def test_token_never_leaks_in_repr_or_errors() -> None:
    fake = FakeGitHubAPI()
    with _backend(fake) as backend:
        assert TOKEN not in repr(backend)
        with pytest.raises(github.TokenLeakageError) as captured:
            backend.upsert_issue_comment(
                logical_kind="baseline",
                markdown=f"contains {TOKEN}",
                timeout_seconds=5.0,
            )
    assert TOKEN not in str(captured.value)
    assert fake.requests == []


def test_broker_models_forbid_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        github.BrokerRequest.model_validate(
            {
                "request_id": "req-1",
                "operation": "comment.upsert",
                "timeout_seconds": 1.0,
                "logical_kind": "baseline",
                "markdown": "ok",
                "unexpected": True,
            }
        )


@pytest.mark.parametrize("pull_request_mode", ["omitted", "null"])
def test_binding_document_allows_issue_only_pull_request_modes(
    tmp_path: Path,
    pull_request_mode: str,
) -> None:
    path = tmp_path / "binding.json"
    _write_binding(path, pull_request_mode=pull_request_mode)

    binding = github._load_binding_document(path)

    assert binding.issue == ISSUE_BINDING
    assert binding.pull_request is None


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(socket, "AF_UNIX"),
    reason="Unix socket transport is Linux only",
)
def test_unix_socket_broker_round_trips_a_comment_request(tmp_path: Path) -> None:
    fake = FakeGitHubAPI()
    socket_path = tmp_path / "private" / "broker.sock"
    server = github.UnixSocketBrokerServer(
        socket_path=socket_path,
        binding_path=_binding_path(tmp_path),
        token=TOKEN,
        transport=fake.transport(),
    )
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"max_requests": 1},
        daemon=True,
    )
    thread.start()
    try:
        client = github.UnixSocketBrokerClient(socket_path=socket_path)
        receipt = client.upsert_comment(
            request_id="req-1",
            logical_kind="baseline",
            markdown="baseline evidence",
            timeout_seconds=5.0,
        )
        assert receipt.action == "created"
        assert fake.comments[0]["body"].endswith("baseline evidence")
    finally:
        server.close()
        thread.join(timeout=5.0)


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(socket, "AF_UNIX"),
    reason="Unix socket transport is Linux only",
)
@pytest.mark.parametrize("pull_request_mode", ["omitted", "null"])
def test_unix_socket_broker_allows_issue_only_binding_for_comment_upsert(
    tmp_path: Path,
    pull_request_mode: str,
) -> None:
    fake = FakeGitHubAPI()
    binding_path = tmp_path / "binding.json"
    _write_binding(binding_path, pull_request_mode=pull_request_mode)
    socket_path = tmp_path / "private" / "broker.sock"
    server = github.UnixSocketBrokerServer(
        socket_path=socket_path,
        binding_path=binding_path,
        token=TOKEN,
        transport=fake.transport(),
    )
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"max_requests": 1},
        daemon=True,
    )
    thread.start()
    try:
        client = github.UnixSocketBrokerClient(socket_path=socket_path)
        receipt = client.upsert_comment(
            request_id="req-issue-only",
            logical_kind="baseline",
            markdown="baseline evidence",
            timeout_seconds=5.0,
        )
        assert receipt.action == "created"
        assert fake.requests == [("GET", COMMENTS_PATH), ("POST", COMMENTS_PATH)]
    finally:
        server.close()
        thread.join(timeout=5.0)


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(socket, "AF_UNIX"),
    reason="Unix socket transport is Linux only",
)
def test_unix_socket_broker_discovers_and_persists_pull_request_binding(
    tmp_path: Path,
) -> None:
    fake = FakeGitHubAPI()
    binding_path = tmp_path / "binding.json"
    _write_binding(binding_path, pull_request_mode="omitted")
    socket_path = tmp_path / "private" / "broker.sock"
    server = github.UnixSocketBrokerServer(
        socket_path=socket_path,
        binding_path=binding_path,
        token=TOKEN,
        transport=fake.transport(),
    )
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"max_requests": 1},
        daemon=True,
    )
    thread.start()
    try:
        client = github.UnixSocketBrokerClient(socket_path=socket_path)
        receipt = client.ensure_pull_request_binding(
            request_id="req-bind",
            head_branch=PULL_REQUEST_BINDING.head_branch,
            timeout_seconds=5.0,
        )
        assert receipt.pull_request_number == PULL_REQUEST_BINDING.pull_request_number
        assert receipt.expected_author_login == PULL_REQUEST_BINDING.expected_author_login
        assert receipt.expected_author_type == PULL_REQUEST_BINDING.expected_author_type
        persisted = github._load_binding_document(binding_path)
        assert persisted.pull_request is not None
        assert persisted.pull_request.head_sha == PULL_REQUEST_HEAD_SHA
        assert fake.requests == [
            ("GET", ISSUE_TIMELINE_PATH),
            ("GET", PULL_REQUESTS_PATH),
        ]
    finally:
        server.close()
        thread.join(timeout=5.0)


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(socket, "AF_UNIX"),
    reason="Unix socket transport is Linux only",
)
def test_unix_socket_broker_rejects_close_until_pull_request_binding_is_added(
    tmp_path: Path,
) -> None:
    fake = FakeGitHubAPI()
    binding_path = tmp_path / "binding.json"
    _write_binding(binding_path, pull_request_mode="omitted")
    socket_path = tmp_path / "private" / "broker.sock"
    server = github.UnixSocketBrokerServer(
        socket_path=socket_path,
        binding_path=binding_path,
        token=TOKEN,
        transport=fake.transport(),
    )
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"max_requests": 2},
        daemon=True,
    )
    thread.start()
    final_decision_receipt = github.FinalDecisionReceipt(
        decision=github.FinalDecision.NO_WINNER,
        comment_receipt=_final_comment_receipt(),
    )
    try:
        client = github.UnixSocketBrokerClient(socket_path=socket_path)
        rejected = client.send(
            github.BrokerRequest(
                request_id="req-close-1",
                operation=github.BrokerOperation.PULL_REQUEST_CLOSE_NO_WINNER,
                timeout_seconds=5.0,
                final_decision_receipt=final_decision_receipt,
            )
        )
        assert rejected.ok is False
        assert rejected.error_type == "GitHubPolicyError"
        assert rejected.error_message is not None
        assert "pull request binding" in rejected.error_message
        assert fake.requests == []

        _replace_binding(binding_path, pull_request_mode="full")

        closed = client.close_no_winner(
            request_id="req-close-2",
            final_decision_receipt=final_decision_receipt,
            timeout_seconds=5.0,
        )
        assert closed.action == "closed"
        assert fake.requests == [
            ("GET", PULL_REQUEST_PATH),
            ("PATCH", PULL_REQUEST_PATH),
            ("GET", PULL_REQUEST_PATH),
        ]
    finally:
        server.close()
        thread.join(timeout=5.0)


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(socket, "AF_UNIX"),
    reason="Unix socket transport is Linux only",
)
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (b"not-json\n", "BrokerProtocolError"),
        (
            b'{"request_id":"req-1","operation":"comment.upsert","operation":"x","timeout_seconds":1.0,"logical_kind":"baseline","markdown":"ok"}\n',
            "BrokerProtocolError",
        ),
        (
            b'{"request_id":"req-2","operation":"comment.upsert","timeout_seconds":1.0,"logical_kind":"baseline","markdown":"ok","extra":1}\n',
            "BrokerProtocolError",
        ),
        (
            b'{"request_id":"req-3","operation":"issue.delete","timeout_seconds":1.0,"logical_kind":"baseline","markdown":"ok"}\n',
            "BrokerOperationError",
        ),
    ],
)
def test_unix_socket_broker_rejects_malformed_frames(
    tmp_path: Path,
    payload: bytes,
    expected: str,
) -> None:
    fake = FakeGitHubAPI()
    socket_path = tmp_path / "private" / "broker.sock"
    server = github.UnixSocketBrokerServer(
        socket_path=socket_path,
        binding_path=_binding_path(tmp_path),
        token=TOKEN,
        transport=fake.transport(),
    )
    thread = threading.Thread(target=server.serve_once, daemon=True)
    thread.start()
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5.0)
        client.connect(str(socket_path))
        client.sendall(payload)
        raw = bytearray()
        while not raw.endswith(b"\n"):
            raw.extend(client.recv(4096))
        client.close()
        response = github.BrokerResponse.model_validate(json.loads(raw.decode("ascii")))
        assert response.ok is False
        assert response.error_type == expected
        assert TOKEN not in response.model_dump_json()
    finally:
        server.close()
        thread.join(timeout=5.0)


@pytest.mark.skipif(
    os.name == "nt" or not hasattr(socket, "AF_UNIX"),
    reason="Unix socket transport is Linux only",
)
def test_unix_socket_broker_sets_private_parent_and_socket_permissions(
    tmp_path: Path,
) -> None:
    fake = FakeGitHubAPI()
    socket_path = tmp_path / "private" / "broker.sock"
    server = github.UnixSocketBrokerServer(
        socket_path=socket_path,
        binding_path=_binding_path(tmp_path),
        token=TOKEN,
        transport=fake.transport(),
    )
    try:
        parent_mode = socket_path.parent.stat().st_mode & 0o777
        socket_mode = socket_path.stat().st_mode & 0o777
        assert parent_mode & 0o077 == 0
        assert stat.S_ISSOCK(socket_path.stat().st_mode)
        assert socket_mode == 0o600
    finally:
        server.close()
