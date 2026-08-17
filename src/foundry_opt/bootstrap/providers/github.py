from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from foundry_opt.bootstrap.canonical import canonical_sha256
from foundry_opt.bootstrap.contracts import BootstrapAction, BootstrapPlan, BootstrapReceipt, FingerprintRecord
from foundry_opt.bootstrap.errors import BootstrapApplyError, BootstrapProviderError

_OWNER_REPO_PATTERN = re.compile(r"^(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}[A-Za-z0-9])?)/(?P<repo>[A-Za-z0-9_.-]{1,100})$")
_LINK_REL_PATTERN = re.compile(r'<([^>]+)>;\s*rel="([^"]+)"')
_ENVIRONMENTS = ("copilot", "foundry-production")
_VAR_NAME = "AZURE_OPTIMIZER_CLIENT_ID"
_API_VERSION = "2022-11-28"
_JSON_LIMIT = 64 * 1024


class GitHubProviderError(BootstrapProviderError):
    pass


class GitHubProviderApplyError(BootstrapApplyError):
    pass


class GitHubProviderTransportError(GitHubProviderError):
    pass


@dataclass(frozen=True)
class _VariableState:
    exists: bool
    value: str | None


@dataclass(frozen=True)
class _BranchPolicyState:
    exists: bool
    policy_id: int | None
    name: str | None
    type: str | None


@dataclass(frozen=True)
class _EnvironmentState:
    name: str
    exists: bool
    deployment_branch_policy: Mapping[str, object] | None
    variables: tuple[Mapping[str, object], ...]
    variable_state: _VariableState
    branch_policies: tuple[Mapping[str, object], ...]
    requested_branch_policy: _BranchPolicyState


@dataclass(frozen=True)
class _ActionSnapshot:
    action_id: str
    kind: str
    target: str
    before: object
    expected_after: object
    ownership: str
    rollback: tuple[tuple[str, str, object], ...]


@dataclass(frozen=True)
class _ApplyBinding:
    receipt_hash: str
    operation_id: str
    repository: str
    snapshots: tuple[_ActionSnapshot, ...]


def _bounded_text(value: object, *, field: str, max_length: int = 255) -> str:
    if not isinstance(value, str) or not value:
        raise GitHubProviderError(f"{field} must be a non-empty string")
    if len(value) > max_length:
        raise GitHubProviderError(f"{field} exceeds its bounded length")
    return value


def _canonical_repo(repository: str) -> tuple[str, str]:
    value = _bounded_text(repository, field="repository")
    match = _OWNER_REPO_PATTERN.fullmatch(value)
    if match is None:
        raise GitHubProviderError("repository must be canonical owner/repo")
    return match.group("owner"), match.group("repo")


def _parse_links(value: str | None) -> Mapping[str, str]:
    if not value:
        return {}
    return {rel: url for url, rel in _LINK_REL_PATTERN.findall(value)}


def _redacted_error(message: str) -> GitHubProviderApplyError:
    error = GitHubProviderApplyError(message)
    error.__cause__ = None
    error.__context__ = None
    return error


def _redacted_transport_error(message: str) -> GitHubProviderTransportError:
    error = GitHubProviderTransportError(message)
    error.__cause__ = None
    error.__context__ = None
    return error


def _fingerprint(label: str, value: object) -> FingerprintRecord:
    return FingerprintRecord(label=label, sha256=canonical_sha256(value))


