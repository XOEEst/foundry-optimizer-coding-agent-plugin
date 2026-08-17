"""Compatibility exports for the legacy distribution bootstrap API."""

from __future__ import annotations

from foundry_opt.distribution import (
    BootstrapError,
    BootstrapPlan,
    BootstrapReceipt,
    BootstrapReceiptError,
    BootstrapVerificationError,
    ExternalCheckoutPlan,
    FrozenDependencyInstallPlan,
    UserSkillInstallPlan,
    build_bootstrap_plan,
    load_shared_pin,
    read_bootstrap_receipt,
    verify_shared_checkout,
    write_bootstrap_receipt,
)

__all__ = [
    "BootstrapError",
    "BootstrapPlan",
    "BootstrapReceipt",
    "BootstrapReceiptError",
    "BootstrapVerificationError",
    "ExternalCheckoutPlan",
    "FrozenDependencyInstallPlan",
    "UserSkillInstallPlan",
    "build_bootstrap_plan",
    "load_shared_pin",
    "read_bootstrap_receipt",
    "verify_shared_checkout",
    "write_bootstrap_receipt",
]
