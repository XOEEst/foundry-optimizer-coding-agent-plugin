from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
import yaml

from foundry_opt.optimizer_runtime import (
    FoundryOptRuntime,
    OptimizerRuntime,
    OptimizerRuntimeError,
    OptimizerRuntimeSession,
    RUNTIME_CAPABILITIES,
    _rfc8785_bytes,
)
from foundry_opt.optimizer_adapters import (
    FoundryEvaluationAdapter,
    FoundryHostedTargetAdapter,
    FoundryPromptTargetAdapter,
)

LEGACY_ASSETS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "foundry_opt"
    / "legacy_runtime_assets"
)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
        },
    )
    return completed.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, Path, str]:
    repository = tmp_path / "repository"
    agent_root = repository / "agents" / "alpha"
    (agent_root / "src").mkdir(parents=True)
    (agent_root / "src" / "agent.txt").write_text(
        "baseline\n",
        encoding="utf-8",
    )
    (repository / ".gitignore").write_text(
        "experiment_tracking/\n",
        encoding="utf-8",
    )
    _git(repository, "init")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "baseline")
    return repository, agent_root, _git(repository, "rev-parse", "HEAD")


def _contract(
    runtime: FoundryOptRuntime,
    repository: Path,
    agent_root: Path,
    trusted_root: Path,
    base_commit: str,
    *,
    agent_scope_id: str = "alpha",
) -> dict[str, object]:
    agent_relative_root = agent_root.relative_to(repository).as_posix()
    runtime_manifest = LEGACY_ASSETS / "runtime-backend.yaml"
    manifest_content = runtime_manifest.read_text(encoding="utf-8")
    manifest_content = manifest_content.replace("\r\n", "\n").replace("\r", "\n")
    if not manifest_content.endswith("\n"):
        manifest_content += "\n"
    document: dict[str, object] = {
        "schema_version": 1,
        "run_id": "run-one",
        "runtime": {
            **runtime.identity.model_dump(mode="json"),
            "manifest_hash": (
                "sha256:"
                + hashlib.sha256(manifest_content.encode("utf-8")).hexdigest()
            ),
            "capabilities": RUNTIME_CAPABILITIES,
        },
        "adapters": {
            "workspace": {
                "id": "git-worktree",
                "version": "1.0.0",
                "manifest_status": "implemented",
                "implementation_id": "foundry-opt.git-worktree",
                "implementation_version": "1.0.0",
                "resolved_config": {
                    "repository_root": str(repository),
                    "agent_root": str(agent_root),
                    "agent_scope_id": agent_scope_id,
                    "trusted_state_root": str(trusted_root),
                    "base_commit": base_commit,
                    "source_root": agent_relative_root,
                    "editable_patterns": [f"{agent_relative_root}/**"],
                    "protected_patterns": [
                        ".git/**",
                        f"{agent_relative_root}/experiment_tracking/**",
                    ],
                },
            }
        },
        "storage": {
            "repository_root": str(repository),
            "agent_root": str(agent_root),
            "agent_scope_id": agent_scope_id,
            "trusted_state_root": str(trusted_root),
            "layout": {
                "public_tracking_directory": "experiment_tracking",
                "public_runs_directory": "runs",
                "contract_filename": "contract.json",
                "workspace_artifact_directory": "workspace-artifacts",
            },
        },
        "canonicalization": "rfc8785",
    }
    document["contract_hash"] = (
        "sha256:" + hashlib.sha256(_rfc8785_bytes(document)).hexdigest()
    )
    return document


def _normalized_hash(path: Path) -> str:
    content = path.read_text(encoding="utf-8")
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    if not content.endswith("\n"):
        content += "\n"
    return "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest()


def _compiled_adapter(
    manifest_path: Path,
    config: dict[str, object],
) -> dict[str, object]:
    manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    return {
        "id": manifest["id"],
        "version": manifest["version"],
        "manifest_status": manifest["status"],
        "implementation_id": manifest["implementation"]["id"],
        "implementation_version": manifest["implementation"]["version"],
        "manifest_hash": _normalized_hash(manifest_path),
        "config_hash": (
            "sha256:" + hashlib.sha256(_rfc8785_bytes(config)).hexdigest()
        ),
        "capabilities": manifest["capabilities"],
        "resolved_config": config,
    }


