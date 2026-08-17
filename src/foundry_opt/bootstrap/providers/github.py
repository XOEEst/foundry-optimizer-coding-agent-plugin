from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping, Sequence
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
class _PolicyState:
    exists: bool
    policy_id: int | None
    name: str | None


@dataclass(frozen=True)
class _EnvironmentState:
    name: str
    exists: bool
    protection_rules: tuple[Mapping[str, object], ...]
    deployment_branch_policy: Mapping[str, object] | None
    variables: tuple[Mapping[str, object], ...]
    variable_state: _VariableState
    branch_policies: tuple[Mapping[str, object], ...]
    default_branch_policy: _PolicyState


@dataclass(frozen=True)
class _ActionSnapshot:
    action: BootstrapAction
    before: object
    expected_after: object
    ownership: str
    rollback: tuple[tuple[str, str, object], ...]


@dataclass(frozen=True)
class _ApplyState:
    snapshots: tuple[_ActionSnapshot, ...]


def _redact(value: str | None) -> str:
    return "<redacted>"


def _redact_message(message: str, token: str) -> str:
    return message.replace(token, _redact(token))


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


def _fingerprint(label: str, value: object) -> FingerprintRecord:
    return FingerprintRecord(label=label, sha256=canonical_sha256(value))


def _parse_link_header(value: str | None) -> Mapping[str, str]:
    if not value:
        return {}
    return {rel: url for url, rel in _LINK_REL_PATTERN.findall(value)}


def _canonical_environment_payload(payload: Mapping[str, object]) -> Mapping[str, object]:
    return {
        "name": payload.get("name"),
        "protection_rules": payload.get("protection_rules", ()),
        "deployment_branch_policy": payload.get("deployment_branch_policy"),
    }


