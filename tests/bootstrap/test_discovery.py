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


def test_discovery_rejects_symlink_and_blocked_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / "app" / "main.py", "import fastapi\napp = fastapi.FastAPI()\n")
    _write(repo / ".env", "SECRET=1\n")
    target = repo / "app" / "linked.py"
    try:
        os.symlink(repo / "app" / "main.py", target)
    except OSError:
        pytest.skip("symlink creation unavailable")
    with pytest.raises(BootstrapConfigError, match="symlinked path"):
        discover_repository_agents(repo)


def test_discovery_finds_nested_entrypoints_without_root_suppression(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / ".foundry" / "agent-metadata.yaml", "agent_name: Root\nsource_root: rootsrc\npackage_root: rootpkg\n")
    _write(repo / "rootsrc" / "main.py", "import fastapi\napp = fastapi.FastAPI()\n")
    _write(repo / "rootpkg" / "package.json", "{\"name\":\"root\"}\n")
    _write(repo / "agents" / "worker" / "main.py", "from flask import Flask\napp = Flask(__name__)\n")
    result = discover_repository_agents(repo)
    assert [agent.root for agent in result.agents] == [".", "agents/worker"]


def test_discovery_merges_same_root_and_rejects_conflicts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / "service" / ".foundry" / "agent-metadata.yaml", "agent_name: svc\nsource_root: service\npackage_root: service\n")
    _write(repo / "service" / "main.py", "import fastapi\napp = fastapi.FastAPI()\n")
    result = discover_repository_agents(repo)
    assert result.agents[0].root == "service"
    assert any(item.kind == "agent-metadata" for item in result.agents[0].evidence)
    assert any(item.kind == "entrypoint" for item in result.agents[0].evidence)

    _write(repo / "other" / "README.md", "other\n")
    _write(repo / "service" / ".foundry" / "agent-metadata.alt.yaml", "agent_name: svc\nsource_root: other\npackage_root: service\n")
    with pytest.raises(BootstrapConfigError, match="conflicting discovery roots"):
        discover_repository_agents(repo)


def test_selection_requires_validated_roots_and_duplicate_ids_fail(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / "svc" / "main.py", "import fastapi\napp = fastapi.FastAPI()\n")
    assert discover_repository_agents(repo, selected_agents=()).agents == ()
    result = discover_repository_agents(repo, selected_agents=({"repoAgentId": "SVC", "root": "svc"},))
    assert [agent.repoAgentId for agent in result.agents] == ["svc"]
    with pytest.raises(BootstrapConfigError, match="duplicate selected repoAgentId"):
        discover_repository_agents(repo, selected_agents=({"repoAgentId": "svc", "root": "svc"}, {"repoAgentId": "SVC", "root": "other"}))
    with pytest.raises(BootstrapConfigError, match="selected roots were not discovered"):
        discover_repository_agents(repo, selected_agents=({"repoAgentId": "missing", "root": "missing"},))


def test_unapproved_overlap_blocks_until_explicitly_approved(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / ".foundry" / "agent-metadata.yaml", "agent_name: root\nsource_root: shared\npackage_root: shared\n")
    _write(repo / "shared" / "main.py", "import fastapi\napp = fastapi.FastAPI()\n")
    _write(repo / "shared" / "nested" / "main.py", "from flask import Flask\napp = Flask(__name__)\n")
    blocked = discover_repository_agents(repo)
    blocked_map = {agent.root: agent for agent in blocked.agents}
    assert blocked_map["."].bindingAssessment.classification == "not-ready"
    assert any(blocker.code == "unapproved-shared-source" for blocker in blocked_map["."].blockers)
    approved = discover_repository_agents(repo, approved_shared_sources={".": ["shared/nested"]})
    approved_map = {agent.root: agent for agent in approved.agents}
    assert approved_map["."].approvedSharedSourceRepoAgentIds == ("shared/nested",)


def test_declared_roots_must_resolve_and_fingerprints_use_source_and_package_roots(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / ".foundry" / "agent-metadata.yaml", "agent_name: root\nsource_root: src\npackage_root: pkg\n")
    _write(repo / "src" / "main.py", "import fastapi\napp = fastapi.FastAPI()\n")
    _write(repo / "pkg" / "package.json", "{\"name\":\"pkg\"}\n")
    result = discover_repository_agents(repo)
    agent = result.agents[0]
    assert agent.sourceRoot == "src"
    assert agent.packageRoot == "pkg"
    assert agent.sourceFingerprint != agent.packageFingerprint
    _write(repo / ".foundry" / "agent-metadata.yaml", "agent_name: root\nsource_root: ../bad\npackage_root: pkg\n")
    with pytest.raises(BootstrapConfigError, match="outside repository root"):
        discover_repository_agents(repo)


def test_binding_assessment_requires_injected_binding_evidence(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / ".foundry" / "agent-metadata.yaml", "agent_name: root\nsource_root: app\npackage_root: app\nproject_endpoint: https://example\nexpected_version: v1\n")
    _write(repo / "app" / "main.py", "import fastapi\napp = fastapi.FastAPI()\n")
    unknown = discover_repository_agents(repo)
    assert unknown.agents[0].bindingAssessment.classification == "bound-unknown"
    aligned = discover_repository_agents(
        repo,
        binding_evidence_by_root={
            ".": {
                "project_endpoint": "https://example",
                "agent_name": "root",
                "expected_version": "v1",
                "source_fingerprint": "a" * 64,
                "package_fingerprint": "b" * 64,
            }
        },
    )
    assert aligned.agents[0].bindingAssessment.classification == "bound-aligned"


def test_ready_unbound_does_not_require_workflow(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / "svc" / "main.py", "import fastapi\napp = fastapi.FastAPI()\n")
    result = discover_repository_agents(repo)
    assert result.agents[0].bindingAssessment.classification == "ready-unbound"
