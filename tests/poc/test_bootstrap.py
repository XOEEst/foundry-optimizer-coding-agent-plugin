from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from foundry_opt.distribution import (
    CANONICAL_OPTIMIZER_SKILL_PATH,
    LEGACY_OPTIMIZER_SKILL_PATH,
)
from foundry_opt.poc.bootstrap import (
    BootstrapPlan,
    BootstrapReceipt,
    BootstrapReceiptError,
    BootstrapVerificationError,
    build_bootstrap_plan,
    load_shared_pin,
    read_bootstrap_receipt,
    verify_shared_checkout,
    write_bootstrap_receipt,
)


def _git(path: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *arguments],
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    return completed.stdout.strip()


def _shared_checkout(tmp_path: Path) -> tuple[Path, str, str]:
    checkout = tmp_path / "shared-checkout"
    (checkout / "src" / "foundry_opt" / "poc").mkdir(parents=True)
    (checkout / "skills" / "foundry-agent-optimizer").mkdir(parents=True)
    (checkout / "src" / "foundry_opt" / "poc" / "__init__.py").write_text(
        "",
        encoding="utf-8",
        newline="\n",
    )
    (checkout / "skills" / "foundry-agent-optimizer" / "SKILL.md").write_text(
        "# Shared skill\n",
        encoding="utf-8",
        newline="\n",
    )
    (checkout / "uv.lock").write_text(
        "version = 1\n",
        encoding="utf-8",
        newline="\n",
    )
    _git(checkout, "init")
    _git(checkout, "config", "user.name", "Bootstrap Test")
    _git(checkout, "config", "user.email", "bootstrap@example.invalid")
    _git(checkout, "config", "core.autocrlf", "false")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-m", "initial")
    head = _git(checkout, "rev-parse", "HEAD")
    digest = hashlib.sha256(
        (checkout / "uv.lock").read_bytes()
    ).hexdigest()
    return checkout, head, digest


def _write_shared_pin(
    path: Path,
    *,
    commit: str,
    digest: str,
    package_path: str = "src/foundry_opt/poc",
) -> None:
    path.write_text(
        "\n".join(
            (
                "schema_version: 1",
                "repository_url: https://github.com/example/foundry-shared.git",
                f"commit: '{commit}'",
                f"package_path: '{package_path}'",
                "skill_path: skills/foundry-agent-optimizer",
                f"uv_lock_sha256: '{digest}'",
                "",
            )
        ),
        encoding="utf-8",
        newline="\n",
    )


def test_load_shared_pin_and_verify_exact_checkout(tmp_path: Path) -> None:
    checkout, head, digest = _shared_checkout(tmp_path)
    pin_path = tmp_path / "shared-pin.yaml"
    _write_shared_pin(pin_path, commit=head, digest=digest)

    pin = load_shared_pin(pin_path)
    receipt = verify_shared_checkout(pin, checkout)

    assert receipt == BootstrapReceipt.create(
        repository="https://github.com/example/foundry-shared.git",
        commit=head,
        package_path="src/foundry_opt/poc",
        skill_path="skills/foundry-agent-optimizer",
        lock_sha256=digest,
        checkout_root=str(checkout.resolve()),
    )


def test_verify_shared_checkout_rejects_wrong_head(tmp_path: Path) -> None:
    checkout, head, digest = _shared_checkout(tmp_path)
    pin_path = tmp_path / "shared-pin.yaml"
    _write_shared_pin(pin_path, commit=head, digest=digest)
    (checkout / "README.txt").write_text("next\n", encoding="utf-8", newline="\n")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-m", "next")

    pin = load_shared_pin(pin_path)

    with pytest.raises(BootstrapVerificationError):
        verify_shared_checkout(pin, checkout)


def test_verify_shared_checkout_rejects_wrong_lock_digest(tmp_path: Path) -> None:
    checkout, head, _digest = _shared_checkout(tmp_path)
    pin_path = tmp_path / "shared-pin.yaml"
    _write_shared_pin(pin_path, commit=head, digest="0" * 64)

    pin = load_shared_pin(pin_path)

    with pytest.raises(BootstrapVerificationError):
        verify_shared_checkout(pin, checkout)


def test_verify_shared_checkout_accepts_root_package_path(tmp_path: Path) -> None:
    checkout, head, digest = _shared_checkout(tmp_path)
    pin_path = tmp_path / "shared-pin.yaml"
    _write_shared_pin(
        pin_path,
        commit=head,
        digest=digest,
        package_path=".",
    )

    pin = load_shared_pin(pin_path)
    receipt = verify_shared_checkout(pin, checkout)

    assert pin.package_path == "."
    assert receipt.package_path == "."
    assert Path(receipt.checkout_root, receipt.package_path).resolve() == checkout.resolve()


