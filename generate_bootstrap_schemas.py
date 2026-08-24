from __future__ import annotations

import json
from pathlib import Path

from foundry_opt.repository_contracts import BootstrapLock, BootstrapPlan, BootstrapReceipt, BootstrapSidecar, RootRegistry
from foundry_opt.bootstrap.evaluation.execution import EvaluationFinalization, EvaluationOnboardingRequest
from foundry_opt.bootstrap.input_contracts import BindingEvidenceInput, BootstrapPlanInput, TrustedTemplateManifest

ROOT = Path(__file__).resolve().parent
SCHEMAS = ROOT / 'schemas'
SCHEMAS.mkdir(exist_ok=True)

for filename, model in {
    'registry.schema.json': RootRegistry,
    'sidecar.schema.json': BootstrapSidecar,
    'managed-lock.schema.json': BootstrapLock,
    'managed-payloads.schema.json': TrustedTemplateManifest,
    'plan.schema.json': BootstrapPlan,
    'plan-input.schema.json': BootstrapPlanInput,
    'binding-evidence.schema.json': BindingEvidenceInput,
    'evaluation-onboarding-contract.schema.json': EvaluationOnboardingRequest,
    'evaluation-finalization.schema.json': EvaluationFinalization,
    'receipt.schema.json': BootstrapReceipt,
}.items():
    schema = model.model_json_schema()
    (SCHEMAS / filename).write_text(json.dumps(schema, sort_keys=True, indent=2) + '\n', encoding='utf-8', newline='\n')
