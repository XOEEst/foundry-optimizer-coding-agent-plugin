from __future__ import annotations

import json
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PLUGINS_ROOT = REPOSITORY_ROOT / "plugins"
BOOTSTRAP_ROOT = PLUGINS_ROOT / "foundry-bootstrap"
OPTIMIZER_ROOT = PLUGINS_ROOT / "foundry-agent-optimizer"
RUNTIME_OPTIMIZER_ROOT = (
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
    "plugins/foundry-bootstrap/SKILL.md",
    "plugins/foundry-bootstrap/references/README.md",
    "plugins/foundry-bootstrap/scripts/README.md",
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
    assert (BOOTSTRAP_ROOT / "SKILL.md").is_file()
    assert not (OPTIMIZER_ROOT / "SKILL.md").exists()
    assert (RUNTIME_OPTIMIZER_ROOT / "SKILL.md").is_file()


def test_bootstrap_skill_frontmatter_and_owner_contract_are_placeholders_only() -> None:
    frontmatter, body = _parse_frontmatter(BOOTSTRAP_ROOT / "SKILL.md")
    normalized = " ".join(body.split())

    assert frontmatter == {
        "name": "foundry-bootstrap",
        "description": (
            "Establish the top-level bootstrap plugin boundary over the shared "
            "foundry_opt runtime."
        ),
    }
    assert "thin client over shared `foundry_opt` runtime code" in normalized
    assert "placeholder pin contract" in normalized
    assert "Do not assume local launchers or setup workflow entrypoints exist" in normalized


def test_plugin_tree_contains_only_allowed_boundary_files() -> None:
    assert _plugin_files() == EXPECTED_PLUGIN_FILES

    assert "placeholder reserves the top-level plugin folder only" in _read(
        OPTIMIZER_ROOT / "README.md"
    )
    assert "Nothing in this folder is executable in this task." in _read(
        BOOTSTRAP_ROOT / "scripts" / "README.md"
    )
    assert "Store reviewed notes, migration pointers, and source references" in _read(
        BOOTSTRAP_ROOT / "references" / "README.md"
    )
    assert "Do not mirror the existing `src/foundry_opt/templates/` runtime content here" in _read(
        BOOTSTRAP_ROOT / "templates" / "README.md"
    )


def test_lock_template_uses_exact_placeholder_fields_and_no_runtime_executables() -> None:
    template = json.loads(_read(BOOTSTRAP_ROOT / "skill.lock.template.json"))

    assert template == EXPECTED_LOCK_TEMPLATE
    assert not list(PLUGINS_ROOT.rglob("*.py"))
    assert not list(PLUGINS_ROOT.rglob("*.ps1"))
    assert not list(PLUGINS_ROOT.rglob("*.sh"))
    assert not any(
        path.name in {"launch-bootstrap.ps1", "launch-bootstrap.sh"}
        for path in PLUGINS_ROOT.rglob("*")
        if path.is_file()
    )
