# ADR 0013: Immutable GitHub OIDC and enterprise-policy gating

## Status

Accepted

## Context

GitHub changed new, renamed, and transferred repositories to immutable OIDC subjects after July 15, 2026. Separately, some target tenants require GitHub-issued tokens to carry an allowed `enterprise` claim. A personal public pilot repository emitted the correct immutable subject but an empty enterprise claim, so token exchange failed before any Foundry mutation.

## Decision

- Bootstrap inventories and freezes GitHub's exact OIDC subject prefix.
- Registry and plan contracts support both legacy name-based and immutable owner and repository ID prefixes.
- Azure federation uses the exact prefix plus the reviewed GitHub environment.
- Runtime validation recomputes the allowed legacy and immutable prefixes from GitHub environment metadata and rejects any other committed prefix.
- A tenant enterprise-claim rejection is a hard prerequisite failure.
- Bootstrap never falls back to static Azure credentials, client secrets, broader Azure roles, or unreviewed route mutation.
- Live rollout must use either a qualifying enterprise-owned repository or a tenant and project whose federation policy accepts the repository's OIDC claims.

## Consequences

Benefits:

- Repository rename or recycling cannot silently transfer Azure trust.
- Tenant-specific policy cannot weaken the generic OIDC-only contract.
- Fail-closed behavior happens before Foundry mutation, not after a partially trusted apply.

Tradeoffs:

- A technically correct federated credential can still be unusable when the target tenant imposes an enterprise allow-list.
- Personal repositories cannot complete live acceptance against such a tenant.
- Bootstrap discovery must include GitHub OIDC settings, owner ID, repository ID, and tenant policy compatibility.

## Alternatives considered

- **Use the old mutable subject** - rejected because the token is immutable and mutable subjects permit repository-name recycling.
- **Use a client secret or static Azure credential** - rejected because it violates the approved trust model.
- **Broaden RBAC or change the federated subject only** - rejected because neither supplies the missing enterprise claim.

## Evidence

- OIDC subject and tenant-policy guidance in [Identity and RBAC](../identity-rbac.md).
- The skill now inspects and applies exact GitHub environment subjects directly through GitHub/Azure tools.
- Public runtime changes in [PR #84](https://github.com/XOEEst/foundry-optimizer-coding-agent-plugin/pull/84) and [PR #86](https://github.com/XOEEst/foundry-optimizer-coding-agent-plugin/pull/86).
- Detailed tenant-policy acceptance evidence is retained privately.

## Supersedes / Superseded by

- Supersedes: None.
- Superseded by: None.
