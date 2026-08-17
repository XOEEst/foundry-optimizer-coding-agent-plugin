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


def _response(status: int, payload: dict, *, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=payload, headers=headers)


def _body(request: httpx.Request) -> dict:
    raw = request.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def test_environment_put_enables_custom_branch_policies() -> None:
    captured: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/repos/example-org/example-repo"):
            return _response(200, {"id": 7, "default_branch": "main", "full_name": "example-org/example-repo"})
        if path.endswith("/environments/copilot"):
            return _response(404, {})
        if path.endswith("/environments/foundry-production") and request.method == "PUT":
            captured.append(_body(request))
            return _response(200, {})
        if path.endswith("/environments/foundry-production") and request.method == "GET":
            if captured:
                return _response(200, {"name": "foundry-production", "protection_rules": [], "deployment_branch_policy": {"custom_branch_policies": True}})
            return _response(404, {})
        if path.endswith("/environments/foundry-production") and request.method == "DELETE":
            return _response(204, {})
        if path.endswith("/variables/AZURE_OPTIMIZER_CLIENT_ID"):
            return _response(404, {})
        if path.endswith("/variables") or path.endswith("/deployment_branch_policies") or path.endswith("/actions/variables"):
            return _response(200, {"variables": [], "branch_policies": []})
        raise AssertionError((request.method, path))

    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler))
    provider.apply_changes(_plan(BootstrapAction(action_id="env-prod", phase="github", stage="planned", kind="github-environment", diagnostics=("foundry-production",))))
    assert captured == [{"deployment_branch_policy": {"custom_branch_policies": True}}]


def test_inventory_direct_get_avoids_later_page_delete() -> None:
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/repos/example-org/example-repo"):
            return _response(200, {"id": 7, "default_branch": "main", "full_name": "example-org/example-repo"})
        if path.endswith("/environments/copilot"):
            return _response(404, {})
        if path.endswith("/environments/foundry-production"):
            return _response(200, {"name": "foundry-production", "protection_rules": [], "deployment_branch_policy": {"custom_branch_policies": True}})
        if request.method == "DELETE" and "/environments/" in path:
            deleted.append(path)
            return _response(204, {})
        if path.endswith("/variables/AZURE_OPTIMIZER_CLIENT_ID"):
            return _response(200, {"name": "AZURE_OPTIMIZER_CLIENT_ID", "value": "client-1"})
        if path.endswith("/variables"):
            return _response(200, {"variables": [{"name": "AZURE_OPTIMIZER_CLIENT_ID", "value": "client-1"}]})
        if path.endswith("/deployment_branch_policies"):
            return _response(200, {"branch_policies": [{"id": 9, "name": "main", "type": "branch"}]})
        if path.endswith("/actions/variables"):
            return _response(200, {"variables": []})
        raise AssertionError((request.method, path))

    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler))
    receipt = provider.apply_changes(_plan(BootstrapAction(action_id="env-prod", phase="github", stage="planned", kind="github-environment", diagnostics=("foundry-production",))))
    assert receipt.adopted_actions == ("env-prod",)
    assert deleted == []


def test_branch_policy_duplicate_303_verifies_existing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/repos/example-org/example-repo"):
            return _response(200, {"id": 7, "default_branch": "main", "full_name": "example-org/example-repo"})
        if path.endswith("/environments/copilot"):
            return _response(404, {})
        if path.endswith("/environments/foundry-production"):
            return _response(200, {"name": "foundry-production", "protection_rules": [], "deployment_branch_policy": {"custom_branch_policies": True}})
        if path.endswith("/variables/AZURE_OPTIMIZER_CLIENT_ID"):
            return _response(404, {})
        if path.endswith("/variables"):
            return _response(200, {"variables": []})
        if request.method == "POST" and path.endswith("/deployment_branch_policies"):
            return _response(303, {})
        if path.endswith("/deployment_branch_policies"):
            return _response(200, {"branch_policies": [{"id": 11, "name": "main", "type": "branch"}]})
        if path.endswith("/actions/variables"):
            return _response(200, {"variables": []})
        raise AssertionError((request.method, path))

    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler))
    receipt = provider.apply_changes(_plan(BootstrapAction(action_id="branch-prod", phase="github", stage="planned", kind="github-branch-policy", diagnostics=("foundry-production", "main"))))
    assert receipt.adopted_actions == ("branch-prod",)


