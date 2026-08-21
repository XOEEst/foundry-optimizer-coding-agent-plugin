"""End-to-end mocked flow: plan -> approve once -> apply -> receipt-bound activation.

No live Azure, GitHub, or Foundry mutation happens anywhere in this module.
"""

from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

import pytest
import yaml

from foundry_opt.bootstrap.contracts import BootstrapSidecar, RootRegistry
from foundry_opt.bootstrap.drivers import AzurePhaseDriver, EvaluationPhaseDriver, GitHubPhaseDriver, RepositoryPhaseDriver
from foundry_opt.bootstrap.errors import BootstrapApplyError
from foundry_opt.bootstrap.evaluation.activation import finalize_evaluation_activation, read_finalize_journal
from foundry_opt.bootstrap.evaluation.execution import ReplacementLineage, finalization_binding_hash
from foundry_opt.bootstrap.input_contracts import BootstrapPlanInput, TrustedTemplateManifest
from foundry_opt.bootstrap.operation_state import SelectionPlan, read_operation_state
from foundry_opt.bootstrap.orchestrator import BootstrapOrchestrator
from foundry_opt.bootstrap.plan_factory import build_evaluation_actions
from foundry_opt.bootstrap.providers.foundry import FoundryAdapter
from foundry_opt.bootstrap.receipts import ApprovalRecord
from tests.bootstrap.fakes.evaluation_contract import build_contract, evaluation_agent_payload
from tests.bootstrap.fakes.foundry_env import RUBRIC_JOB_ID, build_fake_adapter

RUNTIME_SHA = "a" * 40
RUNTIME_REPOSITORY = "https://github.com/XOEEst/foundry-optimizer-coding-agent-plugin.git"
SIDECAR_PATH = "app/.foundry/foundry-opt.yaml"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "app" / ".foundry").mkdir(parents=True)
    (repo / "app" / ".foundry" / "agent-metadata.yaml").write_text(
        "agent_name: app\nsource_root: app\npackage_root: app\n",
        encoding="utf-8",
    )
    (repo / "app" / "main.py").write_text("import fastapi\napp = fastapi.FastAPI()\n", encoding="utf-8")
    return repo


def _plan_input(tmp_path: Path, *, agents: list[dict[str, object]], required_phases: list[str]) -> Path:
    manifest = TrustedTemplateManifest.load_pinned_manifest()
    tmp_path.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "repository": {
            "schema_version": 1,
            "repository_id": "org/repo",
            "repository_url": "https://github.com/org/repo.git",
            "default_branch": "main",
            "root": ".",
            "selected_agents": [
                {
                    "schema_version": 1,
                    "repo_agent_id": "app",
                    "root": "app",
                    "config_path": SIDECAR_PATH,
                    "editable_paths": ["app/main.py"],
                }
            ],
        },
        "runtime_provenance": {
            "schema_version": 1,
            "runtime_repository_url": RUNTIME_REPOSITORY,
            "runtime_commit": RUNTIME_SHA,
            "uv_lock_sha256": "0" * 64,
        },
        "repository_phase": {
            "schema_version": 1,
            "trusted_manifest_id": manifest.manifest_id,
            "trusted_manifest_version": manifest.manifest_version,
            "trusted_manifest_hash": manifest.manifest_hash,
            "agent_render_contexts": [{"schema_version": 1, "repo_agent_id": "app", "values": []}],
        },
        "offline_plan": False,
        "required_phases": required_phases,
        "evaluations_phase": {"schema_version": 1, "agents": agents},
    }
    path = tmp_path / "plan-input.json"
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path


def _orchestrator(repo: Path, loaded: BootstrapPlanInput, adapter: FoundryAdapter, state_root: Path) -> BootstrapOrchestrator:
    return BootstrapOrchestrator(
        repository_driver=RepositoryPhaseDriver(repository_root=repo, plan_input=loaded),
        github_driver=GitHubPhaseDriver(plan_input=loaded),
        azure_driver=AzurePhaseDriver(plan_input=loaded),
        evaluations_driver=EvaluationPhaseDriver(plan_input=loaded, provider=adapter),
        state_root=state_root,
    )


