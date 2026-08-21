from __future__ import annotations

import json
from pathlib import Path

import pytest

from foundry_opt.bootstrap.contracts import BootstrapLock, SemanticPatchSpec, TemplatePayloadSpec
from foundry_opt.bootstrap.errors import BootstrapApplyError, BootstrapPlanError
from foundry_opt.bootstrap.repository import LOCK_PATH, apply_repository, drift_status, plan_repository, recover_repository_journal, render_template_payload, rollback_repository


def _payload(destination: str, rendered: str, *, template_id: str = "template", patches: tuple[SemanticPatchSpec, ...] = ()) -> TemplatePayloadSpec:
    return TemplatePayloadSpec(template_id=template_id, destination_path=destination, rendered_template=rendered, semantic_patches=patches)


def test_operation_id_validation_and_reserved_collisions(tmp_path: Path) -> None:
    payload = _payload(".foundry/app.yaml", "name: ok\n")
    with pytest.raises(BootstrapPlanError):
        plan_repository(tmp_path, operation_id="../bad", runtime_repository="https://github.com/example/runtime.git", runtime_commit="a" * 40, repository_identity="org/repo", payloads=(payload,))
    with pytest.raises(BootstrapPlanError):
        plan_repository(tmp_path, operation_id="good", runtime_repository="https://github.com/example/runtime.git", runtime_commit="a" * 40, repository_identity="org/repo", payloads=(_payload(LOCK_PATH, "bad\n"),))


def test_first_apply_second_noop_creates_no_journal(tmp_path: Path) -> None:
    payload = _payload(".foundry/app.yaml", b"name: foundry\r\n".decode("utf-8"))
    plan1 = plan_repository(tmp_path, operation_id="op1", runtime_repository="https://github.com/example/runtime.git", runtime_commit="a" * 40, repository_identity="org/repo", payloads=(payload,))
    receipt1, lock1 = apply_repository(tmp_path, plan1)
    assert receipt1.created_actions == (plan1.actions[0].action_id,)
    plan2 = plan_repository(tmp_path, operation_id="op2", runtime_repository="https://github.com/example/runtime.git", runtime_commit="a" * 40, repository_identity="org/repo", payloads=(payload,))
    receipt2, lock2 = apply_repository(tmp_path, plan2)
    assert receipt2.changed_actions == ()
    assert receipt2.skipped_actions == (plan2.actions[0].action_id,)
    assert not (tmp_path / ".foundry-opt" / "journal" / "op2.json").exists()
    assert lock2.sidecar_paths == lock1.sidecar_paths


def test_rollback_removes_first_managed_registry_and_lock(tmp_path: Path) -> None:
    payload = _payload(
        ".foundry-opt/registry.yaml",
        "distribution: {}\n",
        template_id="registry",
    )
    plan = plan_repository(
        tmp_path,
        operation_id="registry-first",
        runtime_repository="https://github.com/example/runtime.git",
        runtime_commit="a" * 40,
        repository_identity="org/repo",
        payloads=(payload,),
    )
    receipt, _ = apply_repository(tmp_path, plan)

    rollback_repository(tmp_path, receipt)

    assert not (tmp_path / ".foundry-opt" / "registry.yaml").exists()
    assert not (tmp_path / LOCK_PATH).exists()


