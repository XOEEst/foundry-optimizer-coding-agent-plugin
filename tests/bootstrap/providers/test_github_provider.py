from __future__ import annotations

import json

import httpx
import pytest

from foundry_opt.bootstrap.contracts import BootstrapAction, BootstrapPlan, FingerprintRecord
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


def _stateful_handler(state: dict[str, object], log: list[tuple[str, str, object | None]]):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if path.endswith("/repos/example-org/example-repo"):
            return _response(200, {"id": 7, "default_branch": "release", "full_name": "example-org/example-repo"})
        if path.endswith("/environments/copilot"):
            return _response(404, {})
        if path.endswith("/actions/variables"):
            return _response(200, {"variables": []})
        if path.endswith("/environments/foundry-production") and method == "GET":
            if not state["env_exists"]:
                return _response(404, {})
            return _response(200, {"name": "foundry-production", "deployment_branch_policy": state["policy"]})
        if path.endswith("/environments/foundry-production") and method == "PUT":
            payload = _body(request)["deployment_branch_policy"]
            log.append((method, path, payload))
            state["env_exists"] = True
            state["policy"] = payload
            return _response(200, {})
        if path.endswith("/environments/foundry-production") and method == "DELETE":
            log.append((method, path, None))
            state["env_exists"] = False
            state["policy"] = None
            state["variable_value"] = None
            state["branch_policies"] = []
            return _response(204, {})
        if path.endswith("/variables/AZURE_OPTIMIZER_CLIENT_ID") and method == "GET":
            if not state["env_exists"] or state["variable_value"] is None:
                return _response(404, {})
            return _response(200, {"name": "AZURE_OPTIMIZER_CLIENT_ID", "value": state["variable_value"]})
        if path.endswith("/variables/AZURE_OPTIMIZER_CLIENT_ID") and method == "PATCH":
            payload = _body(request)
            log.append((method, path, payload))
            state["variable_value"] = payload["value"]
            return _response(204, {})
        if path.endswith("/variables") and method == "GET":
            variables = [] if state["variable_value"] is None else [{"name": "AZURE_OPTIMIZER_CLIENT_ID", "value": state["variable_value"]}]
            return _response(200, {"variables": variables})
        if path.endswith("/variables") and method == "POST":
            payload = _body(request)
            log.append((method, path, payload))
            state["variable_value"] = payload["value"]
            return _response(201, {})
        if path.endswith("/variables/AZURE_OPTIMIZER_CLIENT_ID") and method == "DELETE":
            log.append((method, path, None))
            state["variable_value"] = None
            return _response(204, {})
        if path.endswith("/deployment_branch_policies") and method == "GET":
            return _response(200, {"branch_policies": state["branch_policies"]})
        if path.endswith("/deployment_branch_policies") and method == "POST":
            payload = _body(request)
            log.append((method, path, payload))
            state["branch_policies"] = [{"id": state["next_policy_id"], "name": payload["name"], "type": payload["type"]}]
            return _response(201, {})
        if "/deployment_branch_policies/" in path and method == "DELETE":
            log.append((method, path, None))
            state["branch_policies"] = []
            return _response(204, {})
        raise AssertionError((method, path))

    return handler


def test_export_restore_verify_rollback_restart_flow() -> None:
    state = {
        "env_exists": False,
        "policy": None,
        "variable_value": None,
        "branch_policies": [],
        "next_policy_id": 41,
    }
    log: list[tuple[str, str, object | None]] = []
    transport = FakeGitHubTransport(_stateful_handler(state, log))
    plan = _plan(
        BootstrapAction(action_id="env", phase="github", stage="planned", kind="github-environment", diagnostics=("foundry-production",)),
        BootstrapAction(action_id="var", phase="github", stage="planned", kind="github-variable", diagnostics=("foundry-production", "client-id")),
        BootstrapAction(action_id="branch", phase="github", stage="planned", kind="github-branch-policy", diagnostics=("foundry-production", "release")),
    )

    provider1 = GitHubBootstrapProvider(token="ghp_secret", transport=transport)
    receipt = provider1.apply_changes(plan)
    exported = provider1.export_provider_state(receipt)

    assert exported["receipt_hash"] == receipt.receipt_hash
    assert exported["operation_id"] == receipt.operation_id
    assert exported["repository"] == receipt.repository_identity
    assert "token" not in json.dumps(exported)

    provider2 = GitHubBootstrapProvider(token="ghp_secret", transport=transport)
    provider2.restore_provider_state(exported)
    live = provider2.live_fingerprints(receipt)
    assert {item.label for item in live} == {"env:live", "var:live", "branch:live"}
    assert provider2.verify_changes(receipt) is True

    provider2.rollback_changes(receipt)
    assert provider2.verify_rollback(receipt) is True
    assert state["env_exists"] is False
    assert state["variable_value"] is None
    assert state["branch_policies"] == []
    assert ("DELETE", "/repos/example-org/example-repo/environments/foundry-production/deployment_branch_policies/41", None) in log


