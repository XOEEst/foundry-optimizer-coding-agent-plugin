from __future__ import annotations

import json

import httpx
import pytest

from foundry_opt.bootstrap.contracts import BootstrapAction, BootstrapPlan
from foundry_opt.bootstrap.providers.github import GitHubBootstrapProvider, GitHubProviderApplyError, GitHubProviderError, GitHubProviderTransportError


class FakeGitHubTransport(httpx.MockTransport):
    def __init__(self, handler):
        super().__init__(handler)


def _plan(*actions: BootstrapAction) -> BootstrapPlan:
    return BootstrapPlan.create(
        operation_id="op-1",
        runtime_repository="https://github.com/example/runtime.git",
        runtime_commit="a" * 40,
        repository_identity="example-org/example-repo",
        actions=actions,
    )


def _response(status: int, payload: dict | None = None, *, headers: dict[str, str] | None = None, content: bytes | None = None) -> httpx.Response:
    if content is not None:
        return httpx.Response(status, content=content, headers=headers)
    return httpx.Response(status, json=payload or {}, headers=headers)


def _body(request: httpx.Request) -> dict:
    raw = request.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def test_sends_real_authorization_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer ghp_secret"
        return _response(200, {"id": 7, "default_branch": "main", "full_name": "example-org/example-repo"})

    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler))
    provider.read_repository_settings("example-org/example-repo")


def test_policy_enable_payload_and_restore_none_as_null() -> None:
    puts: list[object] = []
    state = {"policy": None, "branch_exists": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/repos/example-org/example-repo"):
            return _response(200, {"id": 7, "default_branch": "release", "full_name": "example-org/example-repo"})
        if path.endswith("/environments/copilot"):
            return _response(404, {})
        if path.endswith("/environments/foundry-production") and request.method == "GET":
            return _response(200, {"name": "foundry-production", "deployment_branch_policy": state["policy"]})
        if path.endswith("/environments/foundry-production") and request.method == "PUT":
            payload = _body(request)["deployment_branch_policy"]
            puts.append(payload)
            state["policy"] = payload
            return _response(200, {})
        if path.endswith("/variables/AZURE_OPTIMIZER_CLIENT_ID"):
            return _response(404, {})
        if path.endswith("/variables"):
            return _response(200, {"variables": []})
        if path.endswith("/deployment_branch_policies") and request.method == "POST":
            return _response(403, {"message": "forbidden"})
        if path.endswith("/deployment_branch_policies") and request.method == "GET":
            return _response(200, {"branch_policies": []})
        if path.endswith("/actions/variables"):
            return _response(200, {"variables": []})
        raise AssertionError((request.method, path))

    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler))
    with pytest.raises(GitHubProviderApplyError):
        provider.apply_changes(_plan(BootstrapAction(action_id="branch", phase="github", stage="planned", kind="github-branch-policy", diagnostics=("foundry-production", "release"))))
    assert puts[0] == {"protected_branches": False, "custom_branch_policies": True}
    assert puts[-1] is None


def test_registers_rollback_before_post_write_get_failure() -> None:
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/repos/example-org/example-repo"):
            return _response(200, {"id": 7, "default_branch": "main", "full_name": "example-org/example-repo"})
        if path.endswith("/environments/copilot"):
            return _response(404, {})
        if path.endswith("/environments/foundry-production") and request.method == "GET":
            return _response(404, {})
        if path.endswith("/environments/foundry-production") and request.method == "PUT":
            return _response(200, {})
        if path.endswith("/environments/foundry-production") and request.method == "DELETE":
            deleted.append(path)
            return _response(204, {})
        if path.endswith("/variables/AZURE_OPTIMIZER_CLIENT_ID"):
            return _response(404, {})
        if path.endswith("/variables") or path.endswith("/deployment_branch_policies") or path.endswith("/actions/variables"):
            return _response(200, {"variables": [], "branch_policies": []})
        raise AssertionError((request.method, path))

    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler))
    with pytest.raises(GitHubProviderApplyError):
        provider.apply_changes(_plan(BootstrapAction(action_id="env", phase="github", stage="planned", kind="github-environment", diagnostics=("foundry-production",))))
    assert deleted