def _exception(message: str) -> GitHubProviderApplyError:
    error = GitHubProviderApplyError(message)
    error.__cause__ = None
    error.__context__ = None
    return error


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
        if self._json_max_bytes <= 0:
            raise ValueError("json_max_bytes must be positive")
        self._last_apply_state: _ApplyState | None = None

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def read_repository_settings(self, repository: str) -> Mapping[str, object]:
        owner, repo = _canonical_repo(repository)
        repo_payload = self._get_json(f"/repos/{owner}/{repo}")
        repository_id = repo_payload.get("id")
        default_branch = repo_payload.get("default_branch")
        full_name = _bounded_text(repo_payload.get("full_name"), field="full_name")
        if full_name.casefold() != repository.casefold():
            raise GitHubProviderError("repository full_name did not match requested identity")
        canonical_name = full_name
        if not isinstance(repository_id, int) or repository_id <= 0:
            raise GitHubProviderError("repository id is invalid")
        if not isinstance(default_branch, str) or not default_branch:
            raise GitHubProviderError("default branch is invalid")
        environments = self.inventory_environments(canonical_name)
        return {
            "repository": canonical_name,
            "repository_id": repository_id,
            "default_branch": default_branch,
            "environments": environments,
        }

    def inventory_environments(self, repository: str) -> Sequence[Mapping[str, object]]:
        owner, repo = _canonical_repo(repository)
        result: list[Mapping[str, object]] = []
        for env_name in _ENVIRONMENTS:
            env_payload = self._get_json(f"/repos/{owner}/{repo}/environments/{env_name}", allow_404=True)
            state = self._inventory_environment(owner, repo, env_name, env_payload)
            result.append(
                {
                    "name": state.name,
                    "exists": state.exists,
                    "protection_rules": state.protection_rules,
                    "deployment_branch_policy": state.deployment_branch_policy,
                    "variables": state.variables,
                    "branch_policies": state.branch_policies,
                }
            )
        repo_variables = self._list_paginated(f"/repos/{owner}/{repo}/actions/variables", "variables", allow_404=True)
        result.append({"name": "repository", "exists": True, "variables": tuple(repo_variables)})
        return tuple(result)

    def plan_changes(self, plan: BootstrapPlan) -> Sequence[BootstrapAction]:
        repository_state = self.read_repository_settings(plan.repository_identity)
        canonical_repository = repository_state["repository"]
        envs = {item["name"]: item for item in repository_state["environments"] if isinstance(item, Mapping)}
        planned: list[BootstrapAction] = []
        for action in plan.actions:
            if action.phase != "github":
                continue
            if action.kind == "github-environment":
                env = _bounded_text(action.diagnostics[0], field="environment")
                planned.append(action.model_copy(update={"diagnostics": action.diagnostics + (("adopt" if envs.get(env, {}).get("exists") else "create"), canonical_repository)}))
                continue
            planned.append(action.model_copy(update={"diagnostics": action.diagnostics + (canonical_repository,)}))
        return tuple(planned)

    def apply_changes(self, plan: BootstrapPlan) -> BootstrapReceipt:
        repository_state = self.read_repository_settings(plan.repository_identity)
        repository = _bounded_text(repository_state["repository"], field="repository")
        owner, repo = _canonical_repo(repository)
        snapshots: list[_ActionSnapshot] = []
        created: list[str] = []
        adopted: list[str] = []
        changed: list[str] = []
        try:
            for action in self.plan_changes(plan):
                snapshot = self._apply_action(owner, repo, action)
                snapshots.append(snapshot)
                if snapshot.ownership == "created":
                    created.append(action.action_id)
                elif snapshot.ownership == "adopted":
                    adopted.append(action.action_id)
                else:
                    changed.append(action.action_id)
            self._verify_snapshots(owner, repo, snapshots)
        except GitHubProviderError as exc:
            self._rollback_snapshots(owner, repo, snapshots)
            raise _exception(_redact_message(str(exc), self._token))
        self._last_apply_state = _ApplyState(tuple(snapshots))
        receipt = BootstrapReceipt.create(
            operation_id=plan.operation_id,
            runtime_repository=plan.runtime_repository,
            runtime_commit=plan.runtime_commit,
            repository_identity=repository,
            plan_hash=plan.plan_hash,
            before_fingerprints=tuple(_fingerprint(f"{snapshot.action.action_id}:before", snapshot.before) for snapshot in snapshots),
            after_fingerprints=tuple(_fingerprint(f"{snapshot.action.action_id}:after", snapshot.expected_after) for snapshot in snapshots),
            created_actions=tuple(created),
            adopted_actions=tuple(adopted),
            changed_actions=tuple(changed),
        )
        return receipt

    def verify_changes(self, receipt: BootstrapReceipt) -> bool:
        return bool(receipt.before_fingerprints) and len(receipt.before_fingerprints) == len(receipt.after_fingerprints)

    def rollback_changes(self, receipt: BootstrapReceipt) -> None:
        if self._last_apply_state is None:
            raise GitHubProviderApplyError("receipt-driven rollback is unsupported without action snapshots")
        owner, repo = _canonical_repo(receipt.repository_identity)
        self._rollback_snapshots(owner, repo, self._last_apply_state.snapshots)

    def _apply_action(
        self,
        owner: str,
        repo: str,
        action: BootstrapAction,
    ) -> _ActionSnapshot:
        if action.kind == "github-environment":
            env_name = _bounded_text(action.diagnostics[0], field="environment")
            env = self._inventory_environment(owner, repo, env_name)
            before = {"name": env_name, "exists": env.exists, "protection_rules": env.protection_rules, "deployment_branch_policy": env.deployment_branch_policy}
            if env.exists:
                return _ActionSnapshot(action, before, before, "adopted", ())
            payload = {"deployment_branch_policy": {"custom_branch_policies": True}}
            self._put(f"/repos/{owner}/{repo}/environments/{env_name}", payload)
            after = self._inventory_environment(owner, repo, env_name)
            if not after.exists:
                raise GitHubProviderError("created environment verification failed")
            expected = {
                "name": env_name,
                "exists": True,
                "protection_rules": after.protection_rules,
                "deployment_branch_policy": after.deployment_branch_policy,
            }
            rollback = (("delete_environment", env_name, None),)
            return _ActionSnapshot(action, before, expected, "created", rollback)
        if action.kind == "github-variable":
            env_name = _bounded_text(action.diagnostics[0], field="environment")
            value = _bounded_text(action.diagnostics[1], field="client_id", max_length=512)
            env = self._inventory_environment(owner, repo, env_name)
            before = {"environment": env_name, "variable": {"exists": env.variable_state.exists, "value": env.variable_state.value}}
            if env.variable_state.exists and env.variable_state.value == value:
                return _ActionSnapshot(action, before, before, "adopted", ())
            if env.variable_state.exists:
                self._patch(
                    f"/repos/{owner}/{repo}/environments/{env_name}/variables/{_VAR_NAME}",
                    {"name": _VAR_NAME, "value": value},
                )
                ownership = "changed"
                rollback = (("restore_variable", env_name, env.variable_state.value),)
            else:
                self._post(
                    f"/repos/{owner}/{repo}/environments/{env_name}/variables",
                    {"name": _VAR_NAME, "value": value},
                )
                ownership = "changed"
                rollback = (("delete_variable", env_name, None),)
            after = self._read_environment_variable(owner, repo, env_name, _VAR_NAME)
            expected = {"environment": env_name, "variable": {"exists": True, "value": value}}
            if after.value != value:
                raise GitHubProviderError("variable verification failed")
            return _ActionSnapshot(action, before, expected, ownership, rollback)
        if action.kind == "github-branch-policy":
            env_name = _bounded_text(action.diagnostics[0], field="environment")
            branch_name = _bounded_text(action.diagnostics[1], field="default_branch")
            env = self._inventory_environment(owner, repo, env_name)
            before = {
                "environment": env_name,
                "deployment_branch_policy": env.deployment_branch_policy,
                "default_branch_policy": {"exists": env.default_branch_policy.exists, "policy_id": env.default_branch_policy.policy_id, "name": env.default_branch_policy.name},
            }
            if env.default_branch_policy.exists:
                return _ActionSnapshot(action, before, before, "adopted", ())
            previous_environment_policy = env.deployment_branch_policy
            environment_policy_changed = not (
                previous_environment_policy or {}
            ).get("custom_branch_policies", False)
            self._ensure_environment_policy_enabled(
                owner,
                repo,
                env_name,
                previous_environment_policy,
            )
            try:
                duplicate_allowed = False
                try:
                    self._post(
                        f"/repos/{owner}/{repo}/environments/{env_name}/deployment_branch_policies",
                        {"name": branch_name, "type": "branch"},
                    )
                except GitHubProviderTransportError as exc:
                    if "duplicate" in str(exc).casefold() or "303" in str(exc):
                        duplicate_allowed = True
                    else:
                        raise
                after = self._inventory_environment(owner, repo, env_name)
                if (
                    not after.default_branch_policy.exists
                    or after.default_branch_policy.name != branch_name
                ):
                    if duplicate_allowed:
                        raise GitHubProviderError(
                            "duplicate branch policy response did not verify existing state"
                        )
                    raise GitHubProviderError("branch policy verification failed")
            except Exception:
                if environment_policy_changed:
                    self._restore_environment_policy(
                        owner,
                        repo,
                        env_name,
                        previous_environment_policy,
                    )
                raise
            expected = {
                "environment": env_name,
                "deployment_branch_policy": after.deployment_branch_policy,
                "default_branch_policy": {
                    "exists": True,
                    "policy_id": after.default_branch_policy.policy_id,
                    "name": after.default_branch_policy.name,
                },
            }
            rollback_steps: list[tuple[str, str, object]] = []
            if environment_policy_changed:
                rollback_steps.append(
                    (
                        "restore_environment_policy",
                        env_name,
                        previous_environment_policy,
                    )
                )
            if not env.default_branch_policy.exists and after.default_branch_policy.policy_id is not None:
                rollback_steps.append(("delete_branch_policy", env_name, after.default_branch_policy.policy_id))
            rollback_steps.sort(key=lambda item: 0 if item[0] == "delete_branch_policy" else 1)
            ownership = "changed" if not duplicate_allowed else "adopted"
            return _ActionSnapshot(action, before, expected, ownership, tuple(rollback_steps))
        raise GitHubProviderError(f"unsupported github action kind: {action.kind}")

    def _verify_snapshots(self, owner: str, repo: str, snapshots: Sequence[_ActionSnapshot]) -> None:
        for snapshot in snapshots:
            action = snapshot.action
            if action.kind == "github-environment":
                env = self._inventory_environment(owner, repo, action.diagnostics[0])
                current = {
                    "name": env.name,
                    "exists": env.exists,
                    "protection_rules": env.protection_rules,
                    "deployment_branch_policy": env.deployment_branch_policy,
                }
            elif action.kind == "github-variable":
                variable = self._read_environment_variable(owner, repo, action.diagnostics[0], _VAR_NAME)
                current = {"environment": action.diagnostics[0], "variable": {"exists": variable.exists, "value": variable.value}}
            else:
                env = self._inventory_environment(owner, repo, action.diagnostics[0])
                current = {
                    "environment": action.diagnostics[0],
                    "deployment_branch_policy": env.deployment_branch_policy,
                    "default_branch_policy": {
                        "exists": env.default_branch_policy.exists,
                        "policy_id": env.default_branch_policy.policy_id,
                        "name": env.default_branch_policy.name,
                    },
                }
            if canonical_sha256(current) != canonical_sha256(snapshot.expected_after):
                raise GitHubProviderError(f"verification failed for action {action.action_id}")

    def _rollback_snapshots(self, owner: str, repo: str, snapshots: Sequence[_ActionSnapshot]) -> None:
        operations: list[tuple[str, str, object]] = []
        for snapshot in snapshots:
            operations.extend(snapshot.rollback)
        operations.sort(key=lambda item: 0 if item[0] in {"delete_branch_policy", "delete_environment"} else 1)
        for operation, environment, value in operations:
                if operation == "delete_environment":
                    self._delete(f"/repos/{owner}/{repo}/environments/{environment}", allow_statuses={204, 404})
                elif operation == "delete_variable":
                    self._delete(f"/repos/{owner}/{repo}/environments/{environment}/variables/{_VAR_NAME}", allow_statuses={204, 404})
                elif operation == "restore_variable":
                    assert isinstance(value, str)
                    self._patch(f"/repos/{owner}/{repo}/environments/{environment}/variables/{_VAR_NAME}", {"name": _VAR_NAME, "value": value})
                elif operation == "delete_branch_policy":
                    self._delete(f"/repos/{owner}/{repo}/environments/{environment}/deployment_branch_policies/{value}", allow_statuses={204, 404})
                elif operation == "restore_environment_policy":
                    self._restore_environment_policy(
                        owner,
                        repo,
                        environment,
                        value if isinstance(value, Mapping) else None,
                    )

    def _restore_environment_policy(
        self,
        owner: str,
        repo: str,
        environment: str,
        previous: Mapping[str, object] | None,
    ) -> None:
        payload = (
            dict(previous)
            if previous is not None
            else {"custom_branch_policies": False}
        )
        self._put(
            f"/repos/{owner}/{repo}/environments/{environment}",
            {"deployment_branch_policy": payload},
        )

    def _inventory_environment(
        self,
        owner: str,
        repo: str,
        env_name: str,
        env_payload: Mapping[str, object] | None = None,
    ) -> _EnvironmentState:
        payload = env_payload if env_payload is not None else self._get_json(f"/repos/{owner}/{repo}/environments/{env_name}", allow_404=True)
        if not payload:
            return _EnvironmentState(
                name=env_name,
                exists=False,
                protection_rules=(),
                deployment_branch_policy=None,
                variables=(),
                variable_state=_VariableState(False, None),
                branch_policies=(),
                default_branch_policy=_PolicyState(False, None, None),
            )
        variables = tuple(self._list_paginated(f"/repos/{owner}/{repo}/environments/{env_name}/variables", "variables", allow_404=True))
        variable = self._read_environment_variable(owner, repo, env_name, _VAR_NAME)
        policies = tuple(self._list_paginated(f"/repos/{owner}/{repo}/environments/{env_name}/deployment_branch_policies", "branch_policies", allow_404=True))
        default_policy = _PolicyState(False, None, None)
        for policy in policies:
            if not isinstance(policy, Mapping):
                continue
            if policy.get("type") != "branch":
                continue
            name = policy.get("name")
            if name == "main":
                policy_id = policy.get("id")
                default_policy = _PolicyState(True, policy_id if isinstance(policy_id, int) else None, name if isinstance(name, str) else None)
        protection_rules = payload.get("protection_rules", ())
        if not isinstance(protection_rules, Sequence):
            protection_rules = ()
        deployment_branch_policy = payload.get("deployment_branch_policy")
        if deployment_branch_policy is not None and not isinstance(deployment_branch_policy, Mapping):
            raise GitHubProviderError("deployment_branch_policy payload is invalid")
        return _EnvironmentState(
            name=env_name,
            exists=True,
            protection_rules=tuple(item for item in protection_rules if isinstance(item, Mapping)),
            deployment_branch_policy=deployment_branch_policy,
            variables=variables,
            variable_state=variable,
            branch_policies=policies,
            default_branch_policy=default_policy,
        )

    def _read_environment_variable(self, owner: str, repo: str, environment: str, name: str) -> _VariableState:
        payload = self._get_json(f"/repos/{owner}/{repo}/environments/{environment}/variables/{name}", allow_404=True)
        if not payload:
            return _VariableState(False, None)
        value = payload.get("value")
        return _VariableState(True, value if isinstance(value, str) else None)

    def _ensure_environment_policy_enabled(
        self,
        owner: str,
        repo: str,
        environment: str,
        deployment_branch_policy: Mapping[str, object] | None,
    ) -> None:
        existing = dict(deployment_branch_policy or {})
        if existing.get("custom_branch_policies") is True:
            return
        existing["custom_branch_policies"] = True
        self._put(f"/repos/{owner}/{repo}/environments/{environment}", {"deployment_branch_policy": existing})

    def _list_paginated(self, path: str, field: str, *, allow_404: bool = False) -> list[Mapping[str, object]]:
        items: list[Mapping[str, object]] = []
        next_path: str | None = path
        while next_path is not None:
            response = self._request("GET", next_path, allow_404=allow_404)
            if response is None:
                return items
            payload = self._json(response)
            current = payload.get(field, [])
            if not isinstance(current, list):
                raise GitHubProviderError(f"{field} payload is invalid")
            items.extend(item for item in current if isinstance(item, Mapping))
            next_link = _parse_link_header(response.headers.get("Link")).get("next")
            next_path = next_link if next_link else None
        return items

    def _headers(self) -> Mapping[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": "Bearer <redacted>",
            "X-GitHub-Api-Version": _API_VERSION,
        }

    def _get_json(self, path: str, *, allow_404: bool = False) -> Mapping[str, Any]:
        response = self._request("GET", path, allow_404=allow_404)
        if response is None:
            return {}
        return self._json(response)

    def _put(self, path: str, payload: Mapping[str, object]) -> None:
        self._request("PUT", path, json=payload)

    def _post(self, path: str, payload: Mapping[str, object]) -> None:
        self._request("POST", path, json=payload, allow_statuses={200, 201, 303})

    def _patch(self, path: str, payload: Mapping[str, object]) -> None:
        self._request("PATCH", path, json=payload)

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
        try:
            request = self._http.build_request(method, path, headers=self._headers(), json=json)
            streamed = self._http.send(request, stream=True)
            try:
                if streamed.is_redirect:
                    raise GitHubProviderError("redirect responses are not allowed")
                if allow_404 and streamed.status_code == 404:
                    return None
                if allow_statuses and streamed.status_code in allow_statuses:
                    return httpx.Response(streamed.status_code, headers=streamed.headers, content=streamed.read(), request=streamed.request)
                if streamed.status_code == 403 and ("retry-after" in streamed.headers or streamed.headers.get("x-ratelimit-remaining") == "0"):
                    raise GitHubProviderTransportError("GitHub request failed: rate_limited")
                if streamed.status_code == 429:
                    raise GitHubProviderTransportError("GitHub request failed: rate_limited")
                if streamed.status_code == 403:
                    raise GitHubProviderTransportError("GitHub request failed: forbidden")
                if streamed.status_code >= 400:
                    raise GitHubProviderTransportError(f"GitHub request failed with HTTP {streamed.status_code}")
                chunks: list[bytes] = []
                total = 0
                for chunk in streamed.iter_bytes():
                    total += len(chunk)
                    if total > self._json_max_bytes:
                        raise GitHubProviderError("GitHub response exceeds the configured limit")
                    chunks.append(chunk)
                return httpx.Response(streamed.status_code, headers=streamed.headers, content=b"".join(chunks), request=streamed.request)
            finally:
                streamed.close()
        except httpx.TimeoutException:
            raise GitHubProviderTransportError("GitHub request timed out") from None
        except httpx.HTTPError as exc:
            raise GitHubProviderTransportError(_redact_message(f"GitHub transport failed: {exc}", self._token)) from None

    def _json(self, response: httpx.Response) -> Mapping[str, Any]:
        try:
            payload = json.loads(response.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise GitHubProviderError("GitHub response must be a JSON object")
        if not isinstance(payload, Mapping):
            raise GitHubProviderError("GitHub response must be a JSON object")
        return payload


__all__ = ["GitHubBootstrapProvider", "GitHubProviderApplyError", "GitHubProviderError", "GitHubProviderTransportError"]
