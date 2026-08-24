from __future__ import annotations

from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
EXAMPLE_ROOT = REPOSITORY_ROOT / "examples" / "github-actions"
PR_EXAMPLE = EXAMPLE_ROOT / "foundry-opt-pr-evaluation.yml"
MAIN_EXAMPLE = EXAMPLE_ROOT / "foundry-opt-main-deployment-gate.yml"
DOCS_PATH = REPOSITORY_ROOT / "docs" / "get-started" / "evaluation-gates.md"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_example_github_action_yaml_documents_parse() -> None:
    for path in (PR_EXAMPLE, MAIN_EXAMPLE):
        assert yaml.safe_load(_read(path)) is not None, path


def test_examples_live_outside_active_workflow_directory() -> None:
    for path in (PR_EXAMPLE, MAIN_EXAMPLE):
        assert path.is_file(), path
        assert path.relative_to(REPOSITORY_ROOT).parts[:2] == (
            "examples",
            "github-actions",
        )


def test_pr_example_uses_exact_pr_head_verify_only_and_oidc() -> None:
    text = _read(PR_EXAMPLE)

    assert "pull_request:" in text
    assert "workflow_dispatch:" in text
    assert "id-token: write" in text
    assert "github.event.pull_request.head.sha" in text
    assert "--repo-agent-id" in text
    assert "foundry-opt deploy verify-registered" in text
    assert ".foundry-opt/registry.yaml" in text
    assert ".foundry-opt/bootstrap.lock.json" not in text
    assert "uv_lock_sha256" in text
    assert "FOUNDRY_OPT_DEPLOYMENT_CLIENT_ID" in text
    assert "AZURE_TENANT_ID" in text
    assert "GH_TOKEN: ${{ github.token }}" in text
    assert 'export GITHUB_SHA="${{ steps.inputs.outputs.exact_source }}"' in text
    assert "actions/upload-artifact@v4" in text
    assert "WARNING: Unverified deployment permitted" in text


def test_main_example_verifies_before_publish_and_summarizes_gate_behavior() -> None:
    text = _read(MAIN_EXAMPLE)

    assert "push:" in text
    assert "      - main" in text
    assert "environment: foundry-production" in text
    assert "GH_TOKEN" in text
    assert "Stop before publish when verification failed" in text
    assert "required Foundry evaluation" in text
    assert "optional repository checks fallback" in text
    assert "no-evidence / off path permitted by policy" in text
    assert "WARNING: Unverified deployment permitted" in text
    assert text.index("foundry-opt deploy verify-registered") < text.index(
        "foundry-opt deploy publish-registered"
    )


def test_evaluation_gate_docs_reference_copy_paths_and_configuration() -> None:
    text = _read(DOCS_PATH)

    assert "examples/github-actions/foundry-opt-pr-evaluation.yml" in text
    assert ".github/workflows/foundry-opt-pr-evaluation.yml" in text
    assert "examples/github-actions/foundry-opt-main-deployment-gate.yml" in text
    assert ".github/workflows/foundry-opt-main-deployment-gate.yml" in text
    assert "do **not** activate merely by existing there" in text
    assert "foundry-opt deploy verify-registered" in text
    assert "FOUNDRY_OPT_DEPLOYMENT_CLIENT_ID" in text
    assert "GH_TOKEN" in text
