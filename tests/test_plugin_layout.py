from __future__ import annotations

import json
import tomllib
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPOSITORY_ROOT / "plugins"
BOOTSTRAP_ROOT = PLUGINS_ROOT / "foundry-bootstrap"
OPTIMIZER_ROOT = PLUGINS_ROOT / "foundry-agent-optimizer"
LEGACY_OPTIMIZER_ROOT = (
    REPOSITORY_ROOT
    / "src"
    / "foundry_opt"
    / "templates"
    / "skills"
    / "foundry-agent-optimizer"
)
EXPECTED_BOOTSTRAP_FILES = {
    "SKILL.md",
    "release.json",
    "references/README.md",
    "references/discovery.md",
    "references/failure-handling.md",
    "references/migration.md",
    "references/owner-flow.md",
    "references/recovery.md",
    "references/resource-reuse.md",
    "references/security.md",
    "schemas/registry.schema.json",
    "schemas/sidecar.schema.json",
    "templates/README.md",
    "templates/azure.yaml",
    "templates/bootstrap-report.md",
    "templates/copilot-setup-steps.yml",
    "templates/foundry-opt-deploy.yml",
    "templates/foundry-opt.instructions.md",
    "templates/foundry-optimize-agent.yml",
    "templates/registry.yaml",
    "templates/sidecar.yaml",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse_frontmatter(path: Path) -> tuple[dict[str, object], str]:
    text = _read(path)
    lines = text.splitlines()
    assert lines[0] == "---"
    end_index = lines[1:].index("---") + 1
    frontmatter = yaml.safe_load("\n".join(lines[1:end_index]))
    assert isinstance(frontmatter, dict)
    body = "\n".join(lines[end_index + 1 :])
    return frontmatter, body


def _bootstrap_files() -> set[str]:
    return {
        path.relative_to(BOOTSTRAP_ROOT).as_posix()
        for path in BOOTSTRAP_ROOT.rglob("*")
        if path.is_file()
    }


def test_plugins_discovery_boundary_contains_both_skills() -> None:
    assert (BOOTSTRAP_ROOT / "SKILL.md").is_file()
    assert (OPTIMIZER_ROOT / "SKILL.md").is_file()
    assert not LEGACY_OPTIMIZER_ROOT.exists()


def test_bootstrap_skill_is_static_and_uses_one_combined_approval() -> None:
    frontmatter, body = _parse_frontmatter(BOOTSTRAP_ROOT / "SKILL.md")
    normalized = " ".join(body.split())

    assert frontmatter == {
        "name": "foundry-bootstrap",
        "description": (
            "Bootstrap repository agents for Microsoft Foundry with read-only "
            "discovery, one combined approval, static templates, and standard "
            "Git, GitHub, Azure, and azd tools."
        ),
    }
    assert "read-only repository, Git, GitHub, Azure, and Foundry" in normalized
    assert "one combined approval request" in normalized
    assert "session staging area" in normalized
    assert "azd ai agent version" in normalized
    assert "azd deploy" in normalized
    assert "https://packagefeedproxy.microsoft.io/pypi/simple" in body
    assert "https://packagefeedproxy.microsoft.io/nuget/v3/index.json" in body
    assert "UV_DEFAULT_INDEX" in body
    assert "PIP_INDEX_URL" in body
    assert "Never push." in normalized
    assert "Leave successful local and remote changes in place" in normalized
    assert "Do not create datasets, evaluators, evaluation definitions, or evaluation runs." in normalized


def test_bootstrap_skill_resolves_ignored_contract_paths_before_approval() -> None:
    _, body = _parse_frontmatter(BOOTSTRAP_ROOT / "SKILL.md")
    discovery = _read(BOOTSTRAP_ROOT / "references" / "discovery.md")
    combined = f"{body}\n{discovery}"

    assert "git check-ignore -v --no-index" in combined
    assert ".gitignore" in combined
    assert ".foundry-opt/registry.yaml" in combined
    assert ".foundry-opt/bootstrap-report.md" in combined
    assert "git add -f" in combined


def test_bootstrap_tree_is_exactly_static_content() -> None:
    assert _bootstrap_files() == EXPECTED_BOOTSTRAP_FILES
    assert not (BOOTSTRAP_ROOT / "scripts").exists()
    assert not any(
        path.suffix.casefold() in {".py", ".ps1", ".sh", ".cmd", ".bat", ".exe"}
        for path in BOOTSTRAP_ROOT.rglob("*")
        if path.is_file()
    )

    combined = "\n".join(
        _read(path)
        for path in BOOTSTRAP_ROOT.rglob("*")
        if path.is_file() and path.suffix.casefold() in {".md", ".json", ".yaml", ".yml"}
    ).casefold()
    for retired_term in (
        "bootstrap.py",
        "skill.lock",
        "operation-id",
        "operation_id",
        "receipt",
        "rollback",
        "compensation",
    ):
        assert retired_term not in combined


def test_static_template_map_and_release_contract_are_complete() -> None:
    template_readme = _read(BOOTSTRAP_ROOT / "templates" / "README.md")
    for name in (
        "registry.yaml",
        "sidecar.yaml",
        "azure.yaml",
        "foundry-opt-deploy.yml",
        "copilot-setup-steps.yml",
        "foundry-opt.instructions.md",
        "foundry-optimize-agent.yml",
        "bootstrap-report.md",
    ):
        assert f"`{name}`" in template_readme

    release = json.loads(_read(BOOTSTRAP_ROOT / "release.json"))
    assert list(release) == [
        "repository",
        "commit",
        "package_path",
        "uv_lock_sha256",
        "optimizer_skill_path",
    ]
    assert all(value.startswith("__") and value.endswith("__") for value in release.values())
    assert "azd" not in release
    assert "azure.ai.agents" not in release


def test_build_configuration_includes_plugin_skills_in_source_artifacts() -> None:
    pyproject = tomllib.loads(_read(REPOSITORY_ROOT / "pyproject.toml"))
    project = pyproject["project"]
    build_backend = pyproject["tool"]["uv"]["build-backend"]

    assert project["scripts"] == {"foundry-opt": "foundry_opt:main"}
    assert "plugins/**" in build_backend["source-include"]
    assert "foundry-bootstrap" not in project["scripts"]
