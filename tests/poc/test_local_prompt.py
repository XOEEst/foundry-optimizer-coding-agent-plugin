from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from foundry_opt.local_prompt import (
    EvaluationRow,
    LocalPromptError,
    deterministic_partition,
    load_rows,
    load_target_config,
)


def _git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
        },
    )


def test_deterministic_partition_is_disjoint_stable_and_bounded() -> None:
    rows = [
        EvaluationRow(row_id=f"row-{index}", query=f"query {index}", ground_truth="{}")
        for index in range(20)
    ]

    first_search, first_confirmation = deterministic_partition(
        rows,
        confirmation_fraction=0.2,
        seed=7,
        max_search_samples=8,
    )
    second_search, second_confirmation = deterministic_partition(
        tuple(reversed(rows)),
        confirmation_fraction=0.2,
        seed=7,
        max_search_samples=8,
    )

    assert first_search == second_search
    assert first_confirmation == second_confirmation
    assert len(first_search) == 8
    assert len(first_confirmation) == 4
    assert {row.row_id for row in first_search}.isdisjoint(
        row.row_id for row in first_confirmation
    )


def test_load_rows_rejects_duplicate_ids(tmp_path: Path) -> None:
    dataset = tmp_path / "rows.jsonl"
    dataset.write_text(
        "\n".join(
            [
                json.dumps({"id": "same", "query": "a", "ground_truth": "{}"}),
                json.dumps({"id": "same", "query": "b", "ground_truth": "{}"}),
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(LocalPromptError, match="duplicate row id"):
        load_rows(dataset)


def test_load_target_config_freezes_instruction_only_inputs(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    agent_dir = repository / "agents" / "ifbench"
    datasets = agent_dir / "datasets"
    datasets.mkdir(parents=True)
    (agent_dir / "instructions.md").write_text("baseline\n", encoding="utf-8")
    row = json.dumps({"id": "1", "query": "q", "ground_truth": "{}"}) + "\n"
    (datasets / "train.jsonl").write_text(row, encoding="utf-8")
    (datasets / "validation.jsonl").write_text(row, encoding="utf-8")
    (agent_dir / "agent.yaml").write_text(
        "\n".join(
            [
                "name: ifbench",
                "model: gpt-4o-mini",
                "instructions_file: instructions.md",
                "eval:",
                "  dataset:",
                "    train: datasets/train.jsonl",
                "    validation: datasets/validation.jsonl",
                "  evaluator:",
                "    type: ifbench",
                "  optimization:",
                "    max_train_samples: 12",
                "    max_concurrency: 3",
                "    faos:",
                "      project_endpoint: https://example.services.ai.azure.com/api/projects/p",
                "      max_candidates: 4",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    _git(repository, "init")
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "initial")

    target = load_target_config(
        agent_dir,
        agent_name="ifbench-tenzing-instruction-only",
    )

    assert target.agent_name == "ifbench-tenzing-instruction-only"
    assert target.instructions == "baseline\n"
    assert target.max_train_samples == 12
    assert target.max_concurrency == 3
