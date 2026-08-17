from __future__ import annotations

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


def _response(status: int, payload: dict) -> httpx.Response:
    return httpx.Response(status, json=payload)


def test_inventory_existing_resources_and_plan_adopts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/repos/example-org/example-repo"):
            return _response(200, {"id": 7, "default_branch": "main"})
        if path.endswith("/environments"):
            return _response(200, {"environments": [{"name": "copilot"}, {"name": "foundry-production"}]})
        if path.endswith("/environments/copilot/variables"):
            return _response(200, {"variables": [{"name": "AZURE_OPTIMIZER_CLIENT_ID", "value": "client-1"}]})
        if path.endswith("/environments/foundry-production/variables"):
            return _response(200, {"variables": [{"name": "AZURE_OPTIMIZER_CLIENT_ID", "value": "client-1"}]})
        if path.endswith("/environments/copilot/deployment-branch-policies"):
            return _response(200, {"branch_policies": []})
        if path.endswith("/environments/foundry-production/deployment-branch-policies"):
            return _response(200, {"branch_policies": [{"name": "main"}]})
        if path.endswith("/actions/variables"):
            return _response(200, {"variables": []})
        raise AssertionError(path)

    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler))
    actions = provider.plan_changes(
        _plan(BootstrapAction(action_id="env-prod", phase="github", stage="planned", kind="github-environment", diagnostics=("foundry-production",)))
    )
    assert actions[0].diagnostics[-1] == "adopt"


def test_apply_missing_resources_creates_and_verifies() -> None:
    calls: list[tuple[str, str]] = []
    state = {"prod_var": None, "policy": False, "envs": set(), "created": False}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        path = request.url.path
        if request.method == "GET" and path.endswith("/repos/example-org/example-repo"):
            return _response(200, {"id": 7, "default_branch": "main"})
        if request.method == "GET" and path.endswith("/environments"):
            envs = [{"name": name} for name in sorted(state["envs"])]
            return _response(200, {"environments": envs})
        if request.method == "PUT" and path.endswith("/environments/foundry-production"):
            state["envs"].add("foundry-production")
            state["created"] = True
            return _response(200, {})
        if request.method == "GET" and path.endswith("/environments/copilot/variables"):
            return _response(200, {"variables": []})
        if request.method == "GET" and path.endswith("/environments/foundry-production/variables"):
            vars_ = [] if state["prod_var"] is None else [{"name": "AZURE_OPTIMIZER_CLIENT_ID", "value": state["prod_var"]}]
            return _response(200, {"variables": vars_})
        if request.method == "GET" and path.endswith("/environments/copilot/deployment-branch-policies"):
            return _response(200, {"branch_policies": []})
        if request.method == "GET" and path.endswith("/environments/foundry-production/deployment-branch-policies"):
            return _response(200, {"branch_policies": [{"name": "main"}] if state["policy"] else []})
        if request.method == "POST" and path.endswith("/environments/foundry-production/variables"):
            state["prod_var"] = request.read().decode()
            return _response(201, {})
        if request.method == "POST" and path.endswith("/environments/foundry-production/deployment-branch-policies"):
            state["policy"] = True
            return _response(200, {})
        if request.method == "DELETE" and path.endswith("/environments/foundry-production"):
            state["envs"].discard("foundry-production")
            return _response(204, {})
        if request.method == "GET" and path.endswith("/actions/variables"):
            return _response(200, {"variables": []})
        if request.method == "GET" and path.endswith("/environments/foundry-production/variables/AZURE_OPTIMIZER_CLIENT_ID"):
            return _response(404, {})
        raise AssertionError((request.method, path))

    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler))
    receipt = provider.apply_changes(
        _plan(
            BootstrapAction(action_id="env-prod", phase="github", stage="planned", kind="github-environment", diagnostics=("foundry-production",)),
            BootstrapAction(action_id="var-prod", phase="github", stage="planned", kind="github-variable", diagnostics=("foundry-production", "client-1")),
            BootstrapAction(action_id="branch-prod", phase="github", stage="planned", kind="github-branch-policy", diagnostics=("foundry-production", "main")),
        )
    )
    assert receipt.created_actions == ("env-prod",)
    assert set(receipt.changed_actions) == {"var-prod", "branch-prod"}


