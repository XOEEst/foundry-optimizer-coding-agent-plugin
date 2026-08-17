"""Typer command registration for bootstrap operations."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

import typer

from foundry_opt.bootstrap.command_io import BootstrapCliError, BootstrapExitCode, emit_json, load_json_file, write_json_file
from foundry_opt.bootstrap.drivers import AzurePhaseDriver, EvaluationPhaseDriver, GitHubPhaseDriver, RepositoryPhaseDriver
from foundry_opt.bootstrap.errors import BootstrapApplyError
from foundry_opt.bootstrap.operation_state import SelectionPlan, default_state_root, read_operation_state
from foundry_opt.bootstrap.orchestrator import BootstrapOrchestrator
from foundry_opt.bootstrap.receipts import ApprovalRecord, ApplyPhaseName, EvaluationReplacementRecord
from foundry_opt.distribution import load_shared_pin, verify_shared_checkout, write_bootstrap_receipt

_APPROVAL_HASH_FIELD = "approval_hash"


def _runtime_commit() -> str:
    return hashlib.sha1(Path(__file__).read_bytes()).hexdigest()


def _build_orchestrator(*, repo_root: Path, state_root: Path | None = None) -> BootstrapOrchestrator:
    return BootstrapOrchestrator(
        repository_driver=RepositoryPhaseDriver(repository_root=repo_root, payloads=()),
        github_driver=GitHubPhaseDriver(),
        azure_driver=AzurePhaseDriver(),
        evaluations_driver=EvaluationPhaseDriver(),
        state_root=state_root,
    )


def _handle_error(exc: Exception) -> None:
    if isinstance(exc, BootstrapCliError):
        emit_json({"status": "error", "error": {"code": exc.code, "message": exc.message, "details": exc.details}})
        raise typer.Exit(code=int(exc.exit_code))
    if isinstance(exc, BootstrapApplyError):
        emit_json({"status": "error", "error": {"code": "bootstrap-apply-error", "message": str(exc)}})
        raise typer.Exit(code=int(BootstrapExitCode.APPLY))
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
        runtime_commit: str = typer.Option(_runtime_commit(), "--runtime-commit"),
    ) -> None:
        try:
            op_id = operation_id or f"bootstrap-{uuid.uuid4().hex[:12]}"
            orch = _build_orchestrator(repo_root=repo_root, state_root=state_root)
            envelope = orch.discover(repo_root, repository_id=repository_id, operation_id=op_id, runtime_repository=f"https://github.com/{repository_id}.git", runtime_commit=runtime_commit)
            emit_json({"status": "ok", "command": "discover", "operation_id": op_id, "repo_root": str(repo_root.resolve()), "state_root": str(state_root.resolve()), "runtime_commit": runtime_commit, "selected": list(envelope.selection_plan.selected_agent_ids), "candidates": [item.model_dump(mode="json") for item in envelope.selection_plan.binding_assessments]})
        except Exception as exc:
            _handle_error(exc)

    @app.command("plan")
    def plan(
        selection_file: Path = typer.Option(..., "--selection-file"),
        repository_id: str = typer.Option(..., "--repository-id"),
        repo_root: Path = typer.Option(..., "--repo-root"),
        operation_id: str = typer.Option(..., "--operation-id"),
        state_root: Path = typer.Option(default_state_root(), "--state-root"),
        runtime_commit: str = typer.Option(_runtime_commit(), "--runtime-commit"),
    ) -> None:
        try:
            selection_payload = load_json_file(selection_file, subject="selection")
            selected = tuple(str(item["repoAgentId"]) for item in selection_payload.get("selectedAgents", ()))
            if not selected:
                raise BootstrapCliError("selection-required", "plan requires at least one selected agent", exit_code=BootstrapExitCode.CONFIG)
            orch = _build_orchestrator(repo_root=repo_root, state_root=state_root)
            discovery = read_operation_state(repository_id, operation_id, state_root=state_root)
            _require_exact_runtime(discovery.runtime_commit, runtime_commit)
            selection = SelectionPlan.model_validate({**discovery.selection_plan.model_dump(mode="json"), "selected_agent_ids": selected})
            phases = ("repository",) if selection_payload.get("offline", True) else ("repository", "github", "azure", "evaluations")
            envelope = orch.build_plan(repository_id=repository_id, operation_id=operation_id, runtime_repository=f"https://github.com/{repository_id}.git", runtime_commit=runtime_commit, selection_plan=selection, evaluation_requests=selection_payload.get("desiredConfiguration", ()), phases=phases)
            plan_path = state_root / "plans" / f"{operation_id}.json"
            write_json_file(plan_path, envelope.bootstrap_plan.model_dump(mode="json"))
            emit_json({"status": "ok", "command": "plan", "operation_id": operation_id, "plan_file": str(plan_path.resolve()), "plan_hash": envelope.bootstrap_plan.plan_hash, "runtime_commit": runtime_commit, "required_phases": list(envelope.required_phases), "action_summary": [{"phase": action.phase, "action_id": action.action_id, "kind": action.kind} for action in envelope.bootstrap_plan.actions]})
        except Exception as exc:
            _handle_error(exc)

    @app.command("status")
    def status(
        repository_id: str = typer.Option(..., "--repository-id"),
        operation_id: str = typer.Option(..., "--operation-id"),
        state_root: Path = typer.Option(default_state_root(), "--state-root"),
        runtime_commit: str = typer.Option(_runtime_commit(), "--runtime-commit"),
    ) -> None:
        try:
            payload = _build_orchestrator(repo_root=Path.cwd(), state_root=state_root).status(repository_id=repository_id, operation_id=operation_id, runtime_commit=runtime_commit)
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
        state_root: Path = typer.Option(default_state_root(), "--state-root"),
        runtime_commit: str = typer.Option(_runtime_commit(), "--runtime-commit"),
    ) -> None:
        try:
            payload = load_json_file(approval_file, subject="approval")
            if _APPROVAL_HASH_FIELD not in payload:
                raise BootstrapCliError("approval-hash-required", "approval record hash is required", exit_code=BootstrapExitCode.CONFIG)
            approval = ApprovalRecord.model_validate(payload)
            current = read_operation_state(repository_id, operation_id, state_root=state_root)
            _require_exact_runtime(current.runtime_commit, runtime_commit)
            if approval.parent_plan_hash != current.bootstrap_plan.plan_hash:
                raise BootstrapCliError("stale-approval", "approval file does not match active plan hash", exit_code=BootstrapExitCode.STALE, details={"active_plan_hash": current.bootstrap_plan.plan_hash, "approval_plan_hash": approval.parent_plan_hash})
            receipt = _build_orchestrator(repo_root=Path.cwd(), state_root=state_root).apply_phase(repository_id=repository_id, operation_id=operation_id, phase=phase, approval=approval, runtime_commit=runtime_commit)
            emit_json({"status": "ok", "command": "apply", "phase": phase, "operation_id": operation_id, "runtime_commit": runtime_commit, "receipt": receipt.model_dump(mode="json")})
        except Exception as exc:
            _handle_error(exc)

    @app.command("rollback")
    def rollback(
        repository_id: str = typer.Option(..., "--repository-id"),
        operation_id: str = typer.Option(..., "--operation-id"),
        phase: ApplyPhaseName = typer.Option(..., "--phase"),
        state_root: Path = typer.Option(default_state_root(), "--state-root"),
        runtime_commit: str = typer.Option(_runtime_commit(), "--runtime-commit"),
    ) -> None:
        try:
            current = read_operation_state(repository_id, operation_id, state_root=state_root)
            _require_exact_runtime(current.runtime_commit, runtime_commit)
            receipt = _build_orchestrator(repo_root=Path.cwd(), state_root=state_root).rollback_phase(repository_id=repository_id, operation_id=operation_id, phase=phase, runtime_commit=runtime_commit)
            emit_json({"status": "ok", "command": "rollback", "phase": phase, "receipt": receipt.model_dump(mode="json")})
        except Exception as exc:
            _handle_error(exc)

    @evaluation_app.command("inventory")
    def evaluation_inventory(
        repository_id: str = typer.Option(..., "--repository-id"),
        operation_id: str = typer.Option(..., "--operation-id"),
        state_root: Path = typer.Option(default_state_root(), "--state-root"),
        runtime_commit: str = typer.Option(_runtime_commit(), "--runtime-commit"),
    ) -> None:
        try:
            current = read_operation_state(repository_id, operation_id, state_root=state_root)
            _require_exact_runtime(current.runtime_commit, runtime_commit)
            emit_json({"status": "ok", "command": "evaluation inventory", "operation_id": operation_id, "runtime_commit": runtime_commit, "default_bundle": "auto_generated_unreviewed", "provenance": "issue/default/pinned evaluators", "lineage": current.evaluator_replacement.lineage_hash if current.evaluator_replacement else None})
        except Exception as exc:
            _handle_error(exc)

    @evaluation_app.command("plan")
    def evaluation_plan(replacement_file: Path = typer.Option(..., "--replacement-file")) -> None:
        try:
            replacement = EvaluationReplacementRecord.model_validate(load_json_file(replacement_file, subject="replacement"))
            emit_json({"status": "ok", "command": "evaluation plan", "replacement": replacement.model_dump(mode="json"), "summary": ["inspect default bundle", "explicit replace", "approval-gated apply"]})
        except Exception as exc:
            _handle_error(exc)

    @evaluation_app.command("apply")
    def evaluation_apply(replacement_file: Path = typer.Option(..., "--replacement-file")) -> None:
        try:
            replacement = EvaluationReplacementRecord.model_validate(load_json_file(replacement_file, subject="replacement"))
            emit_json({"status": "ok", "command": "evaluation apply", "replacement": replacement.model_dump(mode="json"), "result": "provider-backed through evaluations phase"})
        except Exception as exc:
            _handle_error(exc)

    @evaluation_app.command("status")
    def evaluation_status(
        repository_id: str = typer.Option(..., "--repository-id"),
        operation_id: str = typer.Option(..., "--operation-id"),
        state_root: Path = typer.Option(default_state_root(), "--state-root"),
    ) -> None:
        try:
            current = read_operation_state(repository_id, operation_id, state_root=state_root)
            emit_json({"status": "ok", "command": "evaluation status", "operation_id": operation_id, "replacement": current.evaluator_replacement.model_dump(mode="json") if current.evaluator_replacement else None})
        except Exception as exc:
            _handle_error(exc)

    @evaluation_app.command("inspect")
    def evaluation_inspect(
        repository_id: str = typer.Option(..., "--repository-id"),
        operation_id: str = typer.Option(..., "--operation-id"),
        state_root: Path = typer.Option(default_state_root(), "--state-root"),
    ) -> None:
        try:
            state = read_operation_state(repository_id, operation_id, state_root=state_root)
            emit_json({"status": "ok", "command": "evaluation inspect", "operation_id": operation_id, "bundle": state.evaluator_replacement.model_dump(mode="json") if state.evaluator_replacement else None, "lineage": state.evaluator_replacement.lineage_hash if state.evaluator_replacement else None, "provenance": "default/issue/pinned evaluators"})
        except Exception as exc:
            _handle_error(exc)

    @evaluation_app.command("replace")
    def evaluation_replace(replacement_file: Path = typer.Option(..., "--replacement-file")) -> None:
        try:
            replacement = EvaluationReplacementRecord.model_validate(load_json_file(replacement_file, subject="replacement"))
            emit_json({"status": "ok", "command": "evaluation replace", "replacement": replacement.model_dump(mode="json"), "explicit_replace": True, "human_rubric_editor": False})
        except Exception as exc:
            _handle_error(exc)


__all__ = ["register_bootstrap_commands"]
