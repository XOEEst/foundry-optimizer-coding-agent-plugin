from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import yaml

from foundry_opt.repository_contracts import BootstrapSidecar, RootRegistry

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
CUSTOMER_TEMPLATE_ROOT = REPOSITORY_ROOT / "src" / "foundry_opt" / "templates" / "customer-repo"
REGISTRY_PATH = CUSTOMER_TEMPLATE_ROOT / ".foundry-opt" / "registry.yaml"
SIDECAR_PATH = CUSTOMER_TEMPLATE_ROOT / "agent" / ".foundry" / "foundry-opt.yaml"
INSTRUCTIONS_PATH = CUSTOMER_TEMPLATE_ROOT / ".github" / "instructions" / "foundry-opt.instructions.md"
ISSUE_FORM_PATH = CUSTOMER_TEMPLATE_ROOT / ".github" / "ISSUE_TEMPLATE" / "foundry-optimize-agent.yml"
WORKFLOW_ROOT = CUSTOMER_TEMPLATE_ROOT / ".github" / "workflows"
OPTIONAL_CUSTOM_AGENT = (
    REPOSITORY_ROOT / "examples" / "custom-agents" / "foundry-optimizer.agent.md"
)
EXPECTED_WORKFLOWS = {"agent-ci.yml", "copilot-setup-steps.yml", "foundry-opt-validation.yml", "foundry-opt-deploy.yml"}
RUNTIME_SHA = "770ad878f0658e9368b042d9a7f6732e49ff0200"
FORBIDDEN_STRINGS = ("FOUNDRY_OPT_SHARED_REPO_SSH_KEY", "git@github.com", "known_hosts", "StrictHostKeyChecking")


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
    assert "persist-credentials: false" in text, path
    return text


def test_every_customer_template_yaml_document_parses() -> None:
    for path in _yaml_paths():
        assert yaml.safe_load(_read(path)) is not None, path


def test_registry_sidecars_and_pin_align_to_runtime_sha() -> None:
    registry = RootRegistry.from_document(_read(REGISTRY_PATH))
    sidecar = BootstrapSidecar.from_document(_read(SIDECAR_PATH))
    assert registry.distribution.pin == RUNTIME_SHA
    assert sidecar.editable_paths == ("agent/main.py", "agent/prompts/**", "tests/agent/**")
    assert sidecar.verification.repository_checks == ()
    assert sidecar.verification.evaluation_gate_policy == "allow_no_evidence"


def test_customer_templates_do_not_ship_the_legacy_shared_pin() -> None:
    assert not (CUSTOMER_TEMPLATE_ROOT / ".github" / "foundry-opt.lock.yml").exists()
    assert not (
        CUSTOMER_TEMPLATE_ROOT / ".github" / "foundry-optimizer.yaml"
    ).exists()
    assert not (
        CUSTOMER_TEMPLATE_ROOT / ".foundry" / "agent-metadata.yaml"
    ).exists()
    assert not (
        CUSTOMER_TEMPLATE_ROOT / ".foundry-opt" / "bootstrap.lock.json"
    ).exists()
    assert not any(
        CUSTOMER_TEMPLATE_ROOT.rglob("managed-payloads.manifest.yaml")
    )
    combined = "\n".join(
        _read(path)
        for path in CUSTOMER_TEMPLATE_ROOT.rglob("*")
        if path.is_file() and path.suffix in {".yml", ".yaml", ".md"}
    )
    assert ".github/foundry-opt.lock.yml" not in combined


def test_default_bootstrap_does_not_install_a_custom_agent() -> None:
    assert not (
        CUSTOMER_TEMPLATE_ROOT
        / ".github"
        / "agents"
        / "foundry-optimizer.agent.md"
    ).exists()
    text = _read(OPTIONAL_CUSTOM_AGENT)
    assert "target: github-copilot" in text
    assert "explicitly selects" in text


def test_setup_uses_venv_python_and_offline_unsets_broker() -> None:
    text = _workflow_text(WORKFLOW_ROOT / "copilot-setup-steps.yml")
    assert "\"$FOUNDRY_OPT_PACKAGE_ROOT/.venv/bin/python\" - <<'PY'" in text
    assert 'python3 -m pip install' in text
    assert '"uv==0.11.6"' in text
    assert 'FOUNDRY_OPT_SKILL_SOURCE=$shared_root/plugins/foundry-agent-optimizer' in text
    assert 'src/foundry_opt/templates/skills/foundry-agent-optimizer' not in text
    assert 'unset FOUNDRY_OPT_GITHUB_BINDING' in text
    assert 'unset FOUNDRY_OPT_BROKER_SOCKET' in text
    assert '--head-ref' in text and '--ref-name' in text