def test_restore_rejects_tampered_hash_repo_and_operation() -> None:
    state = {"env_exists": False, "policy": None, "variable_value": None, "branch_policies": [], "next_policy_id": 41}
    transport = FakeGitHubTransport(_stateful_handler(state, []))
    provider1 = GitHubBootstrapProvider(token="ghp_secret", transport=transport)
    receipt = provider1.apply_changes(_plan(BootstrapAction(action_id="env", phase="github", stage="planned", kind="github-environment", diagnostics=("foundry-production",))))
    exported = provider1.export_provider_state(receipt)

    provider2 = GitHubBootstrapProvider(token="ghp_secret", transport=transport)
    provider2.restore_provider_state(exported)

    bad_hash = dict(exported)
    bad_hash["receipt_hash"] = "0" * 64
    provider3 = GitHubBootstrapProvider(token="ghp_secret", transport=transport)
    provider3.restore_provider_state(bad_hash)
    with pytest.raises(GitHubProviderApplyError, match="does not match"):
        provider3.verify_changes(receipt)

    bad_repo = dict(exported)
    bad_repo["repository"] = "example-org/other-repo"
    provider4 = GitHubBootstrapProvider(token="ghp_secret", transport=transport)
    provider4.restore_provider_state(bad_repo)
    with pytest.raises(GitHubProviderApplyError, match="does not match"):
        provider4.rollback_changes(receipt)

    bad_op = dict(exported)
    bad_op["operation_id"] = "other-op"
    provider5 = GitHubBootstrapProvider(token="ghp_secret", transport=transport)
    provider5.restore_provider_state(bad_op)
    with pytest.raises(GitHubProviderApplyError, match="does not match"):
        provider5.live_fingerprints(receipt)


def test_restore_rejects_partial_state() -> None:
    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(lambda request: _response(200, {})))
    with pytest.raises(GitHubProviderApplyError, match="version"):
        provider.restore_provider_state({"receipt_hash": "0" * 64})
    with pytest.raises(GitHubProviderApplyError, match="repository"):
        provider.restore_provider_state({"version": 1, "receipt_hash": "0" * 64, "operation_id": "op-1", "snapshots": []})


def test_restart_rollback_preserves_adopted_and_restores_changed_state() -> None:
    state = {
        "env_exists": True,
        "policy": {"protected_branches": True, "custom_branch_policies": False},
        "variable_value": "old-client",
        "branch_policies": [],
        "next_policy_id": 52,
    }
    log: list[tuple[str, str, object | None]] = []
    transport = FakeGitHubTransport(_stateful_handler(state, log))
    plan = _plan(
        BootstrapAction(action_id="env", phase="github", stage="planned", kind="github-environment", diagnostics=("foundry-production",)),
        BootstrapAction(action_id="var", phase="github", stage="planned", kind="github-variable", diagnostics=("foundry-production", "new-client")),
        BootstrapAction(action_id="branch", phase="github", stage="planned", kind="github-branch-policy", diagnostics=("foundry-production", "release")),
    )

    provider1 = GitHubBootstrapProvider(token="ghp_secret", transport=transport)
    receipt = provider1.apply_changes(plan)
    assert receipt.adopted_actions == ("env",)
    exported = provider1.export_provider_state(receipt)

    provider2 = GitHubBootstrapProvider(token="ghp_secret", transport=transport)
    provider2.restore_provider_state(exported)
    provider2.rollback_changes(receipt)
    assert provider2.verify_rollback(receipt) is True
    assert state["env_exists"] is True
    assert state["variable_value"] == "old-client"
    assert state["policy"] == {"protected_branches": True, "custom_branch_policies": False}
    assert state["branch_policies"] == []
    delete_calls = [entry for entry in log if entry[0] == "DELETE" and "/environments/foundry-production" == entry[1]]
    assert delete_calls == []


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
