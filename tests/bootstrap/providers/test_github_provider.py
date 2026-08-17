from __future__ import annotations

import json

import httpx
import pytest

from foundry_opt.bootstrap.contracts import BootstrapAction, BootstrapPlan, FingerprintRecord
from foundry_opt.bootstrap.providers.github import (
    GitHubBootstrapProvider,
    GitHubProviderApplyError,
    GitHubProviderError,
    GitHubProviderRollbackError,
    GitHubProviderTransportError,
    rollback_failure_details,
)


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
    with pytest.raises(GitHubProviderApplyError, match="state hash"):
        provider3.restore_provider_state(bad_hash)

    bad_repo = dict(exported)
    bad_repo["repository"] = "example-org/other-repo"
    provider4 = GitHubBootstrapProvider(token="ghp_secret", transport=transport)
    with pytest.raises(GitHubProviderApplyError, match="state hash"):
        provider4.restore_provider_state(bad_repo)

    bad_op = dict(exported)
    bad_op["operation_id"] = "other-op"
    provider5 = GitHubBootstrapProvider(token="ghp_secret", transport=transport)
    with pytest.raises(GitHubProviderApplyError, match="state hash"):
        provider5.restore_provider_state(bad_op)


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


def test_apply_failure_error_graph_is_sanitized() -> None:
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
            return _response(204, {})
        if path.endswith("/variables") or path.endswith("/deployment_branch_policies") or path.endswith("/actions/variables"):
            return _response(200, {"variables": [], "branch_policies": []})
        raise AssertionError((request.method, path))

    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler))
    with pytest.raises(GitHubProviderApplyError) as exc:
        provider.apply_changes(_plan(BootstrapAction(action_id="env", phase="github", stage="planned", kind="github-environment", diagnostics=("foundry-production",))))
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


def test_malformed_json_response_error_graph_is_sanitized() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _response(200, content=b"not-json-super-secret-token-abc")

    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler))
    with pytest.raises(GitHubProviderError) as exc:
        provider.read_repository_settings("example-org/example-repo")
    assert exc.value.__cause__ is None
    assert exc.value.__context__ is None


def test_receipt_and_export_track_changed_and_created_compensation_components() -> None:
    state: dict[str, object] = {"env_exists": False, "value": None}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if path.endswith("/repos/example-org/example-repo"):
            return _response(200, {"id": 7, "default_branch": "main", "full_name": "example-org/example-repo"})
        if path.endswith("/environments/copilot"):
            return _response(404, {})
        if path.endswith("/environments/foundry-production") and method == "GET":
            if state["env_exists"]:
                return _response(200, {"name": "foundry-production", "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}})
            return _response(404, {})
        if path.endswith("/environments/foundry-production") and method == "PUT":
            state["env_exists"] = True
            return _response(200, {})
        if path.endswith("/variables/AZURE_OPTIMIZER_CLIENT_ID") and method == "GET":
            if state["value"] is None:
                return _response(404, {})
            return _response(200, {"name": "AZURE_OPTIMIZER_CLIENT_ID", "value": state["value"]})
        if path.endswith("/variables") and method == "POST":
            state["value"] = _body(request)["value"]
            return _response(201, {})
        if path.endswith("/variables") and method == "GET":
            return _response(200, {"variables": [] if state["value"] is None else [{"name": "AZURE_OPTIMIZER_CLIENT_ID", "value": state["value"]}]})
        if path.endswith("/deployment_branch_policies"):
            return _response(200, {"branch_policies": [{"id": 1, "name": "main", "type": "branch"}]})
        if path.endswith("/actions/variables"):
            return _response(200, {"variables": []})
        raise AssertionError((method, path))

    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler))
    plan = _plan(
        BootstrapAction(action_id="env", phase="github", stage="planned", kind="github-environment", diagnostics=("foundry-production",)),
        BootstrapAction(action_id="var", phase="github", stage="planned", kind="github-variable", diagnostics=("foundry-production", "client-id")),
        BootstrapAction(action_id="branch", phase="github", stage="planned", kind="github-branch-policy", diagnostics=("foundry-production", "main")),
    )
    receipt = provider.apply_changes(plan)

    assert receipt.created_actions == ("env",)
    assert receipt.changed_actions == ("var",)
    assert receipt.adopted_actions == ("branch",)
    # Components requiring compensation on rollback are exactly the created/changed
    # ones; the adopted branch policy must never be mutated by a later rollback.
    assert receipt.compensation_required_actions == ("env", "var")

    exported = provider.export_provider_state(receipt)
    ownership_by_target = {item["action_id"]: item["ownership"] for item in exported["snapshots"]}
    assert ownership_by_target == {"env": "created", "var": "changed", "branch": "adopted"}
    rollback_by_target = {item["action_id"]: item["rollback"] for item in exported["snapshots"]}
    assert rollback_by_target["branch"] == []