def test_setup_and_validation_allow_missing_inactive_sidecars() -> None:
    for name in ("copilot-setup-steps.yml", "foundry-opt-validation.yml"):
        text = _workflow_text(WORKFLOW_ROOT / name)
        assert 'for agent in registry.agents' in text
        assert 'if not sidecar_path.exists()' in text
        assert 'assert not agent.enabled' in text
        assert 'AgentProfile.from_document' in text


def test_deploy_workflow_computes_dynamic_noop_matrix() -> None:
    text = _workflow_text(WORKFLOW_ROOT / "foundry-opt-deploy.yml")
    assert "repos/${GITHUB_REPOSITORY}" in text
    assert ".default_branch" in text
    assert 'refs/heads/$default_branch' in text
    assert 'commits/$default_branch' in text
    assert 'test "$GITHUB_SHA" = "$default_tip"' in text
    assert 'shared_source_relations' in text
    assert "example-agent" not in text
    assert "matrix = {'include': include}" in text
    assert "toJson(fromJson(needs.discover.outputs.matrix).include) != '[]'" in text
    assert "manual repo_agent_id" in text
    assert "path == '.foundry-opt/registry.yaml'" in text
    assert "path.startswith('.foundry-opt/')" not in text
    assert "MANUAL_REPO_AGENT_ID" in text
    assert "${{ github.event.inputs.repo_agent_id" not in text
    assert "foundry-opt deploy publish-registered" in text
    assert "FOUNDRY_OPT_DEPLOY_PLAN_PATH" in text
    assert "Deployment verification" in text
    assert "WARNING: Unverified deployment permitted" in text
    assert "verification.get(\"warning\")" in text
    assert "FOUNDRY_OPT_DEPLOYMENT_CLIENT_ID" in text
    assert "AZURE_TENANT_ID" in text
    assert "AZURE_DEPLOYMENT_CLIENT_ID" not in text


def test_issue_form_uses_built_in_parser_contract() -> None:
    document = yaml.safe_load(_read(ISSUE_FORM_PATH))
    intro = document["body"][0]["attributes"]["value"]
    serialized = _read(ISSUE_FORM_PATH)
    assert "Parser support is built into the runtime now" in intro
    assert "final post-merge repin" in intro
    assert "qualitative-only fallback" in intro
    assert "Optional narrower model set" not in serialized
    assert "id: candidate_models" not in serialized
    assert "Optional primary metric" in serialized
    assert "Repository agent ID or explicit Foundry target" in serialized
    assert "task_completion" in serialized
    assert (
        "azureml://registries/azureml/evaluators/"
        "builtin.task_completion/versions/19"
    ) in serialized
    assert "definition-scoped inline criteria" in serialized
    assert "Leave blank to reuse repository defaults" in serialized


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
    registry = RootRegistry.from_document(_read(REGISTRY_PATH))
    assert registry.distribution.repository == "https://github.com/XOEEst/foundry-optimizer-coding-agent-plugin.git"
    assert registry.distribution.pin == RUNTIME_SHA
    lock_bytes = subprocess.check_output(["git", "show", f"{registry.distribution.pin}:uv.lock"], cwd=REPOSITORY_ROOT)
    assert lock_bytes
    setup = _read(WORKFLOW_ROOT / "copilot-setup-steps.yml")
    assert "foundry-opt bootstrap" not in setup
    assert "sha256sum" in setup
    assert "RepositoryRegistry.from_document" in setup
    assert hashlib.sha256(lock_bytes).hexdigest() in setup


def test_skill_only_core_templates_include_azd_and_report() -> None:
    azure = yaml.safe_load(_read(CUSTOMER_TEMPLATE_ROOT / "azure.yaml"))
    service = azure["services"]["example-agent"]

    assert service["host"] == "azure.ai.agent"
    assert service["codeConfiguration"]["runtime"] == "python_3_13"
    report = _read(
        CUSTOMER_TEMPLATE_ROOT / ".foundry-opt" / "bootstrap-report.md"
    )
    assert "## GitHub changes" in report
    assert "## Azure changes" in report
    assert "must not contain credentials" in report
