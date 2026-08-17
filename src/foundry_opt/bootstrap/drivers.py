from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

from azure.identity import AzureCliCredential, DefaultAzureCredential
import yaml

from foundry_opt.bootstrap.command_io import BootstrapCliError, BootstrapExitCode
from foundry_opt.bootstrap.contracts import BootstrapAction, BootstrapPlan, BootstrapReceipt, FingerprintRecord, SemanticPatchSpec, TemplatePayloadSpec
from foundry_opt.bootstrap.input_contracts import BootstrapPlanInput, TrustedTemplateManifest
from foundry_opt.bootstrap.orchestrator import PhaseDriver
from foundry_opt.bootstrap.plan_factory import build_phase_actions, load_trusted_manifest
from foundry_opt.bootstrap.providers.azure import AzureArmRestProvider
from foundry_opt.bootstrap.providers.foundry import FoundryAdapter
from foundry_opt.bootstrap.providers.github import GitHubBootstrapProvider
from foundry_opt.bootstrap.repository.engine import apply_repository, inventory_repository, plan_repository, rollback_repository


def _hash_json(value: object) -> str:
    return __import__("hashlib").sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, default=str).encode("utf-8")).hexdigest()


def _load_plan_input(context: Mapping[str, object]) -> BootstrapPlanInput:
    plan_input = context.get("plan_input")
    if isinstance(plan_input, BootstrapPlanInput):
        return plan_input
    raise BootstrapCliError("plan-input-required", "BootstrapPlanInput is required", exit_code=BootstrapExitCode.CONFIG)


def _fallback_plan_input(context: Mapping[str, object]) -> BootstrapPlanInput | None:
    return context.get("fallback_plan_input") if isinstance(context.get("fallback_plan_input"), BootstrapPlanInput) else None


class RepositoryPhaseDriver(PhaseDriver):
    def __init__(
        self,
        *,
        repository_root: Path,
        plan_input: BootstrapPlanInput | None = None,
        payloads: Sequence[object] | None = None,
    ) -> None:
        self._root = repository_root
        self._plan_input = plan_input
        self._locks: dict[str, object] = {}
        self._static_payloads = tuple(payloads or ())

    def _payloads(self, plan_input: BootstrapPlanInput) -> tuple[TemplatePayloadSpec, ...]:
        return tuple(self._static_payloads) or load_trusted_manifest(plan_input)

    def live_fingerprints(self, context: Mapping[str, object]) -> Sequence[FingerprintRecord]:
        plan_input = _contextual_plan_input(context, self._plan_input)
        inventory = inventory_repository(self._root, self._payloads(plan_input))
        return tuple(FingerprintRecord(label=f"repository:{item.path}", sha256=item.sha256 or ("0" * 64)) for item in inventory.entries)

    def plan(self, context: Mapping[str, object]) -> Sequence[BootstrapAction]:
        plan_input = _contextual_plan_input(context, self._plan_input)
        plan = plan_repository(self._root, operation_id=str(context["operation_id"]), runtime_repository=str(context["runtime_repository"]), runtime_commit=str(context["runtime_commit"]), repository_identity=str(context["repository_id"]), payloads=self._payloads(plan_input))
        return plan.actions

    def apply(self, phase_plan: BootstrapPlan) -> BootstrapReceipt:
        receipt, lock = apply_repository(self._root, phase_plan)
        self._locks[receipt.receipt_hash] = lock
        return receipt

    def verify(self, receipt: BootstrapReceipt) -> bool:
        return receipt.receipt_hash in self._locks

    def export_provider_state(self, receipt: BootstrapReceipt) -> Mapping[str, object]:
        lock = self._locks.get(receipt.receipt_hash)
        return {"receipt_hash": receipt.receipt_hash, "lock": lock.model_dump(mode="json") if hasattr(lock, "model_dump") else None}

    def restore_provider_state(self, mapping: Mapping[str, object]) -> None:
        return None

    def rollback(self, receipt: BootstrapReceipt) -> None:
        rollback_repository(self._root, receipt)

    def verify_rollback(self, receipt: BootstrapReceipt) -> bool:
        return not (
            self._root
            / ".foundry-opt"
            / "receipts"
            / f"{receipt.operation_id}.json"
        ).exists()


