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


def test_exact_allowlist_and_blocked_declared_roots(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / ".hidden" / "main.py", "import fastapi\n")
    _write(repo / ".foundry" / "agent-metadata.yaml", "agent_name: root\nsource_root: prompts\npackage_root: prompts\n")
    _write(repo / "prompts" / "main.py", "import fastapi\n")
    with pytest.raises(BootstrapConfigError, match="scan root"):
        discover_repository_agents(repo)


def test_rejects_symlinks_in_path_components(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / "src" / "main.py", "import fastapi\napp = fastapi.FastAPI()\n")
    link = repo / "src-link"
    try:
        os.symlink(repo / "src", link, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation unavailable")
    _write(repo / ".foundry" / "agent-metadata.yaml", "agent_name: root\nsource_root: src-link\npackage_root: src-link\n")
    with pytest.raises(BootstrapConfigError, match="symlink or junction"):
        discover_repository_agents(repo)


def test_expected_version_optional_and_exact_binding_match(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / ".foundry" / "agent-metadata.yaml", "agent_name: root\nsource_root: app\npackage_root: app\nproject_endpoint: https://example\n")
    _write(repo / "app" / "main.py", "import fastapi\napp = fastapi.FastAPI()\n")
    unknown = discover_repository_agents(repo)
    assert unknown.agents[0].bindingAssessment.classification == "bound-unknown"
    aligned = discover_repository_agents(
        repo,
        binding_evidence_by_root={
            ".": {
                "project_endpoint": "https://example",
                "agent_name": "root",
                "source_fingerprint": unknown.agents[0].sourceFingerprint,
                "package_fingerprint": unknown.agents[0].packageFingerprint,
            }
        },
    )
    assert aligned.agents[0].bindingAssessment.classification == "bound-aligned"


def test_entrypoint_root_fallback_only_without_foundry_ancestor(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / "app" / "main.py", "import fastapi\napp = fastapi.FastAPI()\n")
    _write(repo / "agents" / "worker" / ".foundry" / "agent-metadata.yaml", "agent_name: worker\nsource_root: agents/worker\npackage_root: agents/worker\n")
    _write(repo / "agents" / "worker" / "main.py", "from flask import Flask\napp = Flask(__name__)\n")
    result = discover_repository_agents(repo)
    assert [agent.root for agent in result.agents] == [".", "agents/worker"]


def test_true_root_entrypoint_is_discovered(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / "main.py", "import fastapi\napp = fastapi.FastAPI()\n")
    assert discover_repository_agents(repo).agents == ()


def test_casefold_segment_collision_rejected_and_sorted_deterministically(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    if os.name == "nt":
        pytest.skip("segment collision reproduction is filesystem-dependent")
    _write(repo / "src" / "svc" / "main.py", "import fastapi\napp = fastapi.FastAPI()\n")
    _write(repo / "src" / "Svc" / "main.py", "import fastapi\napp = fastapi.FastAPI()\n")
    with pytest.raises(BootstrapConfigError, match="case-fold-colliding"):
        discover_repository_agents(repo)


def test_shared_source_output_uses_repo_agent_ids(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / ".foundry" / "agent-metadata.yaml", "agent_name: root\nsource_root: shared\npackage_root: shared\n")
    _write(repo / "shared" / "main.py", "import fastapi\napp = fastapi.FastAPI()\n")
    _write(repo / "services" / "shared" / ".foundry" / "agent-metadata.yaml", "agent_name: nested\nsource_root: shared/nested\npackage_root: shared/nested\n")
    _write(repo / "shared" / "nested" / "main.py", "from flask import Flask\napp = Flask(__name__)\n")
    approved = discover_repository_agents(repo, approved_shared_sources={".": ["services/shared"]})
    assert {agent.root: agent.approvedSharedSourceRepoAgentIds for agent in approved.agents}["."] == ("shared-services-shared",)
