# ADR 0010: Service-managed latest-version deployment

## Status

Accepted

## Context

This ADR originated in the earlier pre-public optimizer lineage and still defines the production deployment seam preserved in the public plugin. Once reviewed code reaches `main`, deployment still avoids route mutation logic and relies on service-managed latest-version selection.

## Decision

Deploy merged code by packaging the exact merge commit, validating it as a draft, then publishing or reconciling one regular version. Require Foundry service-managed latest-version selection, fail closed when an explicit selector exists, never call route mutation APIs, and verify that the resulting regular version is the latest version with exact source bytes.

## Consequences

Benefits:

- Production deployment has a narrow interface: publish exact code and verify service-managed selection.
- Stable operation IDs and reconciliation support safe reruns without duplicate regular versions.
- Explicit selector rejection prevents silent drift into a routing model this repository does not manage.

Tradeoffs:

- Deployment depends on the service honoring the latest-version contract.
- Post-publication verification adds extra failure paths after version creation.
- Repositories that require explicit routing control need a different deployment design.

## Alternatives considered

- **Explicit route mutation after publication** - rejected because this repository intentionally has no routing adapter for production rollout.
- **Draft-only production with no regular version** - rejected because production needs a durable published version, not a temporary draft.
- **Always publish a new version even when the latest source already matches** - rejected because reconciliation gives better operational leverage and safer reruns.

## Evidence

- Production deployment contract in the root [README](../../README.md).
- Deployment preflight and publication logic in [`src/foundry_opt/poc/deploy.py`](../../src/foundry_opt/poc/deploy.py).
- Trusted deployment profile in
  [`src/foundry_opt/templates/customer-repo/agent/.foundry/foundry-opt.yaml`](../../src/foundry_opt/templates/customer-repo/agent/.foundry/foundry-opt.yaml).
- Deployment tests covering draft validation, guardrails, reconciliation, stable operation IDs, and latest-version verification in [`tests/poc/test_deploy.py`](../../tests/poc/test_deploy.py).

## Supersedes / Superseded by

- Supersedes: None.
- Superseded by: None.
