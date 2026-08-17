"""Typer command registration for bootstrap operations."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path

import typer
from pydantic import ValidationError

from foundry_opt.bootstrap.command_io import BootstrapCliError, BootstrapExitCode, emit_json, load_json_file, write_json_file
from foundry_opt.bootstrap.drivers import AzurePhaseDriver, EvaluationPhaseDriver, GitHubPhaseDriver, RepositoryPhaseDriver
from foundry_opt.bootstrap.errors import (
    BootstrapApplyError,
    BootstrapConfigError,
    BootstrapContractError,
    BootstrapPlanError,
    BootstrapProviderError,
)
from foundry_opt.bootstrap.operation_state import SelectionPlan, default_state_root, read_operation_state
from foundry_opt.bootstrap.orchestrator import BootstrapOrchestrator
from foundry_opt.bootstrap.plan_factory import build_phase_actions, read_live_status
from foundry_opt.bootstrap.receipts import ApprovalRecord, ApplyPhaseName, EvaluationReplacementRecord
from foundry_opt.bootstrap.input_contracts import BootstrapPlanInput, load_bootstrap_plan_input
from foundry_opt.distribution import load_shared_pin, verify_shared_checkout, write_bootstrap_receipt

_APPROVAL_HASH_FIELD = "approval_hash"


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
        evaluations_driver=EvaluationPhaseDriver(plan_input=plan_input),
        state_root=state_root,
    )


def _context_plan_input(plan_input_path: Path | None) -> BootstrapPlanInput | None:
    return load_bootstrap_plan_input(plan_input_path) if plan_input_path is not None else None


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


def register_bootstrap_commands(app: typer.Typer) -> None:
    evaluation_app = typer.Typer(no_args_is_help=True)
    app.add_typer(evaluation_app, name="evaluation")

    @app.command("verify")
    def verify(
        pin_path: Path = typer.Option(..., "--pin"),
        checkout: Path = typer.Option(..., "--checkout"),
        receipt: Path = typer.Option(..., "--receipt"),
    ) -> None:
        pin = load_shared_pin(pin_path)
        verified = verify_shared_checkout(pin, checkout)
        write_bootstrap_receipt(receipt, verified)
        emit_json({"commit": verified.commit, "receipt": str(receipt.resolve()), "receipt_sha256": verified.receipt_sha256, "repository": verified.repository, "status": "verified"})

    @app.command("discover")
    def discover(
        repo_root: Path = typer.Option(..., "--repo-root"),
        repository_id: str = typer.Option(..., "--repository-id"),
        operation_id: str = typer.Option(None, "--operation-id"),
        state_root: Path = typer.Option(default_state_root(), "--state-root"),
        runtime_commit: str | None = typer.Option(None, "--runtime-commit"),
        runtime_repository: str | None = typer.Option(None, "--runtime-repository"),
        plan_input: Path | None = typer.Option(None, "--plan-input"),
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
                        "root": agent.root,
                        "repoAgentId": agent.repo_agent_id,
                    }
                    for agent in loaded.repository.selected_agents
                )
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
            )
            emit_json({"status": "ok", "command": "discover", "operation_id": op_id, "repo_root": str(repo_root.resolve()), "state_root": str(state_root.resolve()), "runtime_commit": resolved_commit, "runtime_repository": resolved_repository, "selected": list(envelope.selection_plan.selected_agent_ids), "candidates": [item.model_dump(mode="json") for item in envelope.selection_plan.binding_assessments]})
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
            orch = _build_orchestrator(
                repo_root=repo_root,
                plan_input=loaded,
                state_root=state_root,
            )
            discovery = read_operation_state(repository_id, operation_id, state_root=state_root)
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
    ) -> None:
        try:
            loaded = load_bootstrap_plan_input(plan_input)
            inventory = EvaluationPhaseDriver(plan_input=loaded).inventory()
            emit_json(
                {
                    "status": "ok",
                    "command": "evaluation inventory",
                    "repo_root": str(repo_root.resolve()),
                    "runtime_commit": loaded.runtime_provenance.runtime_commit,
                    "inventory": inventory,
                }
            )
        except Exception as exc:
            _handle_error(exc)

    @evaluation_app.command("plan")
    def evaluation_plan(
        plan_input: Path = typer.Option(..., "--plan-input"),
        repo_root: Path = typer.Option(Path("."), "--repo-root"),
    ) -> None:
        try:
            loaded = load_bootstrap_plan_input(plan_input)
            actions = [
                action.model_dump(mode="json")
                for action in build_phase_actions(loaded)
                if action.phase == "evaluations"
            ]
            emit_json(
                {
                    "status": "ok",
                    "command": "evaluation plan",
                    "repo_root": str(repo_root.resolve()),
                    "runtime_commit": loaded.runtime_provenance.runtime_commit,
                    "plan_input_hash": loaded.plan_input_hash,
                    "actions": actions,
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
                }
            )
            if receipt.state != "applied":
                raise typer.Exit(code=int(BootstrapExitCode.APPLY))
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
            emit_json({"status": "ok", "command": "evaluation status", "operation_id": operation_id, "repo_root": str(repo_root.resolve()), "runtime_commit": resolved_commit, "replacement": current.evaluator_replacement.model_dump(mode="json") if current.evaluator_replacement else None})
        except Exception as exc:
            _handle_error(exc)

    @evaluation_app.command("inspect")
    def evaluation_inspect(
        repository_id: str = typer.Option(..., "--repository-id"),
        operation_id: str = typer.Option(..., "--operation-id"),
        repo_root: Path = typer.Option(Path("."), "--repo-root"),
        state_root: Path = typer.Option(default_state_root(), "--state-root"),
        runtime_commit: str | None = typer.Option(None, "--runtime-commit"),
    ) -> None:
        try:
            state = read_operation_state(repository_id, operation_id, state_root=state_root)
            resolved_commit = runtime_commit or state.runtime_commit
            _require_exact_runtime(state.runtime_commit, resolved_commit)
            emit_json({"status": "ok", "command": "evaluation inspect", "operation_id": operation_id, "repo_root": str(repo_root.resolve()), "runtime_commit": resolved_commit, "bundle": state.evaluator_replacement.model_dump(mode="json") if state.evaluator_replacement else None, "lineage": state.evaluator_replacement.lineage_hash if state.evaluator_replacement else None, "provenance": "default/issue/pinned evaluators"})
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
            emit_json({"status": "ok", "command": "evaluation replace", "repo_root": str(repo_root.resolve()), "runtime_commit": loaded.runtime_provenance.runtime_commit, "replacement": replacement.model_dump(mode="json"), "plan_input_hash": loaded.plan_input_hash, "explicit_replace": True, "human_rubric_editor": False, "next_action": "run bootstrap plan and approve the evaluations phase"})
        except Exception as exc:
            _handle_error(exc)

__all__ = ["register_bootstrap_commands"]