def test_apply_failure_rollback_refuses_when_earlier_component_drifted_externally() -> None:
    state = {"created": False, "verified_once": False}
    deletes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if path.endswith("/repos/example-org/example-repo"):
            return _response(200, {"id": 7, "default_branch": "main", "full_name": "example-org/example-repo"})
        if path.endswith("/environments/copilot"):
            return _response(404, {})
        if path.endswith("/environments/foundry-production") and method == "GET":
            if not state["created"]:
                return _response(404, {})
            if not state["verified_once"]:
                # This is the create-verification read-back performed immediately
                # after the PUT; it must match what was just written.
                state["verified_once"] = True
                return _response(200, {"name": "foundry-production", "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}})
            # An external actor changes the environment's branch policy after this
            # operation created it but before the failure-triggered rollback runs.
            return _response(200, {"name": "foundry-production", "deployment_branch_policy": {"protected_branches": True, "custom_branch_policies": False}})
        if path.endswith("/environments/foundry-production") and method == "PUT":
            state["created"] = True
            return _response(200, {})
        if path.endswith("/environments/foundry-production") and method == "DELETE":
            deletes.append(path)
            return _response(204, {})
        if path.endswith("/variables/AZURE_OPTIMIZER_CLIENT_ID"):
            return _response(404, {})
        if path.endswith("/variables") and method == "POST":
            return _response(201, {})
        if path.endswith("/variables") and method == "GET":
            return _response(200, {"variables": []})
        if path.endswith("/deployment_branch_policies"):
            return _response(200, {"branch_policies": []})
        if path.endswith("/actions/variables"):
            return _response(200, {"variables": []})
        raise AssertionError((method, path))

    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler))
    plan = _plan(
        BootstrapAction(action_id="env", phase="github", stage="planned", kind="github-environment", diagnostics=("foundry-production",)),
        BootstrapAction(action_id="var", phase="github", stage="planned", kind="github-variable", diagnostics=("foundry-production", "client-id")),
    )
    with pytest.raises(GitHubProviderRollbackError, match="rollback failed"):
        provider.apply_changes(plan)

    # The environment must not be deleted while its live state has drifted from
    # what this operation last observed: rollback refuses external drift.
    assert deletes == []