def test_verifies_final_merged_state_after_variable_then_branch_policy() -> None:
    state = {"policy": None, "value": None, "branch_policies": []}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/repos/example-org/example-repo"):
            return _response(200, {"id": 7, "default_branch": "release", "full_name": "example-org/example-repo"})
        if path.endswith("/environments/copilot"):
            return _response(404, {})
        if path.endswith("/environments/foundry-production") and request.method == "GET":
            return _response(200, {"name": "foundry-production", "deployment_branch_policy": state["policy"]})
        if path.endswith("/environments/foundry-production") and request.method == "PUT":
            state["policy"] = _body(request)["deployment_branch_policy"]
            return _response(200, {})
        if path.endswith("/variables/AZURE_OPTIMIZER_CLIENT_ID") and request.method == "GET":
            return _response(404, {}) if state["value"] is None else _response(200, {"name": "AZURE_OPTIMIZER_CLIENT_ID", "value": state["value"]})
        if path.endswith("/variables") and request.method == "POST":
            state["value"] = _body(request)["value"]
            return _response(201, {})
        if path.endswith("/variables") and request.method == "GET":
            return _response(200, {"variables": [] if state["value"] is None else [{"name": "AZURE_OPTIMIZER_CLIENT_ID", "value": state["value"]}]})
        if path.endswith("/deployment_branch_policies") and request.method == "POST":
            state["branch_policies"] = [{"id": 11, "name": "release", "type": "branch"}]
            return _response(201, {})
        if path.endswith("/deployment_branch_policies") and request.method == "GET":
            return _response(200, {"branch_policies": state["branch_policies"]})
        if path.endswith("/actions/variables"):
            return _response(200, {"variables": []})
        raise AssertionError((request.method, path))

    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler))
    receipt = provider.apply_changes(_plan(
        BootstrapAction(action_id="var", phase="github", stage="planned", kind="github-variable", diagnostics=("foundry-production", "client")),
        BootstrapAction(action_id="branch", phase="github", stage="planned", kind="github-branch-policy", diagnostics=("foundry-production", "release")),
    ))
    assert receipt.changed_actions == ("var", "branch")


def test_receipt_binding_rejects_unrelated_rollback() -> None:
    state = {"environment_exists": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/repos/example-org/example-repo"):
            return _response(200, {"id": 7, "default_branch": "main", "full_name": "example-org/example-repo"})
        if path.endswith("/environments/copilot"):
            return _response(404, {})
        if path.endswith("/environments/foundry-production") and request.method == "GET":
            if state["environment_exists"]:
                return _response(
                    200,
                    {
                        "name": "foundry-production",
                        "deployment_branch_policy": {
                            "protected_branches": False,
                            "custom_branch_policies": True,
                        },
                    },
                )
            return _response(404, {})
        if path.endswith("/environments/foundry-production") and request.method == "PUT":
            state["environment_exists"] = True
            return _response(200, {})
        if path.endswith("/environments/foundry-production") and request.method == "DELETE":
            state["environment_exists"] = False
            return _response(204, {})
        if path.endswith("/variables/AZURE_OPTIMIZER_CLIENT_ID"):
            return _response(404, {})
        if path.endswith("/variables") or path.endswith("/deployment_branch_policies"):
            return _response(200, {"variables": [], "branch_policies": []})
        if path.endswith("/actions/variables"):
            return _response(200, {"variables": []})
        raise AssertionError((request.method, path))

    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler))
    receipt = provider.apply_changes(_plan(BootstrapAction(action_id="env", phase="github", stage="planned", kind="github-environment", diagnostics=("foundry-production",))))
    other = receipt.model_copy(update={"operation_id": "other-op"})
    with pytest.raises(GitHubProviderApplyError, match="does not match"):
        provider.rollback_changes(other)


def test_branch_policy_uses_requested_branch_not_main() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/repos/example-org/example-repo"):
            return _response(200, {"id": 7, "default_branch": "release", "full_name": "example-org/example-repo"})
        if path.endswith("/environments/copilot"):
            return _response(404, {})
        if path.endswith("/environments/foundry-production"):
            return _response(200, {"name": "foundry-production", "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}})
        if path.endswith("/variables/AZURE_OPTIMIZER_CLIENT_ID"):
            return _response(404, {})
        if path.endswith("/variables"):
            return _response(200, {"variables": []})
        if path.endswith("/deployment_branch_policies") and request.method == "POST":
            return _response(303, {})
        if path.endswith("/deployment_branch_policies"):
            return _response(200, {"branch_policies": [{"id": 21, "name": "release", "type": "branch"}]})
        if path.endswith("/actions/variables"):
            return _response(200, {"variables": []})
        raise AssertionError((request.method, path))

    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler))
    receipt = provider.apply_changes(_plan(BootstrapAction(action_id="branch", phase="github", stage="planned", kind="github-branch-policy", diagnostics=("foundry-production", "release"))))
    assert receipt.adopted_actions == ("branch",)


def test_rate_limit_parses_403_body_without_headers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _response(403, {"message": "You have exceeded a secondary rate limit."})

    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler))
    with pytest.raises(GitHubProviderTransportError, match="rate_limited"):
        provider.read_repository_settings("example-org/example-repo")


def test_bounded_reader_applies_to_403_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _response(403, content=b"a" * 70000)

    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler), json_max_bytes=1024)
    with pytest.raises(GitHubProviderError, match="exceeds"):
        provider.read_repository_settings("example-org/example-repo")


def test_error_graph_is_sanitized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.HTTPError("ghp_secret leaked")

    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler))
    with pytest.raises(GitHubProviderTransportError) as exc:
        provider.read_repository_settings("example-org/example-repo")
    assert "ghp_secret" not in str(exc.value)
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None