def test_retired_custom_agent_relinquishes_ownership_and_preserves_file(
    tmp_path: Path,
) -> None:
    active = _payload(".foundry/app.yaml", "name: active\n", template_id="active")
    custom_agent = _payload(
        ".github/agents/foundry-optimizer.agent.md",
        "# Optional custom agent\n",
        template_id="custom-agent",
    )
    initial = plan_repository(
        tmp_path,
        operation_id="initial",
        runtime_repository="https://github.com/example/runtime.git",
        runtime_commit="a" * 40,
        repository_identity="org/repo",
        payloads=(active, custom_agent),
    )
    apply_repository(tmp_path, initial)
    custom_path = (
        tmp_path / ".github" / "agents" / "foundry-optimizer.agent.md"
    )
    original = custom_path.read_bytes()

    retirement = plan_repository(
        tmp_path,
        operation_id="retire",
        runtime_repository="https://github.com/example/runtime.git",
        runtime_commit="b" * 40,
        repository_identity="org/repo",
        payloads=(active,),
    )
    retire_action = next(
        action
        for action in retirement.actions
        if action.kind == "repository-retire-ownership"
    )
    receipt, lock = apply_repository(tmp_path, retirement)

    assert custom_path.read_bytes() == original
    assert not any(
        entry.template_id == "custom-agent" for entry in lock.managed_files
    )
    assert retire_action.action_id in receipt.changed_actions

    rollback_repository(tmp_path, receipt)
    restored = BootstrapLock.from_document(
        (tmp_path / LOCK_PATH).read_text(encoding="utf-8")
    )
    assert custom_path.read_bytes() == original
    assert any(
        entry.template_id == "custom-agent"
        for entry in restored.managed_files
    )


def test_plan_binds_target_lock_sibling_and_mode(tmp_path: Path) -> None:
    payload = _payload(".foundry/app.yaml", "name: one\n")
    plan = plan_repository(tmp_path, operation_id="op1", runtime_repository="https://github.com/example/runtime.git", runtime_commit="a" * 40, repository_identity="org/repo", payloads=(payload,))
    assert "target:missing" in plan.actions[0].diagnostics
    assert "lock:missing" in plan.actions[0].diagnostics
    assert "sibling:missing" in plan.actions[0].diagnostics
    assert "mode:write" in plan.actions[0].diagnostics


def test_lock_drift_cannot_turn_conflict_into_overwrite(tmp_path: Path) -> None:
    payload = _payload(".foundry/app.yaml", "name: one\n")
    plan1 = plan_repository(tmp_path, operation_id="op1", runtime_repository="https://github.com/example/runtime.git", runtime_commit="a" * 40, repository_identity="org/repo", payloads=(payload,))
    apply_repository(tmp_path, plan1)
    target = tmp_path / ".foundry" / "app.yaml"
    target.write_text("customer\n", encoding="utf-8")
    payload2 = _payload(".foundry/app.yaml", "name: two\n")
    plan2 = plan_repository(tmp_path, operation_id="op2", runtime_repository="https://github.com/example/runtime.git", runtime_commit="a" * 40, repository_identity="org/repo", payloads=(payload2,))
    lock = json.loads((tmp_path / ".foundry-opt" / "bootstrap.lock.json").read_text(encoding="utf-8"))
    lock["managed_files"] = []
    (tmp_path / ".foundry-opt" / "bootstrap.lock.json").write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(BootstrapApplyError):
        apply_repository(tmp_path, plan2)


def test_journal_persisted_before_mutation_and_recoverable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import foundry_opt.bootstrap.repository.engine as engine_mod

    payload = _payload(".foundry/app.yaml", "name: one\n")
    plan = plan_repository(tmp_path, operation_id="op1", runtime_repository="https://github.com/example/runtime.git", runtime_commit="a" * 40, repository_identity="org/repo", payloads=(payload,))
    original = engine_mod._atomic_write_bytes
    seen = {"journal": False}

    def flaky(path: Path, data: bytes, *, fsync: bool = False) -> None:
        if path.as_posix().endswith("/journal/op1.json"):
            seen["journal"] = True
        if path.name == "app.yaml" and seen["journal"]:
            raise OSError("crash")
        original(path, data, fsync=fsync)

    monkeypatch.setattr(engine_mod, "_atomic_write_bytes", flaky)
    with pytest.raises(OSError):
        apply_repository(tmp_path, plan)
    assert (tmp_path / ".foundry-opt" / "journal" / "op1.json").exists()
    recover_repository_journal(tmp_path, "op1")
    assert not (tmp_path / ".foundry" / "app.yaml").exists()


