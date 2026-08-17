from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from foundry_opt.bootstrap.contracts import RootRegistry, BootstrapSidecar, SemanticPatchSpec
from foundry_opt.bootstrap.legacy import import_legacy_single_agent_documents
from foundry_opt.poc.config import SharedPin

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CUSTOMER_TEMPLATE_ROOT = REPOSITORY_ROOT / "src" / "foundry_opt" / "templates" / "customer-repo"
PIN_PATH = CUSTOMER_TEMPLATE_ROOT / ".github" / "foundry-opt.lock.yml"
REGISTRY_PATH = CUSTOMER_TEMPLATE_ROOT / ".foundry-opt" / "registry.yaml"
SIDECAR_PATH = CUSTOMER_TEMPLATE_ROOT / "agent" / ".foundry" / "foundry-opt.yaml"
INSTRUCTIONS_PATH = CUSTOMER_TEMPLATE_ROOT / ".github" / "instructions" / "foundry-opt.instructions.md"
ISSUE_FORM_PATH = CUSTOMER_TEMPLATE_ROOT / ".github" / "ISSUE_TEMPLATE" / "optimize-agent.yml"
CUSTOM_AGENT_PATH = CUSTOMER_TEMPLATE_ROOT / ".github" / "agents" / "foundry-optimizer.agent.md"
WORKFLOW_ROOT = CUSTOMER_TEMPLATE_ROOT / ".github" / "workflows"
EXPECTED_WORKFLOWS = {
    "agent-ci.yml",
    "copilot-setup-steps.yml",
    "foundry-optimizer-validation.yml",
    "foundry-optimizer-deploy.yml",
}
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "bootstrap" / "fixtures" / "templates"
FORBIDDEN_STRINGS = (
    "FOUNDRY_OPT_SHARED_REPO_SSH_KEY",
    "git@github.com",
    "known_hosts",
    "StrictHostKeyChecking",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _yaml_paths() -> list[Path]:
    return sorted({*CUSTOMER_TEMPLATE_ROOT.rglob("*.yml"), *CUSTOMER_TEMPLATE_ROOT.rglob("*.yaml"), *FIXTURE_ROOT.rglob("*.yml"), *FIXTURE_ROOT.rglob("*.yaml")})


def _workflow_text(path: Path) -> str:
    text = _read(path)
    assert "persist-credentials: false" in text, path
    return text


def test_every_customer_template_yaml_document_parses() -> None:
    for path in _yaml_paths():
        assert yaml.safe_load(_read(path)) is not None, path


def test_registry_and_sidecar_match_v1_contracts() -> None:
    registry = RootRegistry.from_document(_read(REGISTRY_PATH))
    sidecar = BootstrapSidecar.from_document(_read(SIDECAR_PATH))

    assert registry.distribution.channel == "wave3"
    assert registry.identity.kind == "unresolved_migration"
    assert registry.agents[0].agent_id == "example-agent"
    assert registry.agents[0].config_path == "agent/.foundry/foundry-opt.yaml"
    assert sidecar.repo_agent_id == registry.agents[0].agent_id
    assert sidecar.source_root == "agent"
    assert sidecar.package_root == "agent"
    assert sidecar.max_issue_evaluators == 8
    assert sidecar.deployment.environment == "foundry-production"


def test_legacy_single_agent_files_exist_only_as_migration_fixtures() -> None:
    for name in (
        "legacy-single-agent-foundry-opt.lock.yml",
        "legacy-single-agent-foundry-optimizer.yaml",
        "legacy-single-agent-agent-metadata.yaml",
    ):
        text = _read(FIXTURE_ROOT / name)
        assert "migration_fixture: legacy single-agent input only" in text

    proposal = import_legacy_single_agent_documents(
        lock_document=_read(FIXTURE_ROOT / "legacy-single-agent-foundry-opt.lock.yml"),
        policy_document=_read(FIXTURE_ROOT / "legacy-single-agent-foundry-optimizer.yaml"),
        metadata_document=_read(FIXTURE_ROOT / "legacy-single-agent-agent-metadata.yaml"),
    )
    assert proposal.registry.agents[0].config_path == "agent/.foundry/foundry-opt.yaml"


def test_customer_templates_install_repo_wide_foundry_opt_instructions_only() -> None:
    text = _read(INSTRUCTIONS_PATH)
    assert text.startswith("---\napplyTo: \"**\"\n---\n")
    assert "issue objective" in text
    assert "repository datasets" in text
    assert not (CUSTOMER_TEMPLATE_ROOT / ".github" / "copilot-instructions.md").exists()


def test_issue_form_captures_repo_agent_target_and_weighted_evaluators() -> None:
    document = yaml.safe_load(_read(ISSUE_FORM_PATH))
    body = document["body"]
    labels = {entry.get("attributes", {}).get("label"): entry for entry in body if isinstance(entry, dict)}

    assert labels["Repository agent ID or explicit Foundry target"]["validations"]["required"] is True
    assert labels["Optimization goal"]["validations"]["required"] is True
    assert labels["Observed failures or evidence"]["validations"]["required"] is True
    assert labels["Constraints and guardrails"]["validations"]["required"] is True
    evaluator_text = labels["Optional exact evaluator IDs"]["attributes"]["description"]
    assert "weight=<positive>" in evaluator_text
    assert "Invalid or unclear entries fail preflight" in evaluator_text


def test_custom_agent_freezes_issue_inputs_and_uses_validating_winner_only() -> None:
    text = _read(CUSTOM_AGENT_PATH)
    assert ".foundry-opt/registry.yaml" in text
    assert "Freeze the issue objective, constraints, explicit evaluator set" in text
    assert "write a hypothesis" in text
    assert "validate only the provisional winner" in text
    assert "Deployment uses the repository default bundle" in text


def test_setup_and_validation_workflows_use_public_https_exact_sha_without_ssh() -> None:
    pin = SharedPin.from_document(_read(PIN_PATH))
    setup_text = _workflow_text(WORKFLOW_ROOT / "copilot-setup-steps.yml")
    validation_text = _workflow_text(WORKFLOW_ROOT / "foundry-optimizer-validation.yml")

    for text in (setup_text, validation_text):
        assert f'git -C "$shared_root" remote add origin "{pin.repository_url}"' not in text
        assert 'git -C "$shared_root" remote add origin "$repository"' in text
        assert 'git -C "$shared_root" fetch --depth=1 origin "$commit"' in text
        assert 'git -C "$shared_root" checkout --detach FETCH_HEAD' in text
        assert "FOUNDRY_OPT_SHARED_REPO_SSH_KEY" not in text
        assert "git@github.com" not in text

    assert "FOUNDRY_OPT_RUNTIME_SHA" in setup_text
    assert "FOUNDRY_OPT_RESUME_RECORD" in setup_text
    assert ".foundry-opt/registry.yaml" in setup_text
    assert "agent/.foundry/foundry-opt.yaml" in setup_text
    assert "matrix:" in validation_text
    assert "changed_root: [agent]" in validation_text


def test_deploy_workflow_is_namespaced_and_uses_repository_default_bundle() -> None:
    text = _workflow_text(WORKFLOW_ROOT / "foundry-optimizer-deploy.yml")
    assert "name: Foundry v1 Deploy Agent" in text
    assert "group: foundry-agent-production-${{ github.repository }}-${{ vars.FOUNDRY_REPO_AGENT_ID || 'default-agent' }}" in text
    assert "repository default bundle" in text


def test_semantic_patch_fixtures_are_schema_valid_examples_only() -> None:
    setup_patch = SemanticPatchSpec.from_document(_read(FIXTURE_ROOT / "semantic-patch-setup-workflow.yaml"))
    ssh_patch = SemanticPatchSpec.from_document(_read(FIXTURE_ROOT / "semantic-patch-remove-legacy-ssh.yaml"))

    assert setup_patch.target_path == ".github/workflows/copilot-setup-steps.yml"
    assert ssh_patch.operation == "delete"


def test_customer_templates_omit_forbidden_strings_and_private_material() -> None:
    text_paths = [path for path in CUSTOMER_TEMPLATE_ROOT.rglob("*") if path.is_file()]
    combined = "\n".join(_read(path) for path in text_paths if path.suffix in {".md", ".yml", ".yaml", ".txt", ".gitignore"} or path.name.startswith("."))
    for forbidden in FORBIDDEN_STRINGS:
        assert forbidden not in combined


def test_expected_workflows_present_and_unrelated_examples_preserved() -> None:
    actual = {path.name for path in WORKFLOW_ROOT.glob("*.yml")}
    assert EXPECTED_WORKFLOWS <= actual
    assert (WORKFLOW_ROOT / "agent-ci.yml").exists()


def test_shared_pin_remains_exact_pin_compatible() -> None:
    pin = SharedPin.from_document(_read(PIN_PATH))
    lock_bytes = subprocess.check_output(["git", "show", f"{pin.commit}:uv.lock"], cwd=REPOSITORY_ROOT)
    assert pin.repository_url == "https://github.com/XOEEst/foundry-optimizer-coding-agent-plugin.git"
    assert pin.commit == "92bca79a5faea0718e32101e56b34ebf29c628e3"
    assert pin.package_path == "."
    assert pin.skill_path == "src/foundry_opt/templates/skills/foundry-agent-optimizer"
    assert lock_bytes
