from __future__ import annotations

import json
from pathlib import Path

import pytest

from foundry_opt.poc.candidate import PreparedCandidate
from foundry_opt.poc.state import (
    CandidateState,
    JobIdentity,
    JobRuntimeDigests,
    JobStateStore,
    StateConflictError,
    StateValidationError,
)


def _identity() -> JobIdentity:
    return JobIdentity(
        job_id="job-1",
        repository="owner/repo",
        issue_number=123,
        shared_commit="a" * 40,
        base_commit="a" * 40,
        source_root="src",
        route_fingerprint="b" * 64,
        min_candidates=1,
    )


def _prepared() -> PreparedCandidate:
    return PreparedCandidate(
        candidate_id="candidate-one",
        parent_id=None,
        model="gpt-5-mini",
        hypothesis="improve greeting quality",
        base_commit="a" * 40,
        origin_commit="a" * 40,
        workspace_path=Path(r"Q:\trusted\worktrees\candidate-one"),
    )


def _runtime_digests(seed: str = "c") -> JobRuntimeDigests:
    hex_values = (seed, "d", "e", "f", "a")
    return JobRuntimeDigests(
        policy_sha256=hex_values[0] * 64,
        metadata_sha256=hex_values[1] * 64,
        hosted_contracts_sha256=hex_values[2] * 64,
        development_evaluation_sha256=hex_values[3] * 64,
        validating_evaluation_sha256=hex_values[4] * 64,
    )


def test_state_store_initialize_update_load_and_digest(tmp_path: Path) -> None:
    store = JobStateStore(tmp_path / "state")
    first = store.initialize(_identity())
    assert first.generation == 1

    updated = store.update(
        lambda current: current.with_candidate(CandidateState(handoff=_prepared()))
    )
    loaded = store.load()

    assert updated.generation == 2
    assert loaded == updated
    assert loaded.digest_sha256 == updated.digest_sha256
    assert loaded.candidate("candidate-one") is not None


def test_state_store_detects_generation_conflicts_and_replays_initialize(
    tmp_path: Path,
) -> None:
    store = JobStateStore(tmp_path / "state")
    first = store.initialize(_identity())
    assert store.initialize(_identity()) == first

    stale = first.model_copy(update={"terminal_outcome": "no_winner"})

    with pytest.raises(StateConflictError, match="generation changed"):
        store.save(stale, expected_generation=0)


def test_state_store_rejects_tampered_documents(tmp_path: Path) -> None:
    store = JobStateStore(tmp_path / "state")
    store.initialize(_identity())
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["content_sha256"] = "0" * 64
    store.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(StateValidationError, match="digest"):
        store.load()


def test_state_store_backfills_runtime_digests_for_legacy_identity(
    tmp_path: Path,
) -> None:
    store = JobStateStore(tmp_path / "state")
    store.initialize(_identity())

    upgraded_identity = _identity().model_copy(update={"runtime_digests": _runtime_digests()})
    upgraded = store.initialize(upgraded_identity)

    assert upgraded.identity.runtime_digests == upgraded_identity.runtime_digests
    assert store.load().identity.runtime_digests == upgraded_identity.runtime_digests
