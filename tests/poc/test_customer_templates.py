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
WORKFLOW_ROOT = CUSTOMER_TEMPLATE_ROOT / ".github" / "workflows"
EXPECTED_WORKFLOWS = {"agent-ci.yml", "copilot-setup-steps.yml", "foundry-opt-validation.yml", "foundry-opt-deploy.yml"}
FIXTURE_ROOT = REPOSITORY_ROOT / "tests" / "bootstrap" / "fixtures" / "templates"
RUNTIME_SHA = "c899b718f3baebcfd08209ee5184d0cf61d8153d"
FORBIDDEN_STRINGS = ("FOUNDRY_OPT_SHARED_REPO_SSH_KEY", "git@github.com", "known_hosts", "StrictHostKeyChecking")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _yaml_paths() -> list[Path]:
    return sorted({*CUSTOMER_TEMPLATE_ROOT.rglob("*.yml"), *CUSTOMER_TEMPLATE_ROOT.rglob("*.yaml"), *FIXTURE_ROOT.rglob("*.yml"), *FIXTURE_ROOT.rglob("*.yaml")})


def _workflow_text(path: Path) -> str:
    text = _read(path)
    assert "persist-credentials: false" in text, path
    return text


def _apply_semantic_patch(fixture: SemanticPatchSpec, original: str) -> str:
    assert fixture.replacement_text is not None
    return original.replace(fixture.match_text, fixture.replacement_text)


def test_every_customer_template_yaml_document_parses() -> None:
    for path in _yaml_paths():
        assert yaml.safe_load(_read(path)) is not None, path


def test_registry_sidecars_and_pin_align_to_runtime_sha() -> None:
    registry = RootRegistry.from_document(_read(REGISTRY_PATH))
    sidecar = BootstrapSidecar.from_document(_read(SIDECAR_PATH))
    pin = SharedPin.from_document(_read(PIN_PATH))
    assert registry.distribution.pin == RUNTIME_SHA
    assert pin.commit == RUNTIME_SHA
    assert sidecar.editable_paths == ("agent/main.py", "agent/prompts/**", "tests/agent/**")


def test_legacy_single_agent_files_exist_only_as_migration_fixtures() -> None:
    assert f"commit: {RUNTIME_SHA}" in _read(FIXTURE_ROOT / "legacy-single-agent-foundry-opt.lock.yml")
    proposal = import_legacy_single_agent_documents(
        lock_document=_read(FIXTURE_ROOT / "legacy-single-agent-foundry-opt.lock.yml"),
        policy_document=_read(FIXTURE_ROOT / "legacy-single-agent-foundry-optimizer.yaml"),
        metadata_document=_read(FIXTURE_ROOT / "legacy-single-agent-agent-metadata.yaml"),
    )
    assert proposal.registry.agents[0].config_path == "agent/.foundry/foundry-opt.yaml"


def test_setup_uses_venv_python_and_offline_unsets_broker() -> None:
    text = _workflow_text(WORKFLOW_ROOT / "copilot-setup-steps.yml")
    assert "\"$FOUNDRY_OPT_PACKAGE_ROOT/.venv/bin/python\" - <<'PY'" in text
    assert 'unset FOUNDRY_OPT_GITHUB_BINDING' in text
    assert 'unset FOUNDRY_OPT_BROKER_SOCKET' in text
    assert '--head-ref' in text and '--ref-name' in text


def test_setup_and_validation_validate_registry_config_paths_dynamically() -> None:
    for name in ("copilot-setup-steps.yml", "foundry-opt-validation.yml"):
        text = _workflow_text(WORKFLOW_ROOT / name)
        assert 'for agent in registry.agents' in text
        assert 'BootstrapSidecar.from_document' in text
        assert 'for agent in registry.agents' in text


def test_deploy_workflow_computes_dynamic_noop_matrix() -> None:
    text = _workflow_text(WORKFLOW_ROOT / "foundry-opt-deploy.yml")
    assert '.foundry-opt/**' in text
    assert '**/.foundry/foundry-opt.yaml' in text
    assert 'shared_source_relations' in text
    assert "example-agent" not in text
    assert "matrix = {'include': include}" in text
    assert "toJson(fromJson(needs.discover.outputs.matrix).include) != '[]'" in text
    assert "manual repo_agent_id" in text
    assert "MANUAL_REPO_AGENT_ID" in text


def test_issue_form_uses_built_in_parser_contract() -> None:
    document = yaml.safe_load(_read(ISSUE_FORM_PATH))
    intro = document["body"][0]["attributes"]["value"]
    assert "Parser support is built into the runtime now" in intro


def test_semantic_patch_fixture_targets_legacy_workflow_and_applies_cleanly() -> None:
    fixture = SemanticPatchSpec.from_document(_read(FIXTURE_ROOT / "semantic-patch-setup-workflow.yaml"))
    assert fixture.target_path == ".github/workflows/deploy-foundry-agent.yml"
    original = "paths:\n" + fixture.match_text
    patched = _apply_semantic_patch(fixture, original)
    assert yaml.safe_load(patched) == {"paths": [".foundry-opt/registry.yaml", "agent/.foundry/foundry-opt.yaml"]}


def test_customer_templates_omit_forbidden_strings_and_private_material() -> None:
    text_paths = [path for path in CUSTOMER_TEMPLATE_ROOT.rglob("*") if path.is_file()]
    combined = "\n".join(_read(path) for path in text_paths if path.suffix in {".md", ".yml", ".yaml", ".txt", ".gitignore"} or path.name.startswith("."))
    for forbidden in FORBIDDEN_STRINGS:
        assert forbidden not in combined


def test_expected_workflows_present_and_legacy_active_templates_removed() -> None:
    actual = {path.name for path in WORKFLOW_ROOT.glob("*.yml")}
    assert EXPECTED_WORKFLOWS <= actual
    assert "deploy-foundry-agent.yml" not in actual
    assert "foundry-optimizer-validation.yml" not in actual


def test_shared_pin_matches_locked_runtime_artifact() -> None:
    pin = SharedPin.from_document(_read(PIN_PATH))
    lock_bytes = subprocess.check_output(["git", "show", f"{pin.commit}:uv.lock"], cwd=REPOSITORY_ROOT)
    assert pin.repository_url == "https://github.com/XOEEst/foundry-optimizer-coding-agent-plugin.git"
    assert pin.commit == RUNTIME_SHA
    assert lock_bytes