def test_foundry_runtime_opens_frozen_contract_and_preserves_parent_lineage(
    tmp_path: Path,
) -> None:
    repository, agent_root, base_commit = _repository(tmp_path)
    trusted_root = tmp_path / "trusted"
    runtime = FoundryOptRuntime()
    contract = _contract(
        runtime,
        repository,
        agent_root,
        trusted_root,
        base_commit,
    )

    session = runtime.open_run(contract)

    assert isinstance(runtime, OptimizerRuntime)
    assert isinstance(session, OptimizerRuntimeSession)
    assert session.paths.agent_root == agent_root
    assert session.paths.agent_scope_id == "alpha"
    assert session.paths.contract_path == (
        trusted_root / "alpha" / "run-one" / "contract.json"
    )
    assert session.paths.public_run_root == (
        agent_root / "experiment_tracking" / "runs" / "run-one"
    )
    assert json.loads(
        session.paths.contract_path.read_text(encoding="utf-8")
    ) == contract

    parent = session.prepare_candidate(
        "candidate-one",
        model="frozen-model",
        hypothesis="Make the first bounded instruction change.",
    )
    (parent.workspace_path / "agents" / "alpha" / "src" / "agent.txt").write_text(
        "parent\n",
        encoding="utf-8",
    )
    parent_finalized = session.finalize_candidate("candidate-one")

    child = session.prepare_candidate(
        "candidate-two",
        model="frozen-model",
        hypothesis="Refine the parent instruction.",
        parent_id="candidate-one",
    )
    assert child.origin_commit == parent_finalized.candidate_commit
    assert (
        child.workspace_path / "agents" / "alpha" / "src" / "agent.txt"
    ).read_text(encoding="utf-8") == "parent\n"
    (child.workspace_path / "agents" / "alpha" / "src" / "agent.txt").write_text(
        "child\n",
        encoding="utf-8",
    )
    child_finalized = session.finalize_candidate("candidate-two")

    assert child_finalized.parent_id == "candidate-one"
    assert child_finalized.origin_commit == parent_finalized.candidate_commit
    assert child_finalized.patch_path.parent == (
        trusted_root
        / "alpha"
        / "run-one"
        / "workspace-artifacts"
        / "candidate-two"
    )


def test_foundry_runtime_rejects_backend_drift_and_unsafe_storage(
    tmp_path: Path,
) -> None:
    repository, agent_root, base_commit = _repository(tmp_path)
    runtime = FoundryOptRuntime()
    contract = _contract(
        runtime,
        repository,
        agent_root,
        tmp_path / "trusted",
        base_commit,
    )

    contract["runtime"]["backend_id"] = "another-runtime"
    body = {key: value for key, value in contract.items() if key != "contract_hash"}
    contract["contract_hash"] = (
        "sha256:" + hashlib.sha256(_rfc8785_bytes(body)).hexdigest()
    )
    with pytest.raises(OptimizerRuntimeError, match="backend_id"):
        runtime.open_run(contract)

    unsafe = _contract(
        runtime,
        repository,
        agent_root,
        repository / ".optimizer-state",
        base_commit,
    )
    with pytest.raises(OptimizerRuntimeError, match="outside repository_root"):
        runtime.open_run(unsafe)


def test_foundry_runtime_requires_public_tracking_to_be_ignored(
    tmp_path: Path,
) -> None:
    repository, agent_root, base_commit = _repository(tmp_path)
    (repository / ".gitignore").write_text("", encoding="utf-8")
    _git(repository, "add", ".gitignore")
    _git(repository, "commit", "-m", "remove ignore")
    base_commit = _git(repository, "rev-parse", "HEAD")
    runtime = FoundryOptRuntime()
    contract = _contract(
        runtime,
        repository,
        agent_root,
        tmp_path / "trusted",
        base_commit,
    )

    with pytest.raises(OptimizerRuntimeError, match="must be ignored"):
        runtime.open_run(contract)