class GitHubBootstrapProvider:
    def __init__(
        self,
        *,
        token: str,
        transport: httpx.BaseTransport | None = None,
        http_client: httpx.Client | None = None,
        timeout: float = 10.0,
        json_max_bytes: int = _JSON_LIMIT,
    ) -> None:
        if http_client is not None and transport is not None:
            raise ValueError("provide either http_client or transport, not both")
        self._token = _bounded_text(token, field="token", max_length=4096)
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(
            transport=transport,
            base_url="https://api.github.com",
            follow_redirects=False,
            trust_env=False,
            timeout=timeout,
        )
        self._json_max_bytes = int(json_max_bytes)
        self._last_apply_binding: _ApplyBinding | None = None

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def read_repository_settings(self, repository: str) -> Mapping[str, object]:
        owner, repo = _canonical_repo(repository)
        payload = self._get_json(f"/repos/{owner}/{repo}")
        full_name = _bounded_text(payload.get("full_name"), field="full_name")
        if full_name.casefold() != repository.casefold():
            raise GitHubProviderError("repository full_name did not match requested identity")
        repository_id = payload.get("id")
        default_branch = _bounded_text(payload.get("default_branch"), field="default_branch")
        if not isinstance(repository_id, int) or repository_id <= 0:
            raise GitHubProviderError("repository id is invalid")
        return {
            "repository": full_name,
            "repository_id": repository_id,
            "default_branch": default_branch,
            "environments": self.inventory_environments(full_name, default_branch=default_branch),
        }

    def inventory_environments(self, repository: str, *, default_branch: str | None = None) -> Sequence[Mapping[str, object]]:
        owner, repo = _canonical_repo(repository)
        branch = default_branch or self.read_repository_settings(repository)["default_branch"]
        results: list[Mapping[str, object]] = []
        for env_name in _ENVIRONMENTS:
            env = self._inventory_environment(owner, repo, env_name, branch)
            results.append(
                {
                    "name": env.name,
                    "exists": env.exists,
                    "deployment_branch_policy": env.deployment_branch_policy,
                    "variables": env.variables,
                    "branch_policies": env.branch_policies,
                }
            )
        repo_variables = tuple(self._list_paginated(f"/repos/{owner}/{repo}/actions/variables", "variables", allow_404=True))
        results.append({"name": "repository", "exists": True, "variables": repo_variables})
        return tuple(results)

    def plan_changes(self, plan: BootstrapPlan) -> Sequence[BootstrapAction]:
        repository = self.read_repository_settings(plan.repository_identity)["repository"]
        result: list[BootstrapAction] = []
        for action in plan.actions:
            if action.phase != "github":
                continue
            result.append(action.model_copy(update={"diagnostics": action.diagnostics + (repository,)}))
        return tuple(result)

    def apply_changes(self, plan: BootstrapPlan) -> BootstrapReceipt:
        repository_state = self.read_repository_settings(plan.repository_identity)
        repository = _bounded_text(repository_state["repository"], field="repository")
        default_branch = _bounded_text(repository_state["default_branch"], field="default_branch")
        owner, repo = _canonical_repo(repository)
        snapshots: list[_ActionSnapshot] = []
        created: list[str] = []
        adopted: list[str] = []
        changed: list[str] = []
        try:
            for action in self.plan_changes(plan):
                snapshot = self._apply_action(owner, repo, default_branch, action, snapshots)
                if snapshot is None:
                    continue
                snapshots.append(snapshot)
                if snapshot.ownership == "created":
                    created.append(snapshot.action_id)
                elif snapshot.ownership == "adopted":
                    adopted.append(snapshot.action_id)
                else:
                    changed.append(snapshot.action_id)
            self._verify_final_state(owner, repo, default_branch, snapshots)
        except GitHubProviderError as exc:
            self._rollback_snapshots(owner, repo, snapshots)
            raise _redacted_error(str(exc))
        receipt = BootstrapReceipt.create(
            operation_id=plan.operation_id,
            runtime_repository=plan.runtime_repository,
            runtime_commit=plan.runtime_commit,
            repository_identity=repository,
            plan_hash=plan.plan_hash,
            before_fingerprints=tuple(_fingerprint(f"{snapshot.action_id}:before", snapshot.before) for snapshot in snapshots),
            after_fingerprints=tuple(_fingerprint(f"{snapshot.action_id}:after", snapshot.expected_after) for snapshot in snapshots),
            created_actions=tuple(created),
            adopted_actions=tuple(adopted),
            changed_actions=tuple(changed),
        )
        self._last_apply_binding = _ApplyBinding(receipt.receipt_hash, receipt.operation_id, repository, tuple(snapshots))
        return receipt

    def verify_changes(self, receipt: BootstrapReceipt) -> bool:
        binding = self._validate_receipt_binding(receipt)
        owner, repo = _canonical_repo(binding.repository)
        default_branch = self.read_repository_settings(binding.repository)["default_branch"]
        self._verify_final_state(owner, repo, default_branch, binding.snapshots)
        return True

    def rollback_changes(self, receipt: BootstrapReceipt) -> None:
        binding = self._validate_receipt_binding(receipt)
        owner, repo = _canonical_repo(binding.repository)
        self._rollback_snapshots(owner, repo, list(binding.snapshots))

    def _validate_receipt_binding(self, receipt: BootstrapReceipt) -> _ApplyBinding:
        binding = self._last_apply_binding
        if binding is None:
            raise GitHubProviderApplyError("no apply binding is available for rollback or verification")
        if binding.receipt_hash != receipt.receipt_hash or binding.operation_id != receipt.operation_id or binding.repository != receipt.repository_identity:
            raise GitHubProviderApplyError("receipt does not match the current provider apply binding")
        return binding

    def _apply_action(
        self,
        owner: str,
        repo: str,
        default_branch: str,
        action: BootstrapAction,
        snapshots: list[_ActionSnapshot],
    ) -> _ActionSnapshot | None:
        if action.kind == "github-environment":
            env_name = _bounded_text(action.diagnostics[0], field="environment")
            env = self._inventory_environment(owner, repo, env_name, default_branch)
            before = {"exists": env.exists, "deployment_branch_policy": env.deployment_branch_policy}
            if env.exists:
                return _ActionSnapshot(action.action_id, action.kind, env_name, before, before, "adopted", ())
            rollback = (("delete_environment", env_name, None),)
            snapshots.append(_ActionSnapshot(action.action_id, action.kind, env_name, before, {"pending": True}, "created", rollback))
            self._put(f"/repos/{owner}/{repo}/environments/{env_name}", {"deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}})
            after = self._inventory_environment(owner, repo, env_name, default_branch)
            if not after.exists:
                raise GitHubProviderError("created environment verification failed")
            snapshots.pop()
            return _ActionSnapshot(
                action.action_id,
                action.kind,
                env_name,
                before,
                {"exists": True, "deployment_branch_policy": after.deployment_branch_policy},
                "created",
                rollback,
            )
        if action.kind == "github-variable":
            env_name = _bounded_text(action.diagnostics[0], field="environment")
            value = _bounded_text(action.diagnostics[1], field="client_id", max_length=512)
            env = self._inventory_environment(owner, repo, env_name, default_branch)
            before = {"exists": env.variable_state.exists, "value": env.variable_state.value}
            if env.variable_state.exists and env.variable_state.value == value:
                return _ActionSnapshot(action.action_id, action.kind, env_name, before, before, "adopted", ())
            rollback = (("restore_variable", env_name, before) if env.variable_state.exists else ("delete_variable", env_name, None),)
            snapshots.append(_ActionSnapshot(action.action_id, action.kind, env_name, before, {"pending": True}, "changed", rollback))
            if env.variable_state.exists:
                self._patch(f"/repos/{owner}/{repo}/environments/{env_name}/variables/{_VAR_NAME}", {"name": _VAR_NAME, "value": value})
            else:
                self._post(f"/repos/{owner}/{repo}/environments/{env_name}/variables", {"name": _VAR_NAME, "value": value})
            variable = self._read_environment_variable(owner, repo, env_name, _VAR_NAME)
            if variable.value != value:
                raise GitHubProviderError("variable verification failed")
            snapshots.pop()
            return _ActionSnapshot(action.action_id, action.kind, env_name, before, {"exists": True, "value": value}, "changed", rollback)
        if action.kind == "github-branch-policy":
            env_name = _bounded_text(action.diagnostics[0], field="environment")
            branch_name = _bounded_text(action.diagnostics[1], field="default_branch")
            env = self._inventory_environment(owner, repo, env_name, branch_name)
            before = {
                "deployment_branch_policy": env.deployment_branch_policy,
                "branch_policy": {
                    "exists": env.requested_branch_policy.exists,
                    "policy_id": env.requested_branch_policy.policy_id,
                    "name": env.requested_branch_policy.name,
                    "type": env.requested_branch_policy.type,
                },
            }
            if env.requested_branch_policy.exists:
                return _ActionSnapshot(action.action_id, action.kind, env_name, before, before, "adopted", ())
            rollback: list[tuple[str, str, object]] = []
            if not self._policy_enabled(env.deployment_branch_policy):
                rollback.append(("restore_environment_policy", env_name, env.deployment_branch_policy))
            snapshots.append(_ActionSnapshot(action.action_id, action.kind, env_name, before, {"pending": True}, "changed", tuple(rollback)))
            self._put(
                f"/repos/{owner}/{repo}/environments/{env_name}",
                {"deployment_branch_policy": self._enabled_policy_payload(env.deployment_branch_policy)},
            )
            response = self._post(
                f"/repos/{owner}/{repo}/environments/{env_name}/deployment_branch_policies",
                {"name": branch_name, "type": "branch"},
                allow_statuses={200, 201, 303},
            )
            duplicate = response is not None and response.status_code == 303
            after = self._inventory_environment(owner, repo, env_name, branch_name)
            if not after.requested_branch_policy.exists:
                raise GitHubProviderError("branch policy verification failed")
            if not duplicate and after.requested_branch_policy.policy_id is not None:
                rollback.insert(0, ("delete_branch_policy", env_name, after.requested_branch_policy.policy_id))
            snapshots.pop()
            return _ActionSnapshot(
                action.action_id,
                action.kind,
                env_name,
                before,
                {
                    "deployment_branch_policy": after.deployment_branch_policy,
                    "branch_policy": {
                        "exists": True,
                        "policy_id": after.requested_branch_policy.policy_id,
                        "name": after.requested_branch_policy.name,
                        "type": after.requested_branch_policy.type,
                    },
                },
                "adopted" if duplicate else "changed",
                tuple(rollback),
            )
        raise GitHubProviderError(f"unsupported github action kind: {action.kind}")

    def _verify_final_state(self, owner: str, repo: str, default_branch: str, snapshots: Sequence[_ActionSnapshot]) -> None:
        merged: dict[tuple[str, str], _ActionSnapshot] = {}
        for snapshot in snapshots:
            merged[(snapshot.kind, snapshot.target)] = snapshot
        branch_policy_targets = {
            snapshot.target
            for snapshot in merged.values()
            if snapshot.kind == "github-branch-policy"
        }
        for snapshot in merged.values():
            if snapshot.kind == "github-environment":
                env = self._inventory_environment(owner, repo, snapshot.target, default_branch)
                if snapshot.target in branch_policy_targets:
                    if not env.exists:
                        raise GitHubProviderError(
                            f"verification failed for action {snapshot.action_id}"
                        )
                    continue
                current = {"exists": env.exists, "deployment_branch_policy": env.deployment_branch_policy}
            elif snapshot.kind == "github-variable":
                variable = self._read_environment_variable(owner, repo, snapshot.target, _VAR_NAME)
                current = {"exists": variable.exists, "value": variable.value}
            else:
                env = self._inventory_environment(owner, repo, snapshot.target, default_branch)
                current = {
                    "deployment_branch_policy": env.deployment_branch_policy,
                    "branch_policy": {
                        "exists": env.requested_branch_policy.exists,
                        "policy_id": env.requested_branch_policy.policy_id,
                        "name": env.requested_branch_policy.name,
                        "type": env.requested_branch_policy.type,
                    },
                }
            if canonical_sha256(current) != canonical_sha256(snapshot.expected_after):
                raise GitHubProviderError(f"verification failed for action {snapshot.action_id}")

    def _rollback_snapshots(self, owner: str, repo: str, snapshots: Sequence[_ActionSnapshot]) -> None:
        operations: list[tuple[str, str, object]] = []
        for snapshot in reversed(snapshots):
            operations.extend(snapshot.rollback)
        for operation, environment, value in operations:
            if operation == "delete_branch_policy":
                self._delete(f"/repos/{owner}/{repo}/environments/{environment}/deployment_branch_policies/{value}", allow_statuses={204, 404})
            elif operation == "restore_variable":
                previous = value if isinstance(value, Mapping) else {}
                previous_value = previous.get("value")
                if isinstance(previous_value, str):
                    self._patch(f"/repos/{owner}/{repo}/environments/{environment}/variables/{_VAR_NAME}", {"name": _VAR_NAME, "value": previous_value})
            elif operation == "delete_variable":
                self._delete(f"/repos/{owner}/{repo}/environments/{environment}/variables/{_VAR_NAME}", allow_statuses={204, 404})
            elif operation == "restore_environment_policy":
                self._put(f"/repos/{owner}/{repo}/environments/{environment}", {"deployment_branch_policy": value})
            elif operation == "delete_environment":
                self._delete(f"/repos/{owner}/{repo}/environments/{environment}", allow_statuses={204, 404})

    def _policy_enabled(self, payload: Mapping[str, object] | None) -> bool:
        return bool(isinstance(payload, Mapping) and payload.get("custom_branch_policies") is True and payload.get("protected_branches") is False)

    def _enabled_policy_payload(self, payload: Mapping[str, object] | None) -> Mapping[str, object]:
        result = dict(payload or {})
        result["protected_branches"] = False
        result["custom_branch_policies"] = True
        return result

    def _inventory_environment(self, owner: str, repo: str, env_name: str, branch_name: str) -> _EnvironmentState:
        payload = self._get_json(f"/repos/{owner}/{repo}/environments/{env_name}", allow_404=True)
        if not payload:
            return _EnvironmentState(env_name, False, None, (), _VariableState(False, None), (), _BranchPolicyState(False, None, None, None))
        policy_payload = payload.get("deployment_branch_policy")
        if policy_payload is not None and not isinstance(policy_payload, Mapping):
            raise GitHubProviderError("deployment_branch_policy payload is invalid")
        variables = tuple(self._list_paginated(f"/repos/{owner}/{repo}/environments/{env_name}/variables", "variables", allow_404=True))
        variable = self._read_environment_variable(owner, repo, env_name, _VAR_NAME)
        policies = tuple(self._list_paginated(f"/repos/{owner}/{repo}/environments/{env_name}/deployment_branch_policies", "branch_policies", allow_404=True))
        branch_policy = _BranchPolicyState(False, None, None, None)
        for policy in policies:
            if not isinstance(policy, Mapping):
                continue
            if policy.get("type") != "branch":
                continue
            if policy.get("name") != branch_name:
                continue
            policy_id = policy.get("id")
            branch_policy = _BranchPolicyState(True, policy_id if isinstance(policy_id, int) else None, branch_name, "branch")
            break
        return _EnvironmentState(env_name, True, policy_payload, variables, variable, policies, branch_policy)

    def _read_environment_variable(self, owner: str, repo: str, env_name: str, name: str) -> _VariableState:
        payload = self._get_json(f"/repos/{owner}/{repo}/environments/{env_name}/variables/{name}", allow_404=True)
        if not payload:
            return _VariableState(False, None)
        value = payload.get("value")
        return _VariableState(True, value if isinstance(value, str) else None)

    def _headers(self) -> Mapping[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": _API_VERSION,
        }

    def _get_json(self, path: str, *, allow_404: bool = False) -> Mapping[str, Any]:
        response = self._request("GET", path, allow_404=allow_404)
        if response is None:
            return {}
        return self._json(response)

    def _list_paginated(self, path: str, field: str, *, allow_404: bool = False) -> list[Mapping[str, object]]:
        items: list[Mapping[str, object]] = []
        next_url: str | None = path
        while next_url is not None:
            response = self._request("GET", next_url, allow_404=allow_404)
            if response is None:
                return items
            payload = self._json(response)
            current = payload.get(field, [])
            if not isinstance(current, list):
                raise GitHubProviderError(f"{field} payload is invalid")
            items.extend(item for item in current if isinstance(item, Mapping))
            next_url = _parse_links(response.headers.get("Link")).get("next")
        return items

    def _put(self, path: str, payload: Mapping[str, object]) -> None:
        self._request("PUT", path, json=payload)

    def _post(
        self,
        path: str,
        payload: Mapping[str, object],
        *,
        allow_statuses: set[int] | None = None,
    ) -> httpx.Response | None:
        return self._request(
            "POST",
            path,
            json=payload,
            allow_statuses=allow_statuses,
        )

    def _patch(self, path: str, payload: Mapping[str, object]) -> None:
        self._request("PATCH", path, json=payload, allow_statuses={200, 204})

    def _delete(self, path: str, *, allow_statuses: set[int] | None = None) -> None:
        self._request("DELETE", path, allow_statuses=allow_statuses or {204})

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, object] | None = None,
        allow_404: bool = False,
        allow_statuses: set[int] | None = None,
    ) -> httpx.Response | None:
        deferred_error: GitHubProviderTransportError | None = None
        try:
            request = self._http.build_request(method, path, headers=self._headers(), json=json)
            response = self._http.send(request, stream=True)
            try:
                if response.status_code == 303 and allow_statuses and 303 in allow_statuses:
                    body = self._read_bounded(response)
                    return httpx.Response(response.status_code, headers=response.headers, content=body, request=response.request)
                if response.is_redirect:
                    raise GitHubProviderError("redirect responses are not allowed")
                body = self._read_bounded(response)
                if allow_404 and response.status_code == 404:
                    return None
                if allow_statuses and response.status_code in allow_statuses:
                    return httpx.Response(response.status_code, headers=response.headers, content=body, request=response.request)
                if response.status_code == 403:
                    lowered = body.decode("utf-8", errors="ignore").casefold()
                    if "secondary rate limit" in lowered or "rate limit" in lowered or "retry-after" in {key.casefold() for key in response.headers.keys()} or response.headers.get("x-ratelimit-remaining") == "0":
                        raise GitHubProviderTransportError("GitHub request failed: rate_limited")
                    raise GitHubProviderTransportError("GitHub request failed: forbidden")
                if response.status_code == 429:
                    raise GitHubProviderTransportError("GitHub request failed: rate_limited")
                if response.status_code >= 400:
                    raise GitHubProviderTransportError(f"GitHub request failed with HTTP {response.status_code}")
                return httpx.Response(response.status_code, headers=response.headers, content=body, request=response.request)
            finally:
                response.close()
        except httpx.TimeoutException:
            deferred_error = _redacted_transport_error("GitHub request timed out")
        except httpx.HTTPError as exc:
            message = str(exc).replace(self._token, "<redacted>")
            deferred_error = _redacted_transport_error(
                f"GitHub transport failed: {message}"
            )
        if deferred_error is not None:
            deferred_error.__cause__ = None
            deferred_error.__context__ = None
            raise deferred_error from None
        raise GitHubProviderTransportError("GitHub request failed without a response")

    def _read_bounded(self, response: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > self._json_max_bytes:
                raise GitHubProviderError("GitHub response exceeds the configured limit")
            chunks.append(chunk)
        return b"".join(chunks)

    def _json(self, response: httpx.Response) -> Mapping[str, Any]:
        try:
            payload = json.loads(response.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise GitHubProviderError("GitHub response must be a JSON object")
        if not isinstance(payload, Mapping):
            raise GitHubProviderError("GitHub response must be a JSON object")
        return payload


__all__ = ["GitHubBootstrapProvider", "GitHubProviderApplyError", "GitHubProviderError", "GitHubProviderTransportError"]
