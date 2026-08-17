from __future__ import annotations

import json
from pathlib import Path

from foundry_opt.bootstrap.discovery import discover_repository_agents, discovery_result_json


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_discovery_is_deterministic_and_read_only(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / ".foundry" / "agent-metadata.yaml", "agent_name: Root Agent\nsource_root: app\n")
    _write(repo / "app" / "main.py", "import fastapi\napp = fastapi.FastAPI()\n")
    _write(repo / ".github" / "workflows" / "deploy-foundry-agent.yml", "name: foundry deploy\n")
    before = sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*"))
    first = discover_repository_agents(repo)
    second = discover_repository_agents(repo)
    assert first == second
    assert discovery_result_json(first) == discovery_result_json(second)
    after = sorted(p.relative_to(repo).as_posix() for p in repo.rglob("*"))
    assert before == after


def test_discovery_normalizes_selection_and_shared_source_approval(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / ".foundry" / "agent-metadata.yaml", "agent_name: Root Agent\n")
    _write(repo / "service" / ".foundry" / "agent-metadata.team.yaml", "agent_name: Service Agent\n")
    _write(repo / "service" / "app.py", "from flask import Flask\napp = Flask(__name__)\n")
    _write(repo / ".github" / "workflows" / "foundry-agent.yml", "foundry workflow\n")
    result = discover_repository_agents(
        repo,
        selected_agents=(
            {"repoAgentId": "REPO", "root": "."},
            {"repoAgentId": "service", "root": "service"},
        ),
        approved_shared_sources={"repo": ["SERVICE"]},
    )
    assert [agent.repoAgentId for agent in result.agents] == ["repo", "service"]
    root_agent = result.agents[0]
    service_agent = result.agents[1]
    assert root_agent.approvedSharedSourceRepoAgentIds == ("service",)
    assert service_agent.approvedSharedSourceRepoAgentIds == ()


def test_discovery_uses_binding_assessment_classes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / "ready" / "main.py", "import fastapi\n")
    _write(repo / "ready" / ".github" / "workflows" / "deploy.yml", "foundry deploy\n")
    _write(repo / "bound" / ".foundry" / "agent-metadata.yaml", "agent_name: Bound\n")
    _write(repo / "bound" / "main.py", "import fastapi\n")
    _write(repo / "bound" / ".github" / "workflows" / "deploy.yml", "foundry deploy\n")
    _write(repo / "unknown" / ".foundry" / "agent-metadata.yaml", "agent_name: Unknown\n")
    _write(repo / "unknown" / "main.py", "import fastapi\n")
    _write(repo / "diverged" / ".foundry" / "agent-metadata.yaml", "agent_name: Diverged\n")
    _write(repo / "notready" / "app.py", "print('hello')\n")

    result = discover_repository_agents(
        repo,
        selected_agents=("ready", "bound", "unknown", "diverged", "notready"),
    )
    classes = {agent.repoAgentId: agent.bindingAssessment.classification for agent in result.agents}
    assert classes == {
        "bound": "bound-aligned",
        "diverged": "bound-diverged",
        "notready": "not-ready",
        "ready": "ready-unbound",
        "unknown": "bound-unknown",
    }
    blockers = {agent.repoAgentId: [blocker.code for blocker in agent.blockers] for agent in result.agents}
    assert blockers["diverged"] == ["missing-runtime-entrypoint"]
    assert "missing-foundry-workflow" in blockers["notready"]


def test_discovery_fixture_ignores_unrelated_instructions_without_azure_yaml(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(repo / "agents" / "assistant" / ".foundry" / "agent-metadata.yaml", "agent_name: Assistant\n")
    _write(repo / "agents" / "assistant" / "package.json", json.dumps({"name": "assistant"}))
    _write(repo / "agents" / "assistant" / "index.js", "const express = require('express')\n")
    _write(repo / "AGENTS.md", "root instructions\n")
    _write(repo / "CLAUDE.md", "other instructions\n")
    _write(repo / "README.md", "unrelated readme\n")
    _write(repo / ".github" / "workflows" / "ci.yml", "name: CI\n")
    _write(repo / ".github" / "workflows" / "deploy-foundry-agent.yml", "foundry deploy\n")
    _write(repo / "skills" / "prompt.txt", "prompt body\n")
    result = discover_repository_agents(repo, selected_agents=("assistant",))
    agent = result.agents[0]
    kinds = [item.kind for item in agent.evidence]
    assert "agent-metadata" in kinds
    assert "instructions" in kinds
    assert "workflow" in kinds
    assert agent.bindingAssessment.classification == "bound-aligned"
