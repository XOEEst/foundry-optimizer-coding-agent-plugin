# Owner review interface

`foundry_opt.bootstrap.owner_review` centralizes owner-facing summaries for
bootstrap discovery, planning, status, and resource links.

## CLI entry points

Owners can stay on concise text output by default:

- `foundry-opt bootstrap review discovery ...`
- `foundry-opt bootstrap review plan ...`
- `foundry-opt bootstrap review status ...`
- `foundry-opt bootstrap resources ...`
- `foundry-opt bootstrap connect plan ...`
- `foundry-opt bootstrap connect approve ...`
- `foundry-opt bootstrap connect apply ...`
- `foundry-opt bootstrap connect status ...`
- `foundry-opt bootstrap connect rollback ...`

Every owner-facing command renders plain text by default, supports `--markdown`
for copy/paste into issues or PRs, and supports `--json` for deterministic
review/connection model output.

## Review builders

- `build_discovery_review(selection_plan, registry=None)`
- `build_plan_review(source, plan_input=None, verified_binding_classifications=None)`
- `build_status_review(source)`
- `build_resource_links(repository_id, plan_input=None, phase_receipts=())`
- `build_owner_review(source, plan_input=None, registry=None, verified_binding_classifications=None)`

Each builder returns frozen Pydantic models with:

- deterministic JSON via `model_dump()` / `model_dump_json()` for automation
- concise owner-facing rendering via `render_markdown()` and `render_text()`

## Connection approvals

`bootstrap connect` is intentionally owner-facing. It stores the composite
GitHub/Azure plan in connection state, lets owners bind the exact approval with
`connect approve --actor --summary`, and also supports one-shot confirmation via
`connect apply --approve --actor --summary`.

Owners never need to author approval JSON by hand. Exact plan, runtime, and
approval hash checks still come from `GitHubAzureConnectionManager`.
