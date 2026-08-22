# ADR 0009: Separate optimization from production deployment

## Status

Accepted

## Context

This ADR originated in the earlier pre-public optimizer lineage and remains part of the public plugin contract. The repository serves two related but different lifecycles: an issue-driven optimize job that explores bounded changes, and a deployment path that publishes reviewed code.

## Decision

Keep issue-driven optimization and production deployment as separate lifecycles with different commands, state, and responsibilities. Optimize jobs operate on issues, candidate workspaces, draft evaluations, and early pull requests. Merge-time deployment operates on merged `main` commits, exact packaging, draft validation, and regular-version publication. Neither lifecycle stands in for the other.

## Consequences

Benefits:

- Each lifecycle can have its own module boundary and failure semantics.
- Review and production publication stay separated by the normal repository merge seam.
- Operational reasoning is simpler because optimization state does not have to double as deployment state.

Tradeoffs:

- Shared concepts such as packaging, OIDC, and Foundry adapters must be reused carefully across two flows.
- Maintainers must understand where behavior is intentionally duplicated versus shared behind interfaces.
- The repository cannot claim a single end-to-end orchestration path from issue to production without the merge event.

## Alternatives considered

- **Use optimize-job winner projection as production release machinery** - rejected because reviewed merge remains the production seam.
- **Build one monolithic controller that handles issues, PRs, and production publication together** - rejected because it would reduce locality and blur trust boundaries.
- **Separate repositories for optimizer and deployment implementation** - rejected because this repository intentionally shares deep implementation while keeping lifecycle interfaces distinct.

## Evidence

- Lifecycle split in the root [README](../../README.md).
- Optimize-job controller and runtime in [`src/foundry_opt/poc/controller.py`](../../src/foundry_opt/poc/controller.py) and [`src/foundry_opt/poc/runtime.py`](../../src/foundry_opt/poc/runtime.py).
- Deployment service in [`src/foundry_opt/poc/deploy.py`](../../src/foundry_opt/poc/deploy.py).
- CLI command groups in [`src/foundry_opt/cli.py`](../../src/foundry_opt/cli.py) and smoke coverage in [`tests/test_smoke.py`](../../tests/test_smoke.py).

## Supersedes / Superseded by

- Supersedes: None.
- Superseded by: None.
