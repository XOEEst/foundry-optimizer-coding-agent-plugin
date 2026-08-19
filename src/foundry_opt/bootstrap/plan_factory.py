from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import yaml

from foundry_opt.bootstrap.contracts import (
    BootstrapAction,
    DistributionSettings,
    ExplicitAgentEntry,
    FingerprintRecord,
    GitHubSettings,
    IdentitySettings,
    RootRegistry,
    SemanticPatchSpec,
    TemplatePayloadSpec,
)
from foundry_opt.bootstrap.errors import BootstrapPlanError
from foundry_opt.bootstrap.input_contracts import (
    BootstrapPlanInput,
    SelectedAgent,
    TrustedTemplateManifest,
)

_TEMPLATE_RUNTIME_COMMIT = "7972654051b9cce644836c69ef94a43c0f68ded1"
_TEMPLATE_LOCK_SHA256 = (
    "74d7bb534c53e71a61ce197f3d5fa3169f2413373c2e42617280e78e83d6c681"
)


def load_trusted_manifest(plan_input: BootstrapPlanInput) -> tuple[TemplatePayloadSpec, ...]:
    manifest = TrustedTemplateManifest.load_pinned_manifest()
    if (
        plan_input.repository_phase.trusted_manifest_id != manifest.manifest_id
        or plan_input.repository_phase.trusted_manifest_version != manifest.manifest_version
        or plan_input.repository_phase.trusted_manifest_hash != manifest.manifest_hash
    ):
        raise BootstrapPlanError("trusted manifest verification failed")
    render_by_agent = {item.repo_agent_id: {entry.key: entry.value for entry in item.values} for item in plan_input.repository_phase.agent_render_contexts}
    payloads: list[TemplatePayloadSpec] = []
    base = Path(__file__).resolve().parents[3]
    for payload in manifest.managed_payloads:
        if payload.template_id == "sidecar":
            # The sidecar is the only managed payload that depends on a successful cloud
            # evaluation activation, so it is written by the receipt-bound finalize step
            # (`foundry-opt bootstrap evaluation activate`), never by the repository phase.
            continue
        targets = plan_input.repository.selected_agents if payload.scope == "agent" else (plan_input.repository.selected_agents[0],)
        for agent in targets:
            source = (base / payload.source_template_path).read_text(encoding="utf-8")
            rendered = _render_managed_payload(
                template_id=payload.template_id,
                source=source,
                plan_input=plan_input,
                selected_agent=agent,
            )
            context = {
                "selected.root": agent.root,
                "selected.config_path": agent.config_path,
                "selected.repo_agent_id": agent.repo_agent_id,
                "repository.id": plan_input.repository.repository_id,
                "repository.url": plan_input.repository.repository_url,
                "repository.default_branch": plan_input.repository.default_branch,
                "runtime.repository": plan_input.runtime_provenance.runtime_repository_url,
                "runtime.commit": plan_input.runtime_provenance.runtime_commit,
                "runtime.uv_lock_sha256": plan_input.runtime_provenance.uv_lock_sha256,
            }
            for key, value in render_by_agent.get(agent.repo_agent_id, {}).items():
                context[f"render.{key}"] = value
            for key, value in context.items():
                rendered = rendered.replace("${" + key + "}", str(value))
            rendered = rendered.replace(
                _TEMPLATE_RUNTIME_COMMIT,
                plan_input.runtime_provenance.runtime_commit,
            ).replace(
                _TEMPLATE_LOCK_SHA256,
                plan_input.runtime_provenance.uv_lock_sha256,
            )
            patches: tuple[SemanticPatchSpec, ...] = ()
            if payload.template_id == "setup-semantic-patch":
                rendered = rendered.replace(
                    "      - name: Fetch exact v1-capable shared revision",
                    "      - id: foundry-opt-checkout\n"
                    "        name: Fetch exact v1-capable shared revision",
                ).replace(
                    "      - name: Install the exact shared CLI and skill",
                    "      - id: foundry-opt-bootstrap\n"
                    "        name: Install the exact shared CLI and skill",
                )
                patches = tuple(
                    SemanticPatchSpec(
                        target_path=payload.destination_path,
                        operation="replace",
                        match_text=f"id: {step_id}",
                        replacement_text=_managed_step_fragment(
                            rendered,
                            step_id,
                        ),
                    )
                    for step_id in (
                        "foundry-opt-checkout",
                        "foundry-opt-bootstrap",
                    )
                )
            destination = payload.destination_path.replace("{selected.root}", agent.root)
            payloads.append(TemplatePayloadSpec(template_id=payload.template_id, destination_path=destination, rendered_template=rendered, semantic_patches=patches))
    return tuple(payloads)


