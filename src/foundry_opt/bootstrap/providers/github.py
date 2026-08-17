from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from foundry_opt.bootstrap.canonical import canonical_sha256, safe_persisted_document
from foundry_opt.bootstrap.contracts import BootstrapAction, BootstrapPlan, BootstrapReceipt, FingerprintRecord, RedactedStatusInfo
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


class GitHubProviderRollbackError(GitHubProviderApplyError):
    """Raised when apply-time compensation itself fails.

    Carries the durable compensation receipt and exportable provider state that
    were journaled before the rollback attempt, so a caller can persist them and
    retry compensation later without losing track of components this operation
    created or changed.
    """

    def __init__(
        self,
        message: str,
        *,
        compensation_receipt: BootstrapReceipt | None = None,
        provider_state: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.compensation_receipt = compensation_receipt
        self.provider_state = dict(provider_state or {})


def rollback_failure_details(exc: BaseException) -> tuple[BootstrapReceipt | None, Mapping[str, object]]:
    if isinstance(exc, GitHubProviderRollbackError):
        return exc.compensation_receipt, dict(exc.provider_state)
    return None, {}


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
    branch_name: str | None = None


@dataclass(frozen=True)
class _ApplyBinding:
    receipt_hash: str
    operation_id: str
    repository: str
    snapshots: tuple[_ActionSnapshot, ...]


_STATE_VERSION = 1


def _bounded_text(value: object, *, field: str, max_length: int = 255, error_type: type[Exception] = GitHubProviderError) -> str:
    if not isinstance(value, str) or not value:
        raise error_type(f"{field} must be a non-empty string")
    if len(value) > max_length:
        raise error_type(f"{field} exceeds its bounded length")
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


def _is_pending_snapshot(snapshot: _ActionSnapshot) -> bool:
    return isinstance(snapshot.expected_after, Mapping) and snapshot.expected_after.get("pending") is True


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


def _decode_rollback_step(value: object) -> tuple[str, str, object]:
    if not isinstance(value, list) or len(value) != 3:
        raise GitHubProviderApplyError("rollback step is invalid")
    operation = _bounded_text(value[0], field="rollback operation", error_type=GitHubProviderApplyError)
    environment = _bounded_text(value[1], field="rollback environment", error_type=GitHubProviderApplyError)
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
        "branch_name": snapshot.branch_name,
    }


def _decode_branch_name(value: object) -> str | None:
    if value is None:
        return None
    return _bounded_text(value, field="snapshot.branch_name", max_length=255, error_type=GitHubProviderApplyError)


