from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from azure.identity import AzureCliCredential, DefaultAzureCredential

from foundry_opt.bootstrap.command_io import BootstrapCliError, BootstrapExitCode
from foundry_opt.bootstrap.contracts import BootstrapAction, BootstrapLock, BootstrapPlan, BootstrapReceipt, DefaultEvaluatorBundle, FingerprintRecord, ImmutableDatasetReference, ImmutableDefinitionReference, RedactedStatusInfo
from foundry_opt.bootstrap.discovery import discover_repository_agents
from foundry_opt.bootstrap.errors import BootstrapApplyError, BootstrapConfigError
from foundry_opt.bootstrap.evaluation.core import ActivationReceipt, ReplacementOperation, choose_default_evaluator_bundle, compute_split_lineage_hash
from foundry_opt.bootstrap.orchestrator import PhaseDriver
from foundry_opt.bootstrap.providers.azure import AzureArmRestProvider
from foundry_opt.bootstrap.providers.foundry import FoundryAdapter
from foundry_opt.bootstrap.providers.github import GitHubBootstrapProvider
from foundry_opt.bootstrap.receipts import EvaluationReplacementRecord
from foundry_opt.bootstrap.repository.engine import LOCK_PATH, RepositoryInventoryEntry, apply_repository, drift_status, inventory_repository, plan_repository, rollback_repository


def _sha(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, default=str).encode("utf-8")).hexdigest()


def _status_info(code: str, summary: str) -> RedactedStatusInfo:
    return RedactedStatusInfo(code=code, summary=summary)


class RepositoryPhaseDriver(PhaseDriver):
    def __init__(self, *, repository_root: Path, payloads: Sequence[object]) -> None:
        self._root = repository_root
        self._payloads = tuple(payloads)
        self._receipts: dict[str, BootstrapLock] = {}

    def live_fingerprints(self, context: Mapping[str, object]) -> Sequence[FingerprintRecord]:
        inventory = inventory_repository(self._root, self._payloads)
        fingerprints = [
            FingerprintRecord(label=f"repository:{item.path}", sha256=item.sha256 or ("0" * 64))
            for item in inventory.entries
        ]
        lock_hash = hashlib.sha256((self._root / LOCK_PATH).read_bytes()).hexdigest() if (self._root / LOCK_PATH).exists() else ("0" * 64)
        fingerprints.append(FingerprintRecord(label="repository:lock", sha256=lock_hash))
        return tuple(fingerprints)

    def plan(self, context: Mapping[str, object]) -> Sequence[BootstrapAction]:
        plan = plan_repository(
            self._root,
            operation_id=str(context["operation_id"]),
            runtime_repository=str(context["runtime_repository"]),
            runtime_commit=str(context["runtime_commit"]),
            repository_identity=str(context["repository_id"]),
            payloads=self._payloads,
        )
        return plan.actions

    def apply(self, phase_plan: BootstrapPlan) -> BootstrapReceipt:
        receipt, lock = apply_repository(self._root, phase_plan)
        self._receipts[receipt.receipt_hash] = lock
        return receipt

    def verify(self, receipt: BootstrapReceipt) -> bool:
        lock = self._receipts.get(receipt.receipt_hash)
        if lock is None:
            return False
        return drift_status(self._root, lock) == ()

    def export_provider_state(self, receipt: BootstrapReceipt) -> Mapping[str, object]:
        lock = self._receipts.get(receipt.receipt_hash)
        return {
            "repository_root": str(self._root),
            "receipt_hash": receipt.receipt_hash,
            "lock_path": LOCK_PATH,
            "lock": lock.model_dump(mode="json") if lock is not None else None,
        }

    def restore_provider_state(self, mapping: Mapping[str, object]) -> None:
        lock_payload = mapping.get("lock")
        if isinstance(lock_payload, Mapping):
            self._receipts[str(mapping.get("receipt_hash"))] = BootstrapLock.model_validate(lock_payload)

    def rollback(self, receipt: BootstrapReceipt) -> None:
        rollback_repository(self._root, receipt)

    def verify_rollback(self, receipt: BootstrapReceipt) -> bool:
        lock = self._receipts.get(receipt.receipt_hash)
        return lock is not None and drift_status(self._root, lock) != ()