def _plan(orch: BootstrapOrchestrator, repo: Path, *, operation_id: str, phases: tuple[str, ...]):
    envelope = orch.discover(
        repo,
        repository_id="org/repo",
        operation_id=operation_id,
        runtime_repository=RUNTIME_REPOSITORY,
        runtime_commit=RUNTIME_SHA,
        selected_agents=({"root": "app", "repoAgentId": "app"},),
    )
    selection = SelectionPlan.model_validate(
        {**envelope.selection_plan.model_dump(mode="json"), "selected_agent_ids": ("app",)}
    )
    return orch.build_plan(
        repository_id="org/repo",
        operation_id=operation_id,
        runtime_repository=RUNTIME_REPOSITORY,
        runtime_commit=RUNTIME_SHA,
        selection_plan=selection,
        phases=phases,
    )


def _apply(orch: BootstrapOrchestrator, envelope, *, operation_id: str, phase: str):
    approval = ApprovalRecord.create(
        parent_plan_hash=envelope.bootstrap_plan.plan_hash,
        phase=phase,
        actor="tester",
        summary=f"approve {phase}",
    )
    return orch.apply_phase(
        repository_id="org/repo",
        operation_id=operation_id,
        phase=phase,
        approval=approval,
        runtime_commit=RUNTIME_SHA,
    )


def _run_phases(
    tmp_path: Path,
    *,
    contract=None,
    operation_id: str = "op-eval",
    replacement_intent: bool = False,
    adapter_kwargs: dict[str, object] | None = None,
    repo: Path | None = None,
):
    repository = repo or _repo(tmp_path)
    state_root = tmp_path / "state"
    approved = contract if contract is not None else build_contract()
    plan_input_path = _plan_input(
        tmp_path,
        agents=[evaluation_agent_payload(approved, replacement_intent=replacement_intent)],
        required_phases=["repository", "evaluations"],
    )
    loaded = BootstrapPlanInput.model_validate(json.loads(plan_input_path.read_text(encoding="utf-8")))
    adapter, fakes = build_fake_adapter(**(adapter_kwargs or {}))
    orch = _orchestrator(repository, loaded, adapter, state_root)
    envelope = _plan(orch, repository, operation_id=operation_id, phases=("repository", "evaluations"))
    repository_receipt = _apply(orch, envelope, operation_id=operation_id, phase="repository")
    evaluations_receipt = _apply(orch, envelope, operation_id=operation_id, phase="evaluations")
    return {
        "repo": repository,
        "state_root": state_root,
        "loaded": loaded,
        "adapter": adapter,
        "fakes": fakes,
        "orchestrator": orch,
        "envelope": envelope,
        "repository_receipt": repository_receipt,
        "evaluations_receipt": evaluations_receipt,
        "operation_id": operation_id,
        "contract": approved,
    }


def _read_state(context, operation_id: str | None = None):
    return read_operation_state("org/repo", operation_id or context["operation_id"], state_root=context["state_root"])


def test_one_composite_action_per_agent_is_planned_and_applied(tmp_path: Path) -> None:
    context = _run_phases(tmp_path)
    actions = [action for action in context["envelope"].bootstrap_plan.actions if action.phase == "evaluations"]

    assert len(actions) == 1
    assert actions[0].kind == "evaluation_onboarding"
    assert context["evaluations_receipt"].state == "applied"
    assert actions[0].action_id in context["evaluations_receipt"].receipt.changed_actions


