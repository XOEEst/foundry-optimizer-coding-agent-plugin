from __future__ import annotations

import json
from pathlib import Path

import pytest

from foundry_opt.bootstrap.contracts import BootstrapLock, SemanticPatchSpec, TemplatePayloadSpec
from foundry_opt.bootstrap.errors import BootstrapApplyError, BootstrapPlanError
from foundry_opt.bootstrap.repository import LOCK_PATH, apply_repository, drift_status, plan_repository, render_template_payload, rollback_repository


def _payload(
    *,
    destination: str,
    rendered: str,
    template_id: str = "template",
    patches: tuple[SemanticPatchSpec, ...] = (),
) -> TemplatePayloadSpec:
    return TemplatePayloadSpec(
        template_id=template_id,
        destination_path=destination,
        rendered_template=rendered,
        semantic_patches=patches,
    )


def test_first_apply_and_second_noop(tmp_path: Path) -> None:
    payload = _payload(destination=".foundry/app.yaml", rendered="name: foundry\n")
    plan1 = plan_repository(tmp_path, operation_id="op1", runtime_repository="https://github.com/example/runtime.git", runtime_commit="a" * 40, repository_identity="org/repo", payloads=(payload,))
    receipt1, lock1 = apply_repository(tmp_path, plan1)
    assert receipt1.created_actions == (plan1.actions[0].action_id,)
    assert drift_status(tmp_path, lock1) == ()
    plan2 = plan_repository(tmp_path, operation_id="op2", runtime_repository="https://github.com/example/runtime.git", runtime_commit="a" * 40, repository_identity="org/repo", payloads=(payload,))
    receipt2, lock2 = apply_repository(tmp_path, plan2)
    assert receipt2.changed_actions == ()
    assert receipt2.skipped_actions == (plan2.actions[0].action_id,)
    assert lock2.managed_files[0].applied_sha256 == lock1.managed_files[0].applied_sha256


def test_customer_edit_blocks_apply_and_writes_proposed_sibling(tmp_path: Path) -> None:
    payload = _payload(destination=".foundry/app.yaml", rendered="name: foundry\n")
    plan1 = plan_repository(tmp_path, operation_id="op1", runtime_repository="https://github.com/example/runtime.git", runtime_commit="a" * 40, repository_identity="org/repo", payloads=(payload,))
    apply_repository(tmp_path, plan1)
    target = tmp_path / ".foundry" / "app.yaml"
    target.write_text("name: customer\r\n", encoding="utf-8")
    payload2 = _payload(destination=".foundry/app.yaml", rendered="name: updated\n")
    plan2 = plan_repository(tmp_path, operation_id="op2", runtime_repository="https://github.com/example/runtime.git", runtime_commit="a" * 40, repository_identity="org/repo", payloads=(payload2,))
    assert any(item.startswith("conflict:.foundry/app.yaml.foundry-proposed") for item in plan2.actions[0].diagnostics)
    receipt2, _ = apply_repository(tmp_path, plan2)
    assert receipt2.changed_actions == ()
    assert receipt2.skipped_actions == (plan2.actions[0].action_id,)
    assert (tmp_path / ".foundry" / "app.yaml.foundry-proposed").read_text(encoding="utf-8") == "name: updated\n"


def test_semantic_yaml_patch_preserves_unrelated_customer_text(tmp_path: Path) -> None:
    workflow = tmp_path / ".github" / "copilot-setup-steps.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "name: CI\non:\n  push:\n    branches: [main]\njobs:\n  build:\n    permissions:\n      contents: read\n    steps:\n      - id: checkout\n        run: echo checkout\n      - id: foundry-opt-checkout\n        run: echo old\n      - id: keep-me\n        run: echo keep\n",
        encoding="utf-8",
    )
    payload = _payload(
        destination=".github/copilot-setup-steps.yml",
        rendered="ignored-base\n",
        patches=(
            SemanticPatchSpec(
                target_path=".github/copilot-setup-steps.yml",
                operation="replace",
                match_text="id: foundry-opt-checkout\nrun: echo old\n",
                replacement_text="id: foundry-opt-checkout\nrun: echo https://github.com/example/repo/archive/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.zip\n",
            ),
        ),
    )
    rendered = render_template_payload(payload, workflow.read_text(encoding="utf-8")).decode("utf-8")
    assert "push:" in rendered
    assert "main" in rendered
    assert "id: keep-me" in rendered
    assert "echo old" not in rendered


def test_ambiguous_yaml_fails_closed() -> None:
    payload = _payload(
        destination=".github/copilot-setup-steps.yml",
        rendered="jobs:\n  a:\n    steps: []\n  b:\n    steps: []\n",
        patches=(
            SemanticPatchSpec(
                target_path=".github/copilot-setup-steps.yml",
                operation="insert_after",
                match_text="id: foundry-opt-checkout\nrun: true\n",
                replacement_text="id: foundry-opt-checkout\nrun: false\n",
            ),
        ),
    )
    with pytest.raises(BootstrapPlanError):
        render_template_payload(payload)


def test_legacy_fetch_removed() -> None:
    payload = _payload(destination=".foundry/app.yaml", rendered="git@github.com\n")
    with pytest.raises(BootstrapPlanError):
        plan_repository(Path.cwd(), operation_id="op1", runtime_repository="https://github.com/example/runtime.git", runtime_commit="a" * 40, repository_identity="org/repo", payloads=(payload,))