def test_rate_limit_403_detected_before_forbidden() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _response(403, {}, headers={"Retry-After": "30"})

    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler))
    with pytest.raises(GitHubProviderTransportError, match="rate_limited"):
        provider.read_repository_settings("example-org/example-repo")


def test_full_name_identity_mismatch_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _response(200, {"id": 7, "default_branch": "main", "full_name": "other-org/example-repo"})

    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler))
    with pytest.raises(GitHubProviderError, match="full_name"):
        provider.read_repository_settings("example-org/example-repo")


def test_streaming_response_limit_is_bounded() -> None:
    payload = b"{" + b'"x":"' + (b"a" * (_body_len := 70000)) + b'"}'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=payload)

    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler), json_max_bytes=1024)
    with pytest.raises(GitHubProviderError, match="exceeds"):
        provider.read_repository_settings("example-org/example-repo")


def test_apply_error_traceback_message_is_redacted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.HTTPError("bad ghp_secret token")

    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler))
    with pytest.raises(GitHubProviderTransportError) as exc:
        provider.read_repository_settings("example-org/example-repo")
    assert "ghp_secret" not in str(exc.value)
    assert exc.value.__cause__ is None


def test_variable_change_and_rollback_restore_only_owned_changes() -> None:
    state = {"env": {"name": "foundry-production", "protection_rules": [], "deployment_branch_policy": {"custom_branch_policies": False}}, "value": "old", "deleted_policy": [], "deleted_variable": [], "puts": []}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/repos/example-org/example-repo"):
            return _response(200, {"id": 7, "default_branch": "main", "full_name": "example-org/example-repo"})
        if path.endswith("/environments/copilot"):
            return _response(404, {})
        if path.endswith("/environments/foundry-production") and request.method == "GET":
            return _response(200, state["env"])
        if path.endswith("/environments/foundry-production") and request.method == "PUT":
            state["puts"].append(_body(request)["deployment_branch_policy"])
            state["env"] = {"name": "foundry-production", "protection_rules": [], "deployment_branch_policy": _body(request)["deployment_branch_policy"]}
            return _response(200, {})
        if path.endswith("/variables/AZURE_OPTIMIZER_CLIENT_ID") and request.method == "GET":
            return _response(200, {"name": "AZURE_OPTIMIZER_CLIENT_ID", "value": state["value"]})
        if path.endswith("/variables/AZURE_OPTIMIZER_CLIENT_ID") and request.method == "PATCH":
            state["value"] = _body(request)["value"]
            return _response(204, {})
        if path.endswith("/variables") and request.method == "GET":
            return _response(200, {"variables": [{"name": "AZURE_OPTIMIZER_CLIENT_ID", "value": state["value"]}]})
        if path.endswith("/deployment_branch_policies") and request.method == "GET":
            return _response(200, {"branch_policies": []})
        if path.endswith("/deployment_branch_policies") and request.method == "POST":
            return _response(403, {})
        if path.endswith("/actions/variables"):
            return _response(200, {"variables": []})
        raise AssertionError((request.method, path))

    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler))
    with pytest.raises(GitHubProviderApplyError):
        provider.apply_changes(_plan(
            BootstrapAction(action_id="var-prod", phase="github", stage="planned", kind="github-variable", diagnostics=("foundry-production", "new")),
            BootstrapAction(action_id="branch-prod", phase="github", stage="planned", kind="github-branch-policy", diagnostics=("foundry-production", "main")),
        ))
    assert state["puts"][-1]["custom_branch_policies"] is False
    assert state["value"] == "old"
    assert state["env"]["deployment_branch_policy"]["custom_branch_policies"] is False
