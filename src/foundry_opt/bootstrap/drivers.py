from __future__ import annotations

import contextlib
import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from azure.identity import AzureCliCredential, DefaultAzureCredential
import yaml

from foundry_opt.bootstrap.command_io import BootstrapCliError, BootstrapExitCode
from foundry_opt.bootstrap.contracts import BootstrapAction, BootstrapPlan, BootstrapReceipt, FingerprintRecord, SemanticPatchSpec, TemplatePayloadSpec
from foundry_opt.bootstrap.input_contracts import BootstrapPlanInput, TrustedTemplateManifest
from foundry_opt.bootstrap.orchestrator import PhaseDriver
from foundry_opt.bootstrap.packaging_policy import PACKAGE_EXCLUDES as _PACKAGE_EXCLUDES
from foundry_opt.bootstrap.plan_factory import build_phase_actions, load_trusted_manifest
from foundry_opt.bootstrap.providers.azure import AzureArmRestProvider
from foundry_opt.bootstrap.providers.foundry import AgentPackage, FoundryAdapter
from foundry_opt.bootstrap.providers.github import GitHubBootstrapProvider
from foundry_opt.bootstrap.repository.engine import apply_repository, inventory_repository, plan_repository, rollback_repository
from foundry_opt.packaging import PackagingError, build_deterministic_zip


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
        self._checkpoint: Callable[[Mapping[str, object]], None] | None = None

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
            setter = getattr(self._provider, "set_checkpoint", None)
            if callable(setter):
                setter(self._checkpoint)
        return self._provider

    def set_checkpoint(
        self,
        checkpoint: Callable[[Mapping[str, object]], None] | None,
    ) -> None:
        self._checkpoint = checkpoint
        if self._provider is not None:
            setter = getattr(self._provider, "set_checkpoint", None)
            if callable(setter):
                setter(checkpoint)

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
        self._checkpoint: Callable[[Mapping[str, object]], None] | None = None

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
            setter = getattr(self._provider, "set_checkpoint", None)
            if callable(setter):
                setter(self._checkpoint)
        return self._provider

    def set_checkpoint(
        self,
        checkpoint: Callable[[Mapping[str, object]], None] | None,
    ) -> None:
        self._checkpoint = checkpoint
        if self._provider is not None:
            setter = getattr(self._provider, "set_checkpoint", None)
            if callable(setter):
                setter(checkpoint)

    def _phase_plan(
        self,
        context: Mapping[str, object],
        plan_input: BootstrapPlanInput,
    ) -> BootstrapPlan:
        return BootstrapPlan.create(
            operation_id=str(context["operation_id"]),
            runtime_repository=str(context["runtime_repository"]),
            runtime_commit=str(context["runtime_commit"]),
            repository_identity=str(context["repository_id"]),
            actions=tuple(
                action
                for action in build_phase_actions(plan_input)
                if action.phase == "azure"
            ),
        )

    def live_fingerprints(self, context: Mapping[str, object]) -> Sequence[FingerprintRecord]:
        plan_input = _contextual_plan_input(context, self._plan_input)
        return self._client(plan_input).live_binding_fingerprints(
            self._phase_plan(context, plan_input)
        )

    def plan(self, context: Mapping[str, object]) -> Sequence[BootstrapAction]:
        plan_input = _contextual_plan_input(context, self._plan_input)
        return self._client(plan_input).plan_bindings(
            self._phase_plan(context, plan_input)
        )

    def apply(self, phase_plan: BootstrapPlan) -> BootstrapReceipt:
        return self._client(self._plan_input).apply_bindings(phase_plan)

    def verify(self, receipt: BootstrapReceipt) -> bool:
        return self._client(self._plan_input).verify_bindings(receipt)

    def export_provider_state(self, receipt: BootstrapReceipt) -> Mapping[str, object]:
        return self._client(self._plan_input).export_provider_state(receipt)

    def restore_provider_state(self, mapping: Mapping[str, object]) -> None:
        self._client(self._plan_input).restore_provider_state(mapping)

    def rollback(self, receipt: BootstrapReceipt) -> None:
        self._client(self._plan_input).rollback_bindings(receipt)

    def verify_rollback(self, receipt: BootstrapReceipt) -> bool:
        return self._client(self._plan_input).verify_rollback(receipt)


