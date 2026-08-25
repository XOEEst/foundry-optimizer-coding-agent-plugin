# Registry and sidecar migration

Migrate existing contracts in their current paths. Do not replace stable agent
IDs, move sidecars, or discard fields merely because the templates use
different examples.

## Registry v1 to v2

Keep `github`, `identity`, and `agents` unchanged unless the combined plan
explicitly patches them. Set:

```yaml
schema_version: 2
distribution:
  repository: <resolved runtime repository>
  channel: reviewed
  pin: <resolved exact runtime commit>
  package_path: <resolved package path>
  uv_lock_sha256: <resolved uv.lock SHA-256>
  optimizer_skill_path: <resolved optimizer skill path>
```

Retain `distribution.schema_version: 1`. Validate the complete result with
`schemas/registry.schema.json`.

## Sidecar v1 to v2

Keep these values unchanged:

- `repo_agent_id`, roots, editable paths, and shared-source relations
- runtime and Foundry project binding
- baseline and allowed models
- candidate bounds, primary metric, and decision policy
- issue evaluator limit and hard guardrails
- deployment settings

Move the v1 evaluation fields without changing their contents:

| v1 field | v2 field |
| --- | --- |
| `development_dataset` | `verification.bundle.development_dataset` |
| `validating_dataset` | `verification.bundle.validating_dataset` |
| `development_definition` | `verification.bundle.development_definition` |
| `validating_definition` | `verification.bundle.validating_definition` |
| `default_evaluator_bundle` | `verification.bundle.default_evaluator_bundle` |
| `evaluation_lineage` | `verification.lineage` |

For a previously enabled deployment, set `verification.mode: required`.
Otherwise set `verification.mode: optional`. Set
`verification.evaluation_gate_policy: require_foundry_evaluation`.

This is preservation, not onboarding. Do not enumerate, create, update, or run
evaluation assets. If a preserved reference cannot be represented by the v2
schema, stop and report the exact incompatibility.

## Existing v2 files

Patch only values justified by current repository or cloud evidence. Preserve
evaluation bundle and lineage fields byte-for-byte when no migration is needed.
Never reset verification to the blank template defaults.