def test_rollback_guards_newer_edits_and_preserves_ledgers(tmp_path: Path) -> None:
    payload = _payload(".foundry/app.yaml", "name: one\n")
    plan1 = plan_repository(tmp_path, operation_id="op1", runtime_repository="https://github.com/example/runtime.git", runtime_commit="a" * 40, repository_identity="org/repo", payloads=(payload,))
    _, _ = apply_repository(tmp_path, plan1)
    payload2 = _payload(".foundry/app.yaml", "name: two\n")
    plan2 = plan_repository(tmp_path, operation_id="op2", runtime_repository="https://github.com/example/runtime.git", runtime_commit="a" * 40, repository_identity="org/repo", payloads=(payload2,))
    receipt2, lock2 = apply_repository(tmp_path, plan2)
    lock_json = json.loads((tmp_path / ".foundry-opt" / "bootstrap.lock.json").read_text(encoding="utf-8"))
    lock_json["github_environments"] = [{"environment": "prod", "variable_names": ["A"]}]
    (tmp_path / ".foundry-opt" / "bootstrap.lock.json").write_text(json.dumps(lock_json), encoding="utf-8")
    (tmp_path / ".foundry" / "app.yaml").write_text("customer edit\n", encoding="utf-8")
    with pytest.raises(BootstrapApplyError):
        rollback_repository(tmp_path, receipt2)
    (tmp_path / ".foundry" / "app.yaml").write_text("name: two\n", encoding="utf-8")
    rollback_repository(tmp_path, receipt2)
    after = json.loads((tmp_path / ".foundry-opt" / "bootstrap.lock.json").read_text(encoding="utf-8"))
    assert after["github_environments"][0]["environment"] == "prod"
    assert after["github_environments"][0]["variable_names"] == ["A"]
    assert lock2.cloud_resources == BootstrapLock.from_document(after).cloud_resources


def test_canonical_workflow_path_and_surgical_patch(tmp_path: Path) -> None:
    workflow = tmp_path / ".github" / "workflows" / "copilot-setup-steps.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_bytes(
        b"name: CI\r\non:\r\n  push:\r\n    branches: [main]\r\njobs:\r\n  build:\r\n    steps:\r\n      - id: keep\r\n        run: echo keep\r\n      - id: foundry-opt-checkout\r\n        run: echo old\r\n"
    )
    payload = _payload(
        ".github/workflows/copilot-setup-steps.yml",
        "ignored\n",
        patches=(SemanticPatchSpec(target_path=".github/workflows/copilot-setup-steps.yml", operation="replace", match_text="id: foundry-opt-checkout\r\n        run: echo old\r\n", replacement_text="id: foundry-opt-checkout\r\n        run: echo new\r\n"),),
    )
    rendered = render_template_payload(payload, workflow.read_bytes())
    assert b"on:\r\n" in rendered
    assert b"echo keep" in rendered
    assert b"echo new" in rendered


def test_legacy_luffy_setup_step_names_migrate_to_reserved_ids(tmp_path: Path) -> None:
    workflow = tmp_path / ".github" / "workflows" / "copilot-setup-steps.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "\n".join(
            (
                "name: Copilot Setup Steps",
                "jobs:",
                "  setup:",
                "    runs-on: ubuntu-latest",
                "    steps:",
                "      - name: Keep customer preflight",
                "        run: echo keep",
                "      - name: Fetch the exact shared revision",
                "        run: echo old-fetch",
                "      - name: Install the frozen shared environment and skill",
                "        run: echo old-install",
                "",
            )
        ),
        encoding="utf-8",
    )
    payload = _payload(
        ".github/workflows/copilot-setup-steps.yml",
        "ignored\n",
        patches=(
            SemanticPatchSpec(
                target_path=".github/workflows/copilot-setup-steps.yml",
                operation="replace",
                match_text="id: foundry-opt-checkout",
                replacement_text=(
                    "id: foundry-opt-checkout\n"
                    "name: Fetch exact v1-capable shared revision\n"
                    "run: echo new-fetch\n"
                ),
            ),
            SemanticPatchSpec(
                target_path=".github/workflows/copilot-setup-steps.yml",
                operation="replace",
                match_text="id: foundry-opt-bootstrap",
                replacement_text=(
                    "id: foundry-opt-bootstrap\n"
                    "name: Install the exact shared CLI and skill\n"
                    "run: echo new-install\n"
                ),
            ),
        ),
    )

    rendered = render_template_payload(payload, workflow.read_bytes())

    assert b"id: foundry-opt-checkout" in rendered
    assert b"id: foundry-opt-bootstrap" in rendered
    assert b"echo new-fetch" in rendered
    assert b"echo new-install" in rendered
    assert b"echo keep" in rendered