class GitHubPhaseDriver(PhaseDriver):
    def __init__(
        self,
        *,
        plan_input: BootstrapPlanInput | None = None,
        provider: GitHubBootstrapProvider | None = None,
    ) -> None:
        self._plan_input = plan_input
        self._provider = provider

    def _resolve_token(self) -> str:
        env = os.environ.get("FOUNDRY_OPT_GITHUB_TOKEN") or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if env:
            return env.strip()
        try:
            return subprocess.run(["gh", "auth", "token"], check=True, capture_output=True, text=True).stdout.strip()
        except Exception as exc:
            raise BootstrapCliError("github-auth-missing", "GitHub token unavailable", exit_code=BootstrapExitCode.AUTH) from exc

    def _client(self) -> GitHubBootstrapProvider:
        if self._provider is None:
            self._provider = GitHubBootstrapProvider(token=self._resolve_token())
        return self._provider

    def live_fingerprints(self, context: Mapping[str, object]) -> Sequence[FingerprintRecord]:
        state = self._client().read_repository_settings(str(context["repository_id"]))
        return (
            FingerprintRecord(label="github:repository", sha256=_hash_json({"repository": state["repository"], "default_branch": state["default_branch"]})),
            FingerprintRecord(label="github:environments", sha256=_hash_json(state["environments"])),
        )

    def plan(self, context: Mapping[str, object]) -> Sequence[BootstrapAction]:
        plan_input = _contextual_plan_input(context, self._plan_input)
        return tuple(action for action in build_phase_actions(plan_input) if action.phase == "github")

    def apply(self, phase_plan: BootstrapPlan) -> BootstrapReceipt:
        return self._client().apply_changes(phase_plan)

    def verify(self, receipt: BootstrapReceipt) -> bool:
        return self._client().verify_changes(receipt)

    def export_provider_state(self, receipt: BootstrapReceipt) -> Mapping[str, object]:
        return self._client().export_provider_state(receipt)

    def restore_provider_state(self, mapping: Mapping[str, object]) -> None:
        self._client().restore_provider_state(mapping)

    def rollback(self, receipt: BootstrapReceipt) -> None:
        self._client().rollback_changes(receipt)

    def verify_rollback(self, receipt: BootstrapReceipt) -> bool:
        return self._client().verify_rollback(receipt)


class AzurePhaseDriver(PhaseDriver):
    def __init__(
        self,
        *,
        plan_input: BootstrapPlanInput | None = None,
        provider: AzureArmRestProvider | None = None,
    ) -> None:
        self._plan_input = plan_input
        self._provider = provider

    def _token_provider(self, scope: str) -> str:
        for credential in (AzureCliCredential(), DefaultAzureCredential(exclude_interactive_browser_credential=True)):
            try:
                return credential.get_token(scope).token
            except Exception:
                continue
        raise BootstrapCliError("azure-auth-missing", "Azure token unavailable", exit_code=BootstrapExitCode.AUTH)

    def _client(self, plan_input: BootstrapPlanInput | None = None) -> AzureArmRestProvider:
        if self._provider is None:
            roles = {}
            if plan_input and plan_input.azure_phase:
                roles = {item.alias: item.role_definition_id for item in plan_input.azure_phase.approved_role_assignments}
            self._provider = AzureArmRestProvider(token_provider=self._token_provider, approved_role_definitions=roles)
        return self._provider

    def live_fingerprints(self, context: Mapping[str, object]) -> Sequence[FingerprintRecord]:
        plan_input = _contextual_plan_input(context, self._plan_input)
        azure = plan_input.azure_phase
        assert azure is not None
        return (FingerprintRecord(label="azure:config", sha256=_hash_json(azure.model_dump(mode="json"))),)

    def plan(self, context: Mapping[str, object]) -> Sequence[BootstrapAction]:
        plan_input = _contextual_plan_input(context, self._plan_input)
        return self._client(plan_input).plan_bindings(BootstrapPlan.create(operation_id=str(context["operation_id"]), runtime_repository=str(context["runtime_repository"]), runtime_commit=str(context["runtime_commit"]), repository_identity=str(context["repository_id"]), actions=tuple(action for action in build_phase_actions(plan_input) if action.phase == "azure")))

    def apply(self, phase_plan: BootstrapPlan) -> BootstrapReceipt:
        return self._client().apply_bindings(phase_plan)

    def verify(self, receipt: BootstrapReceipt) -> bool:
        return self._client().verify_bindings(receipt)

    def export_provider_state(self, receipt: BootstrapReceipt) -> Mapping[str, object]:
        return self._client().export_provider_state(receipt)

    def restore_provider_state(self, mapping: Mapping[str, object]) -> None:
        self._client().restore_provider_state(mapping)

    def rollback(self, receipt: BootstrapReceipt) -> None:
        self._client().rollback_bindings(receipt)

    def verify_rollback(self, receipt: BootstrapReceipt) -> bool:
        return self._client().verify_rollback(receipt)


