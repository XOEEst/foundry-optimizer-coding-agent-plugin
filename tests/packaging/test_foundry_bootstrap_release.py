from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import yaml

from foundry_opt.repository_contracts import BootstrapSidecar, RootRegistry


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_ROOT = REPOSITORY_ROOT / "plugins" / "foundry-bootstrap"
RELEASE_WORKFLOW = (
    REPOSITORY_ROOT
    / ".github"
    / "workflows"
    / "release-foundry-bootstrap-skill.yml"
)
RELEASE_FIELDS = {
    "repository": "__FOUNDRY_OPT_REPOSITORY__",
    "commit": "__FOUNDRY_OPT_COMMIT__",
    "package_path": "__FOUNDRY_OPT_PACKAGE_PATH__",
    "uv_lock_sha256": "__FOUNDRY_OPT_UV_LOCK_SHA256__",
    "optimizer_skill_path": "__FOUNDRY_OPT_OPTIMIZER_SKILL_PATH__",
}
TEMPLATE_VALUES = {
    "__FOUNDRY_OPT_REPOSITORY__": "https://github.com/example/foundry-opt.git",
    "__FOUNDRY_OPT_COMMIT__": "a" * 40,
    "__FOUNDRY_OPT_PACKAGE_PATH__": ".",
    "__FOUNDRY_OPT_UV_LOCK_SHA256__": "b" * 64,
    "__FOUNDRY_OPT_OPTIMIZER_SKILL_PATH__": "plugins/foundry-agent-optimizer",
    "__GITHUB_OWNER__": "example-org",
    "__GITHUB_REPOSITORY__": "example-repo",
    "__AZURE_IDENTITY_RESOURCE_ID__": (
        "/subscriptions/00000000-0000-0000-0000-000000000000/"
        "resourceGroups/example/providers/Microsoft.ManagedIdentity/"
        "userAssignedIdentities/example"
    ),
    "__AZURE_IDENTITY_CLIENT_ID__": "00000000-0000-0000-0000-000000000001",
    "__REPO_AGENT_ID__": "example-agent",
    "__AGENT_ROOT__": "agent",
    "__AGENT_PACKAGE_ROOT__": "agent",
    "__AGENT_SCAN_SCOPE__": "agents",
    "__SELECTED_AGENT_COUNT__": "1",
    "__EXCLUDED_AGENT_COUNT__": "0",
    "__EXCLUDED_REPO_AGENT_ID_OR_NONE__": "none",
    "__EXCLUDED_AGENT_ROOT_OR_NONE__": "none",
    "__SESSION_INVENTORY_MARKDOWN_PATH__": "session/inventory.md",
    "__SESSION_INVENTORY_CSV_PATH__": "session/inventory.csv",
    "__AGENT_SELECTION_EXPRESSION__": "all",
    "__OPTIMIZER_READINESS__": "ready",
    "__OPTIMIZER_REMEDIATION_OR_NONE__": "none",
    "__EXISTING_REPO_AGENT_ID_OR_NONE__": "none",
    "__EXISTING_AGENT_ROOT_OR_NONE__": "none",
    "__FOUNDRY_PROJECT_ENDPOINT__": (
        "https://example.services.ai.azure.com/api/projects/example"
    ),
    "__FOUNDRY_PROJECT_RESOURCE_ID__": (
        "/subscriptions/00000000-0000-0000-0000-000000000000/"
        "resourceGroups/example/providers/Microsoft.CognitiveServices/"
        "accounts/example/projects/example"
    ),
    "__FOUNDRY_ACCOUNT_RESOURCE_ID__": (
        "/subscriptions/00000000-0000-0000-0000-000000000000/"
        "resourceGroups/example/providers/Microsoft.CognitiveServices/"
        "accounts/example"
    ),
    "__FOUNDRY_AGENT_NAME__": "example-agent",
    "__BASELINE_MODEL_DEPLOYMENT__": "baseline-model",
}


def _render_template(name: str) -> str:
    rendered = (BOOTSTRAP_ROOT / "templates" / name).read_text(encoding="utf-8")
    for token, value in TEMPLATE_VALUES.items():
        rendered = rendered.replace(token, value)
    return rendered