def test_legacy_setup_step_name_migration_rejects_ambiguity(tmp_path: Path) -> None:
    workflow = tmp_path / ".github" / "workflows" / "copilot-setup-steps.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text(
        "\n".join(
            (
                "name: Copilot Setup Steps",
                "jobs:",
                "  setup:",
                "    runs-on: ubuntu-latest",
                "    steps:",
                "      - name: Fetch the exact shared revision",
                "        run: echo first",
                "      - name: Fetch the exact shared revision",
                "        run: echo second",
                "",
            )
        ),
        encoding="utf-8",
    )
    payload = _payload(
        ".github/workflows/copilot-setup-steps.yml",
        "ignored\n",
        patches=(
            SemanticPatchSpec(
                target_path=".github/workflows/copilot-setup-steps.yml",
                operation="replace",
                match_text="id: foundry-opt-checkout",
                replacement_text=(
                    "id: foundry-opt-checkout\n"
                    "name: Fetch exact v1-capable shared revision\n"
                    "run: echo new-fetch\n"
                ),
            ),
        ),
    )

    with pytest.raises(
        BootstrapPlanError,
        match="legacy workflow step name must match exactly one",
    ):
        render_template_payload(payload, workflow.read_bytes())


def test_recognized_legacy_setup_workflow_converts_to_managed_template(tmp_path: Path) -> None:
    workflow = tmp_path / ".github" / "workflows" / "copilot-setup-steps.yml"
    workflow.parent.mkdir(parents=True)
    names = (
        "Check out the agent repository",
        "Canonicalize the repository origin",
        "Set up Python",
        "Set up uv",
        "Record trusted state paths",
        "Detect trusted optimize job context",
        "Fetch the exact shared revision",
        "Install the frozen shared environment and skill",
        "Verify bootstrap receipt and target configuration",
        "Launch the minimal GitHub issue broker",
        "Validate the complete setup contract",
    )
    steps = []
    for name in names:
        run = f"echo {name}"
        if name == "Fetch the exact shared revision":
            run = 'pin=".github/foundry-opt.lock.yml"'
        elif name == "Verify bootstrap receipt and target configuration":
            run = (
                "foundry-opt bootstrap verify "
                "--pin .github/foundry-opt.lock.yml\n"
                "foundry-opt validate-config"
            )
        steps.extend(
            (
                f"      - name: {name}",
                "        shell: bash",
                "        run: |",
                *(f"          {line}" for line in run.splitlines()),
            )
        )
    workflow.write_text(
        "\n".join(
            (
                "name: Copilot Setup Steps",
                "jobs:",
                "  setup:",
                "    runs-on: ubuntu-latest",
                "    steps:",
                *steps,
                "",
            )
        ),
        encoding="utf-8",
    )
    managed = (
        "name: Foundry v1 Copilot Setup Steps\n"
        "jobs:\n"
        "  setup:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - id: foundry-opt-checkout\n"
        "        name: Fetch exact v1-capable shared revision\n"
        "        run: echo new-fetch\n"
        "      - id: foundry-opt-bootstrap\n"
        "        name: Install the exact shared CLI and skill\n"
        "        run: echo new-install\n"
    )
    payload = _payload(
        ".github/workflows/copilot-setup-steps.yml",
        managed,
        template_id="setup-semantic-patch",
        patches=(
            SemanticPatchSpec(
                target_path=".github/workflows/copilot-setup-steps.yml",
                operation="replace",
                match_text="id: foundry-opt-checkout",
                replacement_text=(
                    "id: foundry-opt-checkout\n"
                    "name: Fetch exact v1-capable shared revision\n"
                    "run: echo new-fetch\n"
                ),
            ),
            SemanticPatchSpec(
                target_path=".github/workflows/copilot-setup-steps.yml",
                operation="replace",
                match_text="id: foundry-opt-bootstrap",
                replacement_text=(
                    "id: foundry-opt-bootstrap\n"
                    "name: Install the exact shared CLI and skill\n"
                    "run: echo new-install\n"
                ),
            ),
        ),
    )

    plan = plan_repository(
        tmp_path,
        operation_id="legacy-conversion",
        runtime_repository="https://github.com/example/runtime.git",
        runtime_commit="a" * 40,
        repository_identity="org/repo",
        payloads=(payload,),
    )

    assert "mode:write" in plan.actions[0].diagnostics
    receipt, _ = apply_repository(tmp_path, plan)
    assert receipt.changed_actions == (plan.actions[0].action_id,)
    assert workflow.read_text(encoding="utf-8") == managed
    assert not workflow.with_name(
        "copilot-setup-steps.yml.foundry-proposed"
    ).exists()