class GitHubPhaseDriver(PhaseDriver):
    def __init__(self, *, provider: GitHubBootstrapProvider | None = None, token_command: Sequence[str] = ("gh", "auth", "token")) -> None:
        self._provider = provider
        self._token_command = tuple(token_command)

    def _resolve_token(self) -> str:
        env_token = os.environ.get("FOUNDRY_OPT_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if env_token:
            return env_token.strip()
        try:
            completed = subprocess.run(self._token_command, check=True, capture_output=True, text=True)
        except Exception as exc:
            raise BootstrapCliError("github-auth-missing", "GitHub token unavailable", exit_code=BootstrapExitCode.AUTH) from exc
        token = completed.stdout.strip()
        if not token:
            raise BootstrapCliError("github-auth-missing", "GitHub token unavailable", exit_code=BootstrapExitCode.AUTH)
        return token

    def _client(self) -> GitHubBootstrapProvider:
        return self._provider or GitHubBootstrapProvider(token=self._resolve_token())

    def live_fingerprints(self, context: Mapping[str, object]) -> Sequence[FingerprintRecord]:
        repository = str(context["repository_id"])
        try:
            state = self._client().read_repository_settings(repository)
        except BootstrapCliError:
            raise
        except Exception as exc:
            raise BootstrapCliError("github-auth-required", "GitHub phase requires authentication", exit_code=BootstrapExitCode.AUTH) from exc
        return (
            FingerprintRecord(label="github:repository", sha256=_sha({"repository": state["repository"], "default_branch": state["default_branch"]})),
            FingerprintRecord(label="github:environments", sha256=_sha(state["environments"])),
        )

    def plan(self, context: Mapping[str, object]) -> Sequence[BootstrapAction]:
        if context.get("offline", True):
            return ()
        repository = str(context["repository_id"])
        return (
            BootstrapAction(action_id="github-environment-copilot", phase="github", stage="planned", kind="github-environment", diagnostics=("copilot", repository)),
            BootstrapAction(action_id="github-environment-foundry-production", phase="github", stage="planned", kind="github-environment", diagnostics=("foundry-production", repository)),
            BootstrapAction(action_id="github-variable-client-id", phase="github", stage="planned", kind="github-variable", diagnostics=("foundry-production", "AZURE_OPTIMIZER_CLIENT_ID")),
            BootstrapAction(action_id="github-branch-policy", phase="github", stage="planned", kind="github-branch-policy", diagnostics=("foundry-production", "main")),
        )

    def apply(self, phase_plan: BootstrapPlan) -> BootstrapReceipt:
        if not phase_plan.actions:
            return BootstrapReceipt.create(operation_id=phase_plan.operation_id, runtime_repository=phase_plan.runtime_repository, runtime_commit=phase_plan.runtime_commit, repository_identity=phase_plan.repository_identity, plan_hash=phase_plan.plan_hash)
        return self._client().apply_changes(phase_plan)

    def verify(self, receipt: BootstrapReceipt) -> bool:
        return self._client().verify_changes(receipt)

    def export_provider_state(self, receipt: BootstrapReceipt) -> Mapping[str, object]:
        return {
            "receipt_hash": receipt.receipt_hash,
            "operation_id": receipt.operation_id,
            "repository_identity": receipt.repository_identity,
        }

    def restore_provider_state(self, mapping: Mapping[str, object]) -> None:
        return None

    def rollback(self, receipt: BootstrapReceipt) -> None:
        self._client().rollback_changes(receipt)

    def verify_rollback(self, receipt: BootstrapReceipt) -> bool:
        return True


class AzurePhaseDriver(PhaseDriver):
    def __init__(self, *, provider: AzureArmRestProvider | None = None, approved_role_definitions: Mapping[str, str] | None = None) -> None:
        self._provider = provider
        self._approved_role_definitions = dict(approved_role_definitions or {"FoundryProjectReader": "/subscriptions/000/providers/Microsoft.Authorization/roleDefinitions/00000000-0000-0000-0000-000000000111"})

    def _token_provider(self, scope: str) -> str:
        for credential in (AzureCliCredential(), DefaultAzureCredential(exclude_interactive_browser_credential=True)):
            try:
                return credential.get_token(scope).token
            except Exception:
                continue
        raise BootstrapCliError("azure-auth-missing", "Azure token unavailable", exit_code=BootstrapExitCode.AUTH)

    def _client(self) -> AzureArmRestProvider:
        return self._provider or AzureArmRestProvider(token_provider=self._token_provider, approved_role_definitions=self._approved_role_definitions)

    def live_fingerprints(self, context: Mapping[str, object]) -> Sequence[FingerprintRecord]:
        selected = list(context.get("selected_agent_ids", ()))
        payload = {"repository": context["repository_id"], "agents": selected}
        return (FingerprintRecord(label="azure:selected", sha256=_sha(payload)),)

    def plan(self, context: Mapping[str, object]) -> Sequence[BootstrapAction]:
        plan = BootstrapPlan.create(
            operation_id=str(context["operation_id"]),
            runtime_repository=str(context["runtime_repository"]),
            runtime_commit=str(context["runtime_commit"]),
            repository_identity=str(context["repository_id"]),
            actions=(
                BootstrapAction(action_id="azure-identity", phase="azure", stage="planned", kind="managed-identity", diagnostics=("resource_id=/subscriptions/000/resourceGroups/rg/providers/Microsoft.ManagedIdentity/userAssignedIdentities/shared", "subscription_id=000", "name=shared", "location=eastus", "adopted=true", "client_id=11111111-1111-1111-1111-111111111111", "principal_id=22222222-2222-2222-2222-222222222222", "tenant_id=33333333-3333-3333-3333-333333333333")),
                BootstrapAction(action_id="azure-role-project", phase="azure", stage="planned", kind="role-assignment", diagnostics=("role=FoundryProjectReader", "scope=/subscriptions/000/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/acct/projects/proj")),
            ),
        )
        return self._client().plan_bindings(plan)

    def apply(self, phase_plan: BootstrapPlan) -> BootstrapReceipt:
        if not phase_plan.actions:
            return BootstrapReceipt.create(operation_id=phase_plan.operation_id, runtime_repository=phase_plan.runtime_repository, runtime_commit=phase_plan.runtime_commit, repository_identity=phase_plan.repository_identity, plan_hash=phase_plan.plan_hash)
        return self._client().apply_bindings(phase_plan)

    def verify(self, receipt: BootstrapReceipt) -> bool:
        return self._client().verify_bindings(receipt)

    def export_provider_state(self, receipt: BootstrapReceipt) -> Mapping[str, object]:
        return {"receipt_hash": receipt.receipt_hash, "compensation_required_actions": list(receipt.compensation_required_actions)}

    def restore_provider_state(self, mapping: Mapping[str, object]) -> None:
        return None

    def rollback(self, receipt: BootstrapReceipt) -> None:
        self._client().rollback_bindings(receipt)

    def verify_rollback(self, receipt: BootstrapReceipt) -> bool:
        return True


class EvaluationPhaseDriver(PhaseDriver):
    def __init__(self, *, provider: FoundryAdapter | None = None) -> None:
        self._provider = provider
        self._inventory_cache: dict[str, Any] = {}

    def _client(self) -> FoundryAdapter:
        if self._provider is None:
            raise BootstrapCliError("foundry-config-missing", "Foundry evaluation phase requires configured project endpoint", exit_code=BootstrapExitCode.CONFIG)
        return self._provider

    def live_fingerprints(self, context: Mapping[str, object]) -> Sequence[FingerprintRecord]:
        replacement = context.get("evaluator_replacement")
        return (FingerprintRecord(label="evaluations:replacement", sha256=_sha(replacement or {})),)

    def plan(self, context: Mapping[str, object]) -> Sequence[BootstrapAction]:
        return (
            BootstrapAction(action_id="evaluation-dataset", phase="evaluations", stage="planned", kind="dataset", diagnostics=("dataset-a", "1", "https://blob/data.jsonl", "uri_file")),
        )

    def apply(self, phase_plan: BootstrapPlan) -> BootstrapReceipt:
        return self._client().apply_resources(phase_plan)

    def verify(self, receipt: BootstrapReceipt) -> bool:
        return self._client().verify_resources(receipt)

    def export_provider_state(self, receipt: BootstrapReceipt) -> Mapping[str, object]:
        return {"receipt_hash": receipt.receipt_hash, "resource_ids": list(receipt.compensation_required_actions)}

    def restore_provider_state(self, mapping: Mapping[str, object]) -> None:
        return None

    def rollback(self, receipt: BootstrapReceipt) -> None:
        self._client().rollback_resources(receipt)

    def verify_rollback(self, receipt: BootstrapReceipt) -> bool:
        return True

    def inventory(self) -> Mapping[str, object]:
        client = self._client()
        payload = {
            "datasets": client.inventory_datasets(),
            "evaluators": client.inventory_evaluators(),
            "agents": client.inventory_agents(),
            "connections": client.inventory_connections(),
            "deployments": client.inventory_model_deployments(),
        }
        return payload

    def inspect_replacement(self, *, replacement: EvaluationReplacementRecord | None) -> Mapping[str, object]:
        return {
            "default_bundle": replacement.candidate_bundle_id if replacement else None,
            "provenance": "issue objectives vs pinned/default/issue evaluators",
            "lineage": replacement.lineage_hash if replacement else None,
        }