class EvaluationPhaseDriver(PhaseDriver):
    """Routes every evaluation operation to the Foundry project that owns the agent.

    Agents in one repository may live in different Foundry projects, so a single adapter is
    never shared across endpoints. Per-project receipts and provider state are aggregated
    deterministically, and a partially applied multi-project phase compensates the projects
    that already created resources before the failure is re-raised.
    """

    def __init__(
        self,
        *,
        plan_input: BootstrapPlanInput | None = None,
        provider: FoundryAdapter | None = None,
        checkpoint: Callable[[Mapping[str, object]], None] | None = None,
        repository_root: Path | None = None,
    ) -> None:
        self._plan_input = plan_input
        self._provider = provider
        self._providers: dict[str, FoundryAdapter] = {}
        self._checkpoint = checkpoint
        self._project_receipts: dict[str, BootstrapReceipt] = {}
        self._repository_root = Path(repository_root) if repository_root is not None else None

    def set_checkpoint(self, checkpoint: Callable[[Mapping[str, object]], None] | None) -> None:
        self._checkpoint = checkpoint
        for adapter in self._active_providers():
            adapter.set_checkpoint(self._checkpoint_for(adapter))

    def _checkpoint_for(self, adapter: FoundryAdapter) -> Callable[[Mapping[str, object]], None] | None:
        if self._checkpoint is None:
            return None
        endpoint = adapter.project_endpoint

        def _publish(snapshot: Mapping[str, object]) -> None:
            assert self._checkpoint is not None
            self._checkpoint({"schema_version": 1, "checkpoint": True, "projects": {endpoint: dict(snapshot)}})

        return _publish

    def _active_providers(self) -> tuple[FoundryAdapter, ...]:
        if self._provider is not None:
            return (self._provider,)
        try:
            endpoints = self._endpoints()
        except BootstrapCliError:
            endpoints = tuple(sorted(self._providers))
        return tuple(self._client_for(endpoint) for endpoint in endpoints)

    def _agents(self, plan_input: BootstrapPlanInput | None = None) -> tuple[object, ...]:
        resolved = plan_input if plan_input is not None else self._plan_input
        if resolved is None or resolved.evaluations_phase is None:
            raise BootstrapCliError("foundry-config-missing", "Foundry evaluation phase requires configured project input", exit_code=BootstrapExitCode.CONFIG)
        return tuple(resolved.evaluations_phase.agents)

    def _endpoints(self, plan_input: BootstrapPlanInput | None = None) -> tuple[str, ...]:
        return tuple(sorted({str(agent.project_endpoint) for agent in self._agents(plan_input)}))

    def _endpoint_for_agent(self, repo_agent_id: str, plan_input: BootstrapPlanInput | None = None) -> str:
        for agent in self._agents(plan_input):
            if str(agent.repo_agent_id).casefold() == repo_agent_id.casefold():
                return str(agent.project_endpoint)
        raise BootstrapCliError("foundry-project-unresolved", "evaluation action does not map to a reviewed agent project", exit_code=BootstrapExitCode.CONFIG, details={"repo_agent_id": repo_agent_id})

    def _client(self, plan_input: BootstrapPlanInput | None = None) -> FoundryAdapter:
        """Single-project convenience accessor; multi-project paths use `_client_for`."""

        if self._provider is not None:
            return self._provider
        endpoints = self._endpoints(plan_input)
        if len(endpoints) != 1:
            raise BootstrapCliError("foundry-project-ambiguous", "this operation requires an explicit agent project", exit_code=BootstrapExitCode.CONFIG, details={"projects": list(endpoints)})
        return self._client_for(endpoints[0])

    def _client_for(self, endpoint: str) -> FoundryAdapter:
        if self._provider is not None:
            return self._provider
        adapter = self._providers.get(endpoint)
        if adapter is None:
            adapter = FoundryAdapter(endpoint, DefaultAzureCredential(exclude_interactive_browser_credential=True))
            self._providers[endpoint] = adapter
        adapter.set_checkpoint(self._checkpoint_for(adapter))
        return adapter

    def _client_for_agent(self, repo_agent_id: str, plan_input: BootstrapPlanInput | None = None) -> FoundryAdapter:
        return self._client_for(self._endpoint_for_agent(repo_agent_id, plan_input))

    @staticmethod
    def _action_agent_id(action: BootstrapAction) -> str:
        parts = action.action_id.split(":")
        return parts[1] if len(parts) > 2 else ""

    def _group_actions(self, phase_plan: BootstrapPlan) -> dict[str, tuple[BootstrapAction, ...]]:
        grouped: dict[str, list[BootstrapAction]] = {}
        for action in phase_plan.actions:
            if action.phase != "evaluations":
                continue
            endpoint = self._endpoint_for_agent(self._action_agent_id(action))
            grouped.setdefault(endpoint, []).append(action)
        return {endpoint: tuple(actions) for endpoint, actions in sorted(grouped.items())}

    @staticmethod
    def _sub_plan(phase_plan: BootstrapPlan, actions: Sequence[BootstrapAction]) -> BootstrapPlan:
        return BootstrapPlan.create(
            operation_id=phase_plan.operation_id,
            runtime_repository=phase_plan.runtime_repository,
            runtime_commit=phase_plan.runtime_commit,
            repository_identity=phase_plan.repository_identity,
            actions=tuple(actions),
        )

    @staticmethod
    def _merge_receipts(phase_plan: BootstrapPlan, receipts: Mapping[str, BootstrapReceipt]) -> BootstrapReceipt:
        ordered = [receipts[key] for key in sorted(receipts)]
        def _union(field: str) -> tuple[str, ...]:
            return tuple(sorted({item for receipt in ordered for item in getattr(receipt, field)}))
        fingerprints = lambda field: tuple(sorted({(record.label, record.sha256) for receipt in ordered for record in getattr(receipt, field)}))  # noqa: E731
        return BootstrapReceipt.create(
            operation_id=phase_plan.operation_id,
            runtime_repository=phase_plan.runtime_repository,
            runtime_commit=phase_plan.runtime_commit,
            repository_identity=phase_plan.repository_identity,
            plan_hash=phase_plan.plan_hash,
            before_fingerprints=[{"label": label, "sha256": sha} for label, sha in fingerprints("before_fingerprints")],
            after_fingerprints=[{"label": label, "sha256": sha} for label, sha in fingerprints("after_fingerprints")],
            created_actions=_union("created_actions"),
            adopted_actions=_union("adopted_actions"),
            changed_actions=_union("changed_actions"),
            skipped_actions=_union("skipped_actions"),
            compensation_required_actions=_union("compensation_required_actions"),
        )

    def live_fingerprints(self, context: Mapping[str, object]) -> Sequence[FingerprintRecord]:
        plan_input = _contextual_plan_input(context, self._plan_input)
        assert plan_input.evaluations_phase is not None
        inventory_by_endpoint = {endpoint: _hash_json(self.inventory(endpoint=endpoint)) for endpoint in self._endpoints(plan_input)}
        return tuple(
            FingerprintRecord(
                label=f"evaluations:{agent.repo_agent_id}",
                sha256=_hash_json(
                    {
                        "agent": agent.model_dump(mode="json"),
                        "inventory": inventory_by_endpoint[str(agent.project_endpoint)],
                    }
                ),
            )
            for agent in plan_input.evaluations_phase.agents
        )

    def plan(self, context: Mapping[str, object]) -> Sequence[BootstrapAction]:
        plan_input = _contextual_plan_input(context, self._plan_input)
        return tuple(action for action in build_phase_actions(plan_input) if action.phase == "evaluations")

    def apply(self, phase_plan: BootstrapPlan) -> BootstrapReceipt:
        with self._packaged_agents() as packages:
            return self._apply_packaged(phase_plan, packages)

    @contextlib.contextmanager
    def _packaged_agents(self):
        """Package every reviewed agent source tree for the duration of one apply.

        Archives are deterministic (`build_deterministic_zip`), live in a private temporary
        directory, and are removed as soon as the phase finishes; their bytes never enter
        provider state, receipts, or logs.
        """

        agents = [agent for agent in self._agents() if getattr(agent, "onboarding_contract", None) is not None]
        if not agents or self._repository_root is None:
            yield {}
            return
        with tempfile.TemporaryDirectory(prefix="foundry-opt-package-") as scratch:
            packages: dict[str, AgentPackage] = {}
            for agent in agents:
                contract = agent.onboarding_contract
                policy = contract.sidecar_policy
                if policy is None or contract.stopped:
                    continue
                package_root = (self._repository_root / policy.package_root).resolve() if policy.package_root != "." else self._repository_root.resolve()
                if not package_root.is_dir():
                    raise BootstrapCliError("agent-package-root-missing", "reviewed package root does not exist", exit_code=BootstrapExitCode.CONFIG, details={"repo_agent_id": agent.repo_agent_id, "package_root": policy.package_root})
                destination = Path(scratch) / f"{agent.repo_agent_id}.zip"
                try:
                    result = build_deterministic_zip(
                        package_root,
                        destination,
                        includes=("**/*",),
                        excludes=_PACKAGE_EXCLUDES,
                        check_deadline=lambda: None,
                    )
                except PackagingError as exc:
                    raise BootstrapCliError("agent-package-failed", "reviewed agent source could not be packaged", exit_code=BootstrapExitCode.CONFIG, details={"repo_agent_id": agent.repo_agent_id, "reason": str(exc)[:200]}) from None
                packages[agent.repo_agent_id] = AgentPackage(
                    repo_agent_id=agent.repo_agent_id,
                    zip_path=str(result.zip_path),
                    zip_sha256=result.zip_sha256,
                    tree_sha256=result.tree_sha256,
                    file_count=len(result.entries),
                    size_bytes=result.size_bytes,
                )
            yield packages

    def _apply_packaged(self, phase_plan: BootstrapPlan, packages: Mapping[str, AgentPackage]) -> BootstrapReceipt:
        groups = self._group_actions(phase_plan)
        if len(groups) <= 1:
            endpoint = next(iter(groups), None) or self._endpoints()[0]
            client = self._client_for(endpoint)
            self._install_packages(client, endpoint, packages)
            receipt = client.apply_resources(phase_plan)
            self._project_receipts = {endpoint: receipt}
            return receipt
        receipts: dict[str, BootstrapReceipt] = {}
        try:
            for endpoint, actions in groups.items():
                client = self._client_for(endpoint)
                self._install_packages(client, endpoint, packages)
                receipts[endpoint] = client.apply_resources(self._sub_plan(phase_plan, actions))
        except Exception:
            # Created-only compensation across projects: everything already created by this
            # apply is rolled back before the failure surfaces.
            for applied_endpoint, applied_receipt in receipts.items():
                self._client_for(applied_endpoint).rollback_resources(applied_receipt)
            self._project_receipts = {}
            raise
        self._project_receipts = dict(receipts)
        return self._merge_receipts(phase_plan, receipts)

    def _packages_for(self, endpoint: str, packages: Mapping[str, AgentPackage]) -> dict[str, AgentPackage]:
        """Route each package to the adapter that owns that agent's Foundry project."""

        owned = {str(agent.repo_agent_id) for agent in self._agents() if str(agent.project_endpoint) == endpoint}
        return {key: value for key, value in packages.items() if key in owned}

    def _install_packages(self, client: FoundryAdapter, endpoint: str, packages: Mapping[str, AgentPackage]) -> None:
        routed = self._packages_for(endpoint, packages)
        if routed:
            client.set_agent_packages(routed)

    def _receipt_for(self, endpoint: str, receipt: BootstrapReceipt) -> BootstrapReceipt:
        recorded = self._project_receipts.get(endpoint)
        return recorded if recorded is not None else receipt

    def verify(self, receipt: BootstrapReceipt) -> bool:
        if len(self._project_receipts) <= 1:
            return self._client_for(self._only_endpoint()).verify_resources(self._receipt_for(self._only_endpoint(), receipt))
        return all(self._client_for(endpoint).verify_resources(item) for endpoint, item in sorted(self._project_receipts.items()))

    def _only_endpoint(self) -> str:
        if self._project_receipts:
            return sorted(self._project_receipts)[0]
        endpoints = self._endpoints()
        return endpoints[0]

    def export_provider_state(self, receipt: BootstrapReceipt) -> Mapping[str, object]:
        if len(self._project_receipts) <= 1:
            endpoint = self._only_endpoint()
            return self._client_for(endpoint).export_provider_state(self._receipt_for(endpoint, receipt))
        return {
            "schema_version": 1,
            "multi_project": True,
            "projects": {
                endpoint: {
                    "receipt": item.model_dump(mode="json"),
                    "provider_state": self._client_for(endpoint).export_provider_state(item),
                }
                for endpoint, item in sorted(self._project_receipts.items())
            },
        }

    def restore_provider_state(self, mapping: Mapping[str, object]) -> None:
        if mapping.get("multi_project"):
            projects = mapping.get("projects")
            if not isinstance(projects, Mapping):
                raise BootstrapCliError("provider-state-invalid", "multi-project provider state is invalid", exit_code=BootstrapExitCode.CONFIG)
            restored: dict[str, BootstrapReceipt] = {}
            for endpoint, payload in sorted(projects.items()):
                if not isinstance(payload, Mapping):
                    raise BootstrapCliError("provider-state-invalid", "multi-project provider state entry is invalid", exit_code=BootstrapExitCode.CONFIG)
                adapter = self._client_for(str(endpoint))
                state = payload.get("provider_state")
                if isinstance(state, Mapping):
                    adapter.restore_provider_state(state)
                receipt_payload = payload.get("receipt")
                if isinstance(receipt_payload, Mapping):
                    restored[str(endpoint)] = BootstrapReceipt.model_validate(dict(receipt_payload))
            self._project_receipts = restored
            return
        if mapping.get("checkpoint"):
            projects = mapping.get("projects")
            if not isinstance(projects, Mapping):
                raise BootstrapCliError("provider-state-invalid", "checkpoint provider state is invalid", exit_code=BootstrapExitCode.CONFIG)
            for endpoint, snapshot in sorted(projects.items()):
                if isinstance(snapshot, Mapping):
                    self._client_for(str(endpoint)).restore_checkpoint(snapshot)
            return
        self._client(self._plan_input).restore_provider_state(mapping)

    def rollback(self, receipt: BootstrapReceipt) -> None:
        if len(self._project_receipts) <= 1:
            endpoint = self._only_endpoint()
            self._client_for(endpoint).rollback_resources(self._receipt_for(endpoint, receipt))
            return
        for endpoint, item in sorted(self._project_receipts.items()):
            self._client_for(endpoint).rollback_resources(item)

    def verify_rollback(self, receipt: BootstrapReceipt) -> bool:
        if len(self._project_receipts) <= 1:
            endpoint = self._only_endpoint()
            return self._client_for(endpoint).verify_rollback(self._receipt_for(endpoint, receipt))
        return all(self._client_for(endpoint).verify_rollback(item) for endpoint, item in sorted(self._project_receipts.items()))

    def inventory(self, *, endpoint: str | None = None) -> Mapping[str, object]:
        target = endpoint or self._endpoints(self._plan_input)[0]
        client = self._client_for(target)
        return {
            "agents": client.inventory_agents(),
            "datasets": client.inventory_datasets(),
            "evaluators": client.inventory_evaluators(include_builtin=True),
            "connections": client.inventory_connections(),
            "model_deployments": client.inventory_model_deployments(),
        }

    def inventory_by_project(self) -> Mapping[str, Mapping[str, object]]:
        return {endpoint: self.inventory(endpoint=endpoint) for endpoint in self._endpoints(self._plan_input)}

    def onboarding_finalizations(self) -> Mapping[str, Mapping[str, object]]:
        merged: dict[str, Mapping[str, object]] = {}
        for adapter in self._active_providers():
            merged.update(adapter.onboarding_finalizations())
        return merged

    def observe_agent_binding(self, *, repo_agent_id: str | None = None, agent_name: str, agent_version: str, source_root: str, package_root: str) -> Mapping[str, object]:
        client = self._client_for_agent(repo_agent_id, self._plan_input) if repo_agent_id else self._client(self._plan_input)
        return client.observe_agent_binding(agent_name=agent_name, agent_version=agent_version, source_root=source_root, package_root=package_root)


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
