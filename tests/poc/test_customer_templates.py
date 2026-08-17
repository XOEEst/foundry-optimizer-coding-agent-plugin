from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import yaml

from foundry_opt.poc.bootstrap import load_shared_pin
from foundry_opt.poc.config import load_agent_metadata, load_repository_policy

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CUSTOMER_TEMPLATE_ROOT = (
    REPOSITORY_ROOT / "src" / "foundry_opt" / "templates" / "customer-repo"
)
PIN_PATH = CUSTOMER_TEMPLATE_ROOT / ".github" / "foundry-opt.lock.yml"
POLICY_PATH = CUSTOMER_TEMPLATE_ROOT / ".github" / "foundry-optimizer.yaml"
METADATA_PATH = CUSTOMER_TEMPLATE_ROOT / ".foundry" / "agent-metadata.yaml"
INSTRUCTIONS_PATH = (
    CUSTOMER_TEMPLATE_ROOT / ".github" / "instructions" / "foundry-opt.instructions.md"
)
WORKFLOW_ROOT = CUSTOMER_TEMPLATE_ROOT / ".github" / "workflows"
EXPECTED_WORKFLOWS = {
    "agent-ci.yml",
    "copilot-setup-steps.yml",
    "deploy-foundry-agent.yml",
    "foundry-optimizer-validation.yml",
}
FORBIDDEN_STRINGS = (
    "FOUNDRY_OPT_SHARED_REPO_SSH_KEY",
    "git@github.com",
    "https://github.com/XOEEst/foundry-cloud-coding-agents-002.git",
    "luffy-test-agent-repo-002",
    "luechen-swedencentral-foundry",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _yaml_paths() -> list[Path]:
    return sorted(
        {
            *CUSTOMER_TEMPLATE_ROOT.rglob("*.yml"),
            *CUSTOMER_TEMPLATE_ROOT.rglob("*.yaml"),
        }
    )


def _workflow_text(path: Path) -> str:
    text = _read(path)
    uses = re.findall(
        r"^\s*uses:\s*[^@\s]+@([0-9a-f]{40})(?:\s+#.*)?$",
        text,
        flags=re.MULTILINE,
    )
    assert uses, path
    return text


def test_every_customer_template_yaml_document_parses() -> None:
    for path in _yaml_paths():
        assert yaml.safe_load(_read(path)) is not None, path


def test_public_loaders_accept_the_customer_template_contract() -> None:
    pin = load_shared_pin(PIN_PATH)
    policy = load_repository_policy(POLICY_PATH, metadata_path=METADATA_PATH)
    metadata = load_agent_metadata(METADATA_PATH)
    lock_bytes = subprocess.check_output(
        ["git", "show", f"{pin.commit}:uv.lock"],
        cwd=REPOSITORY_ROOT,
    )

    assert (
        pin.repository_url
        == "https://github.com/XOEEst/foundry-optimizer-coding-agent-plugin.git"
    )
    assert pin.commit == "92bca79a5faea0718e32101e56b34ebf29c628e3"
    assert pin.package_path == "."
    assert pin.skill_path == "src/foundry_opt/templates/skills/foundry-agent-optimizer"
    assert pin.uv_lock_sha256 == (
        "74d7bb534c53e71a61ce197f3d5fa3169f2413373c2e42617280e78e83d6c681"
    )
    assert hashlib.sha256(lock_bytes).hexdigest() == pin.uv_lock_sha256
    assert (REPOSITORY_ROOT / pin.skill_path).is_dir()

    assert policy.min_candidates == 2
    assert policy.max_candidates == 2
    assert policy.metadata_path == ".foundry/agent-metadata.yaml"

    assert metadata.authentication_method == "oidc"
    assert metadata.static_credentials_allowed is False


def test_customer_templates_install_foundry_opt_instructions_without_managed_root_instructions() -> None:
    text = _read(INSTRUCTIONS_PATH)
    assert text.startswith("---\napplyTo: \"**\"\n---\n")
    assert "# Foundry optimization repository instructions" in text
    assert not (CUSTOMER_TEMPLATE_ROOT / ".github" / "copilot-instructions.md").exists()


def test_customer_template_workflows_use_https_public_shared_fetch() -> None:
    for name in EXPECTED_WORKFLOWS - {"agent-ci.yml"}:
        text = _workflow_text(WORKFLOW_ROOT / name)
        assert r'[[ "$repository" =~ ^https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\.git$ ]]' in text
        assert 'git -C "$shared_root" remote add origin "$repository"' in text
        assert 'git -C "$shared_root" fetch --depth=1 origin "$commit"' in text
        assert 'git -C "$shared_root" checkout --detach FETCH_HEAD' in text
        assert "persist-credentials: false" in text
        assert "FOUNDRY_OPT_SHARED_REPO_SSH_KEY" not in text
        assert "git@github.com" not in text
        assert "known_hosts" not in text
        assert "StrictHostKeyChecking" not in text


def test_customer_template_validation_workflow_tracks_instruction_path() -> None:
    text = _read(WORKFLOW_ROOT / "foundry-optimizer-validation.yml")
    assert ".github/instructions/foundry-opt.instructions.md" in text
    assert ".github/copilot-instructions.md" not in text


def test_customer_templates_omit_forbidden_strings() -> None:
    text_paths = [path for path in CUSTOMER_TEMPLATE_ROOT.rglob("*") if path.is_file()]
    combined = "\n".join(
        _read(path)
        for path in text_paths
        if path.suffix in {".md", ".yml", ".yaml", ".txt", ".gitignore"}
        or path.name.startswith(".")
    )
    for forbidden in FORBIDDEN_STRINGS:
        assert forbidden not in combined
