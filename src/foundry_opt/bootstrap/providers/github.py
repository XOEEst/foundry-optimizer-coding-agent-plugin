from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from foundry_opt.bootstrap.canonical import canonical_sha256
from foundry_opt.bootstrap.contracts import BootstrapAction, BootstrapPlan, BootstrapReceipt, FingerprintRecord
from foundry_opt.bootstrap.errors import BootstrapApplyError, BootstrapProviderError

_OWNER_REPO_PATTERN = re.compile(r"^(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}[A-Za-z0-9])?)/(?P<repo>[A-Za-z0-9_.-]{1,100})$")
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


def _redact(value: str | None) -> str:
    return "<redacted>"


def _redact_message(message: str, token: str) -> str:
    return message.replace(token, _redact(token))


def _bounded_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise GitHubProviderError(f"{field} must be a non-empty string")
    if len(value) > 255:
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


@dataclass(frozen=True)
class _ActionState:
    action: BootstrapAction
    disposition: str
    resource: str


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
        self._token = _bounded_text(token, field="token")
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

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def read_repository_settings(self, repository: str) -> Mapping[str, object]:
        owner, repo = _canonical_repo(repository)
        repo_payload = self._get_json(f"/repos/{owner}/{repo}")
        repository_id = repo_payload.get("id")
        default_branch = repo_payload.get("default_branch")
        if not isinstance(repository_id, int) or repository_id <= 0:
            raise GitHubProviderError("repository id is invalid")
        if not isinstance(default_branch, str) or not default_branch:
            raise GitHubProviderError("default branch is invalid")
        environments = self.inventory_environments(repository)
        return {
            "repository": repository,
            "repository_id": repository_id,
            "default_branch": default_branch,
            "environments": environments,
        }

    def inventory_environments(self, repository: str) -> Sequence[Mapping[str, object]]:
        owner, repo = _canonical_repo(repository)
        envs_payload = self._get_json(f"/repos/{owner}/{repo}/environments")
        environments = envs_payload.get("environments")
        if not isinstance(environments, list):
            raise GitHubProviderError("environments payload is invalid")
        result: list[Mapping[str, object]] = []
        for env_name in _ENVIRONMENTS:
            env_payload = next((item for item in environments if isinstance(item, Mapping) and item.get("name") == env_name), None)
            variables_payload = self._get_json(f"/repos/{owner}/{repo}/environments/{env_name}/variables", allow_404=True)
            deployment_policy = self._get_json(
                f"/repos/{owner}/{repo}/environments/{env_name}/deployment-branch-policies",
                allow_404=True,
            )
            result.append(
                {
                    "name": env_name,
                    "exists": env_payload is not None,
                    "variables": tuple(self._read_variables(variables_payload)),
                    "branch_policies": tuple(self._read_branch_policies(deployment_policy)),
                }
            )
        repo_variables = self._get_json(f"/repos/{owner}/{repo}/actions/variables", allow_404=True)
        result.append({"name": "repository", "exists": True, "variables": tuple(self._read_variables(repo_variables))})
        return tuple(result)

    def plan_changes(self, plan: BootstrapPlan) -> Sequence[BootstrapAction]:
        inventory = self.read_repository_settings(plan.repository_identity)
        envs = {item["name"]: item for item in inventory["environments"] if isinstance(item, Mapping)}
        actions: list[BootstrapAction] = []
        for action in plan.actions:
            if action.kind == "github-environment":
                env = _bounded_text(action.diagnostics[0], field="environment") if action.diagnostics else ""
                state = envs.get(env)
                if not state or not bool(state.get("exists")):
                    actions.append(action.model_copy(update={"diagnostics": action.diagnostics + ("create",)}))
                else:
                    actions.append(action.model_copy(update={"diagnostics": action.diagnostics + ("adopt",)}))
            elif action.kind == "github-variable":
                actions.append(action)
            elif action.kind == "github-branch-policy":
                actions.append(action)
        return tuple(actions)

    def apply_changes(self, plan: BootstrapPlan) -> BootstrapReceipt:
        before = self.read_repository_settings(plan.repository_identity)
        created: list[str] = []
        adopted: list[str] = []
        changed: list[str] = []
        compensation: list[str] = []
        applied: list[_ActionState] = []
        try:
            for action in self.plan_changes(plan):
                if action.kind == "github-environment":
                    env = action.diagnostics[0]
                    exists = any(item["name"] == env and item["exists"] for item in before["environments"])
                    if exists:
                        adopted.append(action.action_id)
                        applied.append(_ActionState(action, "adopted", f"environment:{env}"))
                    else:
                        self._put(f"/repos/{plan.repository_identity}/environments/{env}", {})
                        created.append(action.action_id)
                        applied.append(_ActionState(action, "created", f"environment:{env}"))
                elif action.kind == "github-variable":
                    env = action.diagnostics[0]
                    value = action.diagnostics[1]
                    status = self._upsert_variable(plan.repository_identity, env, _VAR_NAME, value)
                    (changed if status == "changed" else adopted).append(action.action_id)
                    applied.append(_ActionState(action, status, f"variable:{env}:{_VAR_NAME}"))
                elif action.kind == "github-branch-policy":
                    env = action.diagnostics[0]
                    default_branch = action.diagnostics[1]
                    status = self._ensure_branch_policy(plan.repository_identity, env, default_branch)
                    (changed if status == "changed" else adopted).append(action.action_id)
                    applied.append(_ActionState(action, status, f"branch-policy:{env}:{default_branch}"))
        except Exception as exc:
            compensation = [item.action.action_id for item in reversed(applied) if item.disposition in {"created", "changed"}]
            self._rollback_applied(plan.repository_identity, applied)
            raise GitHubProviderApplyError(str(exc).replace(self._token, _redact(self._token))) from exc
        after = self.read_repository_settings(plan.repository_identity)
        receipt = BootstrapReceipt.create(
            operation_id=plan.operation_id,
            runtime_repository=plan.runtime_repository,
            runtime_commit=plan.runtime_commit,
            repository_identity=plan.repository_identity,
            plan_hash=plan.plan_hash,
            before_fingerprints=(
                _fingerprint("before", before),
            ),
            after_fingerprints=(
                _fingerprint("after", after),
            ),
            created_actions=tuple(created),
            adopted_actions=tuple(adopted),
            changed_actions=tuple(changed),
            compensation_required_actions=tuple(compensation),
        )
        if not self.verify_changes(receipt):
            raise GitHubProviderApplyError("post-apply verification failed")
        return receipt

    def verify_changes(self, receipt: BootstrapReceipt) -> bool:
        return bool(receipt.after_fingerprints) and receipt.before_fingerprints != receipt.after_fingerprints or bool(receipt.adopted_actions)

    def rollback_changes(self, receipt: BootstrapReceipt) -> None:
        return None

    def _rollback_applied(self, repository: str, applied: Sequence[_ActionState]) -> None:
        for item in reversed(applied):
            if item.resource.startswith("variable:") and item.disposition == "changed":
                continue
            if item.resource.startswith("branch-policy:") and item.disposition == "changed":
                continue
            if item.resource.startswith("environment:") and item.disposition == "created":
                _, env = item.resource.split(":", 1)
                self._delete(f"/repos/{repository}/environments/{env}")

    def _upsert_variable(self, repository: str, environment: str, name: str, value: str) -> str:
        existing = self._get_json(f"/repos/{repository}/environments/{environment}/variables/{name}", allow_404=True)
        if not existing:
            self._post(f"/repos/{repository}/environments/{environment}/variables", {"name": name, "value": value})
            return "changed"
        if isinstance(existing, Mapping) and existing.get("value") == value:
            return "adopted"
        payload = {"name": name, "value": value}
        if isinstance(existing, Mapping):
            self._patch(f"/repos/{repository}/environments/{environment}/variables/{name}", payload)
            return "changed"
        raise GitHubProviderError("existing variable payload is invalid")

    def _ensure_branch_policy(self, repository: str, environment: str, default_branch: str) -> str:
        payload = self._get_json(f"/repos/{repository}/environments/{environment}/deployment-branch-policies", allow_404=True)
        branches = {item.get('name') for item in payload.get('branch_policies', []) if isinstance(item, Mapping)}
        if default_branch in branches:
            return "adopted"
        self._post(
            f"/repos/{repository}/environments/{environment}/deployment-branch-policies",
            {"name": default_branch, "type": "branch"},
        )
        return "changed"

    def _read_variables(self, payload: object) -> list[Mapping[str, object]]:
        if not isinstance(payload, Mapping):
            return []
        variables = payload.get("variables", [])
        if not isinstance(variables, list):
            return []
        return [item for item in variables if isinstance(item, Mapping)]

    def _read_branch_policies(self, payload: object) -> list[Mapping[str, object]]:
        if not isinstance(payload, Mapping):
            return []
        policies = payload.get("branch_policies", [])
        if not isinstance(policies, list):
            return []
        return [item for item in policies if isinstance(item, Mapping)]

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

    def _put(self, path: str, payload: Mapping[str, object]) -> None:
        self._request("PUT", path, json=payload)

    def _post(self, path: str, payload: Mapping[str, object]) -> None:
        self._request("POST", path, json=payload)

    def _patch(self, path: str, payload: Mapping[str, object]) -> None:
        self._request("PATCH", path, json=payload)

    def _delete(self, path: str) -> None:
        self._request("DELETE", path)

    def _request(self, method: str, path: str, *, json: Mapping[str, object] | None = None, allow_404: bool = False) -> httpx.Response | None:
        try:
            response = self._http.request(method, path, headers=self._headers(), json=json)
        except httpx.TimeoutException as exc:
            raise GitHubProviderTransportError("GitHub request timed out") from exc
        except httpx.HTTPError as exc:
            raise GitHubProviderTransportError(_redact_message(f"GitHub transport failed: {exc}", self._token)) from exc
        if response.is_redirect:
            raise GitHubProviderError("redirect responses are not allowed")
        if allow_404 and response.status_code == 404:
            return None
        if response.status_code in {401, 403, 429}:
            summary = "forbidden" if response.status_code == 403 else "rate_limited" if response.status_code == 429 else "unauthorized"
            raise GitHubProviderTransportError(f"GitHub request failed: {summary}")
        if response.status_code >= 400:
            raise GitHubProviderTransportError(f"GitHub request failed with HTTP {response.status_code}")
        return response

    def _json(self, response: httpx.Response) -> Mapping[str, Any]:
        if len(response.content) > self._json_max_bytes:
            raise GitHubProviderError("GitHub response exceeds the configured limit")
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise GitHubProviderError("GitHub response must be a JSON object")
        return payload


__all__ = ["GitHubBootstrapProvider", "GitHubProviderApplyError", "GitHubProviderError", "GitHubProviderTransportError"]