class EvaluationPhaseDriver(PhaseDriver):
    def __init__(
        self,
        *,
        plan_input: BootstrapPlanInput | None = None,
        provider: FoundryAdapter | None = None,
    ) -> None:
        self._plan_input = plan_input
        self._provider = provider

    def _client(self, plan_input: BootstrapPlanInput | None = None) -> FoundryAdapter:
        if self._provider is not None:
            return self._provider
        if plan_input is None or plan_input.evaluations_phase is None:
            raise BootstrapCliError("foundry-config-missing", "Foundry evaluation phase requires configured project input", exit_code=BootstrapExitCode.CONFIG)
        endpoint = plan_input.evaluations_phase.agents[0].project_endpoint
        self._provider = FoundryAdapter(endpoint, DefaultAzureCredential(exclude_interactive_browser_credential=True))
        return self._provider

    def live_fingerprints(self, context: Mapping[str, object]) -> Sequence[FingerprintRecord]:
        plan_input = _contextual_plan_input(context, self._plan_input)
        assert plan_input.evaluations_phase is not None
        inventory_hash = _hash_json(self.inventory())
        return tuple(
            FingerprintRecord(
                label=f"evaluations:{agent.repo_agent_id}",
                sha256=_hash_json(
                    {
                        "agent": agent.model_dump(mode="json"),
                        "inventory": inventory_hash,
                    }
                ),
            )
            for agent in plan_input.evaluations_phase.agents
        )

    def plan(self, context: Mapping[str, object]) -> Sequence[BootstrapAction]:
        plan_input = _contextual_plan_input(context, self._plan_input)
        return tuple(action for action in build_phase_actions(plan_input) if action.phase == "evaluations")

    def apply(self, phase_plan: BootstrapPlan) -> BootstrapReceipt:
        return self._client().apply_resources(phase_plan)

    def verify(self, receipt: BootstrapReceipt) -> bool:
        return self._client().verify_resources(receipt)

    def export_provider_state(self, receipt: BootstrapReceipt) -> Mapping[str, object]:
        return self._client().export_provider_state(receipt)

    def restore_provider_state(self, mapping: Mapping[str, object]) -> None:
        self._client().restore_provider_state(mapping)

    def rollback(self, receipt: BootstrapReceipt) -> None:
        self._client().rollback_resources(receipt)

    def verify_rollback(self, receipt: BootstrapReceipt) -> bool:
        return self._client().verify_rollback(receipt)

    def inventory(self) -> Mapping[str, object]:
        client = self._client(self._plan_input)
        return {
            "agents": client.inventory_agents(),
            "datasets": client.inventory_datasets(),
            "evaluators": client.inventory_evaluators(include_builtin=True),
            "connections": client.inventory_connections(),
            "model_deployments": client.inventory_model_deployments(),
        }

    def observe_agent_binding(self, *, agent_name: str, agent_version: str, source_root: str, package_root: str) -> Mapping[str, object]:
        return self._client(self._plan_input).observe_agent_binding(agent_name=agent_name, agent_version=agent_version, source_root=source_root, package_root=package_root)


def _contextual_plan_input(
    context: Mapping[str, object],
    configured: BootstrapPlanInput | None = None,
) -> BootstrapPlanInput:
    try:
        return _load_plan_input(context)
    except BootstrapCliError:
        fallback = _fallback_plan_input(context)
        if fallback is not None:
            return fallback
        if configured is not None:
            return configured
        raise
