from __future__ import annotations

from foundry_opt import distribution
from foundry_opt.poc import bootstrap as compatibility


def test_legacy_distribution_exports_remain_compatible() -> None:
    assert compatibility.BootstrapReceipt is distribution.BootstrapReceipt
    assert compatibility.load_shared_pin is distribution.load_shared_pin
    assert compatibility.verify_shared_checkout is distribution.verify_shared_checkout
