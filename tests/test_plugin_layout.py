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
EXPECTED_PLUGIN_FILES = {
    "plugins/README.md",
    "plugins/foundry-agent-optimizer/README.md",
    "plugins/foundry-agent-optimizer/SKILL.md",
    "plugins/foundry-agent-optimizer/references/.gitattributes",
    "plugins/foundry-agent-optimizer/references/ADAPTER_MAPPING.md",
    "plugins/foundry-agent-optimizer/references/TENZING_ATTRIBUTION.md",
    "plugins/foundry-agent-optimizer/references/tenzing/.github/ISSUE_TEMPLATE/JitAccess.yml",
    "plugins/foundry-agent-optimizer/references/tenzing/.github/acl/access.yml",
    "plugins/foundry-agent-optimizer/references/tenzing/.github/compliance/inventory.yml",
    "plugins/foundry-agent-optimizer/references/tenzing/.github/policies/jit.yml",
    "plugins/foundry-agent-optimizer/references/tenzing/.gitignore",
    "plugins/foundry-agent-optimizer/references/tenzing/INIT.md",
    "plugins/foundry-agent-optimizer/references/tenzing/LICENSE",
    "plugins/foundry-agent-optimizer/references/tenzing/README.md",
    "plugins/foundry-agent-optimizer/references/tenzing/assets/logo.svg",
    "plugins/foundry-agent-optimizer/references/tenzing/climb.md",
    "plugins/foundry-agent-optimizer/references/tenzing/climb_config/background.md",
    "plugins/foundry-agent-optimizer/references/tenzing/climb_config/data.md",
    "plugins/foundry-agent-optimizer/references/tenzing/climb_config/dos-and-donts.md",
    "plugins/foundry-agent-optimizer/references/tenzing/climb_config/environment.md",
    "plugins/foundry-agent-optimizer/references/tenzing/climb_config/evaluation.md",
    "plugins/foundry-agent-optimizer/references/tenzing/climb_config/objective.md",
    "plugins/foundry-agent-optimizer/references/tenzing/climb_config/tracking-experiments.md",
    "plugins/foundry-agent-optimizer/README.md",
    "plugins/foundry-bootstrap/SKILL.md",
    "plugins/foundry-bootstrap/references/README.md",
    "plugins/foundry-bootstrap/scripts/README.md",
    "plugins/foundry-bootstrap/scripts/bootstrap.py",
    "plugins/foundry-bootstrap/scripts/install-runtime.ps1",
    "plugins/foundry-bootstrap/scripts/install-runtime.sh",
    "plugins/foundry-bootstrap/skill.lock.template.json",
    "plugins/foundry-bootstrap/templates/README.md",
}
EXPECTED_LOCK_TEMPLATE = {
    "schema_version": "__SCHEMA_VERSION__",
    "runtime_repository": "__RUNTIME_REPOSITORY__",
    "runtime_commit": "__RUNTIME_COMMIT__",
    "uv_lock_sha256": "__UV_LOCK_SHA256__",
    "package_path": "__PACKAGE_PATH__",
}


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _plugin_files() -> set[str]:
    return {
        path.relative_to(REPOSITORY_ROOT).as_posix()
        for path in PLUGINS_ROOT.rglob("*")
        if path.is_file()
    }


def _parse_frontmatter(path: Path) -> tuple[dict[str, object], str]:
    text = _read(path)
    lines = text.splitlines()
    assert lines[0] == "---"
    end_index = lines[1:].index("---") + 1
    frontmatter = yaml.safe_load("\n".join(lines[1:end_index]))
    assert isinstance(frontmatter, dict)
    body = "\n".join(lines[end_index + 1 :])
    return frontmatter, body


def test_plugins_readme_and_discovery_boundary_are_explicit() -> None:
    readme = _read(PLUGINS_ROOT / "README.md")
    normalized = " ".join(readme.split())

    assert "`foundry-bootstrap/`" in normalized
    assert "`foundry-agent-optimizer/`" in normalized
    assert "shared `foundry_opt` runtime package" in normalized
    assert "canonical issue-time optimizer skill folder" in normalized
    assert (BOOTSTRAP_ROOT / "SKILL.md").is_file()
    assert (OPTIMIZER_ROOT / "SKILL.md").is_file()
    assert not LEGACY_OPTIMIZER_ROOT.exists()


