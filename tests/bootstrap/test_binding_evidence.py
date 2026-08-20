"""Observed binding evidence: contract, discovery classification, adapter, and CLI.

A deployed baseline can only be classified as `bound-aligned` when reviewed, non-secret
evidence proves the deployed immutable agent version runs the repository's current content.
Metadata alone (endpoint/name/version) is never sufficient.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from foundry_opt.bootstrap.discovery import (
    discover_repository_agents,
    fingerprint_content_sha256,
    fingerprint_files,
    is_fingerprintable_path,
)
from foundry_opt.bootstrap.errors import BootstrapConfigError
from foundry_opt.bootstrap.input_contracts import (
    BindingEvidenceInput,
    BootstrapPlanInput,
    TrustedTemplateManifest,
    load_binding_evidence_input,
)
from foundry_opt.bootstrap.providers.foundry import FoundryPrerequisiteError, FoundryUnsupportedCapabilityError
from foundry_opt.bootstrap.operation_state import read_operation_state
from foundry_opt.bootstrap import drivers
from foundry_opt.cli import app
from tests.bootstrap.fakes.evaluation_contract import build_contract, evaluation_agent_payload
from tests.bootstrap.fakes.foundry_env import PROJECT_ENDPOINT, build_code_archive, build_fake_adapter, fake_credential

CONTRACT_ERRORS = (BootstrapConfigError, ValidationError)
RUNTIME_SHA = "a" * 40
SELECTION = ({"root": "app", "repoAgentId": "app"},)
runner = CliRunner()


def _repo(tmp_path: Path, *, agent_name: str = "example-agent", version: str = "1") -> Path:
    repo = tmp_path / "repo"
    (repo / "app" / ".foundry").mkdir(parents=True)
    (repo / "app" / ".foundry" / "agent-metadata.yaml").write_text(
        "\n".join(
            (
                f"agent_name: {agent_name}",
                "source_root: app",
                "package_root: app",
                f"project_endpoint: {PROJECT_ENDPOINT}",
                f'expected_version: "{version}"',
                "",
            )
        ),
        encoding="utf-8",
    )
    (repo / "app" / "main.py").write_text("import fastapi\napp = fastapi.FastAPI()\n", encoding="utf-8")
    return repo


def _discover(repo: Path, evidence: dict[str, dict[str, str]] | None = None):
    return discover_repository_agents(repo, selected_agents=SELECTION, binding_evidence_by_root=evidence)


def _observe(repo: Path, *, agent_name: str = "example-agent", version: str = "1", **kwargs):
    archive = build_code_archive(repo / "app")
    adapter, fakes = build_fake_adapter(code_archive=archive, code_content_hash=hashlib.sha256(archive).hexdigest(), **kwargs)
    observation = adapter.observe_agent_binding(agent_name=agent_name, agent_version=version, source_root="app", package_root="app")
    return observation, adapter, fakes


def _evidence_payload(observation, *, root: str = "app", repo_agent_id: str = "app", agent_name: str = "example-agent", version: str = "1") -> dict[str, object]:
    return {
        "schema_version": 1,
        "root": root,
        "repo_agent_id": repo_agent_id,
        "project_endpoint": PROJECT_ENDPOINT,
        "agent_name": agent_name,
        "agent_version": version,
        "source_fingerprint": observation["source_fingerprint"],
        "package_fingerprint": observation["package_fingerprint"],
        "evidence_provenance": "foundry_agent_code_download",
        "code_content_hash": observation["code_content_hash"],
        "code_content_hash_verified": True,
        "observed_at": "2026-08-17T00:00:00Z",
    }


def _document(observation, **kwargs) -> BindingEvidenceInput:
    return BindingEvidenceInput.model_validate(
        {
            "schema_version": 1,
            "evidence_version": 1,
            "repository_id": "org/repo",
            "agents": [_evidence_payload(observation, **kwargs)],
        }
    )


def _plan_input_payload(tmp_path: Path, *, evidence: BindingEvidenceInput | None = None, evaluations: bool = True) -> dict[str, object]:
    manifest = TrustedTemplateManifest.load_pinned_manifest()
    payload: dict[str, object] = {
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
            "runtime_repository_url": "https://github.com/XOEEst/foundry-optimizer-coding-agent-plugin.git",
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
        "required_phases": ["repository"],
    }
    if evaluations:
        payload["evaluations_phase"] = {"schema_version": 1, "agents": [evaluation_agent_payload(build_contract())]}
    if evidence is not None:
        payload["binding_evidence"] = evidence.model_dump(mode="json", exclude_none=True)
    return payload


def _write_plan_input(tmp_path: Path, payload: dict[str, object], *, name: str = "plan-input.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path


def test_downloaded_agent_version_reproduces_the_local_discovery_fingerprints(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    local = _discover(repo).agents[0]
    assert local.bindingAssessment.classification == "bound-unknown"

    observation, _adapter, fakes = _observe(repo)

    assert observation["source_fingerprint"] == local.sourceFingerprint
    assert observation["package_fingerprint"] == local.packageFingerprint
    assert observation["code_content_hash_verified"] is True
    assert fakes["agents"].download_calls == [("example-agent", "1")]


def test_binding_fingerprints_normalize_text_line_endings(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "app" / "main.py").write_bytes(
        b"import fastapi\r\napp = fastapi.FastAPI()\r\n"
    )
    deployed = tmp_path / "deployed"
    deployed.mkdir()
    (deployed / "main.py").write_bytes(
        b"import fastapi\napp = fastapi.FastAPI()\n"
    )
    archive = build_code_archive(deployed)
    adapter, _fakes = build_fake_adapter(
        code_archive=archive,
        code_content_hash=hashlib.sha256(archive).hexdigest(),
    )

    local = _discover(repo).agents[0]
    observation = adapter.observe_agent_binding(
        agent_name="example-agent",
        agent_version="1",
        source_root="app",
        package_root="app",
    )

    assert observation["source_fingerprint"] == local.sourceFingerprint
    assert observation["package_fingerprint"] == local.packageFingerprint
    aligned = _discover(
        repo,
        _document(observation).by_root(),
    )
    assert aligned.agents[0].bindingAssessment.classification == "bound-aligned"


def test_observed_evidence_classifies_a_deployed_baseline_as_bound_aligned(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    observation, _adapter, _fakes = _observe(repo)

    aligned = _discover(repo, _document(observation).by_root())

    assert aligned.agents[0].bindingAssessment.classification == "bound-aligned"
    assert aligned.agents[0].bindingAssessment.detail == "expected and observed binding evidence exactly match local fingerprints"


def test_changed_repository_content_is_bound_diverged(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    observation, _adapter, _fakes = _observe(repo)
    (repo / "app" / "main.py").write_text("import fastapi\napp = fastapi.FastAPI()\n# drift\n", encoding="utf-8")

    diverged = _discover(repo, _document(observation).by_root())

    assessment = diverged.agents[0].bindingAssessment
    assert assessment.classification == "bound-diverged"
    assert "source-fingerprint" in assessment.detail
    assert "package-fingerprint" in assessment.detail


def test_wrong_deployed_version_is_bound_diverged(tmp_path: Path) -> None:
    repo = _repo(tmp_path, version="3")
    observation, _adapter, _fakes = _observe(repo)

    diverged = _discover(repo, _document(observation).by_root())

    assert diverged.agents[0].bindingAssessment.classification == "bound-diverged"
    assert "version" in diverged.agents[0].bindingAssessment.detail


def test_absent_evidence_is_bound_unknown_and_never_aligned(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    unknown = _discover(repo)

    assert unknown.agents[0].bindingAssessment.classification == "bound-unknown"
    assert unknown.agents[0].bindingAssessment.detail == "expected binding exists without observed evidence"


def test_metadata_only_evidence_can_never_be_bound_aligned(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    result = _discover(
        repo,
        {"app": {"project_endpoint": PROJECT_ENDPOINT, "agent_name": "example-agent", "agent_version": "1"}},
    )

    assessment = result.agents[0].bindingAssessment
    assert assessment.classification == "bound-diverged"
    assert assessment.detail == "observed binding evidence lacks both content fingerprints; metadata alone cannot prove alignment"


def test_partial_content_evidence_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    local = _discover(repo).agents[0]

    result = _discover(repo, {"app": {"agent_name": "example-agent", "source_fingerprint": local.sourceFingerprint}})

    assert result.agents[0].bindingAssessment.classification == "bound-diverged"


def test_unsupported_evidence_keys_fail_closed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    local = _discover(repo).agents[0]

    with pytest.raises(BootstrapConfigError, match="unsupported keys"):
        _discover(repo, {"app": {"sourceFingerprint": local.sourceFingerprint}})


def test_malformed_digest_fails_closed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    with pytest.raises(BootstrapConfigError, match="lowercase sha256 hex digest"):
        _discover(repo, {"app": {"source_fingerprint": "not-a-digest", "package_fingerprint": "b" * 64}})


def test_fingerprint_helpers_are_stable_and_path_filtered() -> None:
    files = {"app/main.py": "a" * 64, "app/.foundry/agent-metadata.yaml": "b" * 64}
    assert fingerprint_files(files) == fingerprint_files(dict(reversed(list(files.items()))))
    assert is_fingerprintable_path("app/main.py") is True
    assert is_fingerprintable_path("app/.foundry/agent-metadata.yaml") is False
    assert is_fingerprintable_path("app/__pycache__/main.pyc") is False
    assert is_fingerprintable_path("../escape.py") is False
    with pytest.raises(BootstrapConfigError):
        fingerprint_files({"app/main.py": "nope"})


def test_content_fingerprint_normalizes_text_but_not_binary() -> None:
    assert fingerprint_content_sha256(
        "app/main.py",
        b"line one\r\nline two\r\n",
    ) == fingerprint_content_sha256(
        "app/main.py",
        b"line one\nline two\n",
    )
    assert fingerprint_content_sha256(
        "app/model.bin",
        b"\x00line one\r\n",
    ) != fingerprint_content_sha256(
        "app/model.bin",
        b"\x00line one\n",
    )


def test_evidence_contract_requires_both_content_fingerprints(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    observation, _adapter, _fakes = _observe(repo)
    payload = _evidence_payload(observation)
    del payload["package_fingerprint"]

    with pytest.raises(CONTRACT_ERRORS):
        BindingEvidenceInput.model_validate({"schema_version": 1, "repository_id": "org/repo", "agents": [payload]})


def test_evidence_contract_rejects_unknown_fields_and_duplicates(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    observation, _adapter, _fakes = _observe(repo)

    with pytest.raises(CONTRACT_ERRORS):
        BindingEvidenceInput.model_validate(
            {"schema_version": 1, "repository_id": "org/repo", "agents": [{**_evidence_payload(observation), "commit": "a" * 40}]}
        )
    with pytest.raises(CONTRACT_ERRORS, match="root"):
        BindingEvidenceInput.model_validate(
            {
                "schema_version": 1,
                "repository_id": "org/repo",
                "agents": [_evidence_payload(observation), _evidence_payload(observation, repo_agent_id="other")],
            }
        )
    with pytest.raises(CONTRACT_ERRORS, match="must not be empty"):
        BindingEvidenceInput.model_validate({"schema_version": 1, "repository_id": "org/repo", "agents": []})


def test_evidence_provenance_rules_are_enforced(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    observation, _adapter, _fakes = _observe(repo)

    downloaded_without_hash = {**_evidence_payload(observation)}
    del downloaded_without_hash["code_content_hash"]
    with pytest.raises(CONTRACT_ERRORS, match="immutable code content hash"):
        BindingEvidenceInput.model_validate({"schema_version": 1, "repository_id": "org/repo", "agents": [downloaded_without_hash]})

    unverified = {**_evidence_payload(observation), "code_content_hash_verified": False}
    with pytest.raises(CONTRACT_ERRORS, match="confirm the code content hash"):
        BindingEvidenceInput.model_validate({"schema_version": 1, "repository_id": "org/repo", "agents": [unverified]})

    attested = {**_evidence_payload(observation), "evidence_provenance": "reviewed_operator_attestation"}
    with pytest.raises(CONTRACT_ERRORS, match="only downloaded binding evidence"):
        BindingEvidenceInput.model_validate({"schema_version": 1, "repository_id": "org/repo", "agents": [attested]})

    reviewed = {key: value for key, value in attested.items() if key not in {"code_content_hash", "code_content_hash_verified"}}
    document = BindingEvidenceInput.model_validate({"schema_version": 1, "repository_id": "org/repo", "agents": [reviewed]})
    assert document.agents[0].evidence_provenance == "reviewed_operator_attestation"
    assert document.evidence_hash != ""


def test_plan_input_binds_evidence_to_the_selected_and_reviewed_agent(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    observation, _adapter, _fakes = _observe(repo)
    document = _document(observation)

    plan_input = BootstrapPlanInput.model_validate(_plan_input_payload(tmp_path, evidence=document))
    assert plan_input.binding_evidence is not None
    assert plan_input.binding_evidence.by_root()["app"]["source_fingerprint"] == observation["source_fingerprint"]

    with pytest.raises(CONTRACT_ERRORS, match="must match a selected agent root"):
        BootstrapPlanInput.model_validate(_plan_input_payload(tmp_path, evidence=_document(observation, root="tests")))
    with pytest.raises(CONTRACT_ERRORS, match="reviewed agent name and version"):
        BootstrapPlanInput.model_validate(_plan_input_payload(tmp_path, evidence=_document(observation, version="9")))

    mismatched_repository = _plan_input_payload(tmp_path, evidence=document)
    mismatched_repository["binding_evidence"]["repository_id"] = "org/other"
    with pytest.raises(CONTRACT_ERRORS, match="repository_id must match"):
        BootstrapPlanInput.model_validate(mismatched_repository)

    mismatched_id = _plan_input_payload(tmp_path, evidence=document)
    mismatched_id["binding_evidence"]["agents"][0]["repo_agent_id"] = "other"
    with pytest.raises(CONTRACT_ERRORS, match="repo_agent_id must match"):
        BootstrapPlanInput.model_validate(mismatched_id)


def test_evidence_file_loader_rejects_unparseable_documents(tmp_path: Path) -> None:
    broken = tmp_path / "evidence.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(BootstrapConfigError, match="strict JSON"):
        load_binding_evidence_input(broken)

    with pytest.raises(BootstrapConfigError, match="could not be read"):
        load_binding_evidence_input(tmp_path / "missing.json")


def test_observation_fails_closed_when_the_published_content_hash_disagrees(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    archive = build_code_archive(repo / "app")
    adapter, _fakes = build_fake_adapter(code_archive=archive, code_content_hash="c" * 64)

    with pytest.raises(FoundryPrerequisiteError, match="does not match the published content hash"):
        adapter.observe_agent_binding(agent_name="example-agent", agent_version="1", source_root="app", package_root="app")


def test_observation_maps_archive_entries_from_package_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "agents" / "example" / ".foundry").mkdir(parents=True)
    (repo / "agents" / "example" / "app").mkdir(parents=True)
    (repo / "agents" / "example" / "app" / "main.py").write_text(
        "import fastapi\napp = fastapi.FastAPI()\n",
        encoding="utf-8",
    )
    (repo / "agents" / "example" / "pyproject.toml").write_text(
        "[project]\nname='agent'\nversion='1.0.0'\n",
        encoding="utf-8",
    )
    (repo / "agents" / "example" / ".foundry" / "agent-metadata.yaml").write_text(
        "agent_name: example-agent\n"
        "source_root: agents/example/app\n"
        "package_root: agents/example\n"
        "expected_version: '1'\n",
        encoding="utf-8",
    )
    expected_source = fingerprint_files(
        {
            "agents/example/app/main.py": fingerprint_content_sha256(
                "agents/example/app/main.py",
                (repo / "agents" / "example" / "app" / "main.py").read_bytes(),
            )
        }
    )
    expected_package = fingerprint_files(
        {
            "agents/example/app/main.py": fingerprint_content_sha256(
                "agents/example/app/main.py",
                (repo / "agents" / "example" / "app" / "main.py").read_bytes(),
            ),
            "agents/example/pyproject.toml": fingerprint_content_sha256(
                "agents/example/pyproject.toml",
                (repo / "agents" / "example" / "pyproject.toml").read_bytes(),
            ),
        }
    )
    archive = build_code_archive(repo / "agents" / "example")
    adapter, _fakes = build_fake_adapter(
        code_archive=archive,
        code_content_hash=hashlib.sha256(archive).hexdigest(),
        agent_versions={
            ("example-agent", "1"): {
                "name": "example-agent",
                "version": "1",
                "definition": {
                    "code_configuration": {
                        "content_hash": hashlib.sha256(archive).hexdigest()
                    }
                },
            }
        },
    )

    observation = adapter.observe_agent_binding(
        agent_name="example-agent",
        agent_version="1",
        source_root="agents/example/app",
        package_root="agents/example",
    )

    assert observation["code_content_hash_verified"] is True
    assert observation["source_fingerprint"] == expected_source
    assert observation["package_fingerprint"] == expected_package


def test_observation_rejects_unreadable_or_empty_archives(tmp_path: Path) -> None:
    adapter, _fakes = build_fake_adapter(code_archive=b"not-a-zip-archive")
    with pytest.raises(FoundryPrerequisiteError, match="not a readable zip archive"):
        adapter.observe_agent_binding(agent_name="example-agent", agent_version="1", source_root="app", package_root="app")

    missing, _fakes = build_fake_adapter()
    with pytest.raises(FoundryPrerequisiteError, match="was not found"):
        missing.observe_agent_binding(agent_name="example-agent", agent_version="1", source_root="app", package_root="app")


def test_observation_ignores_traversing_and_non_fingerprintable_archive_entries(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    local = _discover(repo).agents[0]
    archive = build_code_archive(
        repo / "app",
        extra={"../escape.py": b"print('escape')\n", "__pycache__/main.cpython-312.pyc": b"\x00\x01", "/abs.py": b"print('abs')\n"},
    )
    adapter, _fakes = build_fake_adapter(code_archive=archive, code_content_hash=hashlib.sha256(archive).hexdigest())

    observation = adapter.observe_agent_binding(agent_name="example-agent", agent_version="1", source_root="app", package_root="app")

    assert observation["source_fingerprint"] == local.sourceFingerprint


def test_observation_requires_the_requested_immutable_version(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    archive = build_code_archive(repo / "app")
    adapter, _fakes = build_fake_adapter(
        code_archive=archive,
        agent_versions={("example-agent", "1"): {"name": "example-agent", "version": "2", "code_configuration": {}}},
    )

    with pytest.raises(FoundryPrerequisiteError, match="does not match the requested version"):
        adapter.observe_agent_binding(agent_name="example-agent", agent_version="1", source_root="app", package_root="app")


def test_observation_requires_a_download_capable_project(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    archive = build_code_archive(repo / "app")
    adapter, fakes = build_fake_adapter(code_archive=archive)

    class _AgentsWithoutDownload:
        def get_version(self, agent_name: str, agent_version: str, **kwargs: object) -> object:
            return fakes["agents"].get_version(agent_name, agent_version)

    fakes["client"].agents = _AgentsWithoutDownload()

    with pytest.raises(FoundryUnsupportedCapabilityError, match="code download is unavailable"):
        adapter.observe_agent_binding(agent_name="example-agent", agent_version="1", source_root="app", package_root="app")


def test_cli_observes_evidence_and_discovers_a_bound_aligned_baseline(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    archive = build_code_archive(repo / "app")
    adapter, _fakes = build_fake_adapter(code_archive=archive, code_content_hash=hashlib.sha256(archive).hexdigest())
    monkeypatch.setattr(drivers, "DefaultAzureCredential", lambda **kwargs: fake_credential())
    monkeypatch.setattr(drivers, "FoundryAdapter", lambda endpoint, credential: adapter)
    plan_input = _write_plan_input(tmp_path, _plan_input_payload(tmp_path))
    evidence_file = tmp_path / "binding-evidence.json"

    observed = runner.invoke(app, ["bootstrap", "binding-evidence", "--repo-root", str(repo), "--plan-input", str(plan_input), "--output", str(evidence_file)])
    assert observed.exit_code == 0, observed.stdout
    written = json.loads(evidence_file.read_text(encoding="utf-8"))
    assert written["agents"][0]["evidence_provenance"] == "foundry_agent_code_download"
    assert written["agents"][0]["code_content_hash_verified"] is True

    discovered = runner.invoke(
        app,
        ["bootstrap", "discover", "--repo-root", str(repo), "--repository-id", "org/repo", "--operation-id", "op-binding", "--state-root", str(tmp_path / "state"), "--plan-input", str(plan_input), "--binding-evidence", str(evidence_file)],
    )
    assert discovered.exit_code == 0, discovered.stdout
    payload = json.loads(discovered.stdout)
    assert payload["binding_evidence_roots"] == ["app"]
    assert [item["classification"] for item in payload["candidates"]] == ["bound-aligned"]


def test_cli_discovers_nested_plan_input_evidence(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    observation, _adapter, _fakes = _observe(repo)
    plan_input = _write_plan_input(tmp_path, _plan_input_payload(tmp_path, evidence=_document(observation)))

    discovered = runner.invoke(
        app,
        ["bootstrap", "discover", "--repo-root", str(repo), "--repository-id", "org/repo", "--operation-id", "op-nested", "--state-root", str(tmp_path / "state"), "--plan-input", str(plan_input)],
    )

    assert discovered.exit_code == 0, discovered.stdout
    payload = json.loads(discovered.stdout)
    assert [item["classification"] for item in payload["candidates"]] == ["bound-aligned"]
    assert payload["binding_evidence_hash"]


def test_cli_refuses_conflicting_or_foreign_evidence(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    observation, _adapter, _fakes = _observe(repo)
    document = _document(observation)
    evidence_file = tmp_path / "binding-evidence.json"
    evidence_file.write_text(json.dumps(document.model_dump(mode="json", exclude_none=True), sort_keys=True), encoding="utf-8")
    nested = _write_plan_input(tmp_path, _plan_input_payload(tmp_path, evidence=document), name="plan-input-nested.json")
    plain = _write_plan_input(tmp_path, _plan_input_payload(tmp_path))

    conflict = runner.invoke(
        app,
        ["bootstrap", "discover", "--repo-root", str(repo), "--repository-id", "org/repo", "--operation-id", "op-conflict", "--state-root", str(tmp_path / "state"), "--plan-input", str(nested), "--binding-evidence", str(evidence_file)],
    )
    assert conflict.exit_code != 0
    assert json.loads(conflict.stdout)["error"]["code"] == "binding-evidence-conflict"

    foreign = tmp_path / "foreign.json"
    foreign_payload = document.model_dump(mode="json", exclude_none=True)
    foreign_payload["repository_id"] = "org/other"
    foreign.write_text(json.dumps(foreign_payload, sort_keys=True), encoding="utf-8")
    mismatched = runner.invoke(
        app,
        ["bootstrap", "discover", "--repo-root", str(repo), "--repository-id", "org/repo", "--operation-id", "op-foreign", "--state-root", str(tmp_path / "state"), "--plan-input", str(plain), "--binding-evidence", str(foreign)],
    )
    assert mismatched.exit_code != 0
    assert json.loads(mismatched.stdout)["error"]["code"] == "binding-evidence-repository-mismatch"

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{}", encoding="utf-8")
    broken = runner.invoke(
        app,
        ["bootstrap", "discover", "--repo-root", str(repo), "--repository-id", "org/repo", "--operation-id", "op-malformed", "--state-root", str(tmp_path / "state"), "--plan-input", str(plain), "--binding-evidence", str(malformed)],
    )
    assert broken.exit_code != 0
    assert json.loads(broken.stdout)["status"] == "error"


def test_cli_binding_evidence_requires_evaluation_inputs(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    plan_input = _write_plan_input(tmp_path, _plan_input_payload(tmp_path, evaluations=False))

    result = runner.invoke(app, ["bootstrap", "binding-evidence", "--repo-root", str(repo), "--plan-input", str(plan_input), "--output", str(tmp_path / "evidence.json")])

    assert result.exit_code != 0
    assert json.loads(result.stdout)["error"]["code"] == "binding-evidence-config"


def test_evaluation_plan_verifies_the_approved_binding_claim_against_evidence(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    observation, _adapter, _fakes = _observe(repo)
    plan_input = _write_plan_input(tmp_path, _plan_input_payload(tmp_path, evidence=_document(observation)))

    planned = runner.invoke(app, ["bootstrap", "evaluation", "plan", "--plan-input", str(plan_input), "--repo-root", str(repo)])

    assert planned.exit_code == 0, planned.stdout
    assert json.loads(planned.stdout)["verified_binding_classifications"] == {"app": "bound-aligned"}


def test_evaluation_plan_refuses_a_false_bound_aligned_claim(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    observation, _adapter, _fakes = _observe(repo)
    (repo / "app" / "main.py").write_text("import fastapi\napp = fastapi.FastAPI()\n# drift\n", encoding="utf-8")
    plan_input = _write_plan_input(tmp_path, _plan_input_payload(tmp_path, evidence=_document(observation)))

    planned = runner.invoke(app, ["bootstrap", "evaluation", "plan", "--plan-input", str(plan_input), "--repo-root", str(repo)])

    assert planned.exit_code != 0
    error = json.loads(planned.stdout)["error"]
    assert error["code"] == "binding-classification-mismatch"
    assert error["details"] == {"repo_agent_id": "app", "approved": "bound-aligned", "observed": "bound-diverged"}


def test_evaluation_plan_without_evidence_keeps_the_reviewed_claim(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    plan_input = _write_plan_input(tmp_path, _plan_input_payload(tmp_path))

    planned = runner.invoke(app, ["bootstrap", "evaluation", "plan", "--plan-input", str(plan_input), "--repo-root", str(repo)])

    assert planned.exit_code == 0, planned.stdout
    assert json.loads(planned.stdout)["verified_binding_classifications"] == {}


def test_discover_json_exposes_local_fingerprints_for_evidence_review(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    local = _discover(repo).agents[0]
    plan_input = _write_plan_input(tmp_path, _plan_input_payload(tmp_path))
    state_root = tmp_path / "state"

    discovered = runner.invoke(
        app,
        ["bootstrap", "discover", "--repo-root", str(repo), "--repository-id", "org/repo", "--operation-id", "op-fingerprints", "--state-root", str(state_root), "--plan-input", str(plan_input)],
    )

    assert discovered.exit_code == 0, discovered.stdout
    agents = json.loads(discovered.stdout)["agents"]
    assert len(agents) == 1
    assert agents[0] == {
        "repoAgentId": "app",
        "root": "app",
        "configPath": "app/.foundry/agent-metadata.yaml",
        "sourceRoot": "app",
        "packageRoot": "app",
        "sourceFingerprint": local.sourceFingerprint,
        "packageFingerprint": local.packageFingerprint,
        "classification": "bound-unknown",
        "detail": "expected binding exists without observed evidence",
        "confidence": local.confidence,
        "blockers": [],
        "approvedSharedSourceRepoAgentIds": [],
    }


def test_discovered_records_persist_in_operation_state(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    plan_input = _write_plan_input(tmp_path, _plan_input_payload(tmp_path))
    state_root = tmp_path / "state"
    assert runner.invoke(
        app,
        ["bootstrap", "discover", "--repo-root", str(repo), "--repository-id", "org/repo", "--operation-id", "op-state", "--state-root", str(state_root), "--plan-input", str(plan_input)],
    ).exit_code == 0

    envelope = read_operation_state("org/repo", "op-state", state_root=state_root)

    record = envelope.selection_plan.discovered_agents[0]
    assert (record.repo_agent_id, record.root, record.config_path) == ("app", "app", "app/.foundry/agent-metadata.yaml")
    assert record.source_fingerprint == _discover(repo).agents[0].sourceFingerprint
    assert record.classification == "bound-unknown"


def test_discover_json_reports_blockers_for_unusable_roots(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "app" / ".foundry").mkdir(parents=True)
    (repo / "app" / ".foundry" / "agent-metadata.yaml").write_text(
        f"agent_name: example-agent\nsource_root: app\npackage_root: app\nproject_endpoint: {PROJECT_ENDPOINT}\n",
        encoding="utf-8",
    )
    (repo / "app" / "notes.md").write_text("no entrypoint here\n", encoding="utf-8")
    plan_input = _write_plan_input(tmp_path, _plan_input_payload(tmp_path))

    discovered = runner.invoke(
        app,
        ["bootstrap", "discover", "--repo-root", str(repo), "--repository-id", "org/repo", "--operation-id", "op-blocked", "--state-root", str(tmp_path / "state"), "--plan-input", str(plan_input)],
    )

    assert discovered.exit_code == 0, discovered.stdout
    agent = json.loads(discovered.stdout)["agents"][0]
    assert agent["classification"] == "bound-diverged"
    assert agent["blockers"] == [{"code": "missing-entrypoint", "detail": "binding evidence exists but no supported entrypoint file was found"}]


def test_binding_verification_cannot_be_bypassed_by_skipping_evaluation_plan(tmp_path: Path) -> None:
    """Every approved/apply path re-derives the claim, not just the `evaluation plan` helper."""

    repo = _repo(tmp_path)
    observation, _adapter, _fakes = _observe(repo)
    (repo / "app" / "main.py").write_text("import fastapi\napp = fastapi.FastAPI()\n# drift\n", encoding="utf-8")
    plan_input = _write_plan_input(tmp_path, _plan_input_payload(tmp_path, evidence=_document(observation)))
    state_root = tmp_path / "state"
    approval = tmp_path / "approval.json"
    approval.write_text(json.dumps({"schema_version": 1, "parent_plan_hash": "b" * 64, "phase": "evaluations", "actor": "tester", "summary": "approve", "approval_hash": "c" * 64}), encoding="utf-8")

    invocations = {
        "plan": ["bootstrap", "plan", "--plan-input", str(plan_input), "--repository-id", "org/repo", "--repo-root", str(repo), "--operation-id", "op-bypass", "--state-root", str(state_root)],
        "apply": ["bootstrap", "apply", "--repository-id", "org/repo", "--operation-id", "op-bypass", "--phase", "evaluations", "--approval-file", str(approval), "--plan-input", str(plan_input), "--repo-root", str(repo), "--state-root", str(state_root)],
        "evaluation apply": ["bootstrap", "evaluation", "apply", "--repository-id", "org/repo", "--operation-id", "op-bypass", "--approval-file", str(approval), "--plan-input", str(plan_input), "--repo-root", str(repo), "--state-root", str(state_root)],
        "evaluation activate": ["bootstrap", "evaluation", "activate", "--repository-id", "org/repo", "--operation-id", "op-bypass", "--plan-input", str(plan_input), "--repo-root", str(repo), "--state-root", str(state_root)],
    }
    for label, argv in invocations.items():
        result = runner.invoke(app, argv)
        assert result.exit_code != 0, f"{label} accepted a false bound-aligned claim"
        assert json.loads(result.stdout)["error"]["code"] == "binding-classification-mismatch", label


def test_verified_claims_do_not_block_the_generic_plan_path(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    observation, _adapter, _fakes = _observe(repo)
    plan_input = _write_plan_input(tmp_path, _plan_input_payload(tmp_path, evidence=_document(observation)))
    state_root = tmp_path / "state"
    assert runner.invoke(
        app,
        ["bootstrap", "discover", "--repo-root", str(repo), "--repository-id", "org/repo", "--operation-id", "op-verified", "--state-root", str(state_root), "--plan-input", str(plan_input)],
    ).exit_code == 0

    planned = runner.invoke(
        app,
        ["bootstrap", "plan", "--plan-input", str(plan_input), "--repository-id", "org/repo", "--repo-root", str(repo), "--operation-id", "op-verified", "--state-root", str(state_root)],
    )

    assert planned.exit_code == 0, planned.stdout


def test_persisted_evidence_carries_no_repository_content(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    observation, _adapter, _fakes = _observe(repo)
    document = _document(observation)

    serialized = json.dumps(document.model_dump(mode="json", exclude_none=True))

    assert "fastapi" not in serialized
    assert "main.py" not in serialized
    assert str(repo) not in serialized