def test_full_mocked_flow_activates_a_receipt_derived_sidecar(tmp_path: Path) -> None:
    context = _run_phases(tmp_path)
    repo = context["repo"]
    sidecar = repo / SIDECAR_PATH

    quick_profile_bytes = sidecar.read_bytes()
    quick_profile = BootstrapSidecar.from_document(quick_profile_bytes.decode("utf-8"))
    assert quick_profile.verification.mode == "off"
    assert quick_profile.default_evaluator_bundle is None
    registry = RootRegistry.from_document((repo / ".foundry-opt" / "registry.yaml").read_text(encoding="utf-8"))
    assert registry.agents[0].enabled is False

    activation = finalize_evaluation_activation(
        repository_root=repo,
        plan_input=context["loaded"],
        envelope=_read_state(context),
        runtime_commit=RUNTIME_SHA,
        state_root=context["state_root"],
    )

    assert sidecar.exists()
    document = BootstrapSidecar.from_document(sidecar.read_text(encoding="utf-8"))
    receipt = context["evaluations_receipt"].receipt
    binding = document.evaluation_lineage.activation_binding
    assert binding.plan_hash == context["envelope"].bootstrap_plan.plan_hash
    assert binding.approval_hash == context["evaluations_receipt"].approval_hash
    assert binding.receipt_hash == receipt.receipt_hash
    assert binding.runtime_commit == RUNTIME_SHA
    assert binding.finalization_hash == activation.entries[0].finalization_binding_hash
    assert activation.entries[0].previous_sha256 == sha256(quick_profile_bytes).hexdigest()
    # Immutable ids in the sidecar came from the receipt, not from the approved plan payload.
    approved_payload = [
        action.diagnostics[2]
        for action in context["envelope"].bootstrap_plan.actions
        if action.phase == "evaluations"
    ][0]
    assert document.development_dataset.dataset_id not in approved_payload
    assert document.development_definition.definition_id not in approved_payload
    assert document.evaluation_lineage.evaluator_provenance == "auto_generated_unreviewed"
    assert document.evaluation_lineage.evaluator_generation_operation_id == RUBRIC_JOB_ID
    assert (document.evaluation_lineage.development_case_count, document.evaluation_lineage.validating_case_count) == (20, 10)

    enabled = RootRegistry.from_document((repo / ".foundry-opt" / "registry.yaml").read_text(encoding="utf-8"))
    assert enabled.agents[0].enabled is True
    lock = json.loads((repo / ".foundry-opt" / "bootstrap.lock.json").read_text(encoding="utf-8"))
    assert SIDECAR_PATH in lock["sidecar_paths"]
    assert lock["last_activation"]["outcome"] == "succeeded"
    managed = {item["path"]: item for item in lock["managed_files"]}
    assert managed[SIDECAR_PATH]["applied_sha256"] == sha256(sidecar.read_bytes()).hexdigest()
    journal = read_finalize_journal("org/repo", context["operation_id"], state_root=context["state_root"])
    assert journal["state"] == "completed"
    assert _read_state(context).evaluator_replacement.status == "activated"


def test_only_one_human_approval_is_required_for_the_whole_onboarding(tmp_path: Path) -> None:
    context = _run_phases(tmp_path)
    envelope = _read_state(context)

    evaluation_approvals = [item for item in envelope.approvals if item.phase == "evaluations"]
    assert len(evaluation_approvals) == 1

    finalize_evaluation_activation(
        repository_root=context["repo"],
        plan_input=context["loaded"],
        envelope=envelope,
        runtime_commit=RUNTIME_SHA,
        state_root=context["state_root"],
    )

    # Auto-adopting the generated rubric required no additional approval record.
    assert [item for item in _read_state(context).approvals if item.phase == "evaluations"] == evaluation_approvals
    document = BootstrapSidecar.from_document((context["repo"] / SIDECAR_PATH).read_text(encoding="utf-8"))
    assert document.evaluation_lineage.evaluator_provenance == "auto_generated_unreviewed"


def test_reuse_path_activates_without_generating_a_rubric(tmp_path: Path) -> None:
    context = _run_phases(tmp_path, contract=build_contract(reuse=True), adapter_kwargs={"reuse": True})
    finalize_evaluation_activation(
        repository_root=context["repo"],
        plan_input=context["loaded"],
        envelope=_read_state(context),
        runtime_commit=RUNTIME_SHA,
        state_root=context["state_root"],
    )

    document = BootstrapSidecar.from_document((context["repo"] / SIDECAR_PATH).read_text(encoding="utf-8"))
    assert document.evaluation_lineage.evaluator_provenance == "reused_existing"
    assert document.evaluation_lineage.evaluator_generation_operation_id is None
    assert context["fakes"]["evaluator_jobs"].create_calls == []


