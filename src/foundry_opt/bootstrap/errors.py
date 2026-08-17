from __future__ import annotations


class BootstrapContractError(ValueError):
    """Base error for frozen bootstrap contracts."""


class BootstrapConfigError(BootstrapContractError):
    """Bootstrap configuration input is invalid or unsafe."""


class BootstrapPlanError(BootstrapContractError):
    """Bootstrap plan content is invalid, unsafe, or tampered."""


class BootstrapApplyError(BootstrapContractError):
    """Bootstrap apply state or receipt is invalid."""


class BootstrapProviderError(BootstrapContractError):
    """Provider integration contract is invalid or unsupported."""


__all__ = [
    "BootstrapApplyError",
    "BootstrapConfigError",
    "BootstrapContractError",
    "BootstrapPlanError",
    "BootstrapProviderError",
]
