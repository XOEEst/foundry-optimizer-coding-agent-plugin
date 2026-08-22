# Owner review interface

`foundry_opt.bootstrap.owner_review` centralizes owner-facing summaries for
bootstrap discovery, planning, status, and resource links.

For first-time repository owners, keep bootstrap short and human-first:

1. choose agents
2. resolve enabled agents' Foundry targets
3. review repository setup
4. connect GitHub to Azure
5. choose optional verification
6. review the exact local commit and deployment separately

Use plain-language bullet summaries by default. Reserve JSON for automation,
implementation, or debugging detail.

The installed skill crosses one small runtime interface: `start`, `answer`,
`approve`, `status`, and `rollback`. Repository inspection, Azure account
lookup, and minimal prompting stay in the skill; deterministic validation,
state transitions, mutations, receipts, and compensation stay behind the
runtime interface.

## Advanced compatibility CLI entry points

Normal owners should use `/foundry-bootstrap`. The commands below remain
the advanced compatibility surface for reviewed source checkouts and
automation that still invoke `foundry-opt bootstrap ...`.

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

Compatibility policy: keep the existing command tree stable without
runtime deprecation warning noise in CI. Announce any future retirement
through docs and release notes first.

## Default owner decisions

- **Choose agents** — `bootstrap review discovery` shows which discovered
  `repoAgentId` values are review-ready, blocked, already registered, or
  intended to remain out of scope.
- **Review repository setup** — `bootstrap review plan` shows managed file
  changes, initial registry/profile intent, verification warnings, OIDC
  subjects, RBAC assignments, and the resulting
  `registered` / `enabled` / `verified` / `deployable` posture.
- **Connect GitHub to Azure** — `bootstrap connect plan` previews the one
  combined approval covering GitHub environments, variables, branch policy,
  Azure identity adoption/creation, exactly two federated OIDC subjects, and
  approved RBAC assignments.
- **Verification** — owners may configure Foundry evaluation, defer it, use
  repository checks, or proceed with an explicit no-evidence warning.
- **Commit and deployment** — the skill reviews the exact local commit and
  exact-source deployment as separate approvals.
- **Handoff** — `bootstrap resources` is the final owner handoff for GitHub,
  Azure, Foundry, and optional evaluation links.

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
Internal child receipts remain implementation detail; the owner approves one
combined connection decision.

## Related architecture

- [Skill and runtime seam](architecture/skill-runtime-seam.md)
- [Evidence, state, and receipts](reference/evidence-state-and-receipts.md)
- [ADR 0015: Skill-first bootstrap and optional verification](decisions/0015-skill-first-bootstrap-and-optional-verification.md)