def test_failed_activation_blocks_the_sidecar_mutation(tmp_path: Path) -> None:
    context = _run_phases(tmp_path, adapter_kwargs={"safety_pass_rate": 0.5}, operation_id="op-fail")
    sidecar = context["repo"] / SIDECAR_PATH
    quick_profile_bytes = sidecar.read_bytes()

    assert context["evaluations_receipt"].state in {"failed", "compensation_required"}
    assert sidecar.exists()
    with pytest.raises(BootstrapApplyError, match="requires a successful evaluations phase"):
        finalize_evaluation_activation(
            repository_root=context["repo"],
            plan_input=context["loaded"],
            envelope=_read_state(context),
            runtime_commit=RUNTIME_SHA,
            state_root=context["state_root"],
        )
    assert sidecar.read_bytes() == quick_profile_bytes
    registry = RootRegistry.from_document((context["repo"] / ".foundry-opt" / "registry.yaml").read_text(encoding="utf-8"))
    assert registry.agents[0].enabled is False


def test_restart_resumes_the_recorded_plan_and_runtime_sha(tmp_path: Path) -> None:
    context = _run_phases(tmp_path)
    restored = _read_state(context)

    assert restored.runtime_commit == RUNTIME_SHA
    assert restored.bootstrap_plan.plan_hash == context["envelope"].bootstrap_plan.plan_hash
    with pytest.raises(BootstrapApplyError, match="exact runtime commit"):
        context["orchestrator"].resume(repository_id="org/repo", operation_id=context["operation_id"], runtime_commit="b" * 40)
    with pytest.raises(BootstrapApplyError, match="exact recorded runtime SHA"):
        finalize_evaluation_activation(
            repository_root=context["repo"],
            plan_input=context["loaded"],
            envelope=restored,
            runtime_commit="b" * 40,
            state_root=context["state_root"],
        )
    assert [action.action_id for action in build_evaluation_actions(context["loaded"])] == [
        action.action_id for action in restored.bootstrap_plan.actions if action.phase == "evaluations"
    ]


def test_activation_refuses_a_plan_input_that_does_not_rebuild_the_approved_plan(tmp_path: Path) -> None:
    context = _run_phases(tmp_path)
    other_input = BootstrapPlanInput.model_validate(
        json.loads(
            _plan_input(
                tmp_path / "other",
                agents=[evaluation_agent_payload(build_contract(binding_classification="bound-diverged"))],
                required_phases=["repository", "evaluations"],
            ).read_text(encoding="utf-8")
        )
    )

    with pytest.raises(BootstrapApplyError, match="do not rebuild the approved plan"):
        finalize_evaluation_activation(
            repository_root=context["repo"],
            plan_input=other_input,
            envelope=_read_state(context),
            runtime_commit=RUNTIME_SHA,
            state_root=context["state_root"],
        )
    document = BootstrapSidecar.from_document((context["repo"] / SIDECAR_PATH).read_text(encoding="utf-8"))
    assert document.verification.mode == "off"


