from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from foundry_opt.bootstrap.contracts import BootstrapPlan
from foundry_opt.bootstrap.errors import BootstrapApplyError
from foundry_opt.bootstrap.operation_state import (
    OperationStateEnvelope,
    SelectionPlan,
    lock_file_path as operation_lock_file_path,
    state_file_path as operation_state_file_path,
    write_operation_state,
)
from foundry_opt.bootstrap.runner import (
    BootstrapRunnerStateEnvelope,
    FileBootstrapRunnerStateStore,
    RepositoryBinding,
    RuntimeBinding,
    lock_file_path as runner_lock_file_path,
    state_file_path as runner_state_file_path,
)

RUNTIME_REPOSITORY = "https://github.com/example/runtime.git"
RUNTIME_COMMIT = "a" * 40
REPOSITORY_ID = "example/repository"


def _selection_plan(repository_root: Path) -> SelectionPlan:
    return SelectionPlan(
        repository_root=str(repository_root),
        selected_agent_ids=(),
        binding_assessments=(),
        discovery_fingerprints=(),
    )


def _runner_state(repository_root: Path) -> BootstrapRunnerStateEnvelope:
    return BootstrapRunnerStateEnvelope.create(
        generation=0,
        operation_id="runner-lock-test",
        lifecycle_stage="preflight",
        started_at="2026-08-21T00:00:00Z",
        updated_at="2026-08-21T00:00:00Z",
        repository_binding=RepositoryBinding(
            repository_root=str(repository_root),
            repository_id=REPOSITORY_ID,
            repository_url="https://github.com/example/repository.git",
            head_commit="b" * 40,
            branch_name="main",
        ),
        runtime_binding=RuntimeBinding(
            runtime_repository=RUNTIME_REPOSITORY,
            runtime_commit=RUNTIME_COMMIT,
        ),
        selection_plan=_selection_plan(repository_root),
    )


def _operation_state(repository_root: Path) -> OperationStateEnvelope:
    operation_id = "operation-lock-test"
    plan = BootstrapPlan.create(
        operation_id=operation_id,
        runtime_repository=RUNTIME_REPOSITORY,
        runtime_commit=RUNTIME_COMMIT,
        repository_identity=REPOSITORY_ID,
        actions=(),
    )
    return OperationStateEnvelope.create(
        generation=0,
        repository_id=REPOSITORY_ID,
        operation_id=operation_id,
        runtime_repository=RUNTIME_REPOSITORY,
        runtime_commit=RUNTIME_COMMIT,
        selection_plan=_selection_plan(repository_root),
        bootstrap_plan=plan,
        discovery_fingerprints=(),
    )


def test_runner_store_recovers_stale_lock_and_orphaned_temp(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "runner-state"
    envelope = _runner_state(tmp_path)
    path = runner_state_file_path(
        envelope.operation_id,
        state_root=state_root,
    )
    lock = runner_lock_file_path(
        envelope.operation_id,
        state_root=state_root,
    )
    path.parent.mkdir(parents=True)
    lock.write_bytes(b"preserve-lock-metadata")
    orphan = path.with_name(
        f"{path.stem}.{envelope.generation_hash}.tmp"
    )
    orphan.write_bytes(b"partial")
    unknown = path.with_name("state.unknown.tmp")
    unknown.write_bytes(b"preserve")

    FileBootstrapRunnerStateStore(state_root=state_root).save(envelope)

    assert path.is_file()
    assert not orphan.exists()
    assert unknown.read_bytes() == b"preserve"
    assert lock.exists()
    assert lock.read_bytes() == b"preserve-lock-metadata"


def test_operation_store_recovers_after_lock_owner_crash(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "operation-state"
    envelope = _operation_state(tmp_path)
    path = operation_state_file_path(
        envelope.repository_id,
        envelope.operation_id,
        state_root=state_root,
    )
    lock = operation_lock_file_path(
        envelope.repository_id,
        envelope.operation_id,
        state_root=state_root,
    )
    path.parent.mkdir(parents=True)

    source_root = Path(__file__).parents[2] / "src"
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        (
            str(source_root),
            environment.get("PYTHONPATH", ""),
        )
    )
    script = "\n".join(
        (
            "import os",
            "import sys",
            "from pathlib import Path",
            "from foundry_opt.bootstrap.state_lock import state_file_lock",
            "with state_file_lock(",
            "    Path(sys.argv[1]),",
            "    locked_message='operation state is locked by another writer',",
            "):",
            "    os._exit(17)",
        )
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(lock)],
        check=False,
        env=environment,
    )

    assert completed.returncode == 17

    write_operation_state(envelope, state_root=state_root)

    assert path.is_file()
    assert lock.exists()


def test_runner_store_fails_closed_for_live_writer(tmp_path: Path) -> None:
    state_root = tmp_path / "runner-state"
    envelope = _runner_state(tmp_path)
    lock = runner_lock_file_path(
        envelope.operation_id,
        state_root=state_root,
    )
    lock.parent.mkdir(parents=True)

    from foundry_opt.bootstrap.state_lock import state_file_lock

    with state_file_lock(
        lock,
        locked_message="bootstrap runner state is locked by another writer",
    ):
        with pytest.raises(
            BootstrapApplyError,
            match="locked by another writer",
        ):
            FileBootstrapRunnerStateStore(state_root=state_root).save(
                envelope
            )
