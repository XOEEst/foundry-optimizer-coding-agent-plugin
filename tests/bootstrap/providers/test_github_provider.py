from __future__ import annotations

import json

import httpx
import pytest

from foundry_opt.bootstrap.canonical import canonical_sha256
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
            existing = next((item for item in state["branch_policies"] if item["name"] == payload["name"] and item["type"] == payload["type"]), None)
            if existing is not None:
                return _response(303, {})
            state["branch_policies"] = [{"id": state["next_policy_id"], "name": payload["name"], "type": payload["type"]}]
            return _response(201, {})
        if "/deployment_branch_policies/" in path and method == "DELETE":
            log.append((method, path, None))
            policy_id = int(path.rsplit("/", 1)[1])
            state["branch_policies"] = [item for item in state["branch_policies"] if item["id"] != policy_id]
            return _response(204, {})
        raise AssertionError((method, path))

    return handler


def test_exported_state_uses_state_hash_and_redacts_token() -> None:
    state = {"env_exists": False, "policy": None, "variable_value": None, "branch_policies": [], "next_policy_id": 41}
    transport = FakeGitHubTransport(_stateful_handler(state, []))
    provider = GitHubBootstrapProvider(token="ghp_secret", transport=transport)
    receipt = provider.apply_changes(_plan(BootstrapAction(action_id="env", phase="github", stage="planned", kind="github-environment", diagnostics=("foundry-production",))))
    exported = provider.export_provider_state(receipt)

    assert exported["state_hash"] == provider._last_apply_binding.state_hash
    assert exported["environments"] == [{"name": "foundry-production", "exists": False, "deployment_branch_policy": None, "variable": {"exists": False, "value": None}, "branch_policy": {"exists": False, "policy_id": None, "name": None, "type": None}}]
    assert "token" not in json.dumps(exported)


def test_restore_rejects_tampered_state_hash_and_target_with_same_receipt() -> None:
    state = {"env_exists": False, "policy": None, "variable_value": None, "branch_policies": [], "next_policy_id": 41}
    transport = FakeGitHubTransport(_stateful_handler(state, []))
    provider = GitHubBootstrapProvider(token="ghp_secret", transport=transport)
    receipt = provider.apply_changes(_plan(BootstrapAction(action_id="env", phase="github", stage="planned", kind="github-environment", diagnostics=("foundry-production",))))
    exported = provider.export_provider_state(receipt)

    tampered_hash = dict(exported)
    tampered_hash["state_hash"] = "0" * 64
    with pytest.raises(GitHubProviderApplyError, match="state hash"):
        GitHubBootstrapProvider(token="ghp_secret", transport=transport).restore_provider_state(tampered_hash)

    tampered_target = json.loads(json.dumps(exported))
    tampered_target["snapshots"][0]["target"] = "copilot"
    with pytest.raises(GitHubProviderApplyError, match="state hash"):
        GitHubBootstrapProvider(token="ghp_secret", transport=transport).restore_provider_state(tampered_target)


def test_restore_rejects_invalid_snapshot_kind_and_rollback_operation() -> None:
    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(lambda request: _response(200, {})))
    base = {
        "version": 2,
        "receipt_hash": "1" * 64,
        "operation_id": "op-1",
        "repository": "example-org/example-repo",
        "snapshots": [{"action_id": "env", "kind": "github-environment", "target": "foundry-production", "before": {}, "expected_after": {}, "ownership": "created", "rollback": [["delete_environment", "foundry-production", None]]}],
        "environments": [{"name": "foundry-production", "exists": False, "deployment_branch_policy": None, "variable": {"exists": False, "value": None}, "branch_policy": {"exists": False, "policy_id": None, "name": None, "type": None}}],
    }
    payload = dict(base)
    payload["state_hash"] = canonical_sha256({k: v for k, v in payload.items() if k != "state_hash"})
    bad_kind = json.loads(json.dumps(payload))
    bad_kind["snapshots"][0]["kind"] = "github-arbitrary"
    bad_kind["state_hash"] = canonical_sha256({k: v for k, v in bad_kind.items() if k != "state_hash"})
    with pytest.raises(GitHubProviderApplyError, match="snapshot kind"):
        provider.restore_provider_state(bad_kind)
    bad_rollback = json.loads(json.dumps(payload))
    bad_rollback["snapshots"][0]["rollback"] = [["exec", "foundry-production", None]]
    bad_rollback["state_hash"] = canonical_sha256({k: v for k, v in bad_rollback.items() if k != "state_hash"})
    with pytest.raises(GitHubProviderApplyError, match="rollback operation"):
        provider.restore_provider_state(bad_rollback)