def _decode_snapshot(value: object) -> _ActionSnapshot:
    mapping = _as_mapping(value, field="snapshot")
    action_id = _bounded_text(mapping.get("action_id"), field="snapshot.action_id", error_type=GitHubProviderApplyError)
    kind = _bounded_text(mapping.get("kind"), field="snapshot.kind", error_type=GitHubProviderApplyError)
    target = _bounded_text(mapping.get("target"), field="snapshot.target", error_type=GitHubProviderApplyError)
    ownership = _bounded_text(mapping.get("ownership"), field="snapshot.ownership", error_type=GitHubProviderApplyError)
    if ownership not in {"created", "adopted", "changed"}:
        raise GitHubProviderApplyError("snapshot ownership is invalid")
    rollback_raw = mapping.get("rollback", [])
    if not isinstance(rollback_raw, list):
        raise GitHubProviderApplyError("snapshot.rollback must be a list")
    branch_name = _decode_branch_name(mapping.get("branch_name"))
    snapshot = _ActionSnapshot(
        action_id=action_id,
        kind=kind,
        target=target,
        before=_canonicalized_document(mapping.get("before")),
        expected_after=_canonicalized_document(mapping.get("expected_after")),
        ownership=ownership,
        rollback=tuple(_decode_rollback_step(item) for item in rollback_raw),
        branch_name=branch_name,
    )
    allowed_operations = {
        "github-environment": {"delete_environment"},
        "github-variable": {"restore_variable", "delete_variable"},
        "github-branch-policy": {
            "delete_branch_policy",
            "restore_environment_policy",
        },
    }
    if snapshot.kind not in allowed_operations:
        raise GitHubProviderApplyError("snapshot kind is invalid")
    if snapshot.branch_name is not None and snapshot.kind != "github-branch-policy":
        raise GitHubProviderApplyError("snapshot branch_name is only valid for github-branch-policy")
    for operation, environment, _ in snapshot.rollback:
        if operation not in allowed_operations[snapshot.kind]:
            raise GitHubProviderApplyError("snapshot rollback operation is invalid")
        if environment != snapshot.target:
            raise GitHubProviderApplyError("snapshot rollback target is invalid")
    return snapshot


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
        final_error: GitHubProviderApplyError | None = None
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
            message = str(exc)
            compensation_receipt: BootstrapReceipt | None = None
            provider_state: Mapping[str, object] = {}
            if snapshots:
                # Journal the compensation intent durably (bound to an exportable
                # receipt) before attempting any rollback mutation whose own
                # read-back can fail, so callers never lose track of components
                # this operation created or changed.
                compensation_receipt = self._build_receipt(
                    plan,
                    repository,
                    snapshots,
                    created,
                    adopted,
                    changed,
                    error_info=RedactedStatusInfo(code="apply_failed", summary="github apply failed"),
                )
                self._last_apply_binding = _ApplyBinding(compensation_receipt.receipt_hash, compensation_receipt.operation_id, repository, tuple(snapshots))
                provider_state = self.export_provider_state(compensation_receipt)
            try:
                self._rollback_snapshots(owner, repo, snapshots, default_branch=default_branch, verify_expected=True)
            except GitHubProviderError:
                final_error = GitHubProviderRollbackError(
                    "github rollback failed after apply failure",
                    compensation_receipt=compensation_receipt,
                    provider_state=provider_state,
                )
            else:
                final_error = _redacted_error(message)
        # Raised outside the except block so the sanitized error never inherits
        # __context__ from the original (potentially secret-bearing) exception.
        if final_error is not None:
            raise final_error from None
        receipt = self._build_receipt(plan, repository, snapshots, created, adopted, changed)
        self._last_apply_binding = _ApplyBinding(receipt.receipt_hash, receipt.operation_id, repository, tuple(snapshots))
        return receipt

    def _build_receipt(
        self,
        plan: BootstrapPlan,
        repository: str,
        snapshots: Sequence[_ActionSnapshot],
        created: Sequence[str],
        adopted: Sequence[str],
        changed: Sequence[str],
        *,
        error_info: RedactedStatusInfo | None = None,
    ) -> BootstrapReceipt:
        return BootstrapReceipt.create(
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
            compensation_required_actions=tuple(snapshot.action_id for snapshot in snapshots if snapshot.rollback),
            error_info=error_info,
        )

    def export_provider_state(self, receipt: BootstrapReceipt) -> Mapping[str, object]:
        binding = self._validate_receipt_binding(receipt)
        payload = {
            "version": _STATE_VERSION,
            "receipt_hash": binding.receipt_hash,
            "operation_id": binding.operation_id,
            "repository": binding.repository,
            "snapshots": [_encode_snapshot(snapshot) for snapshot in binding.snapshots],
        }
        state = {
            **payload,
            "state_hash": canonical_sha256(payload),
        }
        safe = _canonicalized_document(state)
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
        owner, repo = _canonical_repo(repository)
        canonical_repository = f"{owner}/{repo}"
        snapshots_raw = payload.get("snapshots", [])
        if not isinstance(snapshots_raw, list):
            raise GitHubProviderApplyError("provider state snapshots must be a list")
        snapshots = tuple(_decode_snapshot(item) for item in snapshots_raw)
        state_hash = _bounded_text(
            payload.get("state_hash"),
            field="provider state state_hash",
            max_length=64,
            error_type=GitHubProviderApplyError,
        )
        hash_payload = {
            "version": version,
            "receipt_hash": receipt_hash,
            "operation_id": operation_id,
            "repository": repository,
            "snapshots": [_encode_snapshot(snapshot) for snapshot in snapshots],
        }
        if state_hash != canonical_sha256(hash_payload):
            raise GitHubProviderApplyError("provider state hash is invalid")
        safe_persisted_document(payload)
        self._last_apply_binding = _ApplyBinding(receipt_hash, operation_id, canonical_repository, snapshots)

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
        default_branch = self.read_repository_settings(binding.repository)[
            "default_branch"
        ]
        self._rollback_snapshots(
            owner,
            repo,
            list(binding.snapshots),
            default_branch=default_branch,
            verify_expected=True,
        )

    def verify_rollback(self, receipt: BootstrapReceipt) -> bool:
        binding = self._validate_receipt_binding(receipt)
        owner, repo = _canonical_repo(binding.repository)
        default_branch = self.read_repository_settings(binding.repository)["default_branch"]
        self._verify_rollback_state(owner, repo, default_branch, binding.snapshots)
        return True

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
                return _ActionSnapshot(action.action_id, action.kind, env_name, before, before, "adopted", (), branch_name=branch_name)
            rollback: list[tuple[str, str, object]] = []
            if not self._policy_enabled(env.deployment_branch_policy):
                rollback.append(("restore_environment_policy", env_name, env.deployment_branch_policy))
            snapshots.append(_ActionSnapshot(action.action_id, action.kind, env_name, before, {"pending": True}, "changed", tuple(rollback), branch_name=branch_name))
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
                branch_name=branch_name,
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
        # github-branch-policy identity is exact and persisted on the snapshot; never
        # re-derive it from the (possibly since-changed) live repository default branch.
        branch_name = snapshot.branch_name or default_branch
        env = self._inventory_environment(owner, repo, snapshot.target, branch_name)
        branch_policy = {
            "exists": env.requested_branch_policy.exists,
            "policy_id": env.requested_branch_policy.policy_id,
            "name": env.requested_branch_policy.name,
            "type": env.requested_branch_policy.type,
        }
        if not branch_policy["exists"]:
            branch_policy["policy_id"] = None
            branch_policy["name"] = None
            branch_policy["type"] = None
        return {
            "deployment_branch_policy": env.deployment_branch_policy,
            "branch_policy": branch_policy,
        }

    def _verify_rollback_state(self, owner: str, repo: str, default_branch: str, snapshots: Sequence[_ActionSnapshot]) -> None:
        created_environment_targets = {
            snapshot.target
            for snapshot in snapshots
            if snapshot.kind == "github-environment"
            and snapshot.ownership == "created"
            and isinstance(snapshot.before, Mapping)
            and snapshot.before.get("exists") is False
        }
        for snapshot in snapshots:
            if not snapshot.rollback:
                continue
            if (
                snapshot.kind != "github-environment"
                and snapshot.target in created_environment_targets
            ):
                env = self._inventory_environment(
                    owner,
                    repo,
                    snapshot.target,
                    default_branch,
                )
                if not env.exists:
                    continue
            current = self._read_live_state(owner, repo, default_branch, snapshot)
            expected_before = snapshot.before
            if canonical_sha256(current) != canonical_sha256(expected_before):
                raise GitHubProviderError(f"rollback verification failed for action {snapshot.action_id}")

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
                env = self._inventory_environment(owner, repo, snapshot.target, snapshot.branch_name or default_branch)
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

    def _rollback_snapshots(
        self,
        owner: str,
        repo: str,
        snapshots: Sequence[_ActionSnapshot],
        *,
        default_branch: str | None = None,
        verify_expected: bool = False,
    ) -> None:
        if verify_expected:
            branch = default_branch or "main"
            for snapshot in snapshots:
                if not snapshot.rollback:
                    continue
                if _is_pending_snapshot(snapshot):
                    # This action's own apply failed before it could be verified, so
                    # there is no confirmed post-state to compare against; compensate
                    # unconditionally instead of refusing on a manufactured drift.
                    continue
                current = self._read_live_state(owner, repo, branch, snapshot)
                if canonical_sha256(current) != canonical_sha256(
                    snapshot.expected_after
                ):
                    raise GitHubProviderError(
                        f"rollback refused because live state drifted: {snapshot.action_id}"
                    )
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
        parse_failed = False
        try:
            payload = json.loads(response.content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            parse_failed = True
        # Raised outside the except block so the sanitized error never retains a
        # __context__ pointing at the raw (potentially secret-bearing) response
        # body carried on UnicodeDecodeError.object / JSONDecodeError.doc.
        if parse_failed:
            raise GitHubProviderError("GitHub response must be a JSON object")
        if not isinstance(payload, Mapping):
            raise GitHubProviderError("GitHub response must be a JSON object")
        return payload


__all__ = [
    "GitHubBootstrapProvider",
    "GitHubProviderApplyError",
    "GitHubProviderError",
    "GitHubProviderRollbackError",
    "GitHubProviderTransportError",
    "rollback_failure_details",
]
