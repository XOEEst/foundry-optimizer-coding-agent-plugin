# ADR 0003: Repository policy and issue narrowing

## Status

Accepted

## Context

This ADR originated in the earlier pre-public optimizer lineage and remains relevant because the public plugin still ships the same trusted policy seam for issue-driven optimization. Untrusted issue input must stay useful without widening repository-owned constraints.

## Decision

Treat each registry-selected agent sidecar as the maximum repository policy and
trusted agent-specific configuration. Issue input may narrow candidate count
and editable scope, but must never widen any repository constraint. The
compatibility parser still accepts legacy policy and metadata documents, while
newly bootstrapped repositories require only registry v2 and sidecars. The CLI
blocks optimize-job start when any accepted issue input attempts to widen
policy.

## Consequences

Benefits:

- Trusted policy stays authoritative while issue input remains useful and local.
- Narrowed scopes reduce blast radius for candidate workspaces and review.
- Configuration validation moves complexity into one explicit seam rather than scattered runtime checks.

Tradeoffs:

- The issue form is intentionally less expressive than a free-form optimizer request.
- Users may see blocked runs when requested models or paths fall outside repository policy.
- Repository maintainers must keep policy and metadata accurate because they are the durable contract.

## Alternatives considered

- **Let issue authors choose arbitrary files or models** - rejected because it widens trust to unreviewed input.
- **Keep all narrowing logic in ad hoc runtime code** - rejected because a dedicated config interface has better depth and testability.
- **Store one combined mutable config for both trusted metadata and issue wishes** - rejected because it weakens the seam between repository-owned contract and issue-owned request.

## Evidence

- Repository policy and metadata schemas in [`src/foundry_opt/poc/config.py`](../../src/foundry_opt/poc/config.py).
- Current registry and sidecar templates in
  [`src/foundry_opt/templates/customer-repo/.foundry-opt/registry.yaml`](../../src/foundry_opt/templates/customer-repo/.foundry-opt/registry.yaml)
  and
  [`src/foundry_opt/templates/customer-repo/agent/.foundry/foundry-opt.yaml`](../../src/foundry_opt/templates/customer-repo/agent/.foundry/foundry-opt.yaml).
- Strict issue parsing in [`src/foundry_opt/poc/issue.py`](../../src/foundry_opt/poc/issue.py).
- Policy narrowing and widening rejections in [`tests/poc/test_config.py`](../../tests/poc/test_config.py) and [`tests/poc/test_cli.py`](../../tests/poc/test_cli.py).

## Supersedes / Superseded by

- Supersedes: None.
- Superseded by: None.