def test_ready_unbound_agent_is_scaffolded_but_never_activated(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    state_root = tmp_path / "state"
    stopped = build_contract(binding_classification="ready-unbound")
    loaded = BootstrapPlanInput.model_validate(
        json.loads(
            _plan_input(
                tmp_path,
                agents=[evaluation_agent_payload(stopped)],
                required_phases=["repository", "evaluations"],
            ).read_text(encoding="utf-8")
        )
    )
    adapter, _ = build_fake_adapter()
    orch = _orchestrator(repo, loaded, adapter, state_root)
    envelope = _plan(orch, repo, operation_id="op-stop", phases=("repository", "evaluations"))
    _apply(orch, envelope, operation_id="op-stop", phase="repository")
    receipt = _apply(orch, envelope, operation_id="op-stop", phase="evaluations")

    assert [action for action in envelope.bootstrap_plan.actions if action.phase == "evaluations"] == []
    assert receipt.state == "applied"
    assert not (repo / SIDECAR_PATH).exists()
    with pytest.raises(BootstrapApplyError, match="at least one applied onboarding action"):
        finalize_evaluation_activation(
            repository_root=repo,
            plan_input=loaded,
            envelope=read_operation_state("org/repo", "op-stop", state_root=state_root),
            runtime_commit=RUNTIME_SHA,
            state_root=state_root,
        )
    registry = RootRegistry.from_document((repo / ".foundry-opt" / "registry.yaml").read_text(encoding="utf-8"))
    assert registry.agents[0].enabled is False


def test_bound_diverged_agent_is_enabled_for_drafts_but_blocked_from_deployment(tmp_path: Path) -> None:
    context = _run_phases(tmp_path, contract=build_contract(binding_classification="bound-diverged"))
    finalize_evaluation_activation(
        repository_root=context["repo"],
        plan_input=context["loaded"],
        envelope=_read_state(context),
        runtime_commit=RUNTIME_SHA,
        state_root=context["state_root"],
    )

    registry = RootRegistry.from_document((context["repo"] / ".foundry-opt" / "registry.yaml").read_text(encoding="utf-8"))
    assert registry.agents[0].enabled is True
    document = BootstrapSidecar.from_document((context["repo"] / SIDECAR_PATH).read_text(encoding="utf-8"))
    assert document.deployment.enabled is False
    assert document.deployment.require_aligned_binding is True


def test_unexpected_existing_sidecar_requires_explicit_replacement(tmp_path: Path) -> None:
    context = _run_phases(tmp_path)
    sidecar = context["repo"] / SIDECAR_PATH
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    preimage = b"schema_version: 1\n"
    sidecar.write_bytes(preimage)

    with pytest.raises(BootstrapApplyError, match="not a valid managed document"):
        finalize_evaluation_activation(
            repository_root=context["repo"],
            plan_input=context["loaded"],
            envelope=_read_state(context),
            runtime_commit=RUNTIME_SHA,
            state_root=context["state_root"],
        )
    assert sidecar.read_bytes() == preimage


def test_repeated_activation_is_an_idempotent_replay(tmp_path: Path) -> None:
    context = _run_phases(tmp_path)
    first = finalize_evaluation_activation(
        repository_root=context["repo"],
        plan_input=context["loaded"],
        envelope=_read_state(context),
        runtime_commit=RUNTIME_SHA,
        state_root=context["state_root"],
    )
    sidecar_bytes = (context["repo"] / SIDECAR_PATH).read_bytes()

    second = finalize_evaluation_activation(
        repository_root=context["repo"],
        plan_input=context["loaded"],
        envelope=_read_state(context),
        runtime_commit=RUNTIME_SHA,
        state_root=context["state_root"],
    )

    assert (context["repo"] / SIDECAR_PATH).read_bytes() == sidecar_bytes
    assert second.entries[0].applied_sha256 == first.entries[0].applied_sha256
    assert first.entries[0].previous_sha256 is not None
    assert first.entries[0].previous_sha256 != first.entries[0].applied_sha256
    assert second.entries[0].previous_sha256 == second.entries[0].applied_sha256


def _activated_repo(tmp_path: Path, operation_id: str) -> dict[str, object]:
    context = _run_phases(tmp_path, operation_id=operation_id)
    finalize_evaluation_activation(
        repository_root=context["repo"],
        plan_input=context["loaded"],
        envelope=_read_state(context),
        runtime_commit=RUNTIME_SHA,
        state_root=context["state_root"],
    )
    return context


def test_explicit_replacement_activates_and_retains_the_previous_contract(tmp_path: Path) -> None:
    first = _activated_repo(tmp_path / "first", "op-initial")
    repo = first["repo"]
    sidecar = repo / SIDECAR_PATH
    preimage = sidecar.read_bytes()
    previous = BootstrapSidecar.from_document(preimage.decode("utf-8"))
    replacement = ReplacementLineage(
        previous_bundle_objective_hash=previous.default_evaluator_bundle.objective.objective_hash,
        previous_sidecar_sha256=sha256(preimage).hexdigest(),
        previous_development_definition_id=previous.development_definition.definition_id,
        previous_validating_definition_id=previous.validating_definition.definition_id,
    )
    context = _run_phases(
        tmp_path / "second",
        contract=build_contract(replacement=replacement),
        operation_id="op-replace",
        replacement_intent=True,
        repo=repo,
    )
    assert sidecar.read_bytes() == preimage

    receipt = finalize_evaluation_activation(
        repository_root=repo,
        plan_input=context["loaded"],
        envelope=_read_state(context),
        runtime_commit=RUNTIME_SHA,
        state_root=context["state_root"],
    )

    entry = receipt.entries[0]
    assert entry.previous_sha256 == replacement.previous_sidecar_sha256
    assert entry.retained_bundle_objective_hash == replacement.previous_bundle_objective_hash
    assert entry.lifecycle_status.startswith("replaced:")
    replaced = BootstrapSidecar.from_document(sidecar.read_text(encoding="utf-8"))
    assert replaced.evaluation_lineage.activation_binding is not None


def test_failed_replacement_preimage_keeps_the_old_bundle(tmp_path: Path) -> None:
    first = _activated_repo(tmp_path / "first", "op-initial")
    repo = first["repo"]
    sidecar = repo / SIDECAR_PATH
    preimage = sidecar.read_bytes()
    previous = BootstrapSidecar.from_document(preimage.decode("utf-8"))
    replacement = ReplacementLineage(
        previous_bundle_objective_hash=previous.default_evaluator_bundle.objective.objective_hash,
        previous_sidecar_sha256="f" * 64,
        previous_development_definition_id=previous.development_definition.definition_id,
        previous_validating_definition_id=previous.validating_definition.definition_id,
    )
    context = _run_phases(
        tmp_path / "second",
        contract=build_contract(replacement=replacement),
        operation_id="op-replace-fail",
        replacement_intent=True,
        repo=repo,
    )

    with pytest.raises(BootstrapApplyError, match="reviewed replacement preimage"):
        finalize_evaluation_activation(
            repository_root=repo,
            plan_input=context["loaded"],
            envelope=_read_state(context),
            runtime_commit=RUNTIME_SHA,
            state_root=context["state_root"],
        )

    assert sidecar.read_bytes() == preimage
    retained = BootstrapSidecar.from_document(sidecar.read_text(encoding="utf-8"))
    assert retained.default_evaluator_bundle == previous.default_evaluator_bundle


def test_finalization_binding_is_recomputable_from_the_receipt(tmp_path: Path) -> None:
    context = _run_phases(tmp_path)
    envelope = _read_state(context)
    receipt = finalize_evaluation_activation(
        repository_root=context["repo"],
        plan_input=context["loaded"],
        envelope=envelope,
        runtime_commit=RUNTIME_SHA,
        state_root=context["state_root"],
    )
    from foundry_opt.bootstrap.contracts import ActivationBinding
    from foundry_opt.bootstrap.evaluation.execution import EvaluationFinalization

    phase_receipt = next(item for item in envelope.phase_receipts if item.phase == "evaluations")
    ledger = phase_receipt.provider_state["onboarding"]["evaluations:app:onboarding"]
    finalization = EvaluationFinalization.model_validate(ledger["finalization"])
    binding = ActivationBinding(
        operation_id=envelope.operation_id,
        plan_hash=envelope.bootstrap_plan.plan_hash,
        approval_hash=phase_receipt.approval_hash,
        receipt_hash=phase_receipt.receipt.receipt_hash,
        runtime_commit=RUNTIME_SHA,
    )

    assert finalization_binding_hash(binding=binding, finalization=finalization) == receipt.entries[0].finalization_binding_hash


def test_persisted_state_and_receipts_never_carry_raw_content(tmp_path: Path) -> None:
    context = _run_phases(tmp_path)
    finalize_evaluation_activation(
        repository_root=context["repo"],
        plan_input=context["loaded"],
        envelope=_read_state(context),
        runtime_commit=RUNTIME_SHA,
        state_root=context["state_root"],
    )
    state_files = list(context["state_root"].rglob("*.json"))
    assert state_files
    blob = "\n".join(path.read_text(encoding="utf-8") for path in state_files)
    blob += (context["repo"] / SIDECAR_PATH).read_text(encoding="utf-8")
    lowered = blob.lower()
    for forbidden in ("raw_prompt", "transcript", "dataset_row", '"prompt"', '"response"', "case-0"):
        assert forbidden not in lowered
    # Row identifiers never leave the provider: only counts and lineage hashes are persisted.
    document = yaml.safe_load((context["repo"] / SIDECAR_PATH).read_text(encoding="utf-8"))
    assert set(document["verification"]["lineage"]) >= {"split_lineage_hash", "development_case_count"}