def test_foundry_runtime_isolates_agents_with_the_same_run_id(
    tmp_path: Path,
) -> None:
    repository, alpha_root, _ = _repository(tmp_path)
    beta_root = repository / "agents" / "beta"
    (beta_root / "src").mkdir(parents=True)
    (beta_root / "src" / "agent.txt").write_text(
        "baseline\n",
        encoding="utf-8",
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "add beta agent")
    base_commit = _git(repository, "rev-parse", "HEAD")
    trusted_root = tmp_path / "trusted"
    runtime = FoundryOptRuntime()

    alpha = runtime.open_run(
        _contract(
            runtime,
            repository,
            alpha_root,
            trusted_root,
            base_commit,
            agent_scope_id="alpha",
        )
    )
    beta = runtime.open_run(
        _contract(
            runtime,
            repository,
            beta_root,
            trusted_root,
            base_commit,
            agent_scope_id="beta",
        )
    )

    assert alpha.paths.public_run_root != beta.paths.public_run_root
    assert alpha.paths.trusted_run_root != beta.paths.trusted_run_root
    assert alpha.paths.trusted_run_root == trusted_root / "alpha" / "run-one"
    assert beta.paths.trusted_run_root == trusted_root / "beta" / "run-one"
    assert json.loads(
        (trusted_root / "alpha" / "scope.json").read_text(encoding="utf-8")
    )["agent_root"] == str(alpha_root.resolve())
    assert json.loads(
        (trusted_root / "beta" / "scope.json").read_text(encoding="utf-8")
    )["agent_root"] == str(beta_root.resolve())

    conflicting_contract = _contract(
        runtime,
        repository,
        beta_root,
        trusted_root,
        base_commit,
        agent_scope_id="alpha",
    )
    with pytest.raises(OptimizerRuntimeError, match="different agent root"):
        runtime.open_run(conflicting_contract)


def test_runtime_rfc8785_matches_frozen_vectors() -> None:
    canonical = _rfc8785_bytes({"b": 1, "a": "x"})
    assert canonical == b'{"a":"x","b":1}'
    assert (
        hashlib.sha256(canonical).hexdigest()
        == "cdab067e9f3beb32d1252cfd63e492592fecbf591b0d08cadb24bb17f3864246"
    )


def test_runtime_manifest_matches_loaded_implementation() -> None:
    manifest = yaml.safe_load(
        (LEGACY_ASSETS / "runtime-backend.yaml").read_text(encoding="utf-8")
    )
    identity = FoundryOptRuntime().identity

    assert manifest["interface"] == {
        "id": identity.interface_id,
        "version": identity.interface_version,
    }
    assert manifest["id"] == identity.backend_id
    assert manifest["version"] == identity.backend_version
    assert manifest["implementation"]["id"] == identity.implementation_id
    assert (
        manifest["implementation"]["version"]
        == identity.implementation_version
    )
    assert (
        f"sha256:{manifest['implementation']['normalized_sha256']}"
        == identity.implementation_hash
    )