def test_branch_policy_preserve_existing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/repos/example-org/example-repo"):
            return _response(200, {"id": 7, "default_branch": "main"})
        if path.endswith("/environments"):
            return _response(200, {"environments": [{"name": "foundry-production"}]})
        if path.endswith("/environments/copilot/variables") or path.endswith("/environments/foundry-production/variables"):
            return _response(200, {"variables": []})
        if path.endswith("/environments/copilot/deployment-branch-policies"):
            return _response(200, {"branch_policies": []})
        if path.endswith("/environments/foundry-production/deployment-branch-policies"):
            return _response(200, {"branch_policies": [{"name": "release"}, {"name": "main"}]})
        if path.endswith("/actions/variables"):
            return _response(200, {"variables": []})
        raise AssertionError(path)

    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler))
    receipt = provider.apply_changes(_plan(BootstrapAction(action_id="branch-prod", phase="github", stage="planned", kind="github-branch-policy", diagnostics=("foundry-production", "main"))))
    assert receipt.adopted_actions == ("branch-prod",)


@pytest.mark.parametrize("status", [403, 404, 429])
def test_error_statuses(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/repos/example-org/example-repo"):
            return _response(status, {})
        raise AssertionError(request.url.path)

    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler))
    if status == 404:
        with pytest.raises(GitHubProviderTransportError):
            provider.read_repository_settings("example-org/example-repo")
    else:
        with pytest.raises(GitHubProviderTransportError):
            provider.read_repository_settings("example-org/example-repo")


def test_token_redaction_in_apply_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.HTTPError("bad ghp_secret token")

    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler))
    with pytest.raises(GitHubProviderTransportError) as exc:
        provider.read_repository_settings("example-org/example-repo")
    assert "ghp_secret" not in str(exc.value)


def test_duplicate_apply_is_idempotent() -> None:
    state = {"var": "client-1"}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/repos/example-org/example-repo"):
            return _response(200, {"id": 7, "default_branch": "main"})
        if path.endswith("/environments"):
            return _response(200, {"environments": [{"name": "foundry-production"}]})
        if path.endswith("/environments/copilot/variables"):
            return _response(200, {"variables": []})
        if path.endswith("/environments/foundry-production/variables"):
            return _response(200, {"variables": [{"name": "AZURE_OPTIMIZER_CLIENT_ID", "value": "client-1"}]})
        if path.endswith("/environments/foundry-production/variables/AZURE_OPTIMIZER_CLIENT_ID"):
            return _response(200, {"name": "AZURE_OPTIMIZER_CLIENT_ID", "value": state["var"]})
        if path.endswith("/environments/copilot/deployment-branch-policies"):
            return _response(200, {"branch_policies": []})
        if path.endswith("/environments/foundry-production/deployment-branch-policies"):
            return _response(200, {"branch_policies": [{"name": "main"}]})
        if path.endswith("/actions/variables"):
            return _response(200, {"variables": []})
        raise AssertionError(path)

    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler))
    receipt = provider.apply_changes(_plan(BootstrapAction(action_id="var-prod", phase="github", stage="planned", kind="github-variable", diagnostics=("foundry-production", "client-1"))))
    assert receipt.changed_actions == ()
    assert receipt.adopted_actions == ("var-prod",)


def test_partial_apply_compensates_created_environment() -> None:
    state = {"created": False, "deleted": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/repos/example-org/example-repo"):
            return _response(200, {"id": 7, "default_branch": "main"})
        if request.method == "GET" and path.endswith("/environments"):
            return _response(200, {"environments": []})
        if request.method == "PUT" and path.endswith("/environments/foundry-production"):
            state["created"] = True
            return _response(200, {})
        if request.method == "DELETE" and path.endswith("/environments/foundry-production"):
            state["deleted"] = True
            return _response(204, {})
        if request.method == "GET" and (path.endswith("/environments/copilot/variables") or path.endswith("/environments/foundry-production/variables")):
            return _response(200, {"variables": []})
        if request.method == "GET" and (path.endswith("/environments/copilot/deployment-branch-policies") or path.endswith("/environments/foundry-production/deployment-branch-policies")):
            return _response(200, {"branch_policies": []})
        if request.method == "POST" and path.endswith("/environments/foundry-production/variables"):
            return _response(403, {})
        if path.endswith("/environments/foundry-production/variables/AZURE_OPTIMIZER_CLIENT_ID"):
            return _response(404, {})
        if path.endswith("/actions/variables"):
            return _response(200, {"variables": []})
        raise AssertionError((request.method, path))

    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler))
    with pytest.raises(GitHubProviderApplyError):
        provider.apply_changes(_plan(
            BootstrapAction(action_id="env-prod", phase="github", stage="planned", kind="github-environment", diagnostics=("foundry-production",)),
            BootstrapAction(action_id="var-prod", phase="github", stage="planned", kind="github-variable", diagnostics=("foundry-production", "client-1")),
        ))
    assert state["deleted"] is True


def test_invalid_repository_rejected() -> None:
    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(lambda request: _response(200, {})))
    with pytest.raises(GitHubProviderError):
        provider.read_repository_settings("not canonical")
