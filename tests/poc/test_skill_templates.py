from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SKILL_TEMPLATE_ROOT = (
    REPOSITORY_ROOT
    / "src"
    / "foundry_opt"
    / "templates"
    / "skills"
    / "foundry-agent-optimizer"
)
TENZING_ROOT = SKILL_TEMPLATE_ROOT / "references" / "tenzing"
TENZING_SNAPSHOT_PATH = Path(__file__).parent / "snapshots" / "tenzing.sha256"
TENZING_SNAPSHOT_PREFIX = ".github/skills/foundry-agent-optimizer/references/tenzing/"
SKILL_PATH = SKILL_TEMPLATE_ROOT / "SKILL.md"
ADAPTER_PATH = SKILL_TEMPLATE_ROOT / "references" / "ADAPTER_MAPPING.md"
ATTRIBUTION_PATH = SKILL_TEMPLATE_ROOT / "references" / "TENZING_ATTRIBUTION.md"
FORBIDDEN_STRINGS = (
    "luffy-test-agent-repo-002",
    "luechen-swedencentral-foundry",
    "recover-foundry-agent",
    "winner-verification",
    "foundry-target-lease-retire",
    "acceptance-candidates",
    "vendored runtime",
)


def _yaml_paths() -> list[Path]:
    paths = {
        *SKILL_TEMPLATE_ROOT.rglob("*.yml"),
        *SKILL_TEMPLATE_ROOT.rglob("*.yaml"),
    }
    return sorted(paths)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _is_tenzing_reference(path: Path) -> bool:
    if not path.is_relative_to(SKILL_TEMPLATE_ROOT):
        return False
    relative = path.relative_to(SKILL_TEMPLATE_ROOT)
    return relative.parts[:2] == ("references", "tenzing")


def _expected_tenzing_hashes() -> dict[str, str]:
    return {
        path: digest
        for digest, path in (
            line.split("  ", 1)
            for line in TENZING_SNAPSHOT_PATH.read_text(encoding="utf-8").splitlines()
            if line
        )
    }


def test_every_template_yaml_document_parses() -> None:
    for path in _yaml_paths():
        assert yaml.safe_load(_read(path)) is not None, path


def test_templates_omit_forbidden_production_artifacts_and_strings() -> None:
    assert not (SKILL_TEMPLATE_ROOT / "references" / "acceptance-candidates").exists()

    text_paths = [
        path for path in SKILL_TEMPLATE_ROOT.rglob("*.md") if not _is_tenzing_reference(path)
    ]
    combined = "\n".join(_read(path).lower() for path in text_paths)
    for forbidden in FORBIDDEN_STRINGS:
        assert forbidden.lower() not in combined


def test_tenzing_snapshot_hashes_are_exact() -> None:
    expected = _expected_tenzing_hashes()
    actual_paths = {
        f"{TENZING_SNAPSHOT_PREFIX}{path.relative_to(TENZING_ROOT).as_posix()}"
        for path in TENZING_ROOT.rglob("*")
        if path.is_file()
    }

    assert actual_paths == set(expected)
    for relative_path, digest in expected.items():
        relative = Path(relative_path)
        assert relative.parts[:3] == (
            ".github",
            "skills",
            "foundry-agent-optimizer",
        )
        actual = SKILL_TEMPLATE_ROOT / Path(*relative.parts[3:])
        assert hashlib.sha256(actual.read_bytes()).hexdigest() == digest


def test_skill_and_tenzing_attribution_stay_in_sync() -> None:
    skill = _read(SKILL_PATH)
    adapter = _read(ADAPTER_PATH)
    attribution = _read(ATTRIBUTION_PATH)

    assert ".github/foundry-opt.lock.yml" in skill
    assert "OIDC only" in skill
    assert "original issue" in skill
    assert "validating dataset only for the provisional winner" in skill
    assert "early draft pull request" in skill
    assert "read-only reference material" in skill

    assert "redacted, idempotent candidate update to the original issue" in adapter
    assert "fresh baseline and current best" in adapter
    assert "early Copilot pull request" in adapter
    assert "never publishes a regular version" in adapter

    assert "7300a83fc7378f0f1a401dbdf8ed28358ccf1732" in attribution
    assert "read-only snapshot" in attribution
    assert (SKILL_TEMPLATE_ROOT / "references" / "tenzing" / "LICENSE").is_file()
    assert (SKILL_TEMPLATE_ROOT / "references" / "tenzing" / "INIT.md").is_file()