def test_branch_and_env_against_missing_env_rolls_back_by_deleting_environment() -> None:
    state = {"env_exists": False, "policy": None, "variable_value": None, "branch_policies": [], "next_policy_id": 41}
    log: list[tuple[str, str, object | None]] = []
    transport = FakeGitHubTransport(_stateful_handler(state, log))
    plan = _plan(
        BootstrapAction(action_id="env", phase="github", stage="planned", kind="github-environment", diagnostics=("foundry-production",)),
        BootstrapAction(action_id="branch", phase="github", stage="planned", kind="github-branch-policy", diagnostics=("foundry-production", "release")),
    )
    provider1 = GitHubBootstrapProvider(token="ghp_secret", transport=transport)
    receipt = provider1.apply_changes(plan)
    exported = provider1.export_provider_state(receipt)

    provider2 = GitHubBootstrapProvider(token="ghp_secret", transport=transport)
    provider2.restore_provider_state(exported)
    provider2.rollback_changes(receipt)
    assert provider2.verify_rollback(receipt) is True
    assert state["env_exists"] is False
    assert ("DELETE", "/repos/example-org/example-repo/environments/foundry-production", None) in log


def test_duplicate_environment_actions_rollback_and_verify_from_original_aggregate_state() -> None:
    state = {"env_exists": False, "policy": None, "variable_value": None, "branch_policies": [], "next_policy_id": 99}
    log: list[tuple[str, str, object | None]] = []
    transport = FakeGitHubTransport(_stateful_handler(state, log))
    plan = _plan(
        BootstrapAction(action_id="env-1", phase="github", stage="planned", kind="github-environment", diagnostics=("foundry-production",)),
        BootstrapAction(action_id="env-2", phase="github", stage="planned", kind="github-environment", diagnostics=("foundry-production",)),
        BootstrapAction(action_id="var", phase="github", stage="planned", kind="github-variable", diagnostics=("foundry-production", "client-id")),
    )
    provider1 = GitHubBootstrapProvider(token="ghp_secret", transport=transport)
    receipt = provider1.apply_changes(plan)
    assert receipt.created_actions == ("env-1",)
    assert receipt.adopted_actions == ("env-2",)
    exported = provider1.export_provider_state(receipt)

    provider2 = GitHubBootstrapProvider(token="ghp_secret", transport=transport)
    provider2.restore_provider_state(exported)
    provider2.rollback_changes(receipt)
    assert provider2.verify_rollback(receipt) is True
    assert state["env_exists"] is False
    assert state["variable_value"] is None


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
    exported = provider1.export_provider_state(receipt)

    provider2 = GitHubBootstrapProvider(token="ghp_secret", transport=transport)
    provider2.restore_provider_state(exported)
    provider2.rollback_changes(receipt)
    assert provider2.verify_rollback(receipt) is True
    assert state["env_exists"] is True
    assert state["policy"] == {"protected_branches": True, "custom_branch_policies": False}
    assert state["variable_value"] == "old-client"
    assert state["branch_policies"] == []
    assert [entry for entry in log if entry[0] == "DELETE" and entry[1].endswith("/environments/foundry-production")] == []


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
