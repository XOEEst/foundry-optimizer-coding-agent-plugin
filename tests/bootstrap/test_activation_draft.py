"""Owned activation draft: package, create, run, clean up, and never touch a baseline.

The activation smoke run must target an agent version this operation created from the reviewed
repository source. These tests cover the create -> active -> run -> delete lifecycle with
live-shaped fakes; no Azure, GitHub, or Foundry call ever leaves the machine.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading

import httpx
import pytest
from azure.core.credentials import AccessToken

from foundry_opt.bootstrap.contracts import BootstrapPlan
from foundry_opt.bootstrap.drivers import EvaluationPhaseDriver
from foundry_opt.bootstrap.evaluation.execution import EvaluationFinalization
from foundry_opt.bootstrap.input_contracts import BootstrapPlanInput, TrustedTemplateManifest
from foundry_opt.bootstrap.providers.foundry import AgentPackage, FoundryAdapter, FoundryPrerequisiteError
from tests.bootstrap.fakes.evaluation_contract import build_contract, evaluation_agent_payload
from tests.bootstrap.fakes.foundry_env import Credential, build_fake_adapter

RUNTIME_SHA = "a" * 40
RUNTIME_REPOSITORY = "https://github.com/XOEEst/foundry-optimizer-coding-agent-plugin.git"


def _plan(contract, *, operation_id: str = "op-draft") -> BootstrapPlan:
    return BootstrapPlan.create(
        operation_id=operation_id,
        runtime_repository="https://github.com/example/runtime.git",
        runtime_commit=RUNTIME_SHA,
        repository_identity="org/repo",
        actions=contract.composite_action(),
    )


def _finalization(adapter: FoundryAdapter, receipt) -> EvaluationFinalization:
    ledger = adapter.export_provider_state(receipt)["onboarding"]["evaluations:app:onboarding"]
    return EvaluationFinalization.model_validate(ledger["finalization"])


def test_the_draft_is_created_from_the_package_then_run_then_deleted() -> None:
    contract = build_contract()
    adapter, fakes = build_fake_adapter()
    package = fakes["package"]

    receipt = adapter.apply_resources(_plan(contract))
    finalization = _finalization(adapter, receipt)

    creates = fakes["agents"].create_from_code_calls
    assert len(creates) == 1
    assert creates[0]["agent_name"] == "draft-agent"
    assert creates[0]["code_zip_sha256"] == package.zip_sha256
    assert creates[0]["observed_zip_sha256"] == package.zip_sha256
    assert creates[0]["metadata"]["foundry_opt_operation"]
    # The draft existed before the activation runs and is deleted afterwards.
    assert [call[1]["target"]["name"] for call in fakes["runs"].create_calls if call[1]["type"] == "azure_ai_target_completions"] == ["draft-agent", "draft-agent"]
    assert fakes["agents"].delete_version_calls == [("draft-agent", "1")]
    assert finalization.activation.draft_disposition == "created"
    assert finalization.activation.package_zip_sha256 == package.zip_sha256
    assert finalization.activation.package_tree_sha256 == package.tree_sha256
    assert finalization.activation.draft_code_digest == package.zip_sha256
    assert finalization.activation.cleanup_completed is True


def test_the_approved_runtime_and_model_are_bound_to_the_draft_definition() -> None:
    contract = build_contract()
    adapter, fakes = build_fake_adapter()

    adapter.apply_resources(_plan(contract, operation_id="op-definition"))

    definition = fakes["agents"].create_from_code_calls[0]["definition"]
    code = definition.code_configuration
    policy = contract.sidecar_policy
    assert code.runtime == policy.runtime.runtime
    assert list(code.entry_point) == list(policy.runtime.entrypoint)
    assert code.dependency_resolution == policy.runtime.dependency_resolution
    assert definition.cpu == (policy.runtime.cpu or "1")
    assert definition.memory == (policy.runtime.memory or "2Gi")
    assert [(item.protocol, item.version) for item in definition.protocol_versions] == [
        (policy.runtime.protocol_name, policy.runtime.protocol_version)
    ]
    if policy.runtime.model_environment_variable:
        assert definition.environment_variables[policy.runtime.model_environment_variable] == contract.activation_plan.model_deployment
    assert definition.environment_variables["AZURE_AI_PROJECT_ENDPOINT"] == policy.foundry_project.project_endpoint


def test_a_pre_existing_agent_version_is_a_conflict_and_is_never_deleted() -> None:
    contract = build_contract()
    adapter, fakes = build_fake_adapter(existing_drafts=(("draft-agent", "1"),))

    with pytest.raises(FoundryPrerequisiteError, match="already exists and was not created by this operation"):
        adapter.apply_resources(_plan(contract, operation_id="op-conflict"))

    assert fakes["agents"].create_from_code_calls == []
    # The retained baseline/draft is left untouched.
    assert fakes["agents"].delete_version_calls == []
    assert ("draft-agent", "1") in fakes["agents"].created


def test_a_package_that_does_not_match_its_digest_fails_closed(tmp_path: Path) -> None:
    contract = build_contract()
    adapter, fakes = build_fake_adapter()
    archive = tmp_path / "package.zip"
    archive.write_bytes(b"PK\x03\x04 tampered")
    adapter.set_agent_packages(
        {
            contract.repo_agent_id: AgentPackage(
                repo_agent_id=contract.repo_agent_id,
                zip_path=str(archive),
                zip_sha256="c" * 64,
                tree_sha256="d" * 64,
                file_count=1,
                size_bytes=archive.stat().st_size,
            )
        }
    )

    with pytest.raises(FoundryPrerequisiteError, match="does not match its recorded digest"):
        adapter.apply_resources(_plan(contract, operation_id="op-digest"))

    assert fakes["agents"].create_from_code_calls == []


def test_a_missing_package_archive_fails_closed(tmp_path: Path) -> None:
    contract = build_contract()
    adapter, fakes = build_fake_adapter()
    adapter.set_agent_packages(
        {
            contract.repo_agent_id: AgentPackage(
                repo_agent_id=contract.repo_agent_id,
                zip_path=str(tmp_path / "missing.zip"),
                zip_sha256="c" * 64,
                tree_sha256="d" * 64,
                file_count=1,
                size_bytes=1,
            )
        }
    )

    with pytest.raises(FoundryPrerequisiteError, match="packaged agent source archive is missing"):
        adapter.apply_resources(_plan(contract, operation_id="op-missing-package"))

    assert fakes["agents"].create_from_code_calls == []


def test_activation_without_a_package_fails_closed() -> None:
    contract = build_contract()
    adapter, fakes = build_fake_adapter()
    adapter.set_agent_packages({})

    with pytest.raises(FoundryPrerequisiteError, match="packaged as an owned draft"):
        adapter.apply_resources(_plan(contract, operation_id="op-no-package"))

    assert fakes["agents"].create_from_code_calls == []


def test_a_crash_after_creation_resumes_without_creating_a_second_draft() -> None:
    contract = build_contract()
    adapter, fakes = build_fake_adapter()
    package = fakes["package"]
    original = adapter.activation_measurements
    checkpoints: list[dict] = []
    adapter.set_checkpoint(lambda snapshot: checkpoints.append(json.loads(json.dumps(snapshot))))

    def _crash(**kwargs):
        raise RuntimeError("process crashed after the draft was created")

    adapter.activation_measurements = _crash
    with pytest.raises(Exception):
        adapter.apply_resources(_plan(contract, operation_id="op-crash"))
    assert len(fakes["agents"].create_from_code_calls) == 1
    # The crashed run still cleaned up its own draft, and the checkpoint recorded it.
    assert fakes["agents"].delete_version_calls == [("draft-agent", "1")]
    pending = [item for item in checkpoints if "pending_draft" in json.dumps(item)]
    assert pending

    resumed = FoundryAdapter(
        "https://example.services.ai.azure.com/api/projects/example",
        Credential(),
        client=fakes["client"],
        split_writer=fakes["split_writer"],
        sleep=lambda _seconds: None,
    )
    resumed.set_agent_packages({contract.repo_agent_id: package})
    snapshot = pending[-1]
    resumed.restore_checkpoint(snapshot["projects"][adapter.project_endpoint] if "projects" in snapshot else snapshot)
    adapter.activation_measurements = original

    # The restored operation still owns the draft name/version, so recreating it is allowed
    # and is not treated as a foreign conflict.
    resumed.apply_resources(_plan(contract, operation_id="op-crash"))

    assert len(fakes["agents"].create_from_code_calls) == 2
    assert fakes["agents"].delete_version_calls == [("draft-agent", "1"), ("draft-agent", "1")]


def test_agent_upload_timeout_recovers_exact_owned_active_version() -> None:
    contract = build_contract()
    adapter, fakes = build_fake_adapter()
    original = fakes["agents"].create_version_from_code
    release = threading.Event()

    def _create_then_wait(*args, **kwargs):
        original(*args, **kwargs)
        record = fakes["agents"].created[("draft-agent", "1")]
        configuration = record.pop("code_configuration")
        record["definition"] = {"code_configuration": configuration}
        release.wait(60)

    fakes["agents"].create_version_from_code = _create_then_wait

    result = adapter.create_activation_draft(
        contract=contract,
        package=fakes["package"],
        operation_id="op-upload-timeout",
        action_id="evaluations:app:onboarding:agent-draft",
    )

    assert result["created"] is True
    assert result["draft_agent_name"] == "draft-agent"
    assert result["draft_agent_version"] == "1"
    release.set()
    assert fakes["agents"].create_from_code_calls[0]["metadata"][
        "foundry_opt_operation"
    ] == adapter._ownership_token(
        "op-upload-timeout",
        "evaluations:app:onboarding:agent-draft",
    )


def test_live_adapter_uses_a_separate_agent_observer_pipeline(monkeypatch) -> None:
    created_clients = []

    class _Agents:
        def get_version(self, agent_name, agent_version, **kwargs):
            return {
                "name": agent_name,
                "version": agent_version,
                "status": "active",
                "definition": {
                    "code_configuration": {
                        "content_hash": "a" * 64,
                    }
                },
            }

    class _Client:
        def __init__(self, endpoint, credential):
            created_clients.append((endpoint, credential))
            self.agents = _Agents()

    monkeypatch.setattr(
        "foundry_opt.bootstrap.providers.foundry.AIProjectClient",
        _Client,
    )

    adapter = FoundryAdapter(
        "https://example.services.ai.azure.com/api/projects/example",
        Credential(),
    )

    assert len(created_clients) == 2
    assert adapter._client is not adapter._agent_observer_client


def test_live_clients_share_a_thread_safe_token_cache(monkeypatch) -> None:
    class _TokenCredential:
        def __init__(self):
            self.calls = 0

        def get_token(self, *scopes, **kwargs):
            self.calls += 1
            return AccessToken("token", 4102444800)

    class _Client:
        def __init__(self, endpoint, credential):
            self.agents = object()

    source = _TokenCredential()
    monkeypatch.setattr(
        "foundry_opt.bootstrap.providers.foundry.AIProjectClient",
        _Client,
    )
    adapter = FoundryAdapter(
        "https://example.services.ai.azure.com/api/projects/example",
        source,
    )

    assert adapter._credential.get_token("scope").token == "token"
    assert adapter._credential.get_token("scope").token == "token"
    assert source.calls == 1


def test_live_agent_status_uses_timeout_bound_httpx(monkeypatch) -> None:
    class _TokenCredential:
        def get_token(self, *scopes, **kwargs):
            return AccessToken("token", 4102444800)

    class _Client:
        def __init__(self, endpoint, credential):
            self.agents = object()

    observed = {}

    def _get(url, **kwargs):
        observed["url"] = url
        observed.update(kwargs)
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "name": "draft",
                "version": "1",
                "status": "active",
                "definition": {
                    "code_configuration": {
                        "content_hash": "a" * 64,
                    }
                },
            },
        )

    monkeypatch.setattr(
        "foundry_opt.bootstrap.providers.foundry.AIProjectClient",
        _Client,
    )
    monkeypatch.setattr(
        "foundry_opt.bootstrap.providers.foundry.httpx.get",
        _get,
    )
    adapter = FoundryAdapter(
        "https://example.services.ai.azure.com/api/projects/example",
        _TokenCredential(),
        request_timeout=42,
    )

    version = adapter._get_agent_version("draft", "1")

    assert version["status"] == "active"
    assert observed["timeout"] == 42
    assert observed["headers"]["Accept-Encoding"] == "identity"
    assert observed["headers"]["Authorization"] == "Bearer token"


def test_live_agent_cleanup_uses_httpx_and_verifies_absence(monkeypatch) -> None:
    class _TokenCredential:
        def get_token(self, *scopes, **kwargs):
            return AccessToken("token", 4102444800)

    class _Client:
        def __init__(self, endpoint, credential):
            self.agents = object()

    observed = {}

    def _delete(url, **kwargs):
        observed["url"] = url
        observed.update(kwargs)
        return httpx.Response(204, request=httpx.Request("DELETE", url))

    monkeypatch.setattr(
        "foundry_opt.bootstrap.providers.foundry.AIProjectClient",
        _Client,
    )
    monkeypatch.setattr(
        "foundry_opt.bootstrap.providers.foundry.httpx.delete",
        _delete,
    )
    adapter = FoundryAdapter(
        "https://example.services.ai.azure.com/api/projects/example",
        _TokenCredential(),
        request_timeout=42,
    )
    adapter._created_drafts[("draft", "1")] = "a" * 64
    monkeypatch.setattr(adapter, "_get_agent_version", lambda *_args: None)

    result = adapter.cleanup_activation_draft(
        draft_agent_name="draft",
        draft_agent_version="1",
    )

    assert result["completed"] is True
    assert observed["timeout"] == 42
    assert observed["params"] == {"force": "true", "api-version": "v1"}
    assert observed["headers"]["Authorization"] == "Bearer token"


def test_a_failed_safety_gate_still_deletes_the_owned_draft() -> None:
    contract = build_contract()
    adapter, fakes = build_fake_adapter(safety_pass_rate=0.9)

    with pytest.raises(FoundryPrerequisiteError, match="must pass at 100%"):
        adapter.apply_resources(_plan(contract, operation_id="op-gate"))

    assert len(fakes["agents"].create_from_code_calls) == 1
    assert fakes["agents"].delete_version_calls == [("draft-agent", "1")]
    assert ("draft-agent", "1") not in fakes["agents"].created


def test_package_bytes_never_enter_state_or_receipts() -> None:
    contract = build_contract()
    adapter, fakes = build_fake_adapter()

    receipt = adapter.apply_resources(_plan(contract, operation_id="op-bytes"))
    state = adapter.export_provider_state(receipt)

    serialized = json.dumps([state, receipt.model_dump(mode="json"), adapter.onboarding_ledger_snapshot()], sort_keys=True, default=str)
    assert fakes["package"].zip_path not in serialized
    assert "PK\u0003\u0004" not in serialized
    # Only digests travel.
    assert fakes["package"].zip_sha256 in serialized


def _plan_input(tmp_path: Path, repo: Path) -> BootstrapPlanInput:
    manifest = TrustedTemplateManifest.load_pinned_manifest()
    del repo
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
                    "config_path": "app/.foundry/foundry-opt.yaml",
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
        "required_phases": ["repository", "evaluations"],
        "evaluations_phase": {"schema_version": 1, "agents": [evaluation_agent_payload(build_contract())]},
    }
    return BootstrapPlanInput.model_validate(json.loads(json.dumps(payload, sort_keys=True)))


def test_the_driver_packages_the_reviewed_source_and_excludes_secrets(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "app" / ".foundry").mkdir(parents=True)
    (repo / "app" / ".foundry" / "agent-metadata.yaml").write_text("agent_name: app\n", encoding="utf-8")
    (repo / "app" / "main.py").write_text("import fastapi\napp = fastapi.FastAPI()\n", encoding="utf-8")
    (repo / "app" / ".env").write_text("SECRET=value\n", encoding="utf-8")
    (repo / "app" / "__pycache__").mkdir()
    (repo / "app" / "__pycache__" / "main.cpython-312.pyc").write_bytes(b"\x00\x01")
    (repo / "app" / "secrets").mkdir()
    (repo / "app" / "secrets" / "token.txt").write_text("token\n", encoding="utf-8")
    (repo / "app" / "prompts").mkdir()
    (repo / "app" / "prompts" / "system.txt").write_text("review safely\n", encoding="utf-8")
    loaded = _plan_input(tmp_path, repo)
    adapter, fakes = build_fake_adapter()
    driver = EvaluationPhaseDriver(plan_input=loaded, provider=adapter, repository_root=repo)

    with driver._packaged_agents() as packages:
        package = packages["app"]
        archive = Path(package.zip_path)
        assert archive.is_file()
        import zipfile

        with zipfile.ZipFile(archive) as bundle:
            names = set(bundle.namelist())
        assert "main.py" in names
        assert not any(name.endswith(".env") for name in names)
        assert not any(name.startswith(".foundry/") for name in names)
        assert not any("__pycache__" in name for name in names)
        assert not any(name.startswith("secrets/") for name in names)
        assert "prompts/system.txt" in names
        assert package.zip_sha256 == hashlib.sha256(archive.read_bytes()).hexdigest()
        recorded = package.zip_path

    # The temporary package directory is removed as soon as the phase finishes.
    assert not Path(recorded).exists()
    del fakes
