"""Multi-agent, multi-project, and crash-restart coverage for the evaluation phase.

Agents in one repository may live in different Foundry projects and carry independent
evaluator bundles, and a crash mid-generation must resume the recorded job instead of
resubmitting it. No live Azure, GitHub, or Foundry mutation happens anywhere in this module.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from foundry_opt.bootstrap.drivers import AzurePhaseDriver, EvaluationPhaseDriver, GitHubPhaseDriver, RepositoryPhaseDriver
from foundry_opt.bootstrap.evaluation.activation import finalize_evaluation_activation
from foundry_opt.bootstrap.input_contracts import BootstrapPlanInput, TrustedTemplateManifest
from foundry_opt.bootstrap.operation_state import SelectionPlan, read_operation_state, status_from_state
from foundry_opt.bootstrap.orchestrator import BootstrapOrchestrator
from foundry_opt.bootstrap.providers.foundry import FoundryAdapter
from foundry_opt.bootstrap.receipts import ApprovalRecord
from tests.bootstrap.fakes.evaluation_contract import build_contract, evaluation_agent_payload
from tests.bootstrap.fakes.foundry_env import build_fake_adapter

RUNTIME_SHA = "a" * 40
RUNTIME_REPOSITORY = "https://github.com/XOEEst/foundry-optimizer-coding-agent-plugin.git"
SECOND_PROJECT_ENDPOINT = "https://second.services.ai.azure.com/api/projects/second"
SECOND_ACCOUNT_RESOURCE_ID = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/second"


def _repo(tmp_path: Path, roots: tuple[str, ...]) -> Path:
    repo = tmp_path / "repo"
    for root in roots:
        (repo / root / ".foundry").mkdir(parents=True)
        (repo / root / ".foundry" / "agent-metadata.yaml").write_text(
            f"agent_name: {root}\nsource_root: {root}\npackage_root: {root}\n",
            encoding="utf-8",
        )
        (repo / root / "main.py").write_text("import fastapi\napp = fastapi.FastAPI()\n", encoding="utf-8")
    return repo


def _agent_payload(root: str, *, endpoint: str | None = None, account: str | None = None, agent_name: str = "example-agent", reuse: bool = False) -> dict[str, object]:
    contract = build_contract(repo_agent_id=root, root=root, agent_name=agent_name, reuse=reuse)
    payload = evaluation_agent_payload(contract, repo_agent_id=root, root=root)
    payload["agent_name"] = agent_name
    if endpoint is not None:
        payload["project_endpoint"] = endpoint
        payload["account_resource_id"] = account
    return payload


def _plan_input(tmp_path: Path, *, roots: tuple[str, ...], agents: list[dict[str, object]]) -> BootstrapPlanInput:
    manifest = TrustedTemplateManifest.load_pinned_manifest()
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
                    "repo_agent_id": root,
                    "root": root,
                    "config_path": f"{root}/.foundry/foundry-opt.yaml",
                    "editable_paths": [f"{root}/main.py"],
                }
                for root in roots
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
            "agent_render_contexts": [{"schema_version": 1, "repo_agent_id": root, "values": []} for root in roots],
        },
        "offline_plan": False,
        "required_phases": ["repository", "evaluations"],
        "evaluations_phase": {"schema_version": 1, "agents": agents},
    }
    return BootstrapPlanInput.model_validate(json.loads(json.dumps(payload, sort_keys=True)))


def _orchestrator(repo: Path, loaded: BootstrapPlanInput, driver: EvaluationPhaseDriver, state_root: Path) -> BootstrapOrchestrator:
    return BootstrapOrchestrator(
        repository_driver=RepositoryPhaseDriver(repository_root=repo, plan_input=loaded),
        github_driver=GitHubPhaseDriver(plan_input=loaded),
        azure_driver=AzurePhaseDriver(plan_input=loaded),
        evaluations_driver=driver,
        state_root=state_root,
    )


def _plan(orch: BootstrapOrchestrator, repo: Path, *, operation_id: str, roots: tuple[str, ...]):
    envelope = orch.discover(
        repo,
        repository_id="org/repo",
        operation_id=operation_id,
        runtime_repository=RUNTIME_REPOSITORY,
        runtime_commit=RUNTIME_SHA,
        selected_agents=tuple({"root": root, "repoAgentId": root} for root in roots),
    )
    selection = SelectionPlan.model_validate({**envelope.selection_plan.model_dump(mode="json"), "selected_agent_ids": roots})
    return orch.build_plan(
        repository_id="org/repo",
        operation_id=operation_id,
        runtime_repository=RUNTIME_REPOSITORY,
        runtime_commit=RUNTIME_SHA,
        selection_plan=selection,
        phases=("repository", "evaluations"),
    )


def _apply(orch: BootstrapOrchestrator, envelope, *, operation_id: str, phase: str):
    approval = ApprovalRecord.create(
        parent_plan_hash=envelope.bootstrap_plan.plan_hash,
        phase=phase,
        actor="tester",
        summary=f"approve {phase}",
    )
    return orch.apply_phase(repository_id="org/repo", operation_id=operation_id, phase=phase, approval=approval, runtime_commit=RUNTIME_SHA)


class _RoutingDriver(EvaluationPhaseDriver):
    """Evaluation driver whose per-project adapters are offline fakes."""

    def __init__(self, *, plan_input: BootstrapPlanInput, adapters: dict[str, FoundryAdapter]) -> None:
        super().__init__(plan_input=plan_input)
        self._adapters = adapters

    def _client_for(self, endpoint: str) -> FoundryAdapter:
        adapter = self._adapters[endpoint]
        adapter.set_checkpoint(self._checkpoint_for(adapter))
        return adapter


def _two_project_context(tmp_path: Path, *, second_adapter_kwargs: dict[str, object] | None = None, second_reuse: bool = False):
    roots = ("app", "service")
    repo = _repo(tmp_path, roots)
    agents = [
        _agent_payload("app"),
        _agent_payload("service", endpoint=SECOND_PROJECT_ENDPOINT, account=SECOND_ACCOUNT_RESOURCE_ID, agent_name="service-agent", reuse=second_reuse),
    ]
    loaded = _plan_input(tmp_path, roots=roots, agents=agents)
    first, first_fakes = build_fake_adapter()
    second, second_fakes = build_fake_adapter(**(second_adapter_kwargs or {}))
    endpoints = {str(agent.project_endpoint): agent.repo_agent_id for agent in loaded.evaluations_phase.agents}
    adapters = {endpoint: (first if agent_id == "app" else second) for endpoint, agent_id in endpoints.items()}
    driver = _RoutingDriver(plan_input=loaded, adapters=adapters)
    state_root = tmp_path / "state"
    orch = _orchestrator(repo, loaded, driver, state_root)
    envelope = _plan(orch, repo, operation_id="op-multi", roots=roots)
    return {
        "repo": repo,
        "loaded": loaded,
        "orchestrator": orch,
        "envelope": envelope,
        "state_root": state_root,
        "first": first,
        "second": second,
        "first_fakes": first_fakes,
        "second_fakes": second_fakes,
        "driver": driver,
    }


def test_two_projects_are_each_applied_against_their_own_adapter(tmp_path: Path) -> None:
    context = _two_project_context(tmp_path)

    _apply(context["orchestrator"], context["envelope"], operation_id="op-multi", phase="repository")
    receipt = _apply(context["orchestrator"], context["envelope"], operation_id="op-multi", phase="evaluations")

    assert receipt.state == "applied", receipt.summary
    # Every project ran its own agent's composite action, and no agent leaked across projects.
    assert [call["name"] for call in context["first_fakes"]["evals"].create_calls if call["name"].endswith("-def")] == ["dev-def", "val-def"]
    assert [call["name"] for call in context["second_fakes"]["evals"].create_calls if call["name"].endswith("-def")] == ["dev-def", "val-def"]
    assert context["first_fakes"]["agents"].delete_version_calls == [("draft-agent", "1")]
    assert context["second_fakes"]["agents"].delete_version_calls == [("draft-agent", "1")]
    finalizations = context["driver"].onboarding_finalizations()
    assert sorted(finalizations) == ["evaluations:app:onboarding", "evaluations:service:onboarding"]
    # The aggregated receipt is bound to the phase plan and lists both composite actions.
    assert receipt.receipt.plan_hash == context["envelope"].bootstrap_plan.plan_hash or receipt.phase_plan_hash == receipt.receipt.plan_hash
    assert sorted(receipt.receipt.changed_actions) == ["evaluations:app:onboarding", "evaluations:service:onboarding"]


def test_multi_project_state_round_trips_and_rolls_back_per_project(tmp_path: Path) -> None:
    context = _two_project_context(tmp_path)
    _apply(context["orchestrator"], context["envelope"], operation_id="op-multi", phase="repository")
    receipt = _apply(context["orchestrator"], context["envelope"], operation_id="op-multi", phase="evaluations")
    assert receipt.state == "applied"

    state = read_operation_state("org/repo", "op-multi", state_root=context["state_root"])
    evaluations = next(item for item in state.phase_receipts if item.phase == "evaluations")
    assert evaluations.provider_state["multi_project"] is True
    assert sorted(evaluations.provider_state["projects"]) == sorted(
        {str(agent.project_endpoint) for agent in context["loaded"].evaluations_phase.agents}
    )

    rolled = context["orchestrator"].rollback_phase(repository_id="org/repo", operation_id="op-multi", phase="evaluations", runtime_commit=RUNTIME_SHA)

    assert rolled.state == "rolled_back"
    for fakes in (context["first_fakes"], context["second_fakes"]):
        assert sorted(fakes["datasets"].delete_calls) == [
            ("dev-set", "1"),
            ("generated-set", "1"),
            ("val-set", "1"),
        ]


def test_a_failing_second_project_compensates_the_first(tmp_path: Path) -> None:
    context = _two_project_context(tmp_path, second_adapter_kwargs={"safety_pass_rate": 0.9})

    _apply(context["orchestrator"], context["envelope"], operation_id="op-multi", phase="repository")
    receipt = _apply(context["orchestrator"], context["envelope"], operation_id="op-multi", phase="evaluations")

    assert receipt.state != "applied"
    # Created-only compensation: the successful project's resources are removed again.
    assert sorted(context["first_fakes"]["datasets"].delete_calls) == [
        ("dev-set", "1"),
        ("generated-set", "1"),
        ("val-set", "1"),
    ]
    assert context["first_fakes"]["agents"].delete_version_calls == [("draft-agent", "1")]


def test_per_agent_bundles_and_lineages_are_recorded(tmp_path: Path) -> None:
    # The second project reuses reviewed immutable assets, so the two agents legitimately
    # carry different bundles and lineages in the same operation.
    context = _two_project_context(tmp_path, second_reuse=True, second_adapter_kwargs={"reuse": True})
    _apply(context["orchestrator"], context["envelope"], operation_id="op-multi", phase="repository")
    assert _apply(context["orchestrator"], context["envelope"], operation_id="op-multi", phase="evaluations").state == "applied"

    finalize_evaluation_activation(
        repository_root=context["repo"],
        plan_input=context["loaded"],
        envelope=read_operation_state("org/repo", "op-multi", state_root=context["state_root"]),
        runtime_commit=RUNTIME_SHA,
        state_root=context["state_root"],
    )

    state = read_operation_state("org/repo", "op-multi", state_root=context["state_root"])
    replacements = {item.repo_agent_id: item for item in state.evaluator_replacements}
    assert sorted(replacements) == ["app", "service"]
    assert replacements["app"].lineage_hash != replacements["service"].lineage_hash
    assert all(item.status == "activated" for item in replacements.values())
    assert all(item.active_bundle_id for item in replacements.values())
    # The legacy single field is retained only as a compatibility projection.
    assert state.evaluator_replacement is not None
    assert state.evaluator_replacement.lineage_hash == replacements["app"].lineage_hash
    status = status_from_state(state)
    assert [item.repo_agent_id for item in status.evaluator_lineages] == ["app", "service"]
    assert status.evaluator_lineages[1].active_bundle_id == replacements["service"].candidate_bundle_id
    assert status.evaluator_lineage.lineage_hash == replacements["app"].lineage_hash


class _CrashingPoller:
    """Fails the first poll of a job kind, after the continuation has been checkpointed."""

    def __init__(self, adapter: FoundryAdapter, *, job_kind: str) -> None:
        self._adapter = adapter
        self._job_kind = job_kind
        self._original = adapter.poll_generation_job
        self.crashed = False

    def __call__(self, handle, **kwargs):
        if handle.job_kind == self._job_kind and not self.crashed:
            self.crashed = True
            persist = kwargs.get("persist_before_poll")
            if persist is not None:
                persist(handle)
            raise RuntimeError("process crashed while the generation job was running")
        return self._original(handle, **kwargs)


def _single_agent_context(tmp_path: Path, *, contract, operation_id: str):
    repo = _repo(tmp_path, ("app",))
    loaded = _plan_input(tmp_path, roots=("app",), agents=[evaluation_agent_payload(contract)])
    adapter, fakes = build_fake_adapter(generated_samples=20)
    driver = EvaluationPhaseDriver(plan_input=loaded, provider=adapter)
    state_root = tmp_path / "state"
    orch = _orchestrator(repo, loaded, driver, state_root)
    envelope = _plan(orch, repo, operation_id=operation_id, roots=("app",))
    _apply(orch, envelope, operation_id=operation_id, phase="repository")
    return {"repo": repo, "loaded": loaded, "adapter": adapter, "fakes": fakes, "orchestrator": orch, "envelope": envelope, "state_root": state_root, "driver": driver}


@pytest.mark.parametrize(
    ("job_kind", "create_calls_key"),
    (("dataset_generation", "dataset_jobs"), ("evaluator_generation", "evaluator_jobs")),
)
def test_crash_mid_generation_resumes_the_recorded_job_without_resubmission(tmp_path: Path, job_kind: str, create_calls_key: str) -> None:
    contract = build_contract(generation_kind="dataset_trace", useful_trace_samples=20)
    context = _single_agent_context(tmp_path, contract=contract, operation_id=f"op-{job_kind}")
    adapter = context["adapter"]
    crash = _CrashingPoller(adapter, job_kind=job_kind)
    adapter.poll_generation_job = crash

    failed = _apply(context["orchestrator"], context["envelope"], operation_id=f"op-{job_kind}", phase="evaluations")
    assert failed.state != "applied"
    assert crash.crashed

    # The in-flight continuation is durable, not just in memory.
    state = read_operation_state("org/repo", f"op-{job_kind}", state_root=context["state_root"])
    evaluations = next(item for item in state.phase_receipts if item.phase == "evaluations")
    projects = evaluations.provider_state["projects"]
    snapshot = next(iter(projects.values()))
    ledger = snapshot["onboarding"]["evaluations:app:onboarding"]["stages"]
    stage = "generation" if job_kind == "dataset_generation" else "evaluator"
    assert ledger[stage]["handle"]["job_kind"] == job_kind
    assert ledger[stage]["handle"]["continuation_token"]

    submitted_before = len([call for call in context["fakes"][create_calls_key].create_calls if call[0] is not None])
    adapter.poll_generation_job = crash._original

    resumed = _apply(context["orchestrator"], context["envelope"], operation_id=f"op-{job_kind}", phase="evaluations")

    assert resumed.state == "applied", resumed.summary
    # The recorded continuation was resumed; the job itself was never resubmitted.
    assert len([call for call in context["fakes"][create_calls_key].create_calls if call[0] is not None]) == submitted_before


def test_checkpoints_are_written_while_the_phase_is_still_applying(tmp_path: Path) -> None:
    contract = build_contract(generation_kind="dataset_trace", useful_trace_samples=20)
    context = _single_agent_context(tmp_path, contract=contract, operation_id="op-checkpoint")
    observed: list[str] = []
    original = context["adapter"].poll_generation_job

    def _observe(handle, **kwargs):
        persist = kwargs.get("persist_before_poll")
        if persist is not None:
            persist(handle)
        state = read_operation_state("org/repo", "op-checkpoint", state_root=context["state_root"])
        applying = next(item for item in state.phase_receipts if item.phase == "evaluations")
        assert applying.state == "applying"
        observed.append(str(applying.provider_state.get("checkpoint")))
        return original(handle, **kwargs)

    context["adapter"].poll_generation_job = _observe
    receipt = _apply(context["orchestrator"], context["envelope"], operation_id="op-checkpoint", phase="evaluations")

    assert receipt.state == "applied", receipt.summary
    assert observed and all(item == "True" for item in observed)
