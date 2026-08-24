from __future__ import annotations

import json
from pathlib import Path

from foundry_opt.repository_contracts import AgentProfile, RepositoryRegistry

ROOT = Path(__file__).resolve().parent
SCHEMAS = ROOT / 'schemas'
SCHEMAS.mkdir(exist_ok=True)

for filename, model in {
    'registry.schema.json': RepositoryRegistry,
    'sidecar.schema.json': AgentProfile,
}.items():
    schema = model.model_json_schema()
    (SCHEMAS / filename).write_text(json.dumps(schema, sort_keys=True, indent=2) + '\n', encoding='utf-8', newline='\n')
