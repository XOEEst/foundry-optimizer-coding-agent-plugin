from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from foundry_opt.bootstrap.canonical import canonical_sha256, safe_persisted_document
from foundry_opt.bootstrap.contracts import BootstrapAction, BootstrapPlan, BootstrapReceipt, FingerprintRecord
from foundry_opt.bootstrap.errors import BootstrapApplyError, BootstrapProviderError

_OWNER_REPO_PATTERN = re.compile(r"^(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}[A-Za-z0-9])?)/(?P<repo>[A-Za-z0-9_.-]{1,100})$")
_LINK_REL_PATTERN = re.compile(r'<([^>]+)>;\s*rel="([^"]+)"')
_ENVIRONMENTS = ("copilot", "foundry-production")
_VAR_NAME = "AZURE_OPTIMIZER_CLIENT_ID"
_API_VERSION = "2022-11-28"
_JSON_LIMIT = 64 * 1024
_STATE_VERSION = 2
_VALID_SNAPSHOT_KINDS = {"github-environment", "github-variable", "github-branch-policy"}
_VALID_ROLLBACK_OPERATIONS = {"delete_branch_policy", "restore_variable", "delete_variable", "restore_environment_policy", "delete_environment"}


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
class _EnvironmentAggregateState:
    name: str
    exists: bool
    deployment_branch_policy: Mapping[str, object] | None
    variable: Mapping[str, object]
    branch_policy: Mapping[str, object]


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
    state_hash: str
    snapshots: tuple[_ActionSnapshot, ...]
    environment_state: Mapping[str, _EnvironmentAggregateState]


def _bounded_text(value: object, *, field: str, max_length: int = 255, error_type: type[Exception] = GitHubProviderError) -> str:
    if not isinstance(value, str) or not value:
        raise error_type(f"{field} must be a non-empty string")
    if len(value) > max_length:
        raise error_type(f"{field} exceeds its bounded length")
    return value


def _canonical_repo(repository: str, *, error_type: type[Exception] = GitHubProviderError) -> tuple[str, str]:
    value = _bounded_text(repository, field="repository", error_type=error_type)
    match = _OWNER_REPO_PATTERN.fullmatch(value)
    if match is None:
        raise error_type("repository must be canonical owner/repo")
    return match.group("owner"), match.group("repo")


def _canonical_environment_name(name: object, *, field: str, error_type: type[Exception] = GitHubProviderError) -> str:
    value = _bounded_text(name, field=field, max_length=255, error_type=error_type)
    if value not in _ENVIRONMENTS:
        raise error_type(f"{field} must be one of {sorted(_ENVIRONMENTS)!r}")
    return value


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


def _json_safe(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, tuple):
        return [_json_safe(child) for child in value]
    if isinstance(value, list):
        return [_json_safe(child) for child in value]
    return value


