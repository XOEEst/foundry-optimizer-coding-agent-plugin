from __future__ import annotations

import pytest
from pydantic import ValidationError

from foundry_opt.bootstrap.contracts import BootstrapAction, BootstrapPlan, BootstrapReceipt
from foundry_opt.bootstrap.errors import BootstrapPlanError


def test_plan_hash_is_tamper_evident() -> None:
    plan = BootstrapPlan.create(
        operation_id='op-1',
        runtime_repository='https://github.com/example/runtime.git',
        runtime_commit='a' * 40,
        repository_identity='org/repo',
        actions=(BootstrapAction(action_id='action-1', phase='repository', stage='planned', kind='write-file'),),
    )
    tampered = plan.model_copy(update={'operation_id': 'op-2'})
    with pytest.raises(ValidationError):
        BootstrapPlan.model_validate(tampered.model_dump(mode='python'))


def test_receipt_hash_is_tamper_evident() -> None:
    receipt = BootstrapReceipt.create(
        operation_id='op-1',
        runtime_repository='https://github.com/example/runtime.git',
        runtime_commit='a' * 40,
        repository_identity='org/repo',
        plan_hash='b' * 64,
        created_actions=('action-1',),
    )
    tampered = receipt.model_copy(update={'created_actions': ('action-2',)})
    with pytest.raises(ValidationError):
        BootstrapReceipt.model_validate(tampered.model_dump(mode='python'))


def test_plan_rejects_prohibited_prompt_fields() -> None:
    with pytest.raises(BootstrapPlanError):
        BootstrapPlan.create(
            operation_id='op-1',
            runtime_repository='https://github.com/example/runtime.git',
            runtime_commit='a' * 40,
            repository_identity='org/repo',
            actions=(BootstrapAction(action_id='action-1', phase='repository', stage='planned', kind='write'),),
            prompt='secret',
        )