def _managed_step_fragment(document: str, step_id: str) -> str:
    lines = document.splitlines()
    start = next(
        (
            index
            for index, line in enumerate(lines)
            if line.startswith(f"      - id: {step_id}")
        ),
        None,
    )
    if start is None:
        raise BootstrapPlanError(f"managed setup step is missing: {step_id}")
    end = start + 1
    while end < len(lines) and not lines[end].startswith("      - "):
        end += 1
    fragment = lines[start:end]
    fragment[0] = fragment[0].replace("      - ", "", 1)
    return "\n".join(line[6:] if line.startswith("      ") else line for line in fragment) + "\n"


def _render_managed_payload(
    *,
    template_id: str,
    source: str,
    plan_input: BootstrapPlanInput,
    selected_agent: SelectedAgent,
) -> str:
    if template_id == "registry":
        github = plan_input.github_phase
        azure = plan_input.azure_phase
        identity = (
            IdentitySettings(
                kind=azure.identity.identity_kind,
                resource_id=azure.identity.existing_resource_id,
                client_id=azure.identity.existing_client_id,
            )
            if azure is not None
            and not (
                azure.identity.identity_kind == "user_assigned_managed_identity"
                and azure.identity.create_if_missing
            )
            else IdentitySettings(kind="unresolved_migration")
        )
        registry = RootRegistry(
            distribution=DistributionSettings(
                repository=plan_input.runtime_provenance.runtime_repository_url,
                channel="pinned",
                pin=plan_input.runtime_provenance.runtime_commit,
            ),
            github=GitHubSettings(
                optimizer_environment=(
                    github.optimizer_environment if github else "copilot"
                ),
                deployment_environment=(
                    github.deployment_environment
                    if github
                    else "foundry-production"
                ),
                client_id_variable=(
                    github.client_id_variable_name
                    if github
                    else "AZURE_FOUNDRY_OPT_CLIENT_ID"
                ),
            ),
            identity=identity,
            agents=tuple(
                ExplicitAgentEntry(
                    agent_id=agent.repo_agent_id,
                    root=agent.root,
                    config_path=agent.config_path,
                    enabled=False,
                )
                for agent in plan_input.repository.selected_agents
            ),
        )
        return yaml.safe_dump(
            registry.model_dump(mode="json"),
            sort_keys=False,
            allow_unicode=False,
        )
    if template_id == "sidecar":
        raise BootstrapPlanError(
            "sidecar documents are produced from the resolved evaluation execution contract "
            "after successful activation, never rendered during the repository phase"
        )
    return source


