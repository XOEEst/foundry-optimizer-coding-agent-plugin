"""The v1 managed-file contract: eight rendered payloads plus a generated JSON lock.

`.foundry-opt/bootstrap.lock.json` is the authoritative committed lock and is produced by
repository apply, never rendered from a template. The legacy `.github/foundry-opt.lock.yml`
shared pin is migration-only: it is not a managed payload, is not shipped in the customer
templates, and is refused by the trusted manifest.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from foundry_opt.bootstrap.errors import BootstrapConfigError
from foundry_opt.bootstrap.input_contracts import (
    LEGACY_LOCK_PATH,
    MANAGED_LOCK_PATH,
    TrustedTemplateManifest,
)
from foundry_opt.bootstrap.repository.engine import LOCK_PATH
from foundry_opt.cli import app

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_ROOT = REPOSITORY_ROOT / "src" / "foundry_opt" / "templates" / "customer-repo"
MANIFEST_PATHS = (
    TEMPLATE_ROOT / ".foundry-opt" / "managed-payloads.manifest.yaml",
    TEMPLATE_ROOT / "agent" / ".foundry" / "managed-payloads.manifest.yaml",
)
EXPECTED_PAYLOADS = (
    ("registry", ".foundry-opt/registry.yaml"),
    ("sidecar", "{selected.root}/.foundry/foundry-opt.yaml"),
    ("optimizer-instruction", ".github/instructions/foundry-opt.instructions.md"),
    ("optimizer-issue-form", ".github/ISSUE_TEMPLATE/foundry-optimize-agent.yml"),
    ("custom-agent", ".github/agents/foundry-optimizer.agent.md"),
    ("setup-semantic-patch", ".github/workflows/copilot-setup-steps.yml"),
    ("validation-workflow", ".github/workflows/foundry-opt-validation.yml"),
    ("deploy-workflow", ".github/workflows/foundry-opt-deploy.yml"),
)

CONTRACT_ERRORS = (BootstrapConfigError, ValidationError)
runner = CliRunner()


def _git(path: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *arguments],
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    return completed.stdout.strip()


def _runtime_checkout(tmp_path: Path) -> tuple[Path, str, str]:
    checkout = tmp_path / "runtime"
    (checkout / "src" / "foundry_opt" / "templates" / "skills" / "foundry-agent-optimizer").mkdir(parents=True)
    (checkout / "src" / "foundry_opt" / "templates" / "skills" / "foundry-agent-optimizer" / "SKILL.md").write_text(
        "# skill\n", encoding="utf-8", newline="\n"
    )
    (checkout / "uv.lock").write_text("version = 1\n", encoding="utf-8", newline="\n")
    _git(checkout, "init")
    _git(checkout, "config", "user.name", "Managed File Test")
    _git(checkout, "config", "user.email", "managed@example.invalid")
    _git(checkout, "config", "core.autocrlf", "false")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-m", "initial")
    head = _git(checkout, "rev-parse", "HEAD")
    return checkout, head, hashlib.sha256((checkout / "uv.lock").read_bytes()).hexdigest()


def _registry_document(commit: str) -> str:
    return yaml.safe_dump(
        {
            "schema_version": 1,
            "distribution": {
                "schema_version": 1,
                "repository": "https://github.com/XOEEst/foundry-optimizer-coding-agent-plugin.git",
                "channel": "pinned",
                "pin": commit,
            },
            "github": {
                "schema_version": 1,
                "optimizer_environment": "copilot",
                "deployment_environment": "foundry-production",
                "client_id_variable": "AZURE_FOUNDRY_OPT_CLIENT_ID",
            },
            "identity": {"schema_version": 1, "kind": "unresolved_migration"},
            "agents": [],
        },
        sort_keys=False,
    )


def test_trusted_manifest_pins_exactly_the_v1_payload_set() -> None:
    manifest = TrustedTemplateManifest.load_pinned_manifest()

    assert tuple((item.template_id, item.destination_path) for item in manifest.managed_payloads) == EXPECTED_PAYLOADS
    assert len(manifest.managed_payloads) == 8
    assert LEGACY_LOCK_PATH not in {item.destination_path for item in manifest.managed_payloads}
    assert MANAGED_LOCK_PATH not in {item.destination_path for item in manifest.managed_payloads}


@pytest.mark.parametrize("manifest_path", MANIFEST_PATHS, ids=lambda path: path.parent.name)
def test_shipped_manifests_drop_the_legacy_lock_payload(manifest_path: Path) -> None:
    document = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    payloads = document["managed_payloads"]

    assert [item["template_id"] for item in payloads] == [item[0] for item in EXPECTED_PAYLOADS]
    assert "bootstrap-lock" not in {item["template_id"] for item in payloads}
    assert LEGACY_LOCK_PATH not in {item["destination_path"] for item in payloads}
    assert LEGACY_LOCK_PATH not in {item["source_template_path"] for item in payloads}


def test_customer_templates_no_longer_ship_the_legacy_lock() -> None:
    assert not (TEMPLATE_ROOT / ".github" / "foundry-opt.lock.yml").exists()
    assert MANAGED_LOCK_PATH == ".foundry-opt/bootstrap.lock.json" == LOCK_PATH


def test_manifest_refuses_the_legacy_lock_payload() -> None:
    document = yaml.safe_load(MANIFEST_PATHS[0].read_text(encoding="utf-8"))
    document["managed_payloads"].append(
        {
            "schema_version": 1,
            "template_id": "bootstrap-lock",
            "template_version": "1.0.0",
            "source_template_path": "src/foundry_opt/templates/customer-repo/.github/foundry-opt.lock.yml",
            "destination_path": LEGACY_LOCK_PATH,
            "ownership_mode": "owned",
            "semantic_patch_mode": "none",
            "scope": "repository",
            "required": True,
        }
    )

    with pytest.raises(CONTRACT_ERRORS, match="the committed lock is .foundry-opt/bootstrap.lock.json"):
        TrustedTemplateManifest.model_validate(document)


def test_manifest_refuses_rendering_the_generated_lock() -> None:
    document = yaml.safe_load(MANIFEST_PATHS[0].read_text(encoding="utf-8"))
    document["managed_payloads"][0] = {
        **document["managed_payloads"][0],
        "destination_path": MANAGED_LOCK_PATH,
    }

    with pytest.raises(CONTRACT_ERRORS, match="generated by repository apply"):
        TrustedTemplateManifest.model_validate(document)


def test_manifest_hash_reflects_the_reduced_payload_set() -> None:
    manifest = TrustedTemplateManifest.load_pinned_manifest()
    reparsed = TrustedTemplateManifest.from_document(MANIFEST_PATHS[0].read_text(encoding="utf-8"))

    assert manifest.manifest_hash == reparsed.manifest_hash
    assert len(manifest.manifest_hash) == 64


def test_published_schema_is_regenerated_for_the_managed_payload_set() -> None:
    schema_path = REPOSITORY_ROOT / "schemas" / "managed-payloads.schema.json"
    generated = TrustedTemplateManifest.model_json_schema()

    assert json.loads(schema_path.read_text(encoding="utf-8")) == generated


def test_verify_accepts_the_registry_pin_without_a_legacy_lock(tmp_path: Path) -> None:
    checkout, commit, digest = _runtime_checkout(tmp_path)
    registry = tmp_path / "registry.yaml"
    registry.write_text(_registry_document(commit), encoding="utf-8")
    receipt = tmp_path / "receipt.json"

    result = runner.invoke(
        app,
        [
            "bootstrap",
            "verify",
            "--registry",
            str(registry),
            "--uv-lock-sha256",
            digest,
            "--checkout",
            str(checkout),
            "--receipt",
            str(receipt),
        ],
    )

    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["status"] == "verified"
    assert payload["commit"] == commit
    assert json.loads(receipt.read_text(encoding="utf-8"))["commit"] == commit


def test_verify_requires_exactly_one_pin_source(tmp_path: Path) -> None:
    checkout, commit, digest = _runtime_checkout(tmp_path)
    registry = tmp_path / "registry.yaml"
    registry.write_text(_registry_document(commit), encoding="utf-8")

    neither = runner.invoke(
        app,
        ["bootstrap", "verify", "--checkout", str(checkout), "--receipt", str(tmp_path / "a.json")],
    )
    assert neither.exit_code == 20
    assert "exactly one of --pin or --registry" in neither.stdout

    missing_digest = runner.invoke(
        app,
        [
            "bootstrap",
            "verify",
            "--registry",
            str(registry),
            "--checkout",
            str(checkout),
            "--receipt",
            str(tmp_path / "b.json"),
        ],
    )
    assert missing_digest.exit_code == 20
    assert "uv-lock-digest-required" in missing_digest.stdout
    assert digest not in missing_digest.stdout


def test_verify_still_reads_a_legacy_shared_pin_for_migration(tmp_path: Path) -> None:
    checkout, commit, digest = _runtime_checkout(tmp_path)
    pin = tmp_path / "foundry-opt.lock.yml"
    pin.write_text(
        "\n".join(
            (
                "schema_version: 1",
                "repository_url: https://github.com/XOEEst/foundry-optimizer-coding-agent-plugin.git",
                f"commit: '{commit}'",
                "package_path: '.'",
                "skill_path: src/foundry_opt/templates/skills/foundry-agent-optimizer",
                f"uv_lock_sha256: '{digest}'",
                "",
            )
        ),
        encoding="utf-8",
    )
    receipt = tmp_path / "legacy-receipt.json"

    result = runner.invoke(
        app,
        ["bootstrap", "verify", "--pin", str(pin), "--checkout", str(checkout), "--receipt", str(receipt)],
    )

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["commit"] == commit


def test_registry_without_an_exact_pin_fails_closed(tmp_path: Path) -> None:
    checkout, commit, digest = _runtime_checkout(tmp_path)
    document = yaml.safe_load(_registry_document(commit))
    document["distribution"]["pin"] = None
    document["distribution"]["channel"] = "main"
    registry = tmp_path / "registry.yaml"
    registry.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "bootstrap",
            "verify",
            "--registry",
            str(registry),
            "--uv-lock-sha256",
            digest,
            "--checkout",
            str(checkout),
            "--receipt",
            str(tmp_path / "c.json"),
        ],
    )

    assert result.exit_code == 20
    assert "registry-pin-required" in result.stdout


def test_setup_workflow_verifies_against_the_registry() -> None:
    setup = (TEMPLATE_ROOT / ".github" / "workflows" / "copilot-setup-steps.yml").read_text(encoding="utf-8")

    assert "bootstrap verify --registry .foundry-opt/registry.yaml --uv-lock-sha256" in setup
    assert LEGACY_LOCK_PATH not in setup
    assert ".foundry-opt/bootstrap.lock.json" in setup
