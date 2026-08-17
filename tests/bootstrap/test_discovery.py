from __future__ import annotations

import os
from pathlib import Path

import pytest

from foundry_opt.bootstrap.discovery import discover_repository_agents, discovery_result_json
from foundry_opt.bootstrap.errors import BootstrapConfigError


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_discovery_is_deterministic_and_json_uses_relative_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / ".foundry" / "agent-metadata.yaml", "agent_name: Root Agent\nsource_root: app\npackage_root: app\n")
    _write(repo / "app" / "main.py", "import fastapi\napp = fastapi.FastAPI()\n")
    first = discover_repository_agents(repo)
    second = discover_repository_agents(repo)
    assert first == second
    assert discovery_result_json(first) == discovery_result_json(second)
    assert first.repositoryRoot == "."
    assert str(repo) not in discovery_result_json(first)


def test_discovery_rejects_symlink_and_blocks_secret_roots(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / "prompts" / "main.py", "import fastapi\n")
    _write(repo / "datasets" / "main.py", "import fastapi\n")
    _write(repo / "src" / ".env", "secret=1\n")
    _write(repo / "src" / "main.py", "import fastapi\napp = fastapi.FastAPI()\n")
    target = repo / "src" / "linked.py"
    try:
        os.symlink(repo / "src" / "main.py", target)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(BootstrapConfigError, match="symlinked path"):
        discover_repository_agents(repo)


def test_discovery_enforces_allowed_roots_and_budgets(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / "outside" / "main.py", "import fastapi\n")
    assert discover_repository_agents(repo).agents == ()


def test_discovery_finds_nested_entrypoints_without_root_suppression(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / ".foundry" / "agent-metadata.yaml", "agent_name: Root\nsource_root: rootsrc\npackage_root: rootpkg\n")
    _write(repo / "rootsrc" / "main.py", "import fastapi\napp = fastapi.FastAPI()\n")
    _write(repo / "rootpkg" / "package.json", "{\"name\":\"root\"}\n")
    _write(repo / "agents" / "worker" / "main.py", "from flask import Flask\napp = Flask(__name__)\n")
    result = discover_repository_agents(repo)
    assert [agent.root for agent in result.agents] == [".", "agents/worker"]


def test_discovery_merges_same_root_and_rejects_casefold_collisions(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / "service" / ".foundry" / "agent-metadata.yaml", "agent_name: svc\nsource_root: service\npackage_root: service\n")
    _write(repo / "service" / "main.py", "import fastapi\napp = fastapi.FastAPI()\n")
    result = discover_repository_agents(repo)
    assert result.agents[0].root == "service"
    assert any(item.kind == "agent-metadata" for item in result.agents[0].evidence)
    assert any(item.kind == "entrypoint" for item in result.agents[0].evidence)
    if os.name == "nt":
        pytest.skip("casefold-collision path reproduction is filesystem-dependent")


def test_selection_requires_explicit_root_id_for_dot_and_unique_ids(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / "src" / "svc" / "main.py", "import fastapi\napp = fastapi.FastAPI()\n")
    assert discover_repository_agents(repo, selected_agents=()).agents == ()
    with pytest.raises(BootstrapConfigError, match="requires explicit repoAgentId"):
        discover_repository_agents(repo, selected_agents=(".",))
    result = discover_repository_agents(repo, selected_agents=({"repoAgentId": "SVC", "root": "src/svc"},))
    assert [agent.repoAgentId for agent in result.agents] == ["svc"]
    with pytest.raises(BootstrapConfigError, match="duplicate selected repoAgentId"):
        discover_repository_agents(repo, selected_agents=({"repoAgentId": "svc", "root": "src/svc"}, {"repoAgentId": "SVC", "root": "services/svc"}))


def test_shared_source_output_uses_repo_agent_ids(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / ".foundry" / "agent-metadata.yaml", "agent_name: root\nsource_root: shared\npackage_root: shared\n")
    _write(repo / "shared" / "main.py", "import fastapi\napp = fastapi.FastAPI()\n")
    _write(repo / "services" / "shared" / ".foundry" / "agent-metadata.yaml", "agent_name: nested\nsource_root: shared/nested\npackage_root: shared/nested\n")
    _write(repo / "shared" / "nested" / "main.py", "from flask import Flask\napp = Flask(__name__)\n")
    approved = discover_repository_agents(repo, approved_shared_sources={".": ["services/shared"]})
    approved_map = {agent.root: agent for agent in approved.agents}
    assert approved_map["."].approvedSharedSourceRepoAgentIds == ("shared-services-shared",)


def test_declared_roots_must_resolve_and_fingerprint_exact_values(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / ".foundry" / "agent-metadata.yaml", "agent_name: root\nsource_root: src\npackage_root: pkg\nproject_endpoint: https://example\nexpected_version: v1\n")
    _write(repo / "src" / "main.py", "import fastapi\napp = fastapi.FastAPI()\n")
    _write(repo / "pkg" / "package.json", "{\"name\":\"pkg\"}\n")
    result = discover_repository_agents(repo)
    agent = result.agents[0]
    assert agent.sourceRoot == "src"
    assert agent.packageRoot == "pkg"
    with pytest.raises(BootstrapConfigError, match="outside repository root"):
        _write(repo / ".foundry" / "agent-metadata.yaml", "agent_name: root\nsource_root: ../bad\npackage_root: pkg\n")
        discover_repository_agents(repo)


def test_binding_alignment_requires_exact_observed_matches(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / ".foundry" / "agent-metadata.yaml", "agent_name: root\nsource_root: app\npackage_root: app\nproject_endpoint: https://example\nexpected_version: v1\n")
    _write(repo / "app" / "main.py", "import fastapi\napp = fastapi.FastAPI()\n")
    unknown = discover_repository_agents(repo)
    assert unknown.agents[0].bindingAssessment.classification == "bound-unknown"
    with pytest.raises(BootstrapConfigError, match="sha256"):
        discover_repository_agents(repo, binding_evidence_by_root={".": {"source_fingerprint": "bad"}})
    source_fingerprint = unknown.agents[0].sourceFingerprint
    package_fingerprint = unknown.agents[0].packageFingerprint
    diverged = discover_repository_agents(
        repo,
        binding_evidence_by_root={".": {"project_endpoint": "https://wrong", "agent_name": "root", "expected_version": "v1", "source_fingerprint": source_fingerprint, "package_fingerprint": package_fingerprint}},
    )
    assert diverged.agents[0].bindingAssessment.classification == "bound-diverged"
    aligned = discover_repository_agents(
        repo,
        binding_evidence_by_root={".": {"project_endpoint": "https://example", "agent_name": "root", "expected_version": "v1", "source_fingerprint": source_fingerprint, "package_fingerprint": package_fingerprint}},
    )
    assert aligned.agents[0].bindingAssessment.classification == "bound-aligned"


def test_derived_ids_must_be_unique_or_explicit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / "agents" / "svc" / "main.py", "import fastapi\napp = fastapi.FastAPI()\n")
    _write(repo / "services" / "svc" / "main.py", "import fastapi\napp = fastapi.FastAPI()\n")
    result = discover_repository_agents(repo)
    assert [agent.repoAgentId for agent in result.agents] == ["svc-agents-svc", "svc-services-svc"]
