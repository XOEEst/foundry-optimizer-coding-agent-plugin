# Owner review interface

`foundry_opt.bootstrap.owner_review` centralizes owner-facing summaries for
bootstrap discovery, planning, status, and resource links.

## Public builders

- `build_discovery_review(selection_plan, registry=None)`
- `build_plan_review(source, plan_input=None, verified_binding_classifications=None)`
- `build_status_review(source)`
- `build_resource_links(repository_id, plan_input=None, phase_receipts=())`
- `build_owner_review(source, plan_input=None, registry=None, verified_binding_classifications=None)`

Each builder returns frozen Pydantic models with:

- deterministic JSON via `model_dump()` / `model_dump_json()` for automation
- concise owner-facing rendering via `render_markdown()` and `render_text()`

The module is intentionally standalone for later CLI integration. Callers should
pass existing bootstrap plans, operation-state envelopes, plan inputs, and phase
receipts into these builders instead of reinterpreting raw actions or
diagnostics themselves.
