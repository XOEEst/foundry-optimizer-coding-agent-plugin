from __future__ import annotations

import hashlib
from pathlib import Path

import yaml


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = (
    REPOSITORY_ROOT
    / "src"
    / "foundry_opt"
    / "templates"
    / "skills"
    / "agent-optimizer-v2"
)
TENZING_COMMIT = "7300a83fc7378f0f1a401dbdf8ed28358ccf1732"
TENZING_HASH = "8651530161f62650b3f587584301f41deae087f87b66ce83e893ae99f26a6aac"


def _load_yaml(path: Path) -> dict[str, object]:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict), path
    return value


def _normalized_bytes(path: Path) -> bytes:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    if not text.endswith("\n"):
        text += "\n"
    return text.encode("utf-8")


def test_package_is_a_minimal_skill() -> None:
    package = _load_yaml(PACKAGE_ROOT / "package.yaml")
    assert package == {
        "schema_version": 1,
        "id": "agent-optimizer-v2",
        "version": "0.11.0",
        "status": "ready",
        "entrypoint": "SKILL.md",
        "target_template": "templates/target.yaml",
        "loop_guide": "guides/loop.md",
        "operations_guide": "guides/operations.md",
        "plan_guide": "guides/plan.md",
        "data_guide": "guides/data.md",
        "tracking_guide": "guides/tracking.md",
        "scorecard_guide": "guides/scorecard.md",
        "tenzing": {
            "source_manifest": "references/tenzing/SOURCE_MANIFEST.yaml",
            "source": "references/tenzing/climb.md",
        },
        "run_templates": {
            "state": "templates/run-state.yaml",
            "result": "templates/result.yaml",
        },
    }
    assert {path.name for path in PACKAGE_ROOT.iterdir()} == {
        "README.md",
        "SKILL.md",
        "package.yaml",
        "guides",
        "references",
        "templates",
    }
    assert {path.name for path in (PACKAGE_ROOT / "guides").iterdir()} == {
        "loop.md",
        "operations.md",
        "plan.md",
        "data.md",
        "tracking.md",
        "scorecard.md",
    }


def test_target_template_contains_only_user_level_inputs() -> None:
    target = _load_yaml(PACKAGE_ROOT / "templates" / "target.yaml")
    assert set(target) == {
        "agent",
        "mutation",
        "evaluation",
        "split",
        "budget",
        "apply_winner",
    }
    text = (PACKAGE_ROOT / "templates" / "target.yaml").read_text(encoding="utf-8")
    assert "kind" in target["agent"]
    assert "tools" in text
    for forbidden in (
        "provider_id",
        "action_binding",
        "artifact",
        "resource_lease",
        "run_contract",
        "adapter",
    ):
        assert forbidden not in text


def test_skill_understands_target_then_runs_tenzing() -> None:
    skill = (PACKAGE_ROOT / "SKILL.md").read_text(encoding="utf-8")
    assert "Understand the target read-only" in skill
    assert "Providers own API" in skill
    assert "This Skill must not" in skill
    assert "show the complete plan to the user" in skill
    assert "wait for explicit approval" in " ".join(skill.split())
    assert "`guides/data.md`" in skill
    assert "`guides/operations.md`" in skill
    assert "`guides/tracking.md`" in skill
    assert "`guides/scorecard.md`" in skill
    assert "Applying a winner" in skill
    for provider_specific in ("foundry_opt", "FoundryPocClient", "Azure CLI"):
        assert provider_specific not in skill


def test_loop_guide_is_translation_not_another_protocol() -> None:
    guide = (PACKAGE_ROOT / "guides" / "loop.md").read_text(encoding="utf-8")
    assert "only translates" in guide
    assert "not a second methodology" in guide
    assert "| branch | An isolated candidate" in guide
    assert "| commit | Freeze the exact candidate" in guide
    assert "can each use different available tools" in " ".join(guide.split())
    assert "Local Git experiment tracking" in guide
    assert "resume from the journal" in guide


