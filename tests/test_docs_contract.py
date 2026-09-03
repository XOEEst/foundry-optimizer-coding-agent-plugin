from __future__ import annotations

from pathlib import Path

from tools.check_docs import collect_violations


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (REPOSITORY_ROOT / path).read_text(encoding="utf-8")


def test_documentation_links_and_public_safety_are_valid() -> None:
    assert collect_violations() == []


def test_managed_files_document_lists_the_skill_only_core() -> None:
    documentation = _read("docs/managed-files.md")

    for path in (
        ".foundry-opt/registry.yaml",
        ".foundry-opt/bootstrap-report.md",
        "azure.yaml",
        ".github/workflows/copilot-setup-steps.yml",
        ".github/skills/foundry-agent-optimizer",
        ".github/workflows/foundry-opt-deploy.yml",
    ):
        assert path in documentation
    assert "ownership ledger" in documentation


def test_skill_runtime_seam_documents_the_complete_owner_interface() -> None:
    documentation = _read("docs/architecture/skill-runtime-seam.md")

    assert "one approval" in documentation
    assert "no operation ID" in documentation
    assert "does not roll back" in documentation


def test_cli_reference_covers_the_current_public_command_tree() -> None:
    documentation = _read("docs/reference/cli.md")
    commands = (
        "version",
        "validate-config",
        "preflight",
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
        "deploy plan",
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
    assert "registry v2" in documentation.casefold()