def test_bootstrap_receipt_write_is_idempotent_and_tamper_rejected(
    tmp_path: Path,
) -> None:
    checkout, head, digest = _shared_checkout(tmp_path)
    receipt = BootstrapReceipt.create(
        repository="https://github.com/example/foundry-shared.git",
        commit=head,
        package_path="src/foundry_opt/poc",
        skill_path="skills/foundry-agent-optimizer",
        lock_sha256=digest,
        checkout_root=str(checkout.resolve()),
    )
    receipt_path = tmp_path / "bootstrap" / "receipt.json"

    write_bootstrap_receipt(receipt_path, receipt)
    write_bootstrap_receipt(receipt_path, receipt)

    assert read_bootstrap_receipt(receipt_path) == receipt

    payload = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload["commit"] = "f" * 40
    receipt_path.write_text(
        json.dumps(payload, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    with pytest.raises(BootstrapReceiptError):
        read_bootstrap_receipt(receipt_path)


def test_bootstrap_plan_is_deterministic(tmp_path: Path) -> None:
    checkout, head, digest = _shared_checkout(tmp_path)
    pin_path = tmp_path / "shared-pin.yaml"
    _write_shared_pin(pin_path, commit=head, digest=digest)
    pin = load_shared_pin(pin_path)
    receipt_path = tmp_path / "state" / "bootstrap-receipt.json"

    first = build_bootstrap_plan(
        pin,
        checkout_root=checkout,
        receipt_path=receipt_path,
    )
    second = build_bootstrap_plan(
        pin,
        checkout_root=checkout,
        receipt_path=receipt_path,
    )

    assert isinstance(first, BootstrapPlan)
    assert first == second
    assert first.checkout.commit == head
    assert first.dependency_install.frozen is True
    assert first.dependency_install.lock_path == "uv.lock"
    assert first.skill_install.scope == "user"
    assert first.receipt_path == str(receipt_path.resolve())


def test_bootstrap_plan_accepts_root_package_path(tmp_path: Path) -> None:
    checkout, head, digest = _shared_checkout(tmp_path)
    pin_path = tmp_path / "shared-pin.yaml"
    _write_shared_pin(
        pin_path,
        commit=head,
        digest=digest,
        package_path=".",
    )
    pin = load_shared_pin(pin_path)
    receipt_path = tmp_path / "state" / "bootstrap-receipt.json"

    plan = build_bootstrap_plan(
        pin,
        checkout_root=checkout,
        receipt_path=receipt_path,
    )

    assert plan.dependency_install.package_path == "."
    assert Path(plan.checkout.checkout_root, plan.dependency_install.package_path).resolve() == checkout.resolve()
    assert plan.skill_install.skill_path == "skills/foundry-agent-optimizer"


def test_bootstrap_plan_canonicalizes_legacy_optimizer_skill_path(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "shared-checkout"
    (checkout / "src" / "foundry_opt" / "poc").mkdir(parents=True)
    checkout.joinpath(*CANONICAL_OPTIMIZER_SKILL_PATH.split("/")).mkdir(parents=True)
    (checkout / "src" / "foundry_opt" / "poc" / "__init__.py").write_text(
        "",
        encoding="utf-8",
        newline="\n",
    )
    checkout.joinpath(*CANONICAL_OPTIMIZER_SKILL_PATH.split("/"), "SKILL.md").write_text(
        "# Shared skill\n",
        encoding="utf-8",
        newline="\n",
    )
    (checkout / "uv.lock").write_text(
        "version = 1\n",
        encoding="utf-8",
        newline="\n",
    )
    _git(checkout, "init")
    _git(checkout, "config", "user.name", "Bootstrap Test")
    _git(checkout, "config", "user.email", "bootstrap@example.invalid")
    _git(checkout, "config", "core.autocrlf", "false")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-m", "initial")
    head = _git(checkout, "rev-parse", "HEAD")
    digest = hashlib.sha256((checkout / "uv.lock").read_bytes()).hexdigest()
    pin_path = tmp_path / "shared-pin.yaml"
    pin_path.write_text(
        "\n".join(
            (
                "schema_version: 1",
                "repository_url: https://github.com/example/foundry-shared.git",
                f"commit: '{head}'",
                "package_path: 'src/foundry_opt/poc'",
                f"skill_path: {LEGACY_OPTIMIZER_SKILL_PATH}",
                f"uv_lock_sha256: '{digest}'",
                "",
            )
        ),
        encoding="utf-8",
        newline="\n",
    )
    pin = load_shared_pin(pin_path)
    receipt = verify_shared_checkout(pin, checkout)
    plan = build_bootstrap_plan(
        pin,
        checkout_root=checkout,
        receipt_path=tmp_path / "state" / "bootstrap-receipt.json",
    )

    assert receipt.skill_path == LEGACY_OPTIMIZER_SKILL_PATH
    assert plan.skill_install.skill_path == CANONICAL_OPTIMIZER_SKILL_PATH