def test_runtime_dispatches_exact_foundry_adapter_identities(
    tmp_path: Path,
) -> None:
    repository, agent_root, base_commit = _repository(tmp_path)
    target_manifest = (
        LEGACY_ASSETS
        / "adapters"
        / "targets"
        / "foundry-hosted"
        / "adapter.yaml"
    )
    evaluation_manifest = (
        LEGACY_ASSETS
        / "adapters"
        / "evaluators"
        / "foundry-evaluation"
        / "adapter.yaml"
    )
    target_config = {
        "endpoint_allowlist": ["https://foundry.example"],
        "project_endpoint": (
            "https://foundry.example/api/projects/project-one"
        ),
        "agent_name": "agent-one",
        "baseline_version": "7",
        "baseline_commit": base_commit,
        "draft_mode": "image_definition",
        "credential_provider": "azure_cli",
        "instruction_file": "agents/alpha/src/agent.txt",
        "tool_schema_file": "agents/alpha/src/tools.py",
        "tool_config_key": "tool_schemas",
        "config_environment_variable": "AGENT_CONFIG",
        "model": "gpt-4.1",
    }
    evaluation_config = {
        "endpoint_allowlist": ["https://foundry.example"],
        "project_endpoint": (
            "https://foundry.example/api/projects/project-one"
        ),
        "evaluation_id": "eval-one",
        "evaluator_references": ["quality"],
        "dataset_sources": {
            "search_pool": "opaque:search",
            "validation": "opaque:validation",
        },
        "data_mapping": {"query": "{{item.query}}"},
        "credential_provider": "azure_cli",
    }
    runtime = FoundryOptRuntime(
        target_client_factory=lambda _: object(),  # type: ignore[arg-type]
        evaluation_client_factory=lambda _: object(),
    )
    contract = _contract(
        runtime,
        repository,
        agent_root,
        tmp_path / "trusted",
        base_commit,
    )
    contract["adapters"]["target"] = _compiled_adapter(
        target_manifest,
        target_config,
    )
    contract["adapters"]["evaluation"] = _compiled_adapter(
        evaluation_manifest,
        evaluation_config,
    )
    contract["objective"] = {
        "primary_metric": "avgScore",
        "direction": "maximize",
        "evaluators": [
            {
                "reference": "quality",
                "version": "1",
                "definition_hash": f"sha256:{'1' * 64}",
                "weight": 1.0,
                "normalization": {
                    "type": "linear",
                    "minimum": 0.0,
                    "maximum": 1.0,
                },
            }
        ],
        "guardrails": [],
    }
    body = {
        key: value for key, value in contract.items() if key != "contract_hash"
    }
    contract["contract_hash"] = (
        "sha256:" + hashlib.sha256(_rfc8785_bytes(body)).hexdigest()
    )

    session = runtime.open_run(contract)

    assert isinstance(session.target_adapter, FoundryHostedTargetAdapter)
    assert isinstance(session.evaluation_adapter, FoundryEvaluationAdapter)

    contract["adapters"]["target"]["implementation_id"] = "wrong"
    body = {
        key: value for key, value in contract.items() if key != "contract_hash"
    }
    contract["contract_hash"] = (
        "sha256:" + hashlib.sha256(_rfc8785_bytes(body)).hexdigest()
    )
    with pytest.raises(OptimizerRuntimeError, match="implementation_id"):
        runtime.open_run(contract)


def test_runtime_dispatches_exact_foundry_prompt_identity(
    tmp_path: Path,
) -> None:
    repository, agent_root, base_commit = _repository(tmp_path)
    manifest_path = (
        LEGACY_ASSETS
        / "adapters"
        / "targets"
        / "foundry-prompt"
        / "adapter.yaml"
    )
    config = {
        "endpoint_allowlist": ["https://foundry.example"],
        "project_endpoint": (
            "https://foundry.example/api/projects/project-one"
        ),
        "agent_name": "agent-one",
        "baseline_version": "7",
        "baseline_commit": base_commit,
        "credential_provider": "azure_cli",
        "instruction_file": "agents/alpha/src/agent.txt",
        "model": "gpt-4.1",
    }
    runtime = FoundryOptRuntime(
        target_client_factory=lambda _: object(),  # type: ignore[arg-type]
    )
    contract = _contract(
        runtime,
        repository,
        agent_root,
        tmp_path / "trusted",
        base_commit,
    )
    contract["adapters"]["target"] = _compiled_adapter(manifest_path, config)
    body = {
        key: value for key, value in contract.items() if key != "contract_hash"
    }
    contract["contract_hash"] = (
        "sha256:" + hashlib.sha256(_rfc8785_bytes(body)).hexdigest()
    )

    session = runtime.open_run(contract)

    assert isinstance(session.target_adapter, FoundryPromptTargetAdapter)
    contract["adapters"]["target"]["implementation_id"] = "wrong"
    body = {
        key: value for key, value in contract.items() if key != "contract_hash"
    }
    contract["contract_hash"] = (
        "sha256:" + hashlib.sha256(_rfc8785_bytes(body)).hexdigest()
    )
    with pytest.raises(OptimizerRuntimeError, match="implementation_id"):
        runtime.open_run(contract)