def test_static_skill_has_release_contract_templates_schemas_and_no_executables() -> None:
    expected = {
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
    files = {
        path.relative_to(BOOTSTRAP_ROOT).as_posix()
        for path in BOOTSTRAP_ROOT.rglob("*")
        if path.is_file()
    }

    assert files == expected
    assert not (BOOTSTRAP_ROOT / "scripts").exists()
    assert not any(
        path.suffix.casefold() in {".py", ".ps1", ".sh", ".cmd", ".bat", ".exe"}
        for path in BOOTSTRAP_ROOT.rglob("*")
        if path.is_file()
    )
    release_module = (
        REPOSITORY_ROOT
        / "src"
        / "foundry_opt"
        / "packaging"
        / "foundry_bootstrap_release.py"
    )
    release_source = release_module.read_text(encoding="utf-8")
    assert "def build_foundry_bootstrap_skill" not in release_source
    assert "zipfile" not in release_source
    assert "shutil" not in release_source
    assert "subprocess" not in release_source


def test_release_json_is_only_the_runtime_provenance_source_contract() -> None:
    release = json.loads((BOOTSTRAP_ROOT / "release.json").read_text(encoding="utf-8"))

    assert release == RELEASE_FIELDS
    serialized = json.dumps(release, sort_keys=True)
    assert "azd" not in serialized
    assert "azure.ai.agents" not in serialized
    assert "schema_version" not in release


def test_registry_and_sidecar_templates_render_to_current_contracts() -> None:
    registry = RootRegistry.from_document(_render_template("registry.yaml"))
    sidecar = BootstrapSidecar.from_document(_render_template("sidecar.yaml"))

    assert registry.schema_version == 2
    assert registry.has_exact_runtime_provenance is True
    assert registry.distribution.pin == "a" * 40
    assert registry.distribution.uv_lock_sha256 == "b" * 64
    assert registry.distribution.optimizer_skill_path == (
        "plugins/foundry-agent-optimizer"
    )
    assert sidecar.schema_version == 2
    assert sidecar.repo_agent_id == "example-agent"
    assert sidecar.verification.bundle is None
    assert sidecar.verification.lineage is None


def test_static_schemas_cover_v2_registry_and_sidecar() -> None:
    registry_schema = json.loads(
        (BOOTSTRAP_ROOT / "schemas" / "registry.schema.json").read_text(
            encoding="utf-8"
        )
    )
    sidecar_schema = json.loads(
        (BOOTSTRAP_ROOT / "schemas" / "sidecar.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert registry_schema["properties"]["schema_version"] == {"const": 2}
    assert {
        "repository",
        "pin",
        "package_path",
        "uv_lock_sha256",
        "optimizer_skill_path",
    } <= set(
        registry_schema["properties"]["distribution"]["required"]
    )
    assert sidecar_schema["properties"]["schema_version"] == {"const": 2}
    assert {
        "runtime",
        "foundry_project",
        "deployment",
        "verification",
    } <= set(sidecar_schema["required"])
    bundle_schema = sidecar_schema["properties"]["verification"]["properties"][
        "bundle"
    ]
    assert {
        "development_evaluator_ids",
        "validating_evaluator_ids",
    } <= set(bundle_schema["properties"])


def test_release_workflow_uses_standard_static_zip_and_checksum_tools() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "cp -R \"$source_root/.\" \"$package_root/\"" in workflow
    assert "jq -n" in workflow
    assert "zip -X -q -r" in workflow
    assert "sha256sum" in workflow
    assert "release.json" in workflow
    assert "actions/upload-artifact@" in workflow
    assert "build_foundry_bootstrap_skill.py" not in workflow
    assert "foundry_bootstrap_release" not in workflow
    assert "python " not in workflow


def test_legacy_tool_only_points_to_the_static_release_workflow() -> None:
    tool = REPOSITORY_ROOT / "tools" / "build_foundry_bootstrap_skill.py"
    source = tool.read_text(encoding="utf-8")

    assert "foundry_opt.packaging.foundry_bootstrap_release" not in source
    assert "zipfile" not in source
    assert "shutil" not in source
    assert "subprocess" not in source

    completed = subprocess.run(
        [sys.executable, str(tool)],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert completed.returncode == 0, completed.stderr
    assert "release-foundry-bootstrap-skill.yml" in completed.stdout


def test_yaml_templates_are_parseable_before_rendering() -> None:
    for name in (
        "azure.yaml",
        "copilot-setup-steps.yml",
        "foundry-opt-deploy.yml",
        "foundry-optimize-agent.yml",
        "registry.yaml",
        "sidecar.yaml",
    ):
        assert yaml.safe_load(
            (BOOTSTRAP_ROOT / "templates" / name).read_text(encoding="utf-8")
        )


def test_deploy_template_binds_existing_foundry_project_to_azd_environment() -> None:
    workflow = _render_template("foundry-opt-deploy.yml")

    assert "AZURE_AI_PROJECT_ID:" in workflow
    assert "AZURE_AI_PROJECT_ENDPOINT:" in workflow
    assert 'azd env set AZURE_AI_PROJECT_ID "$AZURE_AI_PROJECT_ID"' in workflow
    assert (
        'test "$(azd env get-value AZURE_AI_PROJECT_ID)" '
        '= "$AZURE_AI_PROJECT_ID"'
    ) in workflow