def test_compensation_receipt_survives_rollback_failure_and_can_be_retried() -> None:
    state = {"env_exists": False, "delete_should_fail": True}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if path.endswith("/repos/example-org/example-repo"):
            return _response(200, {"id": 7, "default_branch": "main", "full_name": "example-org/example-repo"})
        if path.endswith("/environments/copilot"):
            return _response(404, {})
        if path.endswith("/environments/foundry-production") and method == "GET":
            if state["env_exists"]:
                return _response(200, {"name": "foundry-production", "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}})
            return _response(404, {})
        if path.endswith("/environments/foundry-production") and method == "PUT":
            state["env_exists"] = True
            return _response(200, {})
        if path.endswith("/environments/foundry-production") and method == "DELETE":
            if state["delete_should_fail"]:
                return _response(403, {"message": "temporary failure, please retry"})
            state["env_exists"] = False
            return _response(204, {})
        if path.endswith("/variables/AZURE_OPTIMIZER_CLIENT_ID") and method == "DELETE":
            return _response(204, {})
        if path.endswith("/variables/AZURE_OPTIMIZER_CLIENT_ID"):
            return _response(404, {})
        if path.endswith("/variables") and method == "POST":
            return _response(201, {})
        if path.endswith("/variables") and method == "GET":
            return _response(200, {"variables": []})
        if path.endswith("/deployment_branch_policies"):
            return _response(200, {"branch_policies": []})
        if path.endswith("/actions/variables"):
            return _response(200, {"variables": []})
        raise AssertionError((method, path))

    transport = FakeGitHubTransport(handler)
    provider = GitHubBootstrapProvider(token="ghp_secret", transport=transport)
    plan = _plan(
        BootstrapAction(action_id="env", phase="github", stage="planned", kind="github-environment", diagnostics=("foundry-production",)),
        BootstrapAction(action_id="var", phase="github", stage="planned", kind="github-variable", diagnostics=("foundry-production", "client-id")),
    )

    with pytest.raises(GitHubProviderRollbackError) as exc:
        provider.apply_changes(plan)

    compensation_receipt, provider_state = rollback_failure_details(exc.value)
    assert compensation_receipt is not None
    assert compensation_receipt.created_actions == ("env",)
    assert compensation_receipt.compensation_required_actions == ("env", "var")
    assert "ghp_secret" not in json.dumps(provider_state)

    # Simulate a process restart with a fresh provider instance retrying compensation
    # after the transient rollback failure has cleared.
    state["delete_should_fail"] = False
    provider2 = GitHubBootstrapProvider(token="ghp_secret", transport=transport)
    provider2.restore_provider_state(provider_state)
    provider2.rollback_changes(compensation_receipt)
    assert provider2.verify_rollback(compensation_receipt) is True
    assert state["env_exists"] is False


def test_branch_policy_survives_default_branch_rename_after_apply() -> None:
    state = {"repo_calls": 0, "branch_policies": []}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if path.endswith("/repos/example-org/example-repo"):
            state["repo_calls"] = state["repo_calls"] + 1
            # The repository's default branch is renamed to "main" immediately
            # after this operation applies its branch policy for "release".
            branch = "release" if state["repo_calls"] == 1 else "main"
            return _response(200, {"id": 7, "default_branch": branch, "full_name": "example-org/example-repo"})
        if path.endswith("/environments/copilot"):
            return _response(404, {})
        if path.endswith("/environments/foundry-production"):
            return _response(200, {"name": "foundry-production", "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}})
        if path.endswith("/variables/AZURE_OPTIMIZER_CLIENT_ID"):
            return _response(404, {})
        if path.endswith("/variables"):
            return _response(200, {"variables": []})
        if path.endswith("/deployment_branch_policies") and method == "POST":
            state["branch_policies"] = [{"id": 5, "name": "release", "type": "branch"}]
            return _response(201, {})
        if path.endswith("/deployment_branch_policies") and method == "GET":
            return _response(200, {"branch_policies": state["branch_policies"]})
        if "/deployment_branch_policies/" in path and method == "DELETE":
            state["branch_policies"] = []
            return _response(204, {})
        if path.endswith("/actions/variables"):
            return _response(200, {"variables": []})
        raise AssertionError((method, path))

    provider = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler))
    receipt = provider.apply_changes(_plan(BootstrapAction(action_id="branch", phase="github", stage="planned", kind="github-branch-policy", diagnostics=("foundry-production", "release"))))
    assert receipt.changed_actions == ("branch",)

    # verify/rollback below observe a repository default_branch of "main", yet the
    # branch policy's exact identity ("release") must still resolve correctly.
    assert provider.verify_changes(receipt) is True
    provider.rollback_changes(receipt)
    assert provider.verify_rollback(receipt) is True
    assert state["branch_policies"] == []


def test_branch_identity_round_trips_through_export_restore() -> None:
    state = {"branch_policies": [{"id": 9, "name": "release", "type": "branch"}]}

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
        if path.endswith("/deployment_branch_policies"):
            return _response(200, {"branch_policies": state["branch_policies"]})
        if path.endswith("/actions/variables"):
            return _response(200, {"variables": []})
        raise AssertionError((request.method, path))

    provider1 = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler))
    receipt = provider1.apply_changes(_plan(BootstrapAction(action_id="branch", phase="github", stage="planned", kind="github-branch-policy", diagnostics=("foundry-production", "release"))))
    exported = provider1.export_provider_state(receipt)
    assert exported["snapshots"][0]["branch_name"] == "release"

    provider2 = GitHubBootstrapProvider(token="ghp_secret", transport=FakeGitHubTransport(handler))
    provider2.restore_provider_state(exported)
    assert provider2.verify_changes(receipt) is True
