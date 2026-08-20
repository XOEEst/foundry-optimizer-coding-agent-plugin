"""Typer command registration for bootstrap operations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import typer
import yaml
from pydantic import ValidationError

from foundry_opt.bootstrap.command_io import BootstrapCliError, BootstrapExitCode, emit_json, load_json_file, write_json_file
from foundry_opt.bootstrap.contracts import RootRegistry
from foundry_opt.bootstrap.drivers import AzurePhaseDriver, EvaluationPhaseDriver, GitHubPhaseDriver, RepositoryPhaseDriver
from foundry_opt.bootstrap.errors import (
    BootstrapApplyError,
    BootstrapConfigError,
    BootstrapContractError,
    BootstrapPlanError,
    BootstrapProviderError,
)
from foundry_opt.bootstrap.evaluation.activation import finalize_evaluation_activation, read_finalize_journal
from foundry_opt.bootstrap.evaluation.inventory import assess_agent_inventory
from foundry_opt.bootstrap.operation_state import SelectionPlan, default_state_root, read_operation_state
from foundry_opt.bootstrap.orchestrator import BootstrapOrchestrator
from foundry_opt.bootstrap.plan_factory import (
    build_evaluation_actions,
    read_live_status,
    stopped_evaluation_agents,
)
from foundry_opt.bootstrap.receipts import ApprovalRecord, ApplyPhaseName, EvaluationReplacementRecord
from foundry_opt.bootstrap.discovery import discover_repository_agents
from foundry_opt.bootstrap.input_contracts import BindingEvidenceInput, BootstrapPlanInput, load_binding_evidence_input, load_bootstrap_plan_input
from foundry_opt.distribution import load_shared_pin, verify_shared_checkout, write_bootstrap_receipt
from foundry_opt.poc.config import SharedPin

_APPROVAL_HASH_FIELD = "approval_hash"
_DEFAULT_PACKAGE_PATH = "."
_DEFAULT_SKILL_PATH = "src/foundry_opt/templates/skills/foundry-agent-optimizer"


def _pin_from_registry(
    registry_path: Path | None,
    *,
    uv_lock_sha256: str | None,
    package_path: str,
    skill_path: str,
) -> SharedPin:
    """Derive the runtime pin from the committed registry distribution settings.

    `.foundry-opt/registry.yaml` is the authoritative desired distribution configuration for a
    v1 repository, so exact-revision verification no longer depends on the legacy
    `.github/foundry-opt.lock.yml` payload.
    """

    if registry_path is None:
        raise BootstrapCliError("registry-required", "registry path is required", exit_code=BootstrapExitCode.CONFIG)
    if not uv_lock_sha256:
        raise BootstrapCliError(
            "uv-lock-digest-required",
            "--uv-lock-sha256 is required when verifying against the registry",
            exit_code=BootstrapExitCode.CONFIG,
        )
    try:
        registry = RootRegistry.from_document(registry_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BootstrapCliError(
            "registry-missing",
            "registry could not be read",
            exit_code=BootstrapExitCode.MISSING,
            details={"path": str(registry_path)},
        ) from exc
    if registry.distribution.pin is None:
        raise BootstrapCliError(
            "registry-pin-required",
            "registry distribution.pin must record the exact runtime commit",
            exit_code=BootstrapExitCode.CONFIG,
        )
    return SharedPin.from_document(
        {
            "schema_version": 1,
            "repository_url": registry.distribution.repository,
            "commit": registry.distribution.pin,
            "package_path": package_path,
            "skill_path": skill_path,
            "uv_lock_sha256": uv_lock_sha256,
        }
    )


def _runtime_commit() -> str:
    value = os.environ.get("FOUNDRY_OPT_RUNTIME_COMMIT")
    if value and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value):
        return value
    raise BootstrapCliError("runtime-commit-required", "runtime commit must come from verified environment or explicit option", exit_code=BootstrapExitCode.CONFIG)


def _runtime_repository() -> str:
    value = os.environ.get("FOUNDRY_OPT_RUNTIME_REPOSITORY")
    if value:
        return value
    raise BootstrapCliError("runtime-repository-required", "runtime repository must come from verified environment or explicit option", exit_code=BootstrapExitCode.CONFIG)


def _build_orchestrator(
    *,
    repo_root: Path,
    plan_input: BootstrapPlanInput | None = None,
    state_root: Path | None = None,
) -> BootstrapOrchestrator:
    return BootstrapOrchestrator(
        repository_driver=RepositoryPhaseDriver(
            repository_root=repo_root,
            plan_input=plan_input,
        ),
        github_driver=GitHubPhaseDriver(plan_input=plan_input),
        azure_driver=AzurePhaseDriver(plan_input=plan_input),
        evaluations_driver=EvaluationPhaseDriver(plan_input=plan_input, repository_root=repo_root),
        state_root=state_root,
    )


def _context_plan_input(plan_input_path: Path | None) -> BootstrapPlanInput | None:
    return load_bootstrap_plan_input(plan_input_path) if plan_input_path is not None else None


def _resolve_binding_evidence(
    evidence_path: Path | None,
    *,
    plan_input: BootstrapPlanInput | None,
    repository_id: str,
) -> BindingEvidenceInput | None:
    """Resolve reviewed binding evidence from exactly one authoritative source."""

    nested = plan_input.binding_evidence if plan_input is not None else None
    if evidence_path is not None and nested is not None:
        raise BootstrapCliError(
            "binding-evidence-conflict",
            "binding evidence must come from either --binding-evidence or the plan input, not both",
            exit_code=BootstrapExitCode.CONFIG,
        )
    evidence = load_binding_evidence_input(evidence_path) if evidence_path is not None else nested
    if evidence is None:
        return None
    if evidence.repository_id.casefold() != repository_id.casefold():
        raise BootstrapCliError(
            "binding-evidence-repository-mismatch",
            "binding evidence repository_id must match the discovered repository",
            exit_code=BootstrapExitCode.CONFIG,
            details={"expected": repository_id, "observed": evidence.repository_id},
        )
    if plan_input is not None:
        selected_roots = {
            agent.discovery_selection_root.casefold()
            for agent in plan_input.repository.selected_agents
        }
        unknown = sorted(item.root for item in evidence.agents if item.root.casefold() not in selected_roots)
        if unknown:
            raise BootstrapCliError(
                "binding-evidence-unknown-root",
                "binding evidence roots must match selected agent roots",
                exit_code=BootstrapExitCode.CONFIG,
                details={"roots": unknown},
            )
    return evidence


def _verify_binding_claims(plan_input: BootstrapPlanInput, *, repo_root: Path) -> dict[str, str]:
    """Re-derive binding classifications from reviewed evidence and refuse false claims.

    Verification runs on every planning, apply, and activation path, not only the
    `evaluation plan` helper, so an operator cannot reach an approved mutation by skipping a
    command. It is a no-op only when the plan input carries no binding evidence: without
    observed content fingerprints there is nothing to check a claim against, and an agent is
    expected to remain `bound-unknown`. When evidence is present, every reviewed onboarding
    contract must claim exactly the classification discovery derives from that evidence.
    """

    evidence = plan_input.binding_evidence
    if evidence is None or plan_input.evaluations_phase is None:
        return {}
    try:
        result = discover_repository_agents(
            repo_root,
            selected_agents=tuple(
                {
                    "root": agent.discovery_selection_root,
                    "repoAgentId": agent.repo_agent_id,
                }
                for agent in plan_input.repository.selected_agents
            ),
            binding_evidence_by_root=evidence.by_root(),
        )
    except BootstrapConfigError as exc:
        raise BootstrapCliError("binding-verification-failed", str(exc), exit_code=BootstrapExitCode.CONFIG) from exc
    observed = {agent.repoAgentId.casefold(): agent.bindingAssessment.classification for agent in result.agents}
    verified: dict[str, str] = {}
    for agent in plan_input.evaluations_phase.agents:
        contract = agent.onboarding_contract
        if contract is None:
            continue
        derived = observed.get(agent.repo_agent_id.casefold())
        if derived is None:
            raise BootstrapCliError(
                "binding-verification-missing",
                "binding evidence was supplied but the agent was not discovered",
                exit_code=BootstrapExitCode.CONFIG,
                details={"repo_agent_id": agent.repo_agent_id},
            )
        if derived != contract.binding_classification:
            raise BootstrapCliError(
                "binding-classification-mismatch",
                "approved binding classification does not match the classification derived from binding evidence",
                exit_code=BootstrapExitCode.CONFIG,
                details={"repo_agent_id": agent.repo_agent_id, "approved": contract.binding_classification, "observed": derived},
            )
        verified[agent.repo_agent_id] = derived
    return verified


def _verify_selected_discovery(
    plan_input: BootstrapPlanInput,
    selection: SelectionPlan,
) -> None:
    discovered_by_id = {
        item.repo_agent_id.casefold(): item
        for item in selection.discovered_agents
    }
    for selected in plan_input.repository.selected_agents:
        discovered = discovered_by_id.get(selected.repo_agent_id.casefold())
        if discovered is None:
            raise BootstrapCliError(
                "selection-discovery-mismatch",
                "selected agent is missing from persisted discovery",
                exit_code=BootstrapExitCode.CONFIG,
                details={"repo_agent_id": selected.repo_agent_id},
            )
        if discovered.root.casefold() != selected.discovery_selection_root.casefold():
            raise BootstrapCliError(
                "selection-discovery-mismatch",
                "selected discovery_root does not match persisted discovery",
                exit_code=BootstrapExitCode.CONFIG,
                details={
                    "repo_agent_id": selected.repo_agent_id,
                    "expected": discovered.root,
                    "observed": selected.discovery_selection_root,
                },
            )
        expected_managed_root = (
            discovered.source_root
            if discovered.root == "."
            else discovered.root
        )
        if expected_managed_root == ".":
            raise BootstrapCliError(
                "selection-root-unsupported",
                "repository-root discovery requires a concrete sourceRoot for managed bootstrap",
                exit_code=BootstrapExitCode.CONFIG,
                details={"repo_agent_id": selected.repo_agent_id},
            )
        if selected.root.casefold() != expected_managed_root.casefold():
            raise BootstrapCliError(
                "selection-root-mismatch",
                "selected managed root does not match the discovered agent root",
                exit_code=BootstrapExitCode.CONFIG,
                details={
                    "repo_agent_id": selected.repo_agent_id,
                    "expected": expected_managed_root,
                    "observed": selected.root,
                },
            )


def _handle_error(exc: Exception) -> None:
    if isinstance(exc, BootstrapCliError):
        emit_json({"status": "error", "error": {"code": exc.code, "message": exc.message, "details": exc.details}})
        raise typer.Exit(code=int(exc.exit_code))
    if isinstance(exc, BootstrapApplyError):
        emit_json({"status": "error", "error": {"code": "bootstrap-apply-error", "message": str(exc)}})
        raise typer.Exit(code=int(BootstrapExitCode.APPLY))
    if isinstance(exc, (BootstrapConfigError, BootstrapPlanError, ValidationError)):
        emit_json({"status": "error", "error": {"code": "bootstrap-config-error", "message": str(exc)}})
        raise typer.Exit(code=int(BootstrapExitCode.CONFIG))
    if isinstance(exc, BootstrapProviderError):
        emit_json({"status": "error", "error": {"code": "bootstrap-provider-error", "message": str(exc)}})
        raise typer.Exit(code=int(BootstrapExitCode.RUNTIME))
    if isinstance(exc, BootstrapContractError):
        emit_json({"status": "error", "error": {"code": "bootstrap-contract-error", "message": str(exc)}})
        raise typer.Exit(code=int(BootstrapExitCode.RUNTIME))
    raise exc


def _require_exact_runtime(recorded: str, provided: str) -> None:
    if recorded != provided:
        raise BootstrapCliError("runtime-sha-mismatch", "resume/apply requires exact recorded runtime SHA", exit_code=BootstrapExitCode.STALE, details={"recorded_runtime_commit": recorded, "provided_runtime_commit": provided})


def _persisted_sidecar(repo_root: Path, sidecar_path: str) -> dict[str, object] | None:
    target = repo_root / sidecar_path
    try:
        data = target.read_bytes()
    except (FileNotFoundError, NotADirectoryError):
        return None
    document = yaml.safe_load(data.decode("utf-8"))
    lineage = document.get("evaluation_lineage") if isinstance(document, dict) else None
    bundle = document.get("default_evaluator_bundle") if isinstance(document, dict) else None
    objective = bundle.get("objective") if isinstance(bundle, dict) else None
    return {
        "path": sidecar_path,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bundle_objective_hash": objective.get("objective_hash") if isinstance(objective, dict) else None,
        "evaluation_lineage": lineage,
    }


def register_bootstrap_commands(app: typer.Typer) -> None:
    evaluation_app = typer.Typer(no_args_is_help=True)
    app.add_typer(evaluation_app, name="evaluation")

    @app.command("verify")
    def verify(
        checkout: Path = typer.Option(..., "--checkout"),
        receipt: Path = typer.Option(..., "--receipt"),
        pin_path: Path | None = typer.Option(None, "--pin"),
        registry_path: Path | None = typer.Option(None, "--registry"),
        uv_lock_sha256: str | None = typer.Option(None, "--uv-lock-sha256"),
        package_path: str = typer.Option(_DEFAULT_PACKAGE_PATH, "--package-path"),
        skill_path: str = typer.Option(_DEFAULT_SKILL_PATH, "--skill-path"),
    ) -> None:
        try:
            if (pin_path is None) == (registry_path is None):
                raise BootstrapCliError(
                    "pin-source-required",
                    "verify requires exactly one of --pin or --registry",
                    exit_code=BootstrapExitCode.CONFIG,
                )
            if pin_path is not None:
                # Legacy `.github/foundry-opt.lock.yml` shared pins remain readable for
                # migration; v1 repositories verify against the committed registry instead.
                pin = load_shared_pin(pin_path)
            else:
                pin = _pin_from_registry(
                    registry_path,
                    uv_lock_sha256=uv_lock_sha256,
                    package_path=package_path,
                    skill_path=skill_path,
                )
            verified = verify_shared_checkout(pin, checkout)
            write_bootstrap_receipt(receipt, verified)
            emit_json({"commit": verified.commit, "receipt": str(receipt.resolve()), "receipt_sha256": verified.receipt_sha256, "repository": verified.repository, "status": "verified"})
        except Exception as exc:
            _handle_error(exc)

    @app.command("discover")
    def discover(
        repo_root: Path = typer.Option(..., "--repo-root"),
        repository_id: str = typer.Option(..., "--repository-id"),
        operation_id: str = typer.Option(None, "--operation-id"),
        state_root: Path = typer.Option(default_state_root(), "--state-root"),
        runtime_commit: str | None = typer.Option(None, "--runtime-commit"),
        runtime_repository: str | None = typer.Option(None, "--runtime-repository"),
        plan_input: Path | None = typer.Option(None, "--plan-input"),
        binding_evidence: Path | None = typer.Option(None, "--binding-evidence"),
    ) -> None:
        try:
            op_id = operation_id or f"bootstrap-{uuid.uuid4().hex[:12]}"
            loaded = _context_plan_input(plan_input)
            resolved_commit = runtime_commit or (
                loaded.runtime_provenance.runtime_commit
                if loaded is not None
                else _runtime_commit()
            )
            resolved_repository = runtime_repository or (
                loaded.runtime_provenance.runtime_repository_url
                if loaded is not None
                else _runtime_repository()
            )
            selected_agents = None
            if loaded is not None:
                repository_id = loaded.repository.repository_id
                resolved_repository = loaded.runtime_provenance.runtime_repository_url
                resolved_commit = loaded.runtime_provenance.runtime_commit
                selected_agents = tuple(
                    {
                        "root": agent.discovery_selection_root,
                        "repoAgentId": agent.repo_agent_id,
                    }
                    for agent in loaded.repository.selected_agents
                )
            evidence = _resolve_binding_evidence(binding_evidence, plan_input=loaded, repository_id=repository_id)
            orch = _build_orchestrator(
                repo_root=repo_root,
                plan_input=loaded,
                state_root=state_root,
            )
            envelope = orch.discover(
                repo_root,
                repository_id=repository_id,
                operation_id=op_id,
                runtime_repository=resolved_repository,
                runtime_commit=resolved_commit,
                selected_agents=selected_agents,
                binding_evidence_by_root=evidence.by_root() if evidence is not None else None,
            )
            emit_json({"status": "ok", "command": "discover", "operation_id": op_id, "repo_root": str(repo_root.resolve()), "state_root": str(state_root.resolve()), "runtime_commit": resolved_commit, "runtime_repository": resolved_repository, "selected": list(envelope.selection_plan.selected_agent_ids), "binding_evidence_hash": evidence.evidence_hash if evidence is not None else None, "binding_evidence_roots": [item.root for item in evidence.agents] if evidence is not None else [], "agents": [item.to_discovery_payload() for item in envelope.selection_plan.discovered_agents], "candidates": [item.model_dump(mode="json") for item in envelope.selection_plan.binding_assessments]})
        except Exception as exc:
            _handle_error(exc)

    @app.command("binding-evidence")
    def binding_evidence_command(
        repo_root: Path = typer.Option(..., "--repo-root"),
        plan_input: Path = typer.Option(..., "--plan-input"),
        output: Path = typer.Option(..., "--output"),
    ) -> None:
        """Observe deployed immutable agent versions and write a reviewable evidence file."""

        try:
            loaded = load_bootstrap_plan_input(plan_input)
            if loaded.evaluations_phase is None:
                raise BootstrapCliError("binding-evidence-config", "binding evidence observation requires evaluations phase inputs", exit_code=BootstrapExitCode.CONFIG)
            selected = {agent.repo_agent_id.casefold(): agent for agent in loaded.repository.selected_agents}
            discovered = discover_repository_agents(
                repo_root,
                selected_agents=tuple(
                    {
                        "root": agent.discovery_selection_root,
                        "repoAgentId": agent.repo_agent_id,
                    }
                    for agent in loaded.repository.selected_agents
                ),
            )
            discovered_by_id = {agent.repoAgentId.casefold(): agent for agent in discovered.agents}
            driver = EvaluationPhaseDriver(plan_input=loaded, repository_root=repo_root)
            observed_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            records = []
            for agent in loaded.evaluations_phase.agents:
                key = agent.repo_agent_id.casefold()
                candidate = discovered_by_id.get(key)
                if candidate is None or key not in selected:
                    raise BootstrapCliError("binding-evidence-missing-agent", "evaluation agent was not discovered in the repository", exit_code=BootstrapExitCode.CONFIG, details={"repo_agent_id": agent.repo_agent_id})
                observation = driver.observe_agent_binding(
                    repo_agent_id=agent.repo_agent_id,
                    agent_name=agent.agent_name,
                    agent_version=agent.agent_version,
                    source_root=candidate.sourceRoot,
                    package_root=candidate.packageRoot,
                )
                records.append(
                    {
                        "schema_version": 1,
                        "root": candidate.root,
                        "repo_agent_id": agent.repo_agent_id,
                        "project_endpoint": agent.project_endpoint,
                        "agent_name": agent.agent_name,
                        "agent_version": agent.agent_version,
                        "source_fingerprint": observation["source_fingerprint"],
                        "package_fingerprint": observation["package_fingerprint"],
                        "evidence_provenance": "foundry_agent_code_download",
                        "code_content_hash": observation["code_content_hash"],
                        "code_content_hash_verified": bool(observation["code_content_hash_verified"]),
                        "observed_at": observed_at,
                    }
                )
            document = BindingEvidenceInput.model_validate(
                {
                    "schema_version": 1,
                    "evidence_version": 1,
                    "repository_id": loaded.repository.repository_id,
                    "agents": records,
                }
            )
            payload = document.model_dump(mode="json", exclude_none=True)
            write_json_file(output, payload)
            emit_json({"status": "ok", "command": "binding-evidence", "output": str(output.resolve()), "evidence_hash": document.evidence_hash, "agents": [{"repo_agent_id": item.repo_agent_id, "root": item.root, "source_fingerprint": item.source_fingerprint, "package_fingerprint": item.package_fingerprint, "code_content_hash_verified": item.code_content_hash_verified} for item in document.agents]})
        except Exception as exc:
            _handle_error(exc)

    @app.command("plan")
    def plan(
        plan_input: Path = typer.Option(..., "--plan-input"),
        selection_file: Path | None = typer.Option(None, "--selection-file"),
        repository_id: str = typer.Option(..., "--repository-id"),
        repo_root: Path = typer.Option(..., "--repo-root"),
        operation_id: str = typer.Option(..., "--operation-id"),
        state_root: Path = typer.Option(default_state_root(), "--state-root"),
        runtime_commit: str | None = typer.Option(None, "--runtime-commit"),
    ) -> None:
        try:
            loaded = load_bootstrap_plan_input(plan_input)
            if loaded.repository.repository_id != repository_id:
                raise BootstrapCliError(
                    "repository-mismatch",
                    "plan input repository does not match --repository-id",
                    exit_code=BootstrapExitCode.CONFIG,
                )
            _verify_binding_claims(loaded, repo_root=repo_root)
            selected = tuple(
                agent.repo_agent_id for agent in loaded.repository.selected_agents
            )
            if selection_file is not None:
                selection_payload = load_json_file(
                    selection_file,
                    subject="selection",
                )
                selected_from_file = tuple(
                    str(item["repoAgentId"])
                    for item in selection_payload.get("selectedAgents", ())
                )
                if tuple(item.casefold() for item in selected_from_file) != tuple(
                    item.casefold() for item in selected
                ):
                    raise BootstrapCliError(
                        "selection-mismatch",
                        "selection file must match plan input selected agents",
                        exit_code=BootstrapExitCode.CONFIG,
                    )
            discovery = read_operation_state(repository_id, operation_id, state_root=state_root)
            _verify_selected_discovery(loaded, discovery.selection_plan)
            orch = _build_orchestrator(
                repo_root=repo_root,
                plan_input=loaded,
                state_root=state_root,
            )
            resolved_commit = runtime_commit or (
                loaded.runtime_provenance.runtime_commit
                if loaded is not None
                else _runtime_commit()
            )
            _require_exact_runtime(discovery.runtime_commit, resolved_commit)
            selection = SelectionPlan.model_validate({**discovery.selection_plan.model_dump(mode="json"), "selected_agent_ids": selected})
            _require_exact_runtime(
                loaded.runtime_provenance.runtime_commit,
                resolved_commit,
            )
            original_build_context = orch._build_context
            orch._build_context = lambda repository_id, operation_id, runtime_repository, runtime_commit, selection_plan, evaluation_requests, evaluator_replacement: dict(original_build_context(repository_id, operation_id, runtime_repository, runtime_commit, selection_plan, evaluation_requests, evaluator_replacement)) | {"plan_input": loaded}
            envelope = orch.build_plan(repository_id=repository_id, operation_id=operation_id, runtime_repository=loaded.runtime_provenance.runtime_repository_url, runtime_commit=resolved_commit, selection_plan=selection, phases=loaded.required_phases)
            plan_path = state_root / "plans" / f"{operation_id}.json"
            write_json_file(plan_path, envelope.bootstrap_plan.model_dump(mode="json"))
            emit_json({"status": "ok", "command": "plan", "operation_id": operation_id, "plan_file": str(plan_path.resolve()), "plan_hash": envelope.bootstrap_plan.plan_hash, "runtime_commit": resolved_commit, "runtime_repository": envelope.runtime_repository, "required_phases": list(envelope.required_phases), "action_summary": [{"phase": action.phase, "action_id": action.action_id, "kind": action.kind} for action in envelope.bootstrap_plan.actions]})
        except Exception as exc:
            _handle_error(exc)

    @app.command("status")
    def status(
        repository_id: str = typer.Option(..., "--repository-id"),
        operation_id: str = typer.Option(..., "--operation-id"),
        repo_root: Path = typer.Option(Path("."), "--repo-root"),
        plan_input: Path | None = typer.Option(None, "--plan-input"),
        state_root: Path = typer.Option(default_state_root(), "--state-root"),
        runtime_commit: str | None = typer.Option(None, "--runtime-commit"),
    ) -> None:
        try:
            loaded = _context_plan_input(plan_input)
            resolved_commit = runtime_commit or (
                loaded.runtime_provenance.runtime_commit
                if loaded is not None
                else _runtime_commit()
            )
            orchestrator = _build_orchestrator(
                repo_root=repo_root,
                plan_input=loaded,
                state_root=state_root,
            )
            payload = orchestrator.status(repository_id=repository_id, operation_id=operation_id, runtime_commit=resolved_commit)
            if loaded is not None:
                payload["live"] = read_live_status(
                    loaded,
                    orchestrator._drivers,
                )
            emit_json({"status": "ok", "command": "status", **payload})
        except Exception as exc:
            _handle_error(exc)

    @app.command("diff")
    def diff(
        current_plan: Path = typer.Option(..., "--current-plan"),
        candidate_plan: Path = typer.Option(..., "--candidate-plan"),
    ) -> None:
        try:
            current = load_json_file(current_plan, subject="current-plan")
            candidate = load_json_file(candidate_plan, subject="candidate-plan")
            from foundry_opt.bootstrap.contracts import BootstrapPlan
            orch = _build_orchestrator(repo_root=Path.cwd())
            lines = orch.diff(current_plan=BootstrapPlan.model_validate(current), candidate_plan=BootstrapPlan.model_validate(candidate))
            emit_json({"status": "ok", "command": "diff", "diff": list(lines)})
        except Exception as exc:
            _handle_error(exc)

    @app.command("apply")
    def apply(
        repository_id: str = typer.Option(..., "--repository-id"),
        operation_id: str = typer.Option(..., "--operation-id"),
        phase: ApplyPhaseName = typer.Option(..., "--phase"),
        approval_file: Path = typer.Option(..., "--approval-file"),
        plan_input: Path = typer.Option(..., "--plan-input"),
        repo_root: Path = typer.Option(Path("."), "--repo-root"),
        state_root: Path = typer.Option(default_state_root(), "--state-root"),
        runtime_commit: str | None = typer.Option(None, "--runtime-commit"),
    ) -> None:
        try:
            loaded = load_bootstrap_plan_input(plan_input)
            payload = load_json_file(approval_file, subject="approval")
            if _APPROVAL_HASH_FIELD not in payload:
                raise BootstrapCliError("approval-hash-required", "approval record hash is required", exit_code=BootstrapExitCode.CONFIG)
            _verify_binding_claims(loaded, repo_root=repo_root)
            approval = ApprovalRecord.model_validate(payload)
            current = read_operation_state(repository_id, operation_id, state_root=state_root)
            resolved_commit = runtime_commit or loaded.runtime_provenance.runtime_commit
            _require_exact_runtime(current.runtime_commit, resolved_commit)
            if approval.parent_plan_hash != current.bootstrap_plan.plan_hash:
                raise BootstrapCliError("stale-approval", "approval file does not match active plan hash", exit_code=BootstrapExitCode.STALE, details={"active_plan_hash": current.bootstrap_plan.plan_hash, "approval_plan_hash": approval.parent_plan_hash})
            receipt = _build_orchestrator(
                repo_root=repo_root,
                plan_input=loaded,
                state_root=state_root,
            ).apply_phase(repository_id=repository_id, operation_id=operation_id, phase=phase, approval=approval, runtime_commit=resolved_commit)
            status_value = "ok" if receipt.state == "applied" else "error"
            emit_json({"status": status_value, "command": "apply", "phase": phase, "operation_id": operation_id, "runtime_commit": resolved_commit, "receipt": receipt.model_dump(mode="json")})
            if receipt.state != "applied":
                raise typer.Exit(code=int(BootstrapExitCode.APPLY))
        except Exception as exc:
            _handle_error(exc)

    @app.command("rollback")
    def rollback(
        repository_id: str = typer.Option(..., "--repository-id"),
        operation_id: str = typer.Option(..., "--operation-id"),
        phase: ApplyPhaseName = typer.Option(..., "--phase"),
        plan_input: Path = typer.Option(..., "--plan-input"),
        repo_root: Path = typer.Option(Path("."), "--repo-root"),
        state_root: Path = typer.Option(default_state_root(), "--state-root"),
        runtime_commit: str | None = typer.Option(None, "--runtime-commit"),
    ) -> None:
        try:
            loaded = load_bootstrap_plan_input(plan_input)
            current = read_operation_state(repository_id, operation_id, state_root=state_root)
            resolved_commit = runtime_commit or loaded.runtime_provenance.runtime_commit
            _require_exact_runtime(current.runtime_commit, resolved_commit)
            receipt = _build_orchestrator(
                repo_root=repo_root,
                plan_input=loaded,
                state_root=state_root,
            ).rollback_phase(repository_id=repository_id, operation_id=operation_id, phase=phase, runtime_commit=resolved_commit)
            emit_json({"status": "ok", "command": "rollback", "phase": phase, "receipt": receipt.model_dump(mode="json")})
        except Exception as exc:
            _handle_error(exc)

    @evaluation_app.command("inventory")
    def evaluation_inventory(
        plan_input: Path = typer.Option(..., "--plan-input"),
        repo_root: Path = typer.Option(Path("."), "--repo-root"),
        probe_file: Path | None = typer.Option(None, "--probe-file"),
        live: bool = typer.Option(False, "--live/--no-live"),
    ) -> None:
        try:
            loaded = load_bootstrap_plan_input(plan_input)
            probes = load_json_file(probe_file, subject="probe") if probe_file is not None else {}
            agents = loaded.evaluations_phase.agents if loaded.evaluations_phase is not None else ()
            assessments = []
            for agent in agents:
                probe = probes.get(agent.repo_agent_id) if isinstance(probes.get(agent.repo_agent_id), dict) else {}
                contract = agent.onboarding_contract
                binding = contract.binding_classification if contract is not None else str(probe.get("binding_classification", "bound-unknown"))
                assessment = assess_agent_inventory(
                    repo_agent_id=agent.repo_agent_id,
                    binding_classification=binding,
                    agent_name=agent.agent_name,
                    agent_version=agent.agent_version,
                    model_deployment=agent.model_deployment,
                    generation_mode=agent.generation_mode,
                    source_paths=[source.path for source in agent.generation_sources],
                    trace_window=agent.trace_window,
                    target_sample_count=agent.target_sample_count,
                    expected_schema=str(probe.get("expected_schema", "agent-evaluation/v1")),
                    dataset_candidates=tuple(probe.get("dataset_candidates", ())),
                    evaluator_candidates=tuple(probe.get("evaluator_candidates", ())),
                    definition_candidates=tuple(probe.get("definition_candidates", ())),
                    trace_prerequisites_available=bool(probe.get("prerequisites_available", False)),
                    useful_trace_samples=int(probe.get("useful_sample_count", 0)),
                )
                assessments.append(
                    {
                        **assessment.model_dump(mode="json"),
                        "approved_reuse_candidates": (
                            contract.dataset_plan.reuse_candidates is not None
                            if contract is not None and contract.dataset_plan is not None
                            else None
                        ),
                        "approved_generation_kind": (
                            contract.dataset_plan.generation_kind
                            if contract is not None and contract.dataset_plan is not None
                            else None
                        ),
                    }
                )
            payload = {
                "status": "ok",
                "command": "evaluation inventory",
                "repo_root": str(repo_root.resolve()),
                "runtime_commit": loaded.runtime_provenance.runtime_commit,
                "assessments": assessments,
            }
            if live:
                # One inventory per Foundry project: agents may live in different projects.
                payload["inventory_by_project"] = EvaluationPhaseDriver(plan_input=loaded, repository_root=repo_root).inventory_by_project()
                payload["inventory"] = next(iter(payload["inventory_by_project"].values()), {})
            emit_json(payload)
        except Exception as exc:
            _handle_error(exc)

    @evaluation_app.command("plan")
    def evaluation_plan(
        plan_input: Path = typer.Option(..., "--plan-input"),
        repo_root: Path = typer.Option(Path("."), "--repo-root"),
    ) -> None:
        try:
            loaded = load_bootstrap_plan_input(plan_input)
            verified_bindings = _verify_binding_claims(loaded, repo_root=repo_root)
            actions = [
                action.model_dump(mode="json")
                for action in build_evaluation_actions(loaded)
            ]
            agents = loaded.evaluations_phase.agents if loaded.evaluations_phase is not None else ()
            emit_json(
                {
                    "status": "ok",
                    "command": "evaluation plan",
                    "repo_root": str(repo_root.resolve()),
                    "runtime_commit": loaded.runtime_provenance.runtime_commit,
                    "plan_input_hash": loaded.plan_input_hash,
                    "verified_binding_classifications": verified_bindings,
                    "actions": actions,
                    "stopped_agents": [dict(item) for item in stopped_evaluation_agents(loaded)],
                    "execution_contracts": [
                        {
                            "repo_agent_id": agent.repo_agent_id,
                            "contract_version": agent.onboarding_contract.contract_version,
                            "contract_hash": agent.onboarding_contract.contract_hash,
                            "generation_kind": (
                                agent.onboarding_contract.dataset_plan.generation_kind
                                if agent.onboarding_contract.dataset_plan is not None
                                else None
                            ),
                            "reuse_candidates_approved": (
                                agent.onboarding_contract.dataset_plan.reuse_candidates is not None
                                if agent.onboarding_contract.dataset_plan is not None
                                else None
                            ),
                        }
                        for agent in agents
                        if agent.onboarding_contract is not None
                    ],
                    "next_action": "approve the evaluations phase once, then run bootstrap evaluation apply",
                }
            )
        except Exception as exc:
            _handle_error(exc)

    @evaluation_app.command("apply")
    def evaluation_apply(
        repository_id: str = typer.Option(..., "--repository-id"),
        operation_id: str = typer.Option(..., "--operation-id"),
        approval_file: Path = typer.Option(..., "--approval-file"),
        plan_input: Path = typer.Option(..., "--plan-input"),
        repo_root: Path = typer.Option(Path("."), "--repo-root"),
        state_root: Path = typer.Option(default_state_root(), "--state-root"),
    ) -> None:
        try:
            loaded = load_bootstrap_plan_input(plan_input)
            approval_payload = load_json_file(approval_file, subject="approval")
            if _APPROVAL_HASH_FIELD not in approval_payload:
                raise BootstrapCliError(
                    "approval-hash-required",
                    "approval record hash is required",
                    exit_code=BootstrapExitCode.CONFIG,
                )
            _verify_binding_claims(loaded, repo_root=repo_root)
            approval = ApprovalRecord.model_validate(approval_payload)
            receipt = _build_orchestrator(
                repo_root=repo_root,
                plan_input=loaded,
                state_root=state_root,
            ).apply_phase(
                repository_id=repository_id,
                operation_id=operation_id,
                phase="evaluations",
                approval=approval,
                runtime_commit=loaded.runtime_provenance.runtime_commit,
            )
            emit_json(
                {
                    "status": "ok" if receipt.state == "applied" else "error",
                    "command": "evaluation apply",
                    "repo_root": str(repo_root.resolve()),
                    "runtime_commit": loaded.runtime_provenance.runtime_commit,
                    "receipt": receipt.model_dump(mode="json"),
                    "sidecar_written": False,
                    "next_action": (
                        "run bootstrap evaluation activate to atomically write the sidecar"
                        if receipt.state == "applied"
                        else "resolve the failed evaluation activation before any repository sidecar mutation"
                    ),
                }
            )
            if receipt.state != "applied":
                raise typer.Exit(code=int(BootstrapExitCode.APPLY))
        except Exception as exc:
            _handle_error(exc)

    @evaluation_app.command("activate")
    def evaluation_activate(
        repository_id: str = typer.Option(..., "--repository-id"),
        operation_id: str = typer.Option(..., "--operation-id"),
        plan_input: Path = typer.Option(..., "--plan-input"),
        repo_root: Path = typer.Option(Path("."), "--repo-root"),
        state_root: Path = typer.Option(default_state_root(), "--state-root"),
        runtime_commit: str | None = typer.Option(None, "--runtime-commit"),
    ) -> None:
        try:
            loaded = load_bootstrap_plan_input(plan_input)
            _verify_binding_claims(loaded, repo_root=repo_root)
            current = read_operation_state(repository_id, operation_id, state_root=state_root)
            resolved_commit = runtime_commit or loaded.runtime_provenance.runtime_commit
            _require_exact_runtime(current.runtime_commit, resolved_commit)
            receipt = finalize_evaluation_activation(
                repository_root=repo_root,
                plan_input=loaded,
                envelope=current,
                runtime_commit=resolved_commit,
                state_root=state_root,
            )
            emit_json(
                {
                    "status": "ok",
                    "command": "evaluation activate",
                    "repo_root": str(repo_root.resolve()),
                    "operation_id": operation_id,
                    "runtime_commit": resolved_commit,
                    "receipt": receipt.model_dump(mode="json"),
                    "sidecar_written": True,
                    "next_action": "commit the reviewed registry, sidecar, and managed lock changes",
                }
            )
        except Exception as exc:
            _handle_error(exc)

    @evaluation_app.command("status")
    def evaluation_status(
        repository_id: str = typer.Option(..., "--repository-id"),
        operation_id: str = typer.Option(..., "--operation-id"),
        repo_root: Path = typer.Option(Path("."), "--repo-root"),
        state_root: Path = typer.Option(default_state_root(), "--state-root"),
        runtime_commit: str | None = typer.Option(None, "--runtime-commit"),
    ) -> None:
        try:
            current = read_operation_state(repository_id, operation_id, state_root=state_root)
            resolved_commit = runtime_commit or current.runtime_commit
            _require_exact_runtime(current.runtime_commit, resolved_commit)
            phase_receipt = next((item for item in current.phase_receipts if item.phase == "evaluations"), None)
            journal = read_finalize_journal(repository_id, operation_id, state_root=state_root)
            phase_state = phase_receipt.state if phase_receipt is not None else "pending"
            sidecar_state = str(journal.get("state")) if journal is not None else "not_started"
            if phase_state != "applied":
                next_action = "approve and run bootstrap evaluation apply"
            elif sidecar_state != "completed":
                next_action = "run bootstrap evaluation activate"
            else:
                next_action = "commit the reviewed registry, sidecar, and managed lock changes"
            emit_json(
                {
                    "status": "ok",
                    "command": "evaluation status",
                    "operation_id": operation_id,
                    "repo_root": str(repo_root.resolve()),
                    "runtime_commit": resolved_commit,
                    "plan_hash": current.bootstrap_plan.plan_hash,
                    "phase_state": phase_state,
                    "sidecar_activation_state": sidecar_state,
                    "activated": phase_state == "applied" and sidecar_state == "completed",
                    "replacement": current.evaluator_replacement.model_dump(mode="json") if current.evaluator_replacement else None,
                    "replacements": [item.model_dump(mode="json") for item in current.evaluator_replacements],
                    "next_action": next_action,
                }
            )
        except Exception as exc:
            _handle_error(exc)

    @evaluation_app.command("inspect")
    def evaluation_inspect(
        repository_id: str = typer.Option(..., "--repository-id"),
        operation_id: str = typer.Option(..., "--operation-id"),
        repo_root: Path = typer.Option(Path("."), "--repo-root"),
        plan_input: Path | None = typer.Option(None, "--plan-input"),
        state_root: Path = typer.Option(default_state_root(), "--state-root"),
        runtime_commit: str | None = typer.Option(None, "--runtime-commit"),
    ) -> None:
        try:
            state = read_operation_state(repository_id, operation_id, state_root=state_root)
            resolved_commit = runtime_commit or state.runtime_commit
            _require_exact_runtime(state.runtime_commit, resolved_commit)
            loaded = _context_plan_input(plan_input)
            phase_receipt = next((item for item in state.phase_receipts if item.phase == "evaluations"), None)
            finalizations = {}
            if phase_receipt is not None and isinstance(phase_receipt.provider_state, dict):
                onboarding = phase_receipt.provider_state.get("onboarding")
                if isinstance(onboarding, dict):
                    finalizations = {
                        str(key): value.get("finalization")
                        for key, value in onboarding.items()
                        if isinstance(value, dict)
                    }
            contracts = []
            for agent in (loaded.evaluations_phase.agents if loaded is not None and loaded.evaluations_phase is not None else ()):
                contract = agent.onboarding_contract
                if contract is None:
                    continue
                persisted = _persisted_sidecar(repo_root, agent.sidecar_path)
                finalization = finalizations.get(f"evaluations:{agent.repo_agent_id}:onboarding")
                contracts.append(
                    {
                        "repo_agent_id": agent.repo_agent_id,
                        "binding_classification": contract.binding_classification,
                        "stopped": contract.stopped,
                        "contract_hash": contract.contract_hash,
                        "bounds": contract.bounds.model_dump(mode="json"),
                        "finalization": finalization,
                        "persisted_sidecar": persisted,
                    }
                )
            emit_json(
                {
                    "status": "ok",
                    "command": "evaluation inspect",
                    "operation_id": operation_id,
                    "repo_root": str(repo_root.resolve()),
                    "runtime_commit": resolved_commit,
                    "bundle": state.evaluator_replacement.model_dump(mode="json") if state.evaluator_replacement else None,
                    "lineage": state.evaluator_replacement.lineage_hash if state.evaluator_replacement else None,
                    "bundles": [item.model_dump(mode="json") for item in state.evaluator_replacements],
                    "lineages": {item.repo_agent_id: item.lineage_hash for item in state.evaluator_replacements},
                    "contracts": contracts,
                    "human_rubric_editor": False,
                    "provenance": "repository default bundle; issue-scoped evaluators never replace it",
                }
            )
        except Exception as exc:
            _handle_error(exc)

    @evaluation_app.command("replace")
    def evaluation_replace(
        replacement_file: Path = typer.Option(..., "--replacement-file"),
        plan_input: Path = typer.Option(..., "--plan-input"),
        repo_root: Path = typer.Option(Path("."), "--repo-root"),
    ) -> None:
        try:
            loaded = load_bootstrap_plan_input(plan_input)
            replacement = EvaluationReplacementRecord.model_validate(load_json_file(replacement_file, subject="replacement"))
            agents = loaded.evaluations_phase.agents if loaded.evaluations_phase is not None else ()
            replacing = tuple(agent for agent in agents if agent.replacement_intent)
            if not replacing:
                raise BootstrapCliError(
                    "replacement-intent-required",
                    "explicit replacement requires replacement_intent and resolved replacement lineage",
                    exit_code=BootstrapExitCode.CONFIG,
                )
            retained = []
            for agent in replacing:
                contract = agent.onboarding_contract
                if contract is None or contract.replacement is None:
                    raise BootstrapCliError(
                        "replacement-lineage-required",
                        "explicit replacement requires the reviewed previous bundle lineage",
                        exit_code=BootstrapExitCode.CONFIG,
                    )
                current = _persisted_sidecar(repo_root, agent.sidecar_path)
                if current is None:
                    raise BootstrapCliError(
                        "replacement-target-missing",
                        "explicit replacement requires an existing active sidecar",
                        exit_code=BootstrapExitCode.MISSING,
                    )
                if current["sha256"] != contract.replacement.previous_sidecar_sha256:
                    raise BootstrapCliError(
                        "replacement-preimage-mismatch",
                        "active sidecar does not match the reviewed replacement preimage",
                        exit_code=BootstrapExitCode.CONFLICT,
                        details={"path": agent.sidecar_path},
                    )
                retained.append(
                    {
                        "repo_agent_id": agent.repo_agent_id,
                        "retained_bundle_objective_hash": contract.replacement.previous_bundle_objective_hash,
                        "retained_sidecar_sha256": contract.replacement.previous_sidecar_sha256,
                        "contract_hash": contract.contract_hash,
                    }
                )
            emit_json(
                {
                    "status": "ok",
                    "command": "evaluation replace",
                    "repo_root": str(repo_root.resolve()),
                    "runtime_commit": loaded.runtime_provenance.runtime_commit,
                    "replacement": replacement.model_dump(mode="json"),
                    "plan_input_hash": loaded.plan_input_hash,
                    "explicit_replace": True,
                    "human_rubric_editor": False,
                    "retained": retained,
                    "next_action": "run bootstrap plan, approve the evaluations phase, apply, then run bootstrap evaluation activate",
                }
            )
        except Exception as exc:
            _handle_error(exc)

__all__ = ["register_bootstrap_commands"]