def test_duplicate_destinations_and_derived_collisions_rejected(tmp_path: Path) -> None:
    one = _payload(".foundry/app.yaml", "name: one\n")
    two = _payload(".foundry/app.yaml", "name: two\n", template_id="other")
    with pytest.raises(BootstrapPlanError):
        plan_repository(tmp_path, operation_id="op1", runtime_repository="https://github.com/example/runtime.git", runtime_commit="a" * 40, repository_identity="org/repo", payloads=(one, two))
    with pytest.raises(BootstrapPlanError):
        plan_repository(tmp_path, operation_id="op1", runtime_repository="https://github.com/example/runtime.git", runtime_commit="a" * 40, repository_identity="org/repo", payloads=(_payload(".foundry-opt/receipts/op1.json", "bad\n"),))


def test_preimages_preserve_crlf_and_identical_crlf_payload_is_noop(tmp_path: Path) -> None:
    target = tmp_path / ".foundry" / "app.yaml"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"name: foundry\r\n")
    lock = BootstrapLock(
        engine="repository-engine",
        runtime_repository="https://github.com/example/runtime.git",
        channel="repository",
        runtime_commit="a" * 40,
        managed_files=(),
        github_environments=(),
        cloud_resources=(),
        sidecar_paths=(),
        last_activation={"outcome": "succeeded"},
    )
    payload = _payload(".foundry/app.yaml", "name: foundry\r\n")
    plan = plan_repository(tmp_path, operation_id="op1", runtime_repository="https://github.com/example/runtime.git", runtime_commit="a" * 40, repository_identity="org/repo", payloads=(payload,))
    receipt, _ = apply_repository(tmp_path, plan)
    assert receipt.skipped_actions == (plan.actions[0].action_id,)
    assert target.read_bytes() == b"name: foundry\r\n"


def test_strict_json_lock_rejects_yaml_and_symlink(tmp_path: Path) -> None:
    lock_path = tmp_path / ".foundry-opt" / "bootstrap.lock.json"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text("engine: yaml\n", encoding="utf-8")
    payload = _payload(".foundry/app.yaml", "name: foundry\n")
    with pytest.raises(BootstrapApplyError):
        plan_repository(tmp_path, operation_id="op1", runtime_repository="https://github.com/example/runtime.git", runtime_commit="a" * 40, repository_identity="org/repo", payloads=(payload,))
