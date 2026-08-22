from __future__ import annotations

import re
from pathlib import Path

import yaml

from tools.check_docs import collect_violations


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (REPOSITORY_ROOT / path).read_text(encoding="utf-8")


def test_documentation_links_and_public_safety_are_valid() -> None:
    assert collect_violations() == []


def test_managed_files_document_matches_the_manifest() -> None:
    manifest = yaml.safe_load(
        _read(
            "src/foundry_opt/templates/customer-repo/"
            ".foundry-opt/managed-payloads.manifest.yaml"
        )
    )
    payloads = manifest["managed_payloads"]
    documentation = _read("docs/managed-files.md")
    documented_ids = set(
        re.findall(r"^\| `([^`]+)` \|", documentation, flags=re.MULTILINE)
    )

    assert documented_ids == {payload["template_id"] for payload in payloads}


def test_skill_runtime_seam_documents_the_complete_owner_interface() -> None:
    documentation = (
        _read("docs/architecture/skill-runtime-seam.md")
        + _read("docs/owner-review.md")
    )

    for operation in ("start", "answer", "approve", "status", "rollback"):
        assert f"`{operation}`" in documentation


def test_cli_reference_covers_the_current_public_command_tree() -> None:
    documentation = _read("docs/reference/cli.md")
    commands = (
        "version",
        "validate-config",
        "preflight",
        "bootstrap verify",
        "bootstrap discover",
        "bootstrap binding-evidence",
        "bootstrap plan",
        "bootstrap status",
        "bootstrap diff",
        "bootstrap apply",
        "bootstrap rollback",
        "bootstrap resources",
        "bootstrap review discovery",
        "bootstrap review plan",
        "bootstrap review status",
        "bootstrap connect plan",
        "bootstrap connect approve",
        "bootstrap connect apply",
        "bootstrap connect status",
        "bootstrap connect rollback",
        "bootstrap evaluation inventory",
        "bootstrap evaluation plan",
        "bootstrap evaluation apply",
        "bootstrap evaluation activate",
        "bootstrap evaluation status",
        "bootstrap evaluation inspect",
        "bootstrap evaluation replace",
        "broker launch",
        "broker bind-pr",
        "issue parse",
        "job start",
        "job status",
        "job handoff",
        "job complete",
        "job finish",
        "job resume",
        "acceptance smoke",
        "deploy preflight",
        "deploy plan",
        "deploy publish",
        "deploy verify-registered",
        "deploy publish-registered",
    )

    for command in commands:
        assert f"`foundry-opt {command}`" in documentation


def test_owner_docs_do_not_reintroduce_candidate_model_selection() -> None:
    owner_docs = (
        _read("docs/get-started/issues-and-monitoring.md")
        + _read("docs/guides/run-an-optimization.md")
        + _read(
            "src/foundry_opt/templates/customer-repo/"
            ".github/ISSUE_TEMPLATE/foundry-optimize-agent.yml"
        )
    )

    assert "Optional narrower model set" not in owner_docs
    assert "id: candidate_models" not in owner_docs


def test_distribution_docs_do_not_publish_a_global_current_sha() -> None:
    documentation = _read("docs/distribution.md")

    assert "current reviewed customer runtime" not in documentation
    assert "operation-specific" in documentation