def build_phase_actions(plan_input: BootstrapPlanInput, inventories: Mapping[str, object] | None = None) -> tuple[BootstrapAction, ...]:
    actions: list[BootstrapAction] = []
    if "github" in plan_input.required_phases and plan_input.github_phase is not None:
        gh = plan_input.github_phase
        actions.extend(
            (
                BootstrapAction(action_id=f"github-environment-{gh.optimizer_environment}", phase="github", stage="planned", kind="github-environment", diagnostics=(gh.optimizer_environment,)),
                BootstrapAction(action_id=f"github-environment-{gh.deployment_environment}", phase="github", stage="planned", kind="github-environment", diagnostics=(gh.deployment_environment,)),
            )
        )
        if gh.default_branch_policy_intent != "preserve_repository_default":
            if (
                gh.default_branch_policy_intent == "require_main"
                and plan_input.repository.default_branch != "main"
            ):
                raise BootstrapPlanError(
                    "require_main branch policy intent requires repository default_branch=main"
                )
            actions.append(
                BootstrapAction(
                    action_id="github-branch-policy",
                    phase="github",
                    stage="planned",
                    kind="github-branch-policy",
                    diagnostics=(
                        gh.deployment_environment,
                        plan_input.repository.default_branch,
                    ),
                )
            )
        if gh.shared_client_id != "azure_identity_resolution_required":
            seen_environments: list[str] = []
            for environment in (gh.optimizer_environment, gh.deployment_environment):
                if environment in seen_environments:
                    # The shared identity is bound to a single client_id, so a
                    # variable action is only emitted once per distinct
                    # environment even when optimizer and deployment share one.
                    continue
                seen_environments.append(environment)
                actions.append(
                    BootstrapAction(
                        action_id=f"github-variable-client-id-{environment}",
                        phase="github",
                        stage="planned",
                        kind="github-variable",
                        diagnostics=(environment, gh.client_id_variable_name, gh.shared_client_id),
                    )
                )
    if "azure" in plan_input.required_phases and plan_input.azure_phase is not None:
        az = plan_input.azure_phase
        identity = az.identity
        diagnostics = [
            f"subscription_id={az.subscription_id}",
            f"tenant_id={az.tenant_id}",
            f"location={az.location}",
            # The exact identity this operation adopts or creates, derived from the reviewed
            # resource id so plans, receipts, and provider state never carry a placeholder.
            f"name={identity.identity_name}",
            f"adopted={'false' if identity.create_if_missing else 'true'}",
        ]
        if identity.existing_resource_id:
            diagnostics.append(f"resource_id={identity.existing_resource_id}")
        if identity.existing_client_id:
            diagnostics.append(f"client_id={identity.existing_client_id}")
        if identity.existing_object_id:
            diagnostics.append(f"principal_id={identity.existing_object_id}")
        identity_kind = (
            "entra-application"
            if identity.identity_kind == "entra_application"
            else "managed-identity"
        )
        actions.append(BootstrapAction(action_id="azure-identity", phase="azure", stage="planned", kind=identity_kind, diagnostics=tuple(diagnostics)))
        actions.append(BootstrapAction(action_id="azure-fic-copilot", phase="azure", stage="planned", kind="federated-credential", diagnostics=(f"subject=repo:{az.github_repository_id}:environment:copilot",)))
        actions.append(BootstrapAction(action_id="azure-fic-foundry-production", phase="azure", stage="planned", kind="federated-credential", diagnostics=(f"subject=repo:{az.github_repository_id}:environment:foundry-production",)))
        for role in az.approved_role_assignments:
            actions.append(BootstrapAction(action_id=f"azure-rbac-{role.alias}", phase="azure", stage="planned", kind="role-assignment", diagnostics=(f"scope={role.scope}", f"role={role.alias}", f"role_definition_id={role.role_definition_id}")))
    if "evaluations" in plan_input.required_phases and plan_input.evaluations_phase is not None:
        actions.extend(build_evaluation_actions(plan_input))
    return tuple(actions)


def build_evaluation_actions(plan_input: BootstrapPlanInput) -> tuple[BootstrapAction, ...]:
    """Build one approval-bound composite onboarding action per evaluation agent."""

    if plan_input.evaluations_phase is None:
        return ()
    actions: list[BootstrapAction] = []
    for agent in plan_input.evaluations_phase.agents:
        contract = agent.onboarding_contract
        if contract is None:
            raise BootstrapPlanError(
                "evaluation action planning requires an approved onboarding contract with "
                "deterministic requested names, generation job ids, reuse candidates, "
                "sidecar policy, and fail-closed bounds"
            )
        actions.extend(contract.composite_action())
    action_ids = [action.action_id for action in actions]
    if len(action_ids) != len(set(action_ids)):
        raise BootstrapPlanError("evaluation action ids must be unique")
    return tuple(actions)


def stopped_evaluation_agents(plan_input: BootstrapPlanInput) -> tuple[Mapping[str, str], ...]:
    """Report agents that must stop before evaluation generation and activation."""

    if plan_input.evaluations_phase is None:
        return ()
    return tuple(
        {
            "repo_agent_id": agent.repo_agent_id,
            "binding_classification": agent.onboarding_contract.binding_classification,
            "stop_reason": agent.onboarding_contract.stop_reason or "",
        }
        for agent in plan_input.evaluations_phase.agents
        if agent.onboarding_contract is not None and agent.onboarding_contract.stopped
    )


def read_live_status(plan_input: BootstrapPlanInput, drivers: Mapping[str, object]) -> dict[str, object]:
    payload: dict[str, object] = {"repository_id": plan_input.repository.repository_id, "required_phases": list(plan_input.required_phases), "phases": {}}
    required = set(plan_input.required_phases)
    if "github" in required and "github" in drivers:
        payload["phases"]["github"] = {"inventory": getattr(drivers["github"], "live_fingerprints")({"repository_id": plan_input.repository.repository_id, "plan_input": plan_input})}
    if "azure" in required and "azure" in drivers:
        payload["phases"]["azure"] = {"inventory": getattr(drivers["azure"], "live_fingerprints")({"repository_id": plan_input.repository.repository_id, "plan_input": plan_input})}
    if "evaluations" in required and "evaluations" in drivers:
        payload["phases"]["evaluations"] = {"inventory": getattr(drivers["evaluations"], "live_fingerprints")({"repository_id": plan_input.repository.repository_id, "plan_input": plan_input})}
    return json.loads(json.dumps(payload, default=lambda value: value.model_dump(mode="json") if hasattr(value, "model_dump") else value))
