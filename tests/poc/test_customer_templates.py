from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from foundry_opt.bootstrap.contracts import BootstrapSidecar, RootRegistry, SemanticPatchSpec
from foundry_opt.bootstrap.legacy import import_legacy_single_agent_documents
from foundry_opt.poc.config import SharedPin

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CUSTOMER_TEMPLATE_ROOT = REPOSITORY_ROOT / "src" / "foundry_opt" / "templates" / "customer-repo"
PIN_PATH = CUSTOMER_TEMPLATE_ROOT / ".github" / "foundry-opt.lock.yml"
REGISTRY_PATH = CUSTOMER_TEMPLATE_ROOT / ".foundry-opt" / "registry.yaml"
SIDECAR_PATH = CUSTOMER_TEMPLATE_ROOT / "agent" / ".foundry" / "foundry-opt.yaml"
INSTRUCTIONS_PATH = CUSTOMER_TEMPLATE_ROOT / ".github" / "instructions" / "foundry-opt.instructions.md"
ISSUE_FORM_PATH = CUSTOMER_TEMPLATE_ROOT / ".github" / "ISSUE_TEMPLATE" / "foundry-optimize-agent.yml"
CUSTOM_AGENT_PATH = CUSTOMER_TEMPLATE_ROOT / ".github" / "agents" / "foundry-optimizer.agent.md"
WORKFLOW_ROOT = CUSTOMER_TEMPLATE_ROOT / ".github" / "workflows"
EXPECTED_WORKFLOWS = {
    "agent-ci.yml",
    "copilot-setup-steps.yml",
    "foundry-opt-validation.yml",
    "foundry-opt-deploy.yml",
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
    assert sidecar.editable_paths == ("agent/main.py", "agent/prompts/**", "tests/agent/**")
    assert sidecar.max_issue_evaluators == 8


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
    assert ".foundry-opt/**" in text
    assert "agent/.foundry/**" in text


def test_issue_form_uses_exact_v1_headings_and_ids() -> None:
    document = yaml.safe_load(_read(ISSUE_FORM_PATH))
    entries = {entry["id"]: entry for entry in document["body"] if isinstance(entry, dict) and "id" in entry}

    assert ISSUE_FORM_PATH.name == "foundry-optimize-agent.yml"
    assert entries["repo_agent_id"]["attributes"]["label"] == "Repository agent ID or explicit Foundry target"
    assert entries["optimization_goal"]["attributes"]["label"] == "Optimization goal"
    assert entries["observed_failures"]["attributes"]["label"] == "Observed failures or evidence"
    assert entries["constraints"]["attributes"]["label"] == "Constraints and guardrails"
    assert entries["candidate_budget"]["attributes"]["label"] == "Changed candidates"
    assert entries["editable_scope"]["attributes"]["label"] == "Optional narrower editable scope"
    assert entries["candidate_models"]["attributes"]["label"] == "Optional narrower model set"
    assert entries["issue_evaluators"]["attributes"]["label"] == "Optional exact evaluator IDs"
    assert "weight=<positive>" in entries["issue_evaluators"]["attributes"]["description"]


def test_custom_agent_freezes_issue_inputs_and_uses_validating_winner_only() -> None:
    text = _read(CUSTOM_AGENT_PATH)
    assert ".foundry-opt/registry.yaml" in text
    assert "Freeze the issue objective, constraints, explicit evaluator set" in text
    assert "validate only the provisional winner" in text
    assert "Deployment uses the repository default bundle" in text


def test_setup_payload_uses_exact_v1_sha_and_validates_registry_and_sidecars() -> None:
    text = _workflow_text(WORKFLOW_ROOT / "copilot-setup-steps.yml")
    assert 'repository="https://github.com/XOEEst/foundry-optimizer-coding-agent-plugin.git"' in text
    assert 'commit="c899b71"' in text
    assert "final pin updated during integration" not in text
    assert "registry.yaml" in text and "foundry-opt.yaml" in text
    assert "FOUNDRY_OPT_BROKER_SOCKET" in text
    assert "FOUNDRY_OPT_BROKER_READY" in text
    assert "FOUNDRY_OPT_BROKER_LIFETIME" in text
    assert "validate-config" not in text


def test_validation_workflow_preserves_root_copilot_instructions_and_uses_new_name() -> None:
    text = _workflow_text(WORKFLOW_ROOT / "foundry-opt-validation.yml")
    assert "name: Foundry v1 Validation" in text
    assert ".github/ISSUE_TEMPLATE/foundry-optimize-agent.yml" in text
    assert "test -f .github/copilot-instructions.md || true" in text
    assert "foundry-opt-validation.yml" in text


def test_deploy_workflow_uses_changed_root_dispatcher_matrix_and_default_contract() -> None:
    text = _workflow_text(WORKFLOW_ROOT / "foundry-opt-deploy.yml")
    assert "matrix:" in text
    assert "changed_root" in text
    assert "concurrency:" in text
    assert "check-eligibility" in text
    assert "use-repository-default-evaluators" in text
    assert "exact-source" in text


def test_semantic_patch_fixtures_keep_distinct_yaml_paths() -> None:
    setup_patch = SemanticPatchSpec.from_document(_read(FIXTURE_ROOT / "semantic-patch-setup-workflow.yaml"))
    ssh_patch = SemanticPatchSpec.from_document(_read(FIXTURE_ROOT / "semantic-patch-remove-legacy-ssh.yaml"))

    assert setup_patch.target_path == ".github/workflows/copilot-setup-steps.yml"
    assert "registry.yaml" in (setup_patch.replacement_text or "")
    assert "agent/.foundry/foundry-opt.yaml" in (setup_patch.replacement_text or "")
    lines = (setup_patch.replacement_text or "").splitlines()
    assert len([line for line in lines if line.strip().startswith("-")]) == 2
    assert ssh_patch.operation == "delete"


def test_customer_templates_omit_forbidden_strings_and_private_material() -> None:
    text_paths = [path for path in CUSTOMER_TEMPLATE_ROOT.rglob("*") if path.is_file()]
    combined = "\n".join(_read(path) for path in text_paths if path.suffix in {".md", ".yml", ".yaml", ".txt", ".gitignore"} or path.name.startswith("."))
    for forbidden in FORBIDDEN_STRINGS:
        assert forbidden not in combined


def test_expected_workflows_present_and_legacy_active_templates_removed() -> None:
    actual = {path.name for path in WORKFLOW_ROOT.glob("*.yml")}
    assert EXPECTED_WORKFLOWS <= actual
    assert "foundry-optimizer-validation.yml" not in actual
    assert "foundry-optimizer-deploy.yml" not in actual


def test_shared_pin_remains_exact_pin_compatible() -> None:
    pin = SharedPin.from_document(_read(PIN_PATH))
    lock_bytes = subprocess.check_output(["git", "show", f"{pin.commit}:uv.lock"], cwd=REPOSITORY_ROOT)
    assert pin.repository_url == "https://github.com/XOEEst/foundry-optimizer-coding-agent-plugin.git"
    assert pin.package_path == "."
    assert pin.skill_path == "src/foundry_opt/templates/skills/foundry-agent-optimizer"
    assert lock_bytes