def _as_mapping(value: object, *, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise GitHubProviderApplyError(f"{field} must be a mapping")
    return value


def _canonicalized_document(value: object) -> object:
    return json.loads(json.dumps(_json_safe(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False))


def _state_hash_payload(*, receipt_hash: str, operation_id: str, repository: str, snapshots: Sequence[Mapping[str, object]], environments: Sequence[Mapping[str, object]]) -> Mapping[str, object]:
    return {
        "version": _STATE_VERSION,
        "receipt_hash": receipt_hash,
        "operation_id": operation_id,
        "repository": repository,
        "snapshots": list(snapshots),
        "environments": list(environments),
    }


def _encode_aggregate_state(state: _EnvironmentAggregateState) -> Mapping[str, object]:
    return {
        "name": state.name,
        "exists": state.exists,
        "deployment_branch_policy": _canonicalized_document(state.deployment_branch_policy),
        "variable": _canonicalized_document(state.variable),
        "branch_policy": _canonicalized_document(state.branch_policy),
    }


def _decode_aggregate_state(value: object) -> _EnvironmentAggregateState:
    mapping = _as_mapping(value, field="environment state")
    name = _canonical_environment_name(mapping.get("name"), field="environment state.name", error_type=GitHubProviderApplyError)
    exists = mapping.get("exists")
    if not isinstance(exists, bool):
        raise GitHubProviderApplyError("environment state.exists must be a bool")
    variable = _as_mapping(mapping.get("variable"), field="environment state.variable")
    branch_policy = _as_mapping(mapping.get("branch_policy"), field="environment state.branch_policy")
    return _EnvironmentAggregateState(
        name=name,
        exists=exists,
        deployment_branch_policy=_canonicalized_document(mapping.get("deployment_branch_policy")),
        variable=_canonicalized_document(variable),
        branch_policy=_canonicalized_document(branch_policy),
    )


def _decode_rollback_step(value: object) -> tuple[str, str, object]:
    if not isinstance(value, list) or len(value) != 3:
        raise GitHubProviderApplyError("rollback step is invalid")
    operation = _bounded_text(value[0], field="rollback operation", error_type=GitHubProviderApplyError)
    if operation not in _VALID_ROLLBACK_OPERATIONS:
        raise GitHubProviderApplyError("rollback operation is invalid")
    environment = _canonical_environment_name(value[1], field="rollback environment", error_type=GitHubProviderApplyError)
    payload = _canonicalized_document(value[2])
    return operation, environment, payload


def _encode_snapshot(snapshot: _ActionSnapshot) -> Mapping[str, object]:
    return {
        "action_id": snapshot.action_id,
        "kind": snapshot.kind,
        "target": snapshot.target,
        "before": _canonicalized_document(snapshot.before),
        "expected_after": _canonicalized_document(snapshot.expected_after),
        "ownership": snapshot.ownership,
        "rollback": [_canonicalized_document([operation, environment, payload]) for operation, environment, payload in snapshot.rollback],
    }


def _decode_snapshot(value: object) -> _ActionSnapshot:
    mapping = _as_mapping(value, field="snapshot")
    action_id = _bounded_text(mapping.get("action_id"), field="snapshot.action_id", error_type=GitHubProviderApplyError)
    kind = _bounded_text(mapping.get("kind"), field="snapshot.kind", error_type=GitHubProviderApplyError)
    if kind not in _VALID_SNAPSHOT_KINDS:
        raise GitHubProviderApplyError("snapshot kind is invalid")
    target = _canonical_environment_name(mapping.get("target"), field="snapshot.target", error_type=GitHubProviderApplyError)
    ownership = _bounded_text(mapping.get("ownership"), field="snapshot.ownership", error_type=GitHubProviderApplyError)
    if ownership not in {"created", "adopted", "changed"}:
        raise GitHubProviderApplyError("snapshot ownership is invalid")
    rollback_raw = mapping.get("rollback", [])
    if not isinstance(rollback_raw, list):
        raise GitHubProviderApplyError("snapshot.rollback must be a list")
    return _ActionSnapshot(
        action_id=action_id,
        kind=kind,
        target=target,
        before=_canonicalized_document(mapping.get("before")),
        expected_after=_canonicalized_document(mapping.get("expected_after")),
        ownership=ownership,
        rollback=tuple(_decode_rollback_step(item) for item in rollback_raw),
    )


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
        environment_state: dict[str, _EnvironmentAggregateState] = {}
        live_environment_exists: dict[str, bool] = {}
        created: list[str] = []
        adopted: list[str] = []
        changed: list[str] = []
        try:
            for action in self.plan_changes(plan):
                env_name = _canonical_environment_name(action.diagnostics[0], field="environment")
                environment_state.setdefault(env_name, self._aggregate_environment_state(owner, repo, env_name, default_branch))
                live_environment_exists.setdefault(env_name, environment_state[env_name].exists)
                snapshot = self._apply_action(owner, repo, default_branch, action, environment_state[env_name], live_environment_exists[env_name])
                if snapshot is not None and snapshot.kind == "github-environment":
                    live_environment_exists[env_name] = True
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
            self._rollback_environment_state(owner, repo, default_branch, environment_state)
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
        self._last_apply_binding = self._build_binding(receipt, repository, snapshots, environment_state)
        return receipt

    def export_provider_state(self, receipt: BootstrapReceipt) -> Mapping[str, object]:
        binding = self._validate_receipt_binding(receipt)
        snapshots = [_encode_snapshot(snapshot) for snapshot in binding.snapshots]
        environments = [_encode_aggregate_state(binding.environment_state[name]) for name in sorted(binding.environment_state)]
        payload = _state_hash_payload(
            receipt_hash=binding.receipt_hash,
            operation_id=binding.operation_id,
            repository=binding.repository,
            snapshots=snapshots,
            environments=environments,
        )
        payload = {**payload, "state_hash": canonical_sha256(payload)}
        safe = _canonicalized_document(payload)
        safe_persisted_document(safe)
        return safe if isinstance(safe, Mapping) else {}

    def restore_provider_state(self, mapping: Mapping[str, object]) -> None:
        payload = _as_mapping(mapping, field="provider state")
        version = payload.get("version")
        if version != _STATE_VERSION:
            raise GitHubProviderApplyError("provider state version is invalid")
        receipt_hash = _bounded_text(payload.get("receipt_hash"), field="provider state receipt_hash", max_length=128, error_type=GitHubProviderApplyError)
        operation_id = _bounded_text(payload.get("operation_id"), field="provider state operation_id", max_length=255, error_type=GitHubProviderApplyError)
        repository = _bounded_text(payload.get("repository"), field="provider state repository", error_type=GitHubProviderApplyError)
        owner, repo = _canonical_repo(repository, error_type=GitHubProviderApplyError)
        canonical_repository = f"{owner}/{repo}"
        snapshots_raw = payload.get("snapshots", [])
        if not isinstance(snapshots_raw, list):
            raise GitHubProviderApplyError("provider state snapshots must be a list")
        environments_raw = payload.get("environments", [])
        if not isinstance(environments_raw, list):
            raise GitHubProviderApplyError("provider state environments must be a list")
        state_hash = _bounded_text(payload.get("state_hash"), field="provider state state_hash", max_length=128, error_type=GitHubProviderApplyError)
        snapshots = tuple(_decode_snapshot(item) for item in snapshots_raw)
        environments = tuple(_decode_aggregate_state(item) for item in environments_raw)
        expected_hash = canonical_sha256(_state_hash_payload(
            receipt_hash=receipt_hash,
            operation_id=operation_id,
            repository=canonical_repository,
            snapshots=[_encode_snapshot(snapshot) for snapshot in snapshots],
            environments=[_encode_aggregate_state(item) for item in sorted(environments, key=lambda item: item.name)],
        ))
        if state_hash != expected_hash:
            raise GitHubProviderApplyError("provider state hash is invalid")
        safe_persisted_document(payload)
        self._last_apply_binding = _ApplyBinding(receipt_hash, operation_id, canonical_repository, state_hash, snapshots, {item.name: item for item in environments})

    def live_fingerprints(self, receipt: BootstrapReceipt) -> Sequence[FingerprintRecord]:
        binding = self._validate_receipt_binding(receipt)
        return tuple(self._live_fingerprints_for_binding(binding))

    def verify_changes(self, receipt: BootstrapReceipt) -> bool:
        binding = self._validate_receipt_binding(receipt)
        owner, repo = _canonical_repo(binding.repository)
        default_branch = self.read_repository_settings(binding.repository)["default_branch"]
        self._verify_final_state(owner, repo, default_branch, binding.snapshots)
        return True

    def rollback_changes(self, receipt: BootstrapReceipt) -> None:
        binding = self._validate_receipt_binding(receipt)
        owner, repo = _canonical_repo(binding.repository)
        default_branch = self.read_repository_settings(binding.repository)["default_branch"]
        self._rollback_environment_state(owner, repo, default_branch, binding.environment_state)

    def verify_rollback(self, receipt: BootstrapReceipt) -> bool:
        binding = self._validate_receipt_binding(receipt)
        owner, repo = _canonical_repo(binding.repository)
        default_branch = self.read_repository_settings(binding.repository)["default_branch"]
        self._verify_rollback_state(owner, repo, default_branch, binding.environment_state)
        return True

    def _build_binding(
        self,
        receipt: BootstrapReceipt,
        repository: str,
        snapshots: Sequence[_ActionSnapshot],
        environment_state: Mapping[str, _EnvironmentAggregateState],
    ) -> _ApplyBinding:
        snapshots_payload = [_encode_snapshot(snapshot) for snapshot in snapshots]
        environments_payload = [_encode_aggregate_state(environment_state[name]) for name in sorted(environment_state)]
        state_hash = canonical_sha256(_state_hash_payload(
            receipt_hash=receipt.receipt_hash,
            operation_id=receipt.operation_id,
            repository=repository,
            snapshots=snapshots_payload,
            environments=environments_payload,
        ))
        return _ApplyBinding(receipt.receipt_hash, receipt.operation_id, repository, state_hash, tuple(snapshots), dict(environment_state))

    def _validate_receipt_binding(self, receipt: BootstrapReceipt) -> _ApplyBinding:
        binding = self._last_apply_binding
        if binding is None:
            raise GitHubProviderApplyError("no apply binding is available for rollback or verification")
        if binding.receipt_hash != receipt.receipt_hash or binding.operation_id != receipt.operation_id or binding.repository != receipt.repository_identity:
            raise GitHubProviderApplyError("receipt does not match the current provider apply binding")
        return binding

    def _aggregate_environment_state(self, owner: str, repo: str, env_name: str, branch_name: str) -> _EnvironmentAggregateState:
        env = self._inventory_environment(owner, repo, env_name, branch_name)
        branch_policy = {
            "exists": env.requested_branch_policy.exists,
            "policy_id": env.requested_branch_policy.policy_id if env.requested_branch_policy.exists else None,
            "name": env.requested_branch_policy.name if env.requested_branch_policy.exists else None,
            "type": env.requested_branch_policy.type if env.requested_branch_policy.exists else None,
        }
        variable = {"exists": env.variable_state.exists, "value": env.variable_state.value}
        return _EnvironmentAggregateState(
            name=env_name,
            exists=env.exists,
            deployment_branch_policy=_canonicalized_document(env.deployment_branch_policy),
            variable=_canonicalized_document(variable),
            branch_policy=_canonicalized_document(branch_policy),
        )

    def _apply_action(
        self,
        owner: str,
        repo: str,
        default_branch: str,
        action: BootstrapAction,
        aggregate_before: _EnvironmentAggregateState,
        environment_exists_now: bool,
    ) -> _ActionSnapshot | None:
        if action.kind == "github-environment":
            env_name = _canonical_environment_name(action.diagnostics[0], field="environment")
            live_before = self._inventory_environment(owner, repo, env_name, default_branch) if environment_exists_now else None
            before = {"exists": aggregate_before.exists, "deployment_branch_policy": aggregate_before.deployment_branch_policy}
            if environment_exists_now:
                current = {"exists": True, "deployment_branch_policy": live_before.deployment_branch_policy if live_before is not None else aggregate_before.deployment_branch_policy}
                return _ActionSnapshot(action.action_id, action.kind, env_name, before, current, "adopted", ())
            self._put(f"/repos/{owner}/{repo}/environments/{env_name}", {"deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}})
            after = self._inventory_environment(owner, repo, env_name, default_branch)
            if not after.exists:
                raise GitHubProviderError("created environment verification failed")
            return _ActionSnapshot(action.action_id, action.kind, env_name, before, {"exists": True, "deployment_branch_policy": after.deployment_branch_policy}, "created", (("delete_environment", env_name, None),))
        if action.kind == "github-variable":
            env_name = _canonical_environment_name(action.diagnostics[0], field="environment")
            value = _bounded_text(action.diagnostics[1], field="client_id", max_length=512)
            before = aggregate_before.variable
            if before.get("exists") is True and before.get("value") == value:
                return _ActionSnapshot(action.action_id, action.kind, env_name, before, before, "adopted", ())
            env = self._inventory_environment(owner, repo, env_name, default_branch)
            if env.variable_state.exists:
                self._patch(f"/repos/{owner}/{repo}/environments/{env_name}/variables/{_VAR_NAME}", {"name": _VAR_NAME, "value": value})
            else:
                self._post(f"/repos/{owner}/{repo}/environments/{env_name}/variables", {"name": _VAR_NAME, "value": value})
            variable = self._read_environment_variable(owner, repo, env_name, _VAR_NAME)
            if variable.value != value:
                raise GitHubProviderError("variable verification failed")
            rollback = (("restore_variable", env_name, before) if before.get("exists") else ("delete_variable", env_name, None),)
            return _ActionSnapshot(action.action_id, action.kind, env_name, before, {"exists": True, "value": value}, "changed", rollback)
        if action.kind == "github-branch-policy":
            env_name = _canonical_environment_name(action.diagnostics[0], field="environment")
            branch_name = _bounded_text(action.diagnostics[1], field="default_branch")
            before = {
                "deployment_branch_policy": aggregate_before.deployment_branch_policy,
                "branch_policy": aggregate_before.branch_policy,
            }
            if aggregate_before.branch_policy.get("exists") is True:
                return _ActionSnapshot(action.action_id, action.kind, env_name, before, before, "adopted", ())
            env = self._inventory_environment(owner, repo, env_name, branch_name)
            if not self._policy_enabled(env.deployment_branch_policy):
                self._put(f"/repos/{owner}/{repo}/environments/{env_name}", {"deployment_branch_policy": self._enabled_policy_payload(env.deployment_branch_policy)})
            response = self._post(f"/repos/{owner}/{repo}/environments/{env_name}/deployment_branch_policies", {"name": branch_name, "type": "branch"}, allow_statuses={200, 201, 303})
            duplicate = response is not None and response.status_code == 303
            after = self._inventory_environment(owner, repo, env_name, branch_name)
            if not after.requested_branch_policy.exists:
                raise GitHubProviderError("branch policy verification failed")
            rollback: list[tuple[str, str, object]] = []
            if not duplicate and after.requested_branch_policy.policy_id is not None:
                rollback.append(("delete_branch_policy", env_name, after.requested_branch_policy.policy_id))
            if not self._policy_enabled(aggregate_before.deployment_branch_policy):
                rollback.append(("restore_environment_policy", env_name, aggregate_before.deployment_branch_policy))
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

    def _live_fingerprints_for_binding(self, binding: _ApplyBinding) -> Sequence[FingerprintRecord]:
        owner, repo = _canonical_repo(binding.repository)
        default_branch = self.read_repository_settings(binding.repository)["default_branch"]
        return tuple(_fingerprint(f"{snapshot.action_id}:live", self._read_live_state(owner, repo, default_branch, snapshot)) for snapshot in binding.snapshots)

    def _read_live_state(self, owner: str, repo: str, default_branch: str, snapshot: _ActionSnapshot) -> object:
        if snapshot.kind == "github-environment":
            env = self._inventory_environment(owner, repo, snapshot.target, default_branch)
            return {"exists": env.exists, "deployment_branch_policy": env.deployment_branch_policy}
        if snapshot.kind == "github-variable":
            variable = self._read_environment_variable(owner, repo, snapshot.target, _VAR_NAME)
            return {"exists": variable.exists, "value": variable.value}
        env = self._inventory_environment(owner, repo, snapshot.target, default_branch)
        return {
            "deployment_branch_policy": env.deployment_branch_policy,
            "branch_policy": {
                "exists": env.requested_branch_policy.exists,
                "policy_id": env.requested_branch_policy.policy_id if env.requested_branch_policy.exists else None,
                "name": env.requested_branch_policy.name if env.requested_branch_policy.exists else None,
                "type": env.requested_branch_policy.type if env.requested_branch_policy.exists else None,
            },
        }

    def _verify_rollback_state(self, owner: str, repo: str, default_branch: str, environment_state: Mapping[str, _EnvironmentAggregateState]) -> None:
        for env_name, expected in environment_state.items():
            current = self._read_live_aggregate_state(owner, repo, env_name, default_branch)
            if canonical_sha256(_encode_aggregate_state(current)) != canonical_sha256(_encode_aggregate_state(expected)):
                raise GitHubProviderError(f"rollback verification failed for environment {env_name}")

    def _read_live_aggregate_state(self, owner: str, repo: str, env_name: str, branch_name: str) -> _EnvironmentAggregateState:
        return self._aggregate_environment_state(owner, repo, env_name, branch_name)

    def _verify_final_state(self, owner: str, repo: str, default_branch: str, snapshots: Sequence[_ActionSnapshot]) -> None:
        merged: dict[tuple[str, str], _ActionSnapshot] = {}
        for snapshot in snapshots:
            merged[(snapshot.kind, snapshot.target)] = snapshot
        branch_policy_targets = {snapshot.target for snapshot in merged.values() if snapshot.kind == "github-branch-policy"}
        for snapshot in merged.values():
            if snapshot.kind == "github-environment":
                env = self._inventory_environment(owner, repo, snapshot.target, default_branch)
                if snapshot.target in branch_policy_targets:
                    if not env.exists:
                        raise GitHubProviderError(f"verification failed for action {snapshot.action_id}")
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
                        "policy_id": env.requested_branch_policy.policy_id if env.requested_branch_policy.exists else None,
                        "name": env.requested_branch_policy.name if env.requested_branch_policy.exists else None,
                        "type": env.requested_branch_policy.type if env.requested_branch_policy.exists else None,
                    },
                }
            if canonical_sha256(current) != canonical_sha256(snapshot.expected_after):
                raise GitHubProviderError(f"verification failed for action {snapshot.action_id}")

    def _rollback_environment_state(self, owner: str, repo: str, default_branch: str, environment_state: Mapping[str, _EnvironmentAggregateState]) -> None:
        for env_name in sorted(environment_state, reverse=True):
            original = environment_state[env_name]
            current = self._inventory_environment(owner, repo, env_name, default_branch)
            if original.exists is False:
                if current.requested_branch_policy.exists and current.requested_branch_policy.policy_id is not None:
                    self._delete(f"/repos/{owner}/{repo}/environments/{env_name}/deployment_branch_policies/{current.requested_branch_policy.policy_id}", allow_statuses={204, 404})
                if current.variable_state.exists:
                    self._delete(f"/repos/{owner}/{repo}/environments/{env_name}/variables/{_VAR_NAME}", allow_statuses={204, 404})
                if current.exists:
                    self._delete(f"/repos/{owner}/{repo}/environments/{env_name}", allow_statuses={204, 404})
                continue
            if not current.exists:
                self._put(f"/repos/{owner}/{repo}/environments/{env_name}", {"deployment_branch_policy": original.deployment_branch_policy})
                current = self._inventory_environment(owner, repo, env_name, default_branch)
            current_policy = current.requested_branch_policy
            original_policy = original.branch_policy
            if current_policy.exists and not original_policy.get("exists") and current_policy.policy_id is not None:
                self._delete(f"/repos/{owner}/{repo}/environments/{env_name}/deployment_branch_policies/{current_policy.policy_id}", allow_statuses={204, 404})
            elif not current_policy.exists and original_policy.get("exists"):
                self._post(f"/repos/{owner}/{repo}/environments/{env_name}/deployment_branch_policies", {"name": original_policy.get("name"), "type": original_policy.get("type")}, allow_statuses={200, 201, 303})
            self._put(f"/repos/{owner}/{repo}/environments/{env_name}", {"deployment_branch_policy": original.deployment_branch_policy})
            current = self._inventory_environment(owner, repo, env_name, default_branch)
            original_variable = original.variable
            if original_variable.get("exists"):
                value = original_variable.get("value")
                if not isinstance(value, str):
                    raise GitHubProviderError("original variable value is invalid")
                if current.variable_state.exists:
                    self._patch(f"/repos/{owner}/{repo}/environments/{env_name}/variables/{_VAR_NAME}", {"name": _VAR_NAME, "value": value})
                else:
                    self._post(f"/repos/{owner}/{repo}/environments/{env_name}/variables", {"name": _VAR_NAME, "value": value})
            elif current.variable_state.exists:
                self._delete(f"/repos/{owner}/{repo}/environments/{env_name}/variables/{_VAR_NAME}", allow_statuses={204, 404})

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

    def _post(self, path: str, payload: Mapping[str, object], *, allow_statuses: set[int] | None = None) -> httpx.Response | None:
        return self._request("POST", path, json=payload, allow_statuses=allow_statuses)

    def _patch(self, path: str, payload: Mapping[str, object]) -> None:
        self._request("PATCH", path, json=payload, allow_statuses={200, 204})

    def _delete(self, path: str, *, allow_statuses: set[int] | None = None) -> None:
        self._request("DELETE", path, allow_statuses=allow_statuses or {204})

    def _request(self, method: str, path: str, *, json: Mapping[str, object] | None = None, allow_404: bool = False, allow_statuses: set[int] | None = None) -> httpx.Response | None:
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
                if 200 <= response.status_code < 300:
                    return httpx.Response(response.status_code, headers=response.headers, content=body, request=response.request)
                deferred_error = self._transport_error_from_response(response, body)
            finally:
                response.close()
        except httpx.HTTPError as exc:
            if deferred_error is not None:
                raise deferred_error
            message = str(exc).replace(self._token, "******")
            raise _redacted_transport_error(f"transport_error:{message[:256]}")
        if deferred_error is not None:
            raise deferred_error
        raise _redacted_transport_error("transport_error:unknown")

    def _transport_error_from_response(self, response: httpx.Response, body: bytes) -> GitHubProviderTransportError:
        payload: Mapping[str, object] = {}
        if body:
            try:
                decoded = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                decoded = {}
            if isinstance(decoded, Mapping):
                payload = decoded
        message = _bounded_text(payload.get("message") or f"http_{response.status_code}", field="message", max_length=256, error_type=GitHubProviderTransportError)
        if response.status_code == 403 and "rate limit" in message.casefold():
            return _redacted_transport_error("rate_limited")
        return _redacted_transport_error(f"http_{response.status_code}:{message}")

    def _read_bounded(self, response: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > self._json_max_bytes:
                raise GitHubProviderError("response body exceeds bounded limit")
            chunks.append(chunk)
        return b"".join(chunks)

    def _json(self, response: httpx.Response) -> Mapping[str, Any]:
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise GitHubProviderError("response payload is not valid json") from exc
        if not isinstance(payload, Mapping):
            raise GitHubProviderError("response payload is not an object")
        return payload