def test_partial_failure_rolls_back_and_persists_receipt_preimages(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import foundry_opt.bootstrap.repository.engine as engine_mod

    payloads = (
        _payload(destination=".foundry/one.yaml", rendered="one: 1\n"),
        _payload(destination=".foundry/two.yaml", rendered="two: 2\n"),
    )
    plan = plan_repository(tmp_path, operation_id="op1", runtime_repository="https://github.com/example/runtime.git", runtime_commit="a" * 40, repository_identity="org/repo", payloads=payloads)
    original = engine_mod._atomic_write_bytes
    counter = {"count": 0}

    def flaky(path: Path, data: bytes) -> None:
        counter["count"] += 1
        if path.name == "two.yaml" and counter["count"] >= 2:
            raise OSError("boom")
        original(path, data)

    monkeypatch.setattr(engine_mod, "_atomic_write_bytes", flaky)
    with pytest.raises(OSError):
        apply_repository(tmp_path, plan)
    assert not (tmp_path / ".foundry" / "one.yaml").exists()
    assert not (tmp_path / ".foundry-opt" / "receipts" / "op1.json").exists()


def test_stale_plan_and_filesystem_drift_rejected(tmp_path: Path) -> None:
    payload = _payload(destination=".foundry/app.yaml", rendered="name: foundry\n")
    plan = plan_repository(tmp_path, operation_id="op1", runtime_repository="https://github.com/example/runtime.git", runtime_commit="a" * 40, repository_identity="org/repo", payloads=(payload,))
    stale = plan.model_copy(update={"plan_hash": "b" * 64})
    with pytest.raises(BootstrapPlanError):
        apply_repository(tmp_path, stale)
    (tmp_path / ".foundry").mkdir()
    (tmp_path / ".foundry" / "app.yaml").write_text("drifted\n", encoding="utf-8")
    with pytest.raises(BootstrapApplyError):
        apply_repository(tmp_path, plan)


def test_casefold_conflict_and_windows_line_endings(tmp_path: Path) -> None:
    payload = _payload(destination=".foundry/bad.yaml", rendered="name: foundry\n")
    with pytest.raises(BootstrapPlanError):
        plan_repository(tmp_path, operation_id="opx", runtime_repository="https://github.com/example/runtime.git", runtime_commit="a" * 40, repository_identity="org/repo", payloads=(payload, _payload(destination=".foundry/BAD.yaml", rendered="name: other\n", template_id="other")))
    plan = plan_repository(tmp_path, operation_id="op1", runtime_repository="https://github.com/example/runtime.git", runtime_commit="a" * 40, repository_identity="org/repo", payloads=(payload,))
    _, lock = apply_repository(tmp_path, plan)
    target = tmp_path / ".foundry" / "bad.yaml"
    target.write_bytes(target.read_bytes().replace(b"\n", b"\r\n"))
    assert drift_status(tmp_path, lock) == ()


def test_rollback_uses_receipt_scoped_preimages(tmp_path: Path) -> None:
    payload = _payload(destination=".foundry/app.yaml", rendered="name: one\n")
    plan1 = plan_repository(tmp_path, operation_id="op1", runtime_repository="https://github.com/example/runtime.git", runtime_commit="a" * 40, repository_identity="org/repo", payloads=(payload,))
    _, lock1 = apply_repository(tmp_path, plan1)
    payload2 = _payload(destination=".foundry/app.yaml", rendered="name: two\n")
    plan2 = plan_repository(tmp_path, operation_id="op2", runtime_repository="https://github.com/example/runtime.git", runtime_commit="a" * 40, repository_identity="org/repo", payloads=(payload2,))
    receipt2, _ = apply_repository(tmp_path, plan2)
    rollback_repository(tmp_path, receipt2)
    assert (tmp_path / ".foundry" / "app.yaml").read_text(encoding="utf-8") == "name: one\n"
    assert drift_status(tmp_path, lock1) == ()


def test_lock_preserves_previous_unmentioned_entries(tmp_path: Path) -> None:
    payload1 = _payload(destination=".foundry/one.yaml", rendered="one: 1\n", template_id="one")
    payload2 = _payload(destination=".foundry/two.yaml", rendered="two: 2\n", template_id="two")
    plan1 = plan_repository(tmp_path, operation_id="op1", runtime_repository="https://github.com/example/runtime.git", runtime_commit="a" * 40, repository_identity="org/repo", payloads=(payload1, payload2))
    _, _ = apply_repository(tmp_path, plan1)
    plan2 = plan_repository(tmp_path, operation_id="op2", runtime_repository="https://github.com/example/runtime.git", runtime_commit="a" * 40, repository_identity="org/repo", payloads=(payload1,))
    _, lock2 = apply_repository(tmp_path, plan2)
    assert {entry.path for entry in lock2.managed_files} == {".foundry/one.yaml", ".foundry/two.yaml"}


def test_lock_written_as_strict_json(tmp_path: Path) -> None:
    payload = _payload(destination=".foundry/app.yaml", rendered="name: foundry\n")
    plan = plan_repository(tmp_path, operation_id="op1", runtime_repository="https://github.com/example/runtime.git", runtime_commit="a" * 40, repository_identity="org/repo", payloads=(payload,))
    _, lock = apply_repository(tmp_path, plan)
    lock_path = tmp_path / Path(LOCK_PATH)
    parsed = json.loads(lock_path.read_text(encoding="utf-8"))
    assert parsed["engine"] == "repository-engine"
    assert BootstrapLock.from_document(parsed).managed_files == lock.managed_files
