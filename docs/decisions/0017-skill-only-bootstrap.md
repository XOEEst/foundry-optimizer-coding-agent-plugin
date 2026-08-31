# ADR 0017: Skill-only bootstrap

## Status

Accepted

## Context

The runtime-backed bootstrap flow accumulated a five-command bridge, four
approval seams, nearly thirty thousand lines of orchestration code, durable
state, receipts, compensation, rollback, fixed repository plans, and a large
test-only provider stack. Owners could not reliably complete the flow.

## Decision

- `/foundry-bootstrap` performs bootstrap directly with general repository,
  Git, GitHub, Azure, Foundry, and azd tools.
- Each invocation scans one owner-confirmed folder, asks the owner to select
  any recognized descendant agents, and binds that group to one
  owner-confirmed Foundry project endpoint. Repeated invocations preserve and
  extend existing registry entries, sidecars, project services, and identity
  configuration.
- The owner reviews one combined plan and gives one approval.
- The skill stages exact proposed files in the session workspace before
  approval.
- Exact remote resources are adopted, missing resources are created, and
  conflicting resources are never overwritten automatically.
- Bootstrap has no Python CLI, runner, operation state, receipt,
  compensation, or rollback.
- `.foundry-opt/bootstrap-report.md` records approved and completed
  repository/GitHub/Azure setup changes.
- The skill creates one local commit and deploys source-code hosted agents with
  the available/latest capability-compatible `azure.ai.agents` azd extension.
- Optimizer and registered deployment retain neutral registry/profile,
  selection, fingerprint, and exact-source runtime modules.
- Repositories may begin with agent code only. Bootstrap generates registry v2
  and per-agent sidecars; optimizer and deployment project their compatibility
  contracts from those files in memory rather than requiring repository-global
  legacy policy or metadata files.
- Evaluation onboarding is deferred to a future dedicated skill.

## Consequences

- Bootstrap is flexible and owner-readable.
- Partial local or remote state may remain after failure.
- One approval covers a broad mutation set.
- Conversational skill sequencing has no automated E2E test.
- azd is beta and unpinned; capability probes are required.
- Repeated azd deployment may create another immutable version.

## Supersedes

- [0011](0011-public-runtime-and-guided-bootstrap.md)
- [0012](0012-receipt-bound-evaluation-activation.md) for bootstrap evaluation onboarding
- [0015](0015-skill-first-bootstrap-and-optional-verification.md)
- [0016](0016-shared-identity-with-environment-federation.md) for implementation ownership only