def test_bootstrap_skill_frontmatter_and_owner_contract_describes_canonical_launchers() -> None:
    frontmatter, body = _parse_frontmatter(BOOTSTRAP_ROOT / "SKILL.md")
    normalized = " ".join(body.split())

    assert frontmatter == {
        "name": "foundry-bootstrap",
        "description": (
            "Guide a first-time owner through the downloadable bootstrap "
            "start/resume/approval loop over the shared foundry_opt runtime."
        ),
    }
    assert "only owner client over `BootstrapRunner`" in normalized
    assert "python scripts/bootstrap.py start --repository ." in normalized
    assert "`<<<FOUNDRY_BOOTSTRAP_OWNER_MARKDOWN>>>`" in normalized
    assert "`<<<FOUNDRY_BOOTSTRAP_TURN>>>`" in normalized
    assert "`next_question.title` plus `next_question.details_markdown`" in normalized
    assert "Never paste or expose its raw JSON to the owner" in normalized
    assert "status --operation-id <id>" in normalized
    assert "rollback --operation-id <id>" in normalized
    assert "Do not create or switch to a custom agent." in normalized
    assert "Do not implement Foundry target resolution" in normalized


def test_plugin_tree_contains_only_allowed_boundary_files() -> None:
    assert _plugin_files() == EXPECTED_PLUGIN_FILES

    assert "canonical issue-time optimizer skill folder" in _read(OPTIMIZER_ROOT / "README.md")
    assert "do not maintain a second full copy" in _read(OPTIMIZER_ROOT / "README.md")
    assert (OPTIMIZER_ROOT / "references" / "ADAPTER_MAPPING.md").is_file()
    assert (OPTIMIZER_ROOT / "references" / "TENZING_ATTRIBUTION.md").is_file()
    scripts_readme = _read(BOOTSTRAP_ROOT / "scripts" / "README.md")
    assert "canonical checked-in home for the reviewed owner bridge" in scripts_readme
    assert "bootstrap.py" in scripts_readme
    assert "only owner client over `BootstrapRunner`" in scripts_readme
    assert "install-runtime.ps1" in scripts_readme
    assert "install-runtime.sh" in scripts_readme
    assert "Store reviewed notes, migration pointers, and source references" in _read(
        BOOTSTRAP_ROOT / "references" / "README.md"
    )
    references_readme = _read(BOOTSTRAP_ROOT / "references" / "README.md")
    assert "Bootstrap bridge quick reference" in references_readme
    assert "status --operation-id <id>" in references_readme
    assert "rollback --operation-id <id>" in references_readme
    assert "Do not mirror the existing `src/foundry_opt/templates/` runtime content here" in _read(
        BOOTSTRAP_ROOT / "templates" / "README.md"
    )


def test_lock_template_uses_exact_placeholder_fields_and_only_canonical_launchers() -> None:
    template = json.loads(_read(BOOTSTRAP_ROOT / "skill.lock.template.json"))

    assert template == EXPECTED_LOCK_TEMPLATE
    assert {
        path.relative_to(REPOSITORY_ROOT).as_posix()
        for path in PLUGINS_ROOT.rglob("*.py")
    } == {"plugins/foundry-bootstrap/scripts/bootstrap.py"}
    assert {
        path.relative_to(REPOSITORY_ROOT).as_posix()
        for path in PLUGINS_ROOT.rglob("*.ps1")
    } == {"plugins/foundry-bootstrap/scripts/install-runtime.ps1"}
    assert {
        path.relative_to(REPOSITORY_ROOT).as_posix()
        for path in PLUGINS_ROOT.rglob("*.sh")
    } == {"plugins/foundry-bootstrap/scripts/install-runtime.sh"}
    assert not any(
        path.name in {"launch-bootstrap.ps1", "launch-bootstrap.sh"}
        for path in PLUGINS_ROOT.rglob("*")
        if path.is_file()
    )


def test_build_configuration_includes_plugin_skill_in_source_artifacts() -> None:
    pyproject = tomllib.loads(_read(REPOSITORY_ROOT / "pyproject.toml"))
    project = pyproject["project"]
    build_backend = pyproject["tool"]["uv"]["build-backend"]
    scripts = pyproject["project"]["scripts"]

    assert project["scripts"] == {"foundry-opt": "foundry_opt:main"}
    assert "plugins/**" in build_backend["source-include"]
    assert scripts == {"foundry-opt": "foundry_opt:main"}
    assert "foundry-bootstrap" not in scripts
