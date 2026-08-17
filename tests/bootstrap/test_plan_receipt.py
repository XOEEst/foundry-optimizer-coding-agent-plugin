from __future__ import annotations

import pytest

from foundry_opt.bootstrap.contracts import BootstrapAction, BootstrapPlan, BootstrapReceipt
from pydantic import ValidationError

from foundry_opt.bootstrap.errors import BootstrapPlanError


def test_plan_hash_is_tamper_evident() -> None:
    plan = BootstrapPlan.create(
        plan_id='plan-1',
        phases=('discovery', 'apply'),
        actions=(BootstrapAction(action_id='action-1', phase='apply', kind='write-file'),),
    )
    tampered = plan.model_copy(update={'plan_id': 'plan-2'})
    with pytest.raises(ValidationError):
        BootstrapPlan.model_validate(tampered.model_dump(mode='json'))


def test_receipt_hash_is_tamper_evident() -> None:
    receipt = BootstrapReceipt.create(plan_hash='a' * 64, applied_actions=('action-1',))
    tampered = receipt.model_copy(update={'applied_actions': ('action-2',)})
    with pytest.raises(ValidationError):
        BootstrapReceipt.model_validate(tampered.model_dump(mode='json'))


def test_plan_rejects_prohibited_prompt_fields() -> None:
    with pytest.raises(BootstrapPlanError):
        BootstrapPlan.create(
            plan_id='plan-1',
            phases=('apply',),
            actions=(BootstrapAction(action_id='action-1', phase='apply', kind='write', template_payload=None),),
            prompt='secret',
        )
