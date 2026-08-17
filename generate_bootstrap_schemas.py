from __future__ import annotations

import json
from pathlib import Path

from foundry_opt.bootstrap.contracts import BootstrapLock, BootstrapPlan, BootstrapReceipt, BootstrapSidecar, RootRegistry

ROOT = Path(__file__).resolve().parent
SCHEMAS = ROOT / 'schemas'
SCHEMAS.mkdir(exist_ok=True)

for filename, model in {
    'registry.schema.json': RootRegistry,
    'sidecar.schema.json': BootstrapSidecar,
    'managed-lock.schema.json': BootstrapLock,
    'plan.schema.json': BootstrapPlan,
    'receipt.schema.json': BootstrapReceipt,
}.items():
    schema = model.model_json_schema()
    (SCHEMAS / filename).write_text(json.dumps(schema, sort_keys=True, indent=2) + '\n', encoding='utf-8', newline='\n')
