"""Mocked end-to-end customer bootstrap acceptance test.

Drives the full customer bootstrap surface -- discovery, repository template
apply, GitHub/Azure/Foundry phase apply, evaluation onboarding/activation,
and deployment-matrix resolution -- against a temporary `git clone` of the
real, frozen public pilot baseline repository (tag `pilot-baseline-bound`), using
only fake GitHub/Azure/Foundry transports and providers. No live cloud,
GitHub, or Foundry mutation happens anywhere in this module; the pilot
repository is only ever read via a throwaway clone under `tmp_path` and is
never modified in place.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path

import httpx
import pytest
import yaml

from foundry_opt.bootstrap.canonical import safe_persisted_document
from foundry_opt.bootstrap.contracts import BootstrapSidecar, RootRegistry
from foundry_opt.bootstrap.discovery import discover_repository_agents
from foundry_opt.bootstrap.drivers import AzurePhaseDriver, EvaluationPhaseDriver, GitHubPhaseDriver, RepositoryPhaseDriver
from foundry_opt.bootstrap.errors import BootstrapApplyError, BootstrapConfigError
from foundry_opt.bootstrap.evaluation.activation import finalize_evaluation_activation
from foundry_opt.bootstrap.evaluation.core import REQUIRED_SAFETY_EVALUATORS
from foundry_opt.bootstrap.evaluation.execution import ReplacementLineage
from foundry_opt.bootstrap.input_contracts import BootstrapPlanInput, TrustedTemplateManifest
from foundry_opt.bootstrap.operation_state import SelectionPlan, read_operation_state
from foundry_opt.bootstrap.orchestrator import BootstrapOrchestrator
from foundry_opt.bootstrap.packaging_policy import PACKAGE_EXCLUDES
from foundry_opt.bootstrap.providers.azure import AzureArmRestProvider
from foundry_opt.bootstrap.providers.foundry import AgentPackage, FoundryAdapter
from foundry_opt.bootstrap.providers.github import GitHubBootstrapProvider
from foundry_opt.bootstrap.receipts import ApprovalRecord
from foundry_opt.bootstrap.workflow_integration import (
    build_changed_path_matrix,
    build_registered_deployment_plan,
    resolve_registry_selection,
)
from foundry_opt.packaging import build_deterministic_zip
from tests.bootstrap.fakes import AzureTransportRecorder
from tests.bootstrap.fakes.evaluation_contract import build_contract, evaluation_agent_payload
from tests.bootstrap.fakes.foundry_env import build_code_archive, build_fake_adapter, fake_agent_package
from tests.bootstrap.fakes.live_dataset_blob import install_live_datasets, synthetic_rows

# ---------------------------------------------------------------------------
# Reviewed provenance pins, exactly as specified by the bootstrap acceptance
# task and TODOs/006-bootstrap-implementation-plan.md.
# ---------------------------------------------------------------------------

PILOT_LOCAL_REPO_SOURCE = Path("Q:/GIT/foundry-bootstrap-pilot")
PILOT_REPO_SOURCE = (
    str(PILOT_LOCAL_REPO_SOURCE)
    if PILOT_LOCAL_REPO_SOURCE.is_dir()
    else "https://github.com/XOEEst/foundry-bootstrap-pilot.git"
)
PILOT_BASELINE_TAG = "pilot-baseline-bound"
PILOT_BASELINE_COMMIT = "f54b3702971fabbd27eb01a24e4379899cfd1ffb"

RUNTIME_REPOSITORY = "https://github.com/XOEEst/foundry-optimizer-coding-agent-plugin.git"
RUNTIME_COMMIT = "5f03a9188eb720489404980458d94fb3c353469c"
UV_LOCK_SHA256 = "74d7bb534c53e71a61ce197f3d5fa3169f2413373c2e42617280e78e83d6c681"

REPOSITORY_ID = "example-customer/foundry-bootstrap-pilot"
REPOSITORY_URL = "https://github.com/example-customer/foundry-bootstrap-pilot.git"
DEFAULT_BRANCH = "main"
OWNER, REPO_NAME = REPOSITORY_ID.split("/")

# Explicit, stable discovery ids for the three pilot roots.
ALIGNED_ID = "travel-approver-live"
ALIGNED_ROOT = "agents/travel-approver-live"
ALIGNED_APP_ROOT = f"{ALIGNED_ROOT}/app"
ALIGNED_MAIN = f"{ALIGNED_APP_ROOT}/main.py"
# The exact reviewed metadata the pilot's own `agent-metadata.yaml` publishes for this root --
# discovery's `bound-aligned` classification requires the observed project endpoint, agent
# name, and version to match this file exactly (metadata alone is never trusted; the two
# content fingerprints must independently match too).
ALIGNED_AGENT_NAME = "foundry-opt-bootstrap-pilot-aligned"
ALIGNED_AGENT_VERSION = "1"
ALIGNED_EXPECTED_PROJECT_ENDPOINT = "https://luechen-eus2-foundry.services.ai.azure.com/api/projects/luechen-eus2-fdp"

UNKNOWN_ID = "claims-review-fixture"
UNKNOWN_ROOT = "agents/claims-review-fixture"
UNKNOWN_APP_ROOT = f"{UNKNOWN_ROOT}/app"
UNKNOWN_MAIN = f"{UNKNOWN_APP_ROOT}/main.py"

UNBOUND_ID = "policy-ready-unbound"
UNBOUND_ROOT = "services/policy-ready-unbound"

SECOND_PROJECT_ENDPOINT = "https://second.services.ai.azure.com/api/projects/second"
SECOND_ACCOUNT_RESOURCE_ID = (
    "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/rg"
    "/providers/Microsoft.CognitiveServices/accounts/second"
)

# Azure identity/RBAC constants, reusing the pilot's own reviewed subscription
# and resource group metadata for realism.
SUBSCRIPTION_ID = "7b43cfa1-da92-48cc-865d-5499466b3b5c"
RESOURCE_GROUP = "luechen-eastus2"
TENANT_ID = "44444444-4444-4444-4444-444444444444"
LOCATION = "eastus2"
ACCOUNT_RESOURCE_ID = (
    f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}"
    "/providers/Microsoft.CognitiveServices/accounts/luechen-eus2-foundry"
)
PROJECT_SCOPE = f"{ACCOUNT_RESOURCE_ID}/projects/luechen-eus2-fdp"
UAMI_NAME = "foundry-opt-shared-uami"
UAMI_RESOURCE_ID = (
    f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/{RESOURCE_GROUP}"
    f"/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{UAMI_NAME}"
)
UAMI_URL = f"https://management.azure.com{UAMI_RESOURCE_ID}"
CLIENT_ID = "11111111-1111-1111-1111-111111111111"
PRINCIPAL_ID = "22222222-2222-2222-2222-222222222222"
ROLE_ALIAS = "foundry-user-shared"
ROLE_GUID = "53ca6127-db72-4b80-b1b0-d745d6d5456d"
ROLE_DEFINITION_ID = f"/subscriptions/{SUBSCRIPTION_ID}/providers/Microsoft.Authorization/roleDefinitions/{ROLE_GUID}"
# Precomputed exactly like `providers.azure._role_assignment_id` (uuid5 over the
# casefolded "scope|principal|role_definition_id" tuple) -- see providers/azure.py.
ASSIGNMENT_ID = "1c703206-1737-535c-9aee-3351ec6bbaf4"
ROLE_URL = f"https://management.azure.com{PROJECT_SCOPE}/providers/Microsoft.Authorization/roleAssignments/{ASSIGNMENT_ID}?api-version=2022-04-01"
# Precomputed exactly like `providers.azure._subjects`/`_fic_name` for REPOSITORY_ID.
FIC_SUBJECTS = {
    "copilot": f"repo:{REPOSITORY_ID}:environment:copilot",
    "foundry-production": f"repo:{REPOSITORY_ID}:environment:foundry-production",
}
FIC_NAMES = {
    "copilot": "2a00e121f2a59e25e639b020",
    "foundry-production": "6d455248490bb0167e64e2e0",
}

GITHUB_OPTIMIZER_ENVIRONMENT = "copilot"
GITHUB_DEPLOYMENT_ENVIRONMENT = "foundry-production"
GITHUB_VARIABLE_NAME = "AZURE_OPTIMIZER_CLIENT_ID"


def _clone_pilot_repo(dest: Path) -> Path:
    """Clone the read-only pilot baseline into a throwaway directory.

    A real `git clone` (rather than a filesystem copy) is required so local,
    gitignored `__pycache__` artifacts never leak into the working copy --
    discovery's local fingerprinting excludes `__pycache__`, but the fake
    Foundry code-archive builder does not, so a raw copy could desync the
    bound-aligned fingerprint comparison.
    """

    subprocess.run(["git", "clone", "--quiet", str(PILOT_REPO_SOURCE), str(dest)], check=True, capture_output=True, text=True)
    subprocess.run(["git", "-C", str(dest), "checkout", "--quiet", PILOT_BASELINE_COMMIT], check=True, capture_output=True, text=True)
    head = subprocess.run(["git", "-C", str(dest), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    assert head == PILOT_BASELINE_COMMIT
    return dest


def _aligned_binding_evidence(repo: Path) -> dict[str, object]:
    """Build reviewed live binding evidence for the aligned agent.

    Mirrors the exact mechanics `discovery._normalize_binding_evidence` expects: a plain
    dict restricted to the five supported keys, produced by observing a throwaway fake
    Foundry adapter pointed at a real code archive of the deployed root. The `bound-aligned`
    classification requires the observed project endpoint/name/version to match the pilot's
    own reviewed `agent-metadata.yaml` exactly, on top of both content fingerprints matching
    the local repository content -- metadata alone is never sufficient.
    """

    archive = build_code_archive(repo / ALIGNED_APP_ROOT)
    digest = sha256(archive).hexdigest()
    adapter, _ = build_fake_adapter(code_archive=archive, code_content_hash=digest)
    observation = adapter.observe_agent_binding(
        agent_name=ALIGNED_AGENT_NAME,
        agent_version=ALIGNED_AGENT_VERSION,
        source_root=ALIGNED_APP_ROOT,
        package_root=ALIGNED_APP_ROOT,
    )
    return {
        "project_endpoint": ALIGNED_EXPECTED_PROJECT_ENDPOINT,
        "agent_name": observation["agent_name"],
        "agent_version": observation["agent_version"],
        "source_fingerprint": observation["source_fingerprint"],
        "package_fingerprint": observation["package_fingerprint"],
    }


def _agent_contract(*, repo_agent_id: str, root: str, app_relpath: str, **kwargs: object):
    """`build_contract` plus the one correction the pilot's nested layout needs.

    `SidecarPolicy.source_root`/`package_root` must equal the *selected agent
    root* exactly (cross-validated in `BootstrapPlanInput`), but the pilot's
    real editable file lives one level down at `<root>/app/main.py`, so the
    policy's `editable_paths` is corrected to point at the real file.
    """

    contract = build_contract(repo_agent_id=repo_agent_id, root=root, **kwargs)
    if contract.sidecar_policy is not None:
        fixed_policy = contract.sidecar_policy.model_copy(update={"editable_paths": (app_relpath,)})
        # `model_copy` does not recompute the sealed `contract_hash`, so the contract
        # must be re-sealed after correcting the nested sidecar policy.
        resealed = {**contract.model_dump(mode="json", exclude={"contract_hash"}), "sidecar_policy": fixed_policy}
        contract = type(contract)._seal(resealed, hash_field="contract_hash")
    return contract


def _agent_payload(
    *,
    repo_agent_id: str,
    root: str,
    app_relpath: str,
    contract,
    endpoint: str | None = None,
    account: str | None = None,
    reuse: bool | None = None,
    replacement_intent: bool = False,
) -> dict[str, object]:
    payload = evaluation_agent_payload(contract, repo_agent_id=repo_agent_id, root=root, reuse=reuse, replacement_intent=replacement_intent)
    payload["generation_sources"] = [{"schema_version": 1, "kind": "reviewed_file", "path": app_relpath}]
    if endpoint is not None:
        payload["project_endpoint"] = endpoint
        payload["account_resource_id"] = account
    return payload


def _selected_agent(*, repo_agent_id: str, root: str, app_relpath: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "repo_agent_id": repo_agent_id,
        "root": root,
        "config_path": f"{root}/.foundry/foundry-opt.yaml",
        "editable_paths": [app_relpath],
    }


def _build_plan_input(
    *,
    selected_agents: list[dict[str, object]],
    evaluation_agents: list[dict[str, object]],
    required_phases: list[str],
    shared_client_id: str = CLIENT_ID,
) -> BootstrapPlanInput:
    manifest = TrustedTemplateManifest.load_pinned_manifest()
    payload = {
        "schema_version": 1,
        "repository": {
            "schema_version": 1,
            "repository_id": REPOSITORY_ID,
            "repository_url": REPOSITORY_URL,
            "default_branch": DEFAULT_BRANCH,
            "root": ".",
            "selected_agents": selected_agents,
        },
        "runtime_provenance": {
            "schema_version": 1,
            "runtime_repository_url": RUNTIME_REPOSITORY,
            "runtime_commit": RUNTIME_COMMIT,
            "uv_lock_sha256": UV_LOCK_SHA256,
        },
        "repository_phase": {
            "schema_version": 1,
            "trusted_manifest_id": manifest.manifest_id,
            "trusted_manifest_version": manifest.manifest_version,
            "trusted_manifest_hash": manifest.manifest_hash,
            "agent_render_contexts": [{"schema_version": 1, "repo_agent_id": agent["repo_agent_id"], "values": []} for agent in selected_agents],
        },
        "offline_plan": False,
        "required_phases": required_phases,
        "github_phase": {
            "schema_version": 1,
            "optimizer_environment": GITHUB_OPTIMIZER_ENVIRONMENT,
            "deployment_environment": GITHUB_DEPLOYMENT_ENVIRONMENT,
            "shared_client_id": shared_client_id,
            "client_id_variable_name": GITHUB_VARIABLE_NAME,
            "default_branch_policy_intent": "preserve_repository_default",
        },
        "azure_phase": {
            "schema_version": 1,
            "tenant_id": TENANT_ID,
            "subscription_id": SUBSCRIPTION_ID,
            "identity": {
                "schema_version": 1,
                "identity_kind": "user_assigned_managed_identity",
                "existing_resource_id": UAMI_RESOURCE_ID,
                "create_if_missing": True,
            },
            "resource_group": RESOURCE_GROUP,
            "location": LOCATION,
            "github_repository_id": REPOSITORY_ID,
            "approved_role_assignments": [
                {
                    "schema_version": 1,
                    "alias": ROLE_ALIAS,
                    "role_definition_id": ROLE_DEFINITION_ID,
                    "scope": PROJECT_SCOPE,
                }
            ],
        },
        "evaluations_phase": {"schema_version": 1, "agents": evaluation_agents},
    }
    return BootstrapPlanInput.model_validate(json.loads(json.dumps(payload, sort_keys=True)))


def _github_response(status: int, payload: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json=payload or {})


def _github_body(request: httpx.Request) -> dict:
    raw = request.read()
    return json.loads(raw.decode("utf-8")) if raw else {}


def _fresh_environment_state() -> dict[str, object]:
    return {"exists": False, "policy": None, "variable_value": None, "branch_policies": [], "next_policy_id": 101}


def _github_state() -> dict[str, dict[str, object]]:
    return {"copilot": _fresh_environment_state(), "foundry-production": _fresh_environment_state()}


def _github_stateful_handler(state: dict[str, dict[str, object]], log: list[tuple[str, str, object | None]]):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        if path.endswith(f"/repos/{OWNER}/{REPO_NAME}"):
            return _github_response(200, {"id": 909001, "default_branch": DEFAULT_BRANCH, "full_name": REPOSITORY_ID})
        if path.endswith("/actions/variables"):
            return _github_response(200, {"variables": []})
        for env in ("copilot", "foundry-production"):
            env_state = state[env]
            if path.endswith(f"/environments/{env}") and method == "GET":
                if not env_state["exists"]:
                    return _github_response(404, {})
                return _github_response(200, {"name": env, "deployment_branch_policy": env_state["policy"]})
            if path.endswith(f"/environments/{env}") and method == "PUT":
                payload = _github_body(request)["deployment_branch_policy"]
                log.append((method, path, payload))
                env_state["exists"] = True
                env_state["policy"] = payload
                return _github_response(200, {})
            if path.endswith(f"/environments/{env}") and method == "DELETE":
                log.append((method, path, None))
                env_state.update({"exists": False, "policy": None, "variable_value": None, "branch_policies": []})
                return _github_response(204, {})
            if path.endswith(f"/environments/{env}/variables/{GITHUB_VARIABLE_NAME}") and method == "GET":
                if not env_state["exists"] or env_state["variable_value"] is None:
                    return _github_response(404, {})
                return _github_response(200, {"name": GITHUB_VARIABLE_NAME, "value": env_state["variable_value"]})
            if path.endswith(f"/environments/{env}/variables/{GITHUB_VARIABLE_NAME}") and method == "PATCH":
                payload = _github_body(request)
                log.append((method, path, payload))
                env_state["variable_value"] = payload["value"]
                return _github_response(204, {})
            if path.endswith(f"/environments/{env}/variables/{GITHUB_VARIABLE_NAME}") and method == "DELETE":
                log.append((method, path, None))
                env_state["variable_value"] = None
                return _github_response(204, {})
            if path.endswith(f"/environments/{env}/variables") and method == "GET":
                variables = [] if env_state["variable_value"] is None else [{"name": GITHUB_VARIABLE_NAME, "value": env_state["variable_value"]}]
                return _github_response(200, {"variables": variables})
            if path.endswith(f"/environments/{env}/variables") and method == "POST":
                payload = _github_body(request)
                log.append((method, path, payload))
                env_state["variable_value"] = payload["value"]
                return _github_response(201, {})
            if path.endswith(f"/environments/{env}/deployment_branch_policies") and method == "GET":
                return _github_response(200, {"branch_policies": env_state["branch_policies"]})
            if path.endswith(f"/environments/{env}/deployment_branch_policies") and method == "POST":
                payload = _github_body(request)
                log.append((method, path, payload))
                env_state["branch_policies"] = [{"id": env_state["next_policy_id"], "name": payload["name"], "type": payload["type"]}]
                return _github_response(201, {})
            if f"/environments/{env}/deployment_branch_policies/" in path and method == "DELETE":
                log.append((method, path, None))
                env_state["branch_policies"] = []
                return _github_response(204, {})
        raise AssertionError((method, path))

    return handler


def _uami_payload() -> dict[str, object]:
    return {"id": UAMI_RESOURCE_ID, "name": UAMI_NAME, "location": LOCATION, "properties": {"clientId": CLIENT_ID, "principalId": PRINCIPAL_ID, "tenantId": TENANT_ID}}


def _fic_payload(subject: str) -> dict[str, object]:
    return {"properties": {"issuer": "https://token.actions.githubusercontent.com", "subject": subject, "audiences": ["api://AzureADTokenExchange"]}}


def _role_payload() -> dict[str, object]:
    return {
        "properties": {
            "principalId": PRINCIPAL_ID,
            "roleDefinitionId": ROLE_DEFINITION_ID,
            "condition": None,
            "conditionVersion": None,
            "delegatedManagedIdentityResourceId": None,
        },
        "id": ROLE_URL.split("?", 1)[0],
    }


def _azure_state() -> dict[str, object]:
    return {"uami_exists": False, "fic_exists": {"copilot": False, "foundry-production": False}, "role_exists": False}


def _register_azure_routes(recorder: AzureTransportRecorder, state: dict[str, object]) -> None:
    uami_url = f"{UAMI_URL}?api-version=2023-01-31"

    def uami_get(request: httpx.Request) -> httpx.Response:
        if state["uami_exists"]:
            return httpx.Response(200, json=_uami_payload(), request=request)
        return httpx.Response(404, json={"error": {}}, request=request)

    def uami_put(request: httpx.Request) -> httpx.Response:
        state["uami_exists"] = True
        return httpx.Response(201, json=_uami_payload(), request=request)

    def uami_delete(request: httpx.Request) -> httpx.Response:
        state["uami_exists"] = False
        return httpx.Response(204, json={}, request=request)

    recorder.add("GET", uami_url, uami_get)
    recorder.add("PUT", uami_url, uami_put)
    recorder.add("DELETE", uami_url, uami_delete)

    for env, subject in FIC_SUBJECTS.items():
        fic_url = f"{UAMI_URL}/federatedIdentityCredentials/{FIC_NAMES[env]}?api-version=2024-11-30"

        def fic_get(request: httpx.Request, env: str = env, subject: str = subject) -> httpx.Response:
            if state["fic_exists"][env]:
                return httpx.Response(200, json=_fic_payload(subject), request=request)
            return httpx.Response(404, json={"error": {}}, request=request)

        def fic_put(request: httpx.Request, env: str = env, subject: str = subject) -> httpx.Response:
            state["fic_exists"][env] = True
            return httpx.Response(201, json=_fic_payload(subject), request=request)

        def fic_delete(request: httpx.Request, env: str = env) -> httpx.Response:
            state["fic_exists"][env] = False
            return httpx.Response(204, json={}, request=request)

        recorder.add("GET", fic_url, fic_get)
        recorder.add("PUT", fic_url, fic_put)
        recorder.add("DELETE", fic_url, fic_delete)

    def role_get(request: httpx.Request) -> httpx.Response:
        if state["role_exists"]:
            return httpx.Response(200, json=_role_payload(), request=request)
        return httpx.Response(404, json={"error": {}}, request=request)

    def role_put(request: httpx.Request) -> httpx.Response:
        state["role_exists"] = True
        return httpx.Response(201, json=_role_payload(), request=request)

    def role_delete(request: httpx.Request) -> httpx.Response:
        state["role_exists"] = False
        return httpx.Response(204, json={}, request=request)

    recorder.add("GET", ROLE_URL, role_get)
    recorder.add("PUT", ROLE_URL, role_put)
    recorder.add("DELETE", ROLE_URL, role_delete)


class _RoutingDriver(EvaluationPhaseDriver):
    """Evaluation driver whose per-project adapters are offline fakes.

    Mirrors the private `_RoutingDriver` in `test_multi_agent_projects.py`:
    the aligned and bound-unknown agents intentionally live in different
    Foundry projects, so a single adapter is never shared across endpoints.
    """

    def __init__(self, *, plan_input: BootstrapPlanInput, adapters: dict[str, FoundryAdapter], repository_root: Path | None = None) -> None:
        super().__init__(plan_input=plan_input, repository_root=repository_root)
        self._adapters = adapters

    def _client_for(self, endpoint: str) -> FoundryAdapter:
        adapter = self._adapters[endpoint]
        adapter.set_checkpoint(self._checkpoint_for(adapter))
        return adapter


def _approve_and_apply(orch: BootstrapOrchestrator, envelope, *, operation_id: str, phase: str):
    approval = ApprovalRecord.create(
        parent_plan_hash=envelope.bootstrap_plan.plan_hash,
        phase=phase,
        actor="tester",
        summary=f"approve {phase}",
    )
    return orch.apply_phase(repository_id=REPOSITORY_ID, operation_id=operation_id, phase=phase, approval=approval, runtime_commit=RUNTIME_COMMIT)


def _plan_for(
    orch: BootstrapOrchestrator,
    repo: Path,
    *,
    operation_id: str,
    roots: tuple[str, ...],
    phases: tuple[str, ...],
    binding_evidence_by_root: Mapping[str, Mapping[str, object]] | None = None,
):
    envelope = orch.discover(
        repo,
        repository_id=REPOSITORY_ID,
        operation_id=operation_id,
        runtime_repository=RUNTIME_REPOSITORY,
        runtime_commit=RUNTIME_COMMIT,
        binding_evidence_by_root=binding_evidence_by_root,
        selected_agents=tuple({"root": root, "repoAgentId": root.rsplit("/", 1)[-1]} for root in roots),
    )
    selection = SelectionPlan.model_validate(
        {**envelope.selection_plan.model_dump(mode="json"), "selected_agent_ids": tuple(root.rsplit("/", 1)[-1] for root in roots)}
    )
    return orch.build_plan(
        repository_id=REPOSITORY_ID,
        operation_id=operation_id,
        runtime_repository=RUNTIME_REPOSITORY,
        runtime_commit=RUNTIME_COMMIT,
        selection_plan=selection,
        phases=phases,
    )


# ---------------------------------------------------------------------------
# The acceptance run itself.
# ---------------------------------------------------------------------------


def test_mocked_customer_bootstrap_end_to_end(tmp_path: Path, request: pytest.FixtureRequest) -> None:
    """One state-tracing acceptance run across every phase of customer bootstrap.

    Everything below only ever talks to (1) a throwaway `git clone` of the read-only
    pilot baseline repository, and (2) hand-built stateful fakes standing in for the
    GitHub REST API, the Azure Resource Manager REST API, and the Foundry SDK. No
    live GitHub/Azure/Foundry mutation happens anywhere here. Hosted CI may clone the
    frozen public pilot fixture over HTTPS when the local Windows checkout is unavailable.
    """

    # -- 0. Clone the pinned pilot baseline; confirm reviewed discovery classification --
    repo = _clone_pilot_repo(tmp_path / "pilot-clone")
    aligned_evidence = _aligned_binding_evidence(repo)

    discovery_only = discover_repository_agents(
        repo,
        binding_evidence_by_root={ALIGNED_ROOT: aligned_evidence},
        selected_agents=(
            {"root": ALIGNED_ROOT, "repoAgentId": ALIGNED_ID},
            {"root": UNKNOWN_ROOT, "repoAgentId": UNKNOWN_ID},
            {"root": UNBOUND_ROOT, "repoAgentId": UNBOUND_ID},
        ),
    )
    classifications = {agent.root: agent.bindingAssessment.classification for agent in discovery_only.agents}
    assert classifications[ALIGNED_ROOT] == "bound-aligned"
    assert classifications[UNKNOWN_ROOT] == "bound-unknown"
    assert classifications[UNBOUND_ROOT] == "ready-unbound"

    # -- 1. Build the approved plan input: two onboarded agents in two Foundry projects. --
    # `policy-ready-unbound` is intentionally never selected for onboarding; it only ever
    # appears in the discovery-only check above.
    aligned_contract = _agent_contract(repo_agent_id=ALIGNED_ID, root=ALIGNED_ROOT, app_relpath=ALIGNED_MAIN, binding_classification="bound-aligned")
    unknown_contract = _agent_contract(repo_agent_id=UNKNOWN_ID, root=UNKNOWN_ROOT, app_relpath=UNKNOWN_MAIN, binding_classification="bound-unknown")

    aligned_payload = _agent_payload(repo_agent_id=ALIGNED_ID, root=ALIGNED_ROOT, app_relpath=ALIGNED_MAIN, contract=aligned_contract, endpoint=ALIGNED_EXPECTED_PROJECT_ENDPOINT, account=ACCOUNT_RESOURCE_ID)
    unknown_payload = _agent_payload(repo_agent_id=UNKNOWN_ID, root=UNKNOWN_ROOT, app_relpath=UNKNOWN_MAIN, contract=unknown_contract, endpoint=SECOND_PROJECT_ENDPOINT, account=SECOND_ACCOUNT_RESOURCE_ID)

    loaded = _build_plan_input(
        selected_agents=[
            _selected_agent(repo_agent_id=ALIGNED_ID, root=ALIGNED_ROOT, app_relpath=ALIGNED_MAIN),
            _selected_agent(repo_agent_id=UNKNOWN_ID, root=UNKNOWN_ROOT, app_relpath=UNKNOWN_MAIN),
        ],
        evaluation_agents=[aligned_payload, unknown_payload],
        required_phases=["repository", "github", "azure", "evaluations"],
    )

    # Both onboarded agents are wired for the *real* default materialization path: no
    # `split_writer` short-circuit, and a loopback HTTP "blob" server standing in for the
    # SAS-protected dataset endpoint, so `dataset_case_index`/`publish_split_dataset`
    # genuinely download and re-upload content through `get_credentials`/`upload_file`
    # rather than the `get_case_index` preview seam. Draft creation is likewise wired to the
    # real deterministic-packaging path (`repository_root=repo`) instead of the fakes'
    # injected default package, so the created draft is provably built from the cloned
    # pilot's own reviewed source.
    aligned_adapter, aligned_fakes = build_fake_adapter(split_writer_available=False)
    unknown_adapter, unknown_fakes = build_fake_adapter(split_writer_available=False)
    aligned_blob_server, aligned_live_datasets = install_live_datasets(
        aligned_adapter, aligned_fakes, dataset_name="generated-set", rows=synthetic_rows(30)
    )
    unknown_blob_server, unknown_live_datasets = install_live_datasets(
        unknown_adapter, unknown_fakes, dataset_name="generated-set", rows=synthetic_rows(30)
    )
    request.addfinalizer(aligned_blob_server.close)
    request.addfinalizer(unknown_blob_server.close)
    evaluations_driver = _RoutingDriver(
        plan_input=loaded,
        adapters={ALIGNED_EXPECTED_PROJECT_ENDPOINT: aligned_adapter, SECOND_PROJECT_ENDPOINT: unknown_adapter},
        repository_root=repo,
    )

    github_state = _github_state()
    github_log: list[tuple[str, str, object | None]] = []
    github_provider = GitHubBootstrapProvider(token="mock-github-token", transport=httpx.MockTransport(_github_stateful_handler(github_state, github_log)))

    azure_state = _azure_state()
    azure_recorder = AzureTransportRecorder()
    _register_azure_routes(azure_recorder, azure_state)
    azure_provider = AzureArmRestProvider(
        token_provider=lambda scope: "mock-azure-token",
        transport=azure_recorder.transport(),
        approved_role_definitions={ROLE_ALIAS: ROLE_DEFINITION_ID},
    )

    state_root = tmp_path / "state"
    orch = BootstrapOrchestrator(
        repository_driver=RepositoryPhaseDriver(repository_root=repo, plan_input=loaded),
        github_driver=GitHubPhaseDriver(plan_input=loaded, provider=github_provider),
        azure_driver=AzurePhaseDriver(plan_input=loaded, provider=azure_provider),
        evaluations_driver=evaluations_driver,
        state_root=state_root,
    )

    # -- 2. op-initial: repository phase apply. --
    unrelated_before = {
        path: (repo / path).read_bytes()
        for path in (
            ".github/copilot-instructions.md",
            ".github/workflows/customer-docs-check.yml",
            "skills/customer-release-digest/SKILL.md",
        )
    }
    setup_workflow_before = (repo / ".github" / "workflows" / "copilot-setup-steps.yml").read_text(encoding="utf-8")
    assert "customer-owned preflight" in setup_workflow_before
    assert "test -f azure.yaml" in setup_workflow_before

    envelope_initial = _plan_for(orch, repo, operation_id="op-initial", roots=(ALIGNED_ROOT, UNKNOWN_ROOT), phases=("repository", "github", "azure", "evaluations"))
    repository_receipt = _approve_and_apply(orch, envelope_initial, operation_id="op-initial", phase="repository")

    assert repository_receipt.state == "applied"
    for path, before_bytes in unrelated_before.items():
        assert (repo / path).read_bytes() == before_bytes, f"unrelated file mutated: {path}"

    # The reserved workflow is a shared-runtime "semantic patch" target: on a repository's
    # very first bootstrap its two reserved step IDs never yet hold the rendered runtime
    # content, so the tool refuses to overwrite it outright and instead proposes the patched
    # content as a `.foundry-proposed` sibling for reviewed, human-merged adoption. The
    # customer's real file -- reserved slots *and* unrelated steps alike -- stays untouched.
    setup_workflow_after = (repo / ".github" / "workflows" / "copilot-setup-steps.yml").read_text(encoding="utf-8")
    assert setup_workflow_after == setup_workflow_before, "reserved workflow must not be overwritten without review"
    assert "customer-owned preflight" in setup_workflow_after
    assert "test -f azure.yaml" in setup_workflow_after

    setup_workflow_action_id = "repository:setup-semantic-patch:.github/workflows/copilot-setup-steps.yml"
    assert setup_workflow_action_id in repository_receipt.receipt.skipped_actions

    setup_workflow_proposed = (repo / ".github" / "workflows" / "copilot-setup-steps.yml.foundry-proposed").read_text(encoding="utf-8")
    assert RUNTIME_COMMIT in setup_workflow_proposed
    assert "customer-owned preflight" in setup_workflow_proposed
    assert "test -f azure.yaml" in setup_workflow_proposed

    registry_path = repo / ".foundry-opt" / "registry.yaml"
    registry_initial = RootRegistry.from_document(registry_path.read_text(encoding="utf-8"))
    assert registry_initial.identity.kind == "unresolved_migration"
    assert {agent.agent_id for agent in registry_initial.agents} == {ALIGNED_ID, UNKNOWN_ID}
    assert all(agent.enabled is False for agent in registry_initial.agents)

    instructions_path = repo / ".github" / "instructions" / "foundry-opt.instructions.md"
    assert instructions_path.exists()

    # -- 3. Idempotent rerun of the repository phase alone (new operation). --
    envelope_repo_idempotent = _plan_for(orch, repo, operation_id="op-repo-idempotent", roots=(ALIGNED_ROOT, UNKNOWN_ROOT), phases=("repository",))
    repo_idempotent_receipt = _approve_and_apply(orch, envelope_repo_idempotent, operation_id="op-repo-idempotent", phase="repository")
    assert repo_idempotent_receipt.state == "applied"
    assert len(repo_idempotent_receipt.receipt.created_actions) == 0
    assert len(repo_idempotent_receipt.receipt.changed_actions) == 0

    # -- 4. Hand-edit + upgrade (three-way preservation): a customer edit to a managed --
    # file is preserved on disk, with the reviewed upgrade offered alongside as a sibling.
    hand_edit = instructions_path.read_text(encoding="utf-8") + "\n<!-- customer note: keep this -->\n"
    instructions_path.write_text(hand_edit, encoding="utf-8")
    envelope_upgrade = _plan_for(orch, repo, operation_id="op-upgrade", roots=(ALIGNED_ROOT, UNKNOWN_ROOT), phases=("repository",))
    upgrade_receipt = _approve_and_apply(orch, envelope_upgrade, operation_id="op-upgrade", phase="repository")
    assert upgrade_receipt.state == "applied"
    assert instructions_path.read_text(encoding="utf-8") == hand_edit
    proposed_path = instructions_path.with_name(instructions_path.name + ".foundry-proposed")
    assert proposed_path.exists()

    # -- 5. op-initial continued: GitHub environments and shared variables plan+apply. --
    # Both environments get the shared UAMI client id. Repository-default branch policy intent
    # creates no custom environment branch-policy action.
    github_receipt = _approve_and_apply(orch, envelope_initial, operation_id="op-initial", phase="github")
    assert github_receipt.state == "applied"
    assert len(github_receipt.receipt.created_actions) > 0
    for env in (GITHUB_OPTIMIZER_ENVIRONMENT, GITHUB_DEPLOYMENT_ENVIRONMENT):
        assert github_state[env]["exists"] is True
        assert github_state[env]["policy"] is not None
        assert github_state[env]["variable_value"] == CLIENT_ID
        assert not github_state[env]["branch_policies"]

    # -- 6. op-initial continued: Azure create-if-missing shared UAMI, two OIDC subjects, role. --
    azure_receipt = _approve_and_apply(orch, envelope_initial, operation_id="op-initial", phase="azure")
    assert azure_receipt.state == "applied"
    assert len(azure_receipt.receipt.created_actions) > 0
    assert azure_state["uami_exists"] is True
    assert azure_state["fic_exists"]["copilot"] is True
    assert azure_state["fic_exists"]["foundry-production"] is True
    assert azure_state["role_exists"] is True

    # -- 7. op-initial continued: Foundry synthetic-only evaluation onboarding for both agents. --
    evaluations_receipt = _approve_and_apply(orch, envelope_initial, operation_id="op-initial", phase="evaluations")
    assert evaluations_receipt.state == "applied"
    evaluations_actions = [action for action in envelope_initial.bootstrap_plan.actions if action.phase == "evaluations"]
    assert {action.action_id.split(":")[1] for action in evaluations_actions} == {ALIGNED_ID, UNKNOWN_ID}
    assert aligned_fakes["evaluator_jobs"].create_calls, "synthetic-only onboarding must generate a rubric"

    # -- 7a. The activation draft is genuinely built from the cloned pilot's own reviewed --
    # source by the driver's real deterministic-packaging path (`repository_root=repo`),
    # never the fakes' injected default package -- proving the draft is created, not
    # pre-seeded, from this operation's own repository content.
    expected_aligned_zip = build_deterministic_zip(
        repo / ALIGNED_ROOT, tmp_path / "verify-aligned-package.zip", includes=("**/*",), excludes=PACKAGE_EXCLUDES, check_deadline=lambda: None
    )
    expected_unknown_zip = build_deterministic_zip(
        repo / UNKNOWN_ROOT, tmp_path / "verify-unknown-package.zip", includes=("**/*",), excludes=PACKAGE_EXCLUDES, check_deadline=lambda: None
    )
    default_package_sha256 = fake_agent_package().zip_sha256
    aligned_draft_creates = aligned_fakes["agents"].create_from_code_calls
    unknown_draft_creates = unknown_fakes["agents"].create_from_code_calls
    assert len(aligned_draft_creates) == 1
    assert len(unknown_draft_creates) == 1
    for creates, expected in ((aligned_draft_creates, expected_aligned_zip), (unknown_draft_creates, expected_unknown_zip)):
        assert creates[0]["code_zip_sha256"] == expected.zip_sha256
        # The fake independently hashes the bytes it actually streamed in, so this proves the
        # claimed digest matches genuinely-uploaded content, not just a label.
        assert creates[0]["observed_zip_sha256"] == expected.zip_sha256
        assert creates[0]["code_zip_sha256"] != default_package_sha256

    # -- 7b. The generated dataset is genuinely downloaded through a SAS credential and each --
    # split is genuinely re-uploaded through `upload_file` -- the default live materialization
    # path, never the injected `split_writer`/`get_case_index` preview shortcuts.
    assert aligned_adapter._split_writer is None
    assert unknown_adapter._split_writer is None
    assert not hasattr(aligned_live_datasets, "get_case_index")
    assert ("generated-set", "1") in aligned_live_datasets.get_credentials_calls
    assert ("generated-set", "1") in unknown_live_datasets.get_credentials_calls
    for live_datasets in (aligned_live_datasets, unknown_live_datasets):
        assert sorted(call["name"] for call in live_datasets.upload_calls) == ["dev-set", "val-set"]
        assert (len(live_datasets.uploaded_rows["dev-set"]), len(live_datasets.uploaded_rows["val-set"])) == (20, 10)
        # The real content actually round-tripped through the split (not merely identifiers).
        assert all(row["query"].startswith("question ") for row in live_datasets.uploaded_rows["dev-set"])
        # Temporary split files are written 0600 and removed immediately after upload.
        assert live_datasets.observed_temp_paths
        assert all(not Path(path).exists() for path in live_datasets.observed_temp_paths)

    aligned_sidecar_path = repo / ALIGNED_ROOT / ".foundry" / "foundry-opt.yaml"
    unknown_sidecar_path = repo / UNKNOWN_ROOT / ".foundry" / "foundry-opt.yaml"
    assert not aligned_sidecar_path.exists()  # never written by the repository phase
    assert not unknown_sidecar_path.exists()

    # -- 8. Finalize activation: receipt-bound sidecars, registry enablement, safety bundle. --
    activation = finalize_evaluation_activation(
        repository_root=repo,
        plan_input=loaded,
        envelope=read_operation_state(REPOSITORY_ID, "op-initial", state_root=state_root),
        runtime_commit=RUNTIME_COMMIT,
        state_root=state_root,
    )
    assert {entry.repo_agent_id for entry in activation.entries} == {ALIGNED_ID, UNKNOWN_ID}

    registry_after_activation = RootRegistry.from_document(registry_path.read_text(encoding="utf-8"))
    assert registry_after_activation.identity.kind == "unresolved_migration"
    assert all(agent.enabled is True for agent in registry_after_activation.agents)

    aligned_sidecar = BootstrapSidecar.from_document(aligned_sidecar_path.read_text(encoding="utf-8"))
    unknown_sidecar = BootstrapSidecar.from_document(unknown_sidecar_path.read_text(encoding="utf-8"))

    # target 30 deterministic 20/10 split, for both onboarded agents
    assert (aligned_sidecar.evaluation_lineage.development_case_count, aligned_sidecar.evaluation_lineage.validating_case_count) == (20, 10)
    assert (unknown_sidecar.evaluation_lineage.development_case_count, unknown_sidecar.evaluation_lineage.validating_case_count) == (20, 10)

    # five-evaluator safety bundle at 100% required pass rate, for both onboarded agents
    for sidecar in (aligned_sidecar, unknown_sidecar):
        guardrail_names = {guardrail.evaluator_name for guardrail in sidecar.hard_guardrails}
        assert guardrail_names == set(REQUIRED_SAFETY_EVALUATORS)
        assert all(guardrail.required_pass_rate == 1.0 for guardrail in sidecar.hard_guardrails)

    # bound-unknown may have a draft optimization config but deployment stays disabled;
    # only the bound-aligned sidecar activates a deployable configuration.
    assert aligned_sidecar.deployment.enabled is True
    assert unknown_sidecar.deployment.enabled is False
    assert unknown_sidecar.default_evaluator_bundle is not None  # draft config exists

    # ready-unbound never gets an activated sidecar at all -- it was never onboarded.
    assert not (repo / UNBOUND_ROOT / ".foundry" / "foundry-opt.yaml").exists()

    lock_after_activation = json.loads((repo / ".foundry-opt" / "bootstrap.lock.json").read_text(encoding="utf-8"))
    assert lock_after_activation["last_activation"]["outcome"] == "succeeded"

    # -- 9. op-idempotent: GitHub+Azure rerun (new operation) is a clean no-op. --
    envelope_idempotent = _plan_for(orch, repo, operation_id="op-idempotent", roots=(ALIGNED_ROOT, UNKNOWN_ROOT), phases=("github", "azure"))
    github_idempotent_receipt = _approve_and_apply(orch, envelope_idempotent, operation_id="op-idempotent", phase="github")
    azure_idempotent_receipt = _approve_and_apply(orch, envelope_idempotent, operation_id="op-idempotent", phase="azure")
    assert github_idempotent_receipt.state == "applied"
    assert len(github_idempotent_receipt.receipt.created_actions) == 0
    assert azure_idempotent_receipt.state == "applied"
    assert len(azure_idempotent_receipt.receipt.created_actions) == 0

    # -- 10. op-drift: external drift between plan and apply is refused, not silently applied. --
    envelope_drift = _plan_for(orch, repo, operation_id="op-drift", roots=(ALIGNED_ROOT, UNKNOWN_ROOT), phases=("github",))
    original_policy = github_state[GITHUB_DEPLOYMENT_ENVIRONMENT]["policy"]
    github_state[GITHUB_DEPLOYMENT_ENVIRONMENT]["policy"] = {"protected_branches": True, "custom_branch_policies": False}
    with pytest.raises(BootstrapApplyError, match="drifted"):
        _approve_and_apply(orch, envelope_drift, operation_id="op-drift", phase="github")
    # Restore the live state the refused apply never touched, so the still-pending op-initial
    # rollback below sees exactly what its own successful apply actually left behind.
    github_state[GITHUB_DEPLOYMENT_ENVIRONMENT]["policy"] = original_policy

    # -- 11. Rollback: GitHub (op-initial) -- created components are compensated. --
    # Both environments were created directly by this operation, so rollback removes both.
    rolled_github = orch.rollback_phase(repository_id=REPOSITORY_ID, operation_id="op-initial", phase="github", runtime_commit=RUNTIME_COMMIT)
    assert rolled_github.state == "rolled_back"
    assert github_state[GITHUB_OPTIMIZER_ENVIRONMENT]["exists"] is False
    assert github_state[GITHUB_DEPLOYMENT_ENVIRONMENT]["exists"] is False
    for env in (GITHUB_OPTIMIZER_ENVIRONMENT, GITHUB_DEPLOYMENT_ENVIRONMENT):
        assert github_state[env]["variable_value"] is None
        assert github_state[env]["branch_policies"] == []

    # -- 12. Rollback: Azure (op-initial) -- only what this operation created is removed. --
    rolled_azure = orch.rollback_phase(repository_id=REPOSITORY_ID, operation_id="op-initial", phase="azure", runtime_commit=RUNTIME_COMMIT)
    assert rolled_azure.state == "rolled_back"
    assert azure_state["uami_exists"] is False
    assert azure_state["fic_exists"]["copilot"] is False
    assert azure_state["fic_exists"]["foundry-production"] is False
    assert azure_state["role_exists"] is False

    # -- 13. Exact-SHA resume: only the pinned reviewed runtime commit resumes the operation. --
    with pytest.raises(BootstrapApplyError, match="exact runtime commit"):
        orch.resume(repository_id=REPOSITORY_ID, operation_id="op-initial", runtime_commit="f" * 40)
    resumed = orch.resume(repository_id=REPOSITORY_ID, operation_id="op-initial", runtime_commit=RUNTIME_COMMIT)
    assert resumed.bootstrap_plan.plan_hash == envelope_initial.bootstrap_plan.plan_hash

    # -- 14. Deployment matrix: changed-root resolution uses repository defaults, never --
    # issue-supplied evaluators, and only touches the agent(s) actually affected by the change.
    # This must run before evaluator replacement below: a single-agent replacement activation
    # only re-enables the agent(s) it targets, so it would otherwise disable `unknown` here.
    selection = resolve_registry_selection(repo, repo_agent_id=ALIGNED_ID)
    assert selection.root == ALIGNED_ROOT

    matrix_only_aligned = build_changed_path_matrix(repo, changed_paths=[ALIGNED_MAIN])
    assert [entry.repo_agent_id for entry in matrix_only_aligned] == [ALIGNED_ID]

    matrix_shared = build_changed_path_matrix(repo, changed_paths=[".foundry-opt/registry.yaml"])
    assert sorted(entry.repo_agent_id for entry in matrix_shared) == sorted([ALIGNED_ID, UNKNOWN_ID])

    with pytest.raises(BootstrapConfigError, match="repository default evaluator bundle"):
        build_registered_deployment_plan(selection, changed_root=ALIGNED_ROOT, exact_source=PILOT_BASELINE_COMMIT, use_repository_default_evaluators=False)

    deployment_plan = build_registered_deployment_plan(selection, changed_root=ALIGNED_ROOT, exact_source=PILOT_BASELINE_COMMIT, use_repository_default_evaluators=True)
    assert deployment_plan.repo_agent_id == ALIGNED_ID
    assert deployment_plan.objective_hash == selection.sidecar.default_evaluator_bundle.objective.objective_hash
    assert set(deployment_plan.default_evaluator_ids) == {item.reference.evaluator_id for item in selection.sidecar.default_evaluator_bundle.objective.evaluators}

    # -- 15. Evaluator replacement: a failed preimage keeps the old sidecar/bundle intact; --
    # a correct preimage swaps atomically.
    aligned_sidecar_bytes_before_replacement = aligned_sidecar_path.read_bytes()
    previous_sidecar = BootstrapSidecar.from_document(aligned_sidecar_bytes_before_replacement.decode("utf-8"))

    def _replacement_plan_input(replacement: ReplacementLineage) -> BootstrapPlanInput:
        contract = _agent_contract(
            repo_agent_id=ALIGNED_ID,
            root=ALIGNED_ROOT,
            app_relpath=ALIGNED_MAIN,
            binding_classification="bound-aligned",
            replacement=replacement,
        )
        payload = _agent_payload(
            repo_agent_id=ALIGNED_ID,
            root=ALIGNED_ROOT,
            app_relpath=ALIGNED_MAIN,
            contract=contract,
            endpoint=ALIGNED_EXPECTED_PROJECT_ENDPOINT,
            account=ACCOUNT_RESOURCE_ID,
            replacement_intent=True,
        )
        return _build_plan_input(
            selected_agents=[_selected_agent(repo_agent_id=ALIGNED_ID, root=ALIGNED_ROOT, app_relpath=ALIGNED_MAIN)],
            evaluation_agents=[payload],
            required_phases=["evaluations"],
        )

    def _run_replacement(replace_input: BootstrapPlanInput, adapter, *, operation_id: str) -> BootstrapOrchestrator:
        replace_orch = BootstrapOrchestrator(
            repository_driver=RepositoryPhaseDriver(repository_root=repo, plan_input=replace_input),
            github_driver=GitHubPhaseDriver(plan_input=replace_input),
            azure_driver=AzurePhaseDriver(plan_input=replace_input),
            evaluations_driver=EvaluationPhaseDriver(plan_input=replace_input, provider=adapter),
            state_root=state_root,
        )
        replace_envelope = _plan_for(replace_orch, repo, operation_id=operation_id, roots=(ALIGNED_ROOT,), phases=("evaluations",))
        _approve_and_apply(replace_orch, replace_envelope, operation_id=operation_id, phase="evaluations")
        return replace_orch

    bad_replacement = ReplacementLineage(
        previous_bundle_objective_hash=previous_sidecar.default_evaluator_bundle.objective.objective_hash,
        previous_sidecar_sha256="f" * 64,
        previous_development_definition_id=previous_sidecar.development_definition.definition_id,
        previous_validating_definition_id=previous_sidecar.validating_definition.definition_id,
    )
    fail_input = _replacement_plan_input(bad_replacement)
    fail_adapter, _ = build_fake_adapter()
    _run_replacement(fail_input, fail_adapter, operation_id="op-replace-fail")
    with pytest.raises(BootstrapApplyError, match="reviewed replacement preimage"):
        finalize_evaluation_activation(
            repository_root=repo,
            plan_input=fail_input,
            envelope=read_operation_state(REPOSITORY_ID, "op-replace-fail", state_root=state_root),
            runtime_commit=RUNTIME_COMMIT,
            state_root=state_root,
        )
    assert aligned_sidecar_path.read_bytes() == aligned_sidecar_bytes_before_replacement
    retained_after_failure = BootstrapSidecar.from_document(aligned_sidecar_path.read_text(encoding="utf-8"))
    assert retained_after_failure.default_evaluator_bundle == previous_sidecar.default_evaluator_bundle

    good_replacement = ReplacementLineage(
        previous_bundle_objective_hash=previous_sidecar.default_evaluator_bundle.objective.objective_hash,
        previous_sidecar_sha256=sha256(aligned_sidecar_bytes_before_replacement).hexdigest(),
        previous_development_definition_id=previous_sidecar.development_definition.definition_id,
        previous_validating_definition_id=previous_sidecar.validating_definition.definition_id,
    )
    ok_input = _replacement_plan_input(good_replacement)
    ok_adapter, _ = build_fake_adapter()
    _run_replacement(ok_input, ok_adapter, operation_id="op-replace-ok")
    replace_receipt = finalize_evaluation_activation(
        repository_root=repo,
        plan_input=ok_input,
        envelope=read_operation_state(REPOSITORY_ID, "op-replace-ok", state_root=state_root),
        runtime_commit=RUNTIME_COMMIT,
        state_root=state_root,
    )
    replace_entry = replace_receipt.entries[0]
    assert replace_entry.previous_sha256 == good_replacement.previous_sidecar_sha256
    assert replace_entry.retained_bundle_objective_hash == good_replacement.previous_bundle_objective_hash
    assert replace_entry.lifecycle_status.startswith("replaced:")
    swapped_sidecar = BootstrapSidecar.from_document(aligned_sidecar_path.read_text(encoding="utf-8"))
    assert swapped_sidecar.evaluation_lineage.activation_binding is not None
    assert swapped_sidecar.deployment.enabled is True

    # -- 16. No raw prompts/responses/traces/dataset rows/secrets are ever persisted. --
    state_files = list(state_root.rglob("*.json"))
    assert state_files
    blob = "\n".join(path.read_text(encoding="utf-8") for path in state_files)
    blob += registry_path.read_text(encoding="utf-8")
    blob += aligned_sidecar_path.read_text(encoding="utf-8")
    blob += unknown_sidecar_path.read_text(encoding="utf-8")
    lowered = blob.lower()
    for forbidden in ("raw_prompt", "transcript", "dataset_row", '"prompt"', '"response"', "case-0", "row_id", "group_id"):
        assert forbidden not in lowered, f"forbidden raw-data marker found in persisted state: {forbidden}"
    safe_persisted_document(json.loads((repo / ".foundry-opt" / "bootstrap.lock.json").read_text(encoding="utf-8")))
    safe_persisted_document(yaml.safe_load(registry_path.read_text(encoding="utf-8")))
    safe_persisted_document(yaml.safe_load(aligned_sidecar_path.read_text(encoding="utf-8")))
    safe_persisted_document(yaml.safe_load(unknown_sidecar_path.read_text(encoding="utf-8")))

    # -- 17. Compact redacted evidence artifact, persisted under the test temp path only. --
    evidence = {
        "schema_version": 1,
        "repository_id": REPOSITORY_ID,
        "runtime_commit": RUNTIME_COMMIT,
        "uv_lock_sha256": UV_LOCK_SHA256,
        "classifications": dict(sorted(classifications.items())),
        "repository_receipt_state": repository_receipt.state,
        "github_created_action_count": len(github_receipt.receipt.created_actions),
        "azure_created_action_count": len(azure_receipt.receipt.created_actions),
        "evaluations_receipt_state": evaluations_receipt.state,
        "split_development_case_count": aligned_sidecar.evaluation_lineage.development_case_count,
        "split_validating_case_count": aligned_sidecar.evaluation_lineage.validating_case_count,
        "safety_evaluator_names": sorted(REQUIRED_SAFETY_EVALUATORS),
        "safety_required_pass_rate": 1.0,
        "aligned_deployment_enabled": aligned_sidecar.deployment.enabled,
        "unknown_deployment_enabled": unknown_sidecar.deployment.enabled,
        "github_idempotent_created_count": len(github_idempotent_receipt.receipt.created_actions),
        "azure_idempotent_created_count": len(azure_idempotent_receipt.receipt.created_actions),
        "drift_refusal_observed": True,
        "github_rollback_state": rolled_github.state,
        "azure_rollback_state": rolled_azure.state,
        "replacement_failure_kept_previous_sha256": retained_after_failure.default_evaluator_bundle == previous_sidecar.default_evaluator_bundle,
        "replacement_success_lifecycle_status": replace_entry.lifecycle_status,
        "changed_root_matrix_ids": sorted(entry.repo_agent_id for entry in matrix_only_aligned),
    }
    safe_persisted_document(evidence)
    evidence_path = tmp_path / "evidence" / "bootstrap-e2e-evidence.json"
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")

    reloaded_evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert reloaded_evidence["classifications"] == {ALIGNED_ROOT: "bound-aligned", UNKNOWN_ROOT: "bound-unknown", UNBOUND_ROOT: "ready-unbound"}
    assert reloaded_evidence["repository_receipt_state"] == "applied"
    assert reloaded_evidence["github_created_action_count"] > 0
    assert reloaded_evidence["azure_created_action_count"] > 0
    assert reloaded_evidence["evaluations_receipt_state"] == "applied"
    assert (reloaded_evidence["split_development_case_count"], reloaded_evidence["split_validating_case_count"]) == (20, 10)
    assert reloaded_evidence["safety_evaluator_names"] == sorted(REQUIRED_SAFETY_EVALUATORS)
    assert reloaded_evidence["aligned_deployment_enabled"] is True
    assert reloaded_evidence["unknown_deployment_enabled"] is False
    assert reloaded_evidence["github_idempotent_created_count"] == 0
    assert reloaded_evidence["azure_idempotent_created_count"] == 0
    assert reloaded_evidence["github_rollback_state"] == "rolled_back"
    assert reloaded_evidence["azure_rollback_state"] == "rolled_back"
    assert reloaded_evidence["replacement_failure_kept_previous_sha256"] is True
    assert reloaded_evidence["replacement_success_lifecycle_status"].startswith("replaced:")
    assert reloaded_evidence["changed_root_matrix_ids"] == [ALIGNED_ID]