def test_operations_guide_lists_atomic_provider_verbs() -> None:
    ops = (PACKAGE_ROOT / "guides" / "operations.md").read_text(encoding="utf-8")
    for verb in (
        "inspect_baseline",
        "open_candidate",
        "apply_mutation",
        "freeze_candidate",
        "invoke_candidate",
        "evaluate",
        "cleanup_candidate",
        "apply_winner",
    ):
        assert verb in ops
    assert "atomic" in ops
    assert "It names verbs only" in ops
    joined = " ".join(ops.split())
    assert "never" in joined
    for provider_specific in ("foundry_opt", "FoundryPocClient", "Azure CLI"):
        assert provider_specific not in ops


def test_run_state_records_provider_binding() -> None:
    state = _load_yaml(PACKAGE_ROOT / "templates" / "run-state.yaml")
    assert "provider_binding" in state
    binding = state["provider_binding"]
    assert "isolation_mechanism" in binding
    assert {
        "inspect_baseline",
        "open_candidate",
        "apply_mutation",
        "freeze_candidate",
        "invoke_candidate",
        "evaluate",
        "cleanup_candidate",
        "apply_winner",
    } <= set(binding["operations"])


def test_tenzing_source_remains_exactly_pinned() -> None:
    manifest = _load_yaml(
        PACKAGE_ROOT / "references" / "tenzing" / "SOURCE_MANIFEST.yaml"
    )
    climb = PACKAGE_ROOT / "references" / "tenzing" / "climb.md"
    assert manifest["commit"] == TENZING_COMMIT
    assert hashlib.sha256(_normalized_bytes(climb)).hexdigest() == TENZING_HASH


def test_run_templates_capture_state_and_result() -> None:
    state = _load_yaml(PACKAGE_ROOT / "templates" / "run-state.yaml")
    result = _load_yaml(PACKAGE_ROOT / "templates" / "result.yaml")
    assert {
        "storage",
        "target",
        "data",
        "baseline",
        "idea_cycles",
        "candidates",
        "provisional_winner",
        "validation",
        "cleanup",
    } <= set(state)
    assert "worktrees" in state
    assert {
        "train_fingerprint",
        "search_row_ids",
        "confirmation_row_ids",
        "validation_fingerprint",
        "expected_counts",
        "zero_overlap",
        "all_train_rows_used",
        "contamination_incidents",
    } <= set(state["data"])
    assert {"outcome", "baseline", "winner", "applied", "artifacts"} <= set(result)
    assert "experiment_tracking" in result["artifacts"]
    assert {
        "all_train_rows_used",
        "zero_overlap",
        "confirmation_clean",
        "validation_clean",
    } <= set(result["data_integrity"])


def test_plan_data_tracking_and_scorecard_guides_are_complete() -> None:
    plan = (PACKAGE_ROOT / "guides" / "plan.md").read_text(encoding="utf-8")
    data = (PACKAGE_ROOT / "guides" / "data.md").read_text(encoding="utf-8")
    tracking = (PACKAGE_ROOT / "guides" / "tracking.md").read_text(encoding="utf-8")
    scorecard = (PACKAGE_ROOT / "guides" / "scorecard.md").read_text(encoding="utf-8")

    assert "Do not start until the user explicitly approves it" in plan
    assert "Training dataset" in plan
    assert "Validation dataset" in plan
    assert "Do not hide an unresolved value behind `auto`" in plan
    assert "use every row exactly once" in data
    assert "Never derive validation from training" in data
    assert "mark the split contaminated" in " ".join(data.split())
    assert "<agent-root>/experiment_tracking/runs/<run-id>/" in tracking
    assert "never through a candidate worktree" in tracking
    assert "Idea and candidate ledger" in scorecard
    assert "Search results" in scorecard
    assert "Confirmation results" in scorecard
    assert "Validation" in scorecard
    assert "Mermaid DAG" in scorecard
