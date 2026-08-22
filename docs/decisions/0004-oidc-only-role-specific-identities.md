# ADR 0004: OIDC-only role-specific identities

## Status

Superseded

## Context

This ADR originated in the earlier pre-public optimizer lineage and records the original default Azure identity split for optimize-job and deployment work. The current public bootstrap default has changed, but the OIDC-only and no-static-credentials principles remain part of the lineage.

## Decision

Use GitHub Actions OIDC only for Azure authentication. Define separate optimizer and deployment principals in trusted metadata, with role-specific client IDs, environments, and subject claims. Refuse static Azure credentials and verify that GitHub-provided OIDC data, workflow variables, repository identity, and expected claims all match trusted metadata before obtaining credentials.

## Consequences

Benefits:

- Removes long-lived Azure secrets from the repository contract.
- Separates optimize-job and deployment privileges with explicit role seams.
- Makes trust checks testable through metadata, environment validation, and claim verification.

Tradeoffs:

- The runtime depends on GitHub Actions OIDC environment support and correct Entra configuration.
- Preflight and runtime wiring are stricter; misconfigured workflow variables or claims block progress immediately.
- Local simulation must stub the same contract rather than bypassing authentication behavior.

## Alternatives considered

- **Static client secrets or certificates in GitHub secrets** - rejected because the repository explicitly forbids static credentials.
- **One shared Azure principal for both optimizer and deployment roles** - rejected in this earlier model because role-specific identities improved locality and least privilege.
- **Opaque Azure CLI ambient login only** - rejected because production contract verification needs explicit claim and variable checks.

## Evidence

- The historical two-principal shape is preserved in [`tests/bootstrap/fixtures/templates/legacy-single-agent-agent-metadata.yaml`](../../tests/bootstrap/fixtures/templates/legacy-single-agent-agent-metadata.yaml) and imported by [`src/foundry_opt/bootstrap/legacy.py`](../../src/foundry_opt/bootstrap/legacy.py).
- OIDC-only auth and separate role handling remain covered by [`src/foundry_opt/poc/auth.py`](../../src/foundry_opt/poc/auth.py), [`src/foundry_opt/poc/runtime.py`](../../src/foundry_opt/poc/runtime.py), [`src/foundry_opt/poc/deploy.py`](../../src/foundry_opt/poc/deploy.py), and their tests in [`tests/poc/test_auth.py`](../../tests/poc/test_auth.py), [`tests/poc/test_runtime.py`](../../tests/poc/test_runtime.py), and [`tests/poc/test_deploy.py`](../../tests/poc/test_deploy.py).
- Current public OIDC-only and no-static-credentials policy is documented in [Identity and RBAC](../identity-rbac.md).

## Supersedes / Superseded by

- Supersedes: None.
- Superseded by: [0016](0016-shared-identity-with-environment-federation.md) as the default bootstrap identity model; OIDC-only authentication and refusal of static credentials remain in force.
