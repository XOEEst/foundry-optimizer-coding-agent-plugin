# ADR 0002: Commit-pinned shared implementation

## Status

Superseded

## Context

This ADR originated in the earlier pre-public optimizer lineage and records the first shared distribution contract. Customer repositories consumed one reviewed shared implementation only if they could pin the exact revision and later prove what had been installed.

## Decision

Customer repositories must reference the shared optimizer CLI and skill through `.github/foundry-opt.lock.yml`, pinned to an exact Git commit and lock hash. Bootstrap receipts record the fetched repository, commit, package path, skill path, and lock hash so later optimize-job and deployment steps can prove they are running the intended shared implementation rather than mutable local copies.

## Consequences

Benefits:

- Shared fixes and reviewed improvements land once and can be adopted by pin update instead of vendored copy drift.
- Exact commit pins create a durable seam between customer configuration and shared implementation.
- Receipts and pin verification make later steps auditable and resumable.

Tradeoffs:

- Customer repositories must manage pin updates explicitly.
- Bootstrap has to verify repository identity, commit, package path, and lock hash before work starts.
- Private or restricted consumers need a reviewed fetch path instead of assuming ambient mutable installation.

## Alternatives considered

- **Vendoring the package and skill into every customer repository** - rejected because it would reduce leverage and create parallel drift.
- **Floating branch or tag installation** - rejected because optimize jobs and deployments need exact reproducibility from a durable revision, not a mutable reference.
- **Unpinned package-manager installation** - rejected because it weakens the trust seam between reviewed shared implementation and consuming repository runs.

## Evidence

- Legacy shared pin schema and receipt verification remain covered by [`src/foundry_opt/poc/config.py`](../../src/foundry_opt/poc/config.py), [`src/foundry_opt/poc/bootstrap.py`](../../src/foundry_opt/poc/bootstrap.py), and [`tests/poc/test_bootstrap.py`](../../tests/poc/test_bootstrap.py).
- Current public exact-pin verification and migration rules are documented in [Managed files](../managed-files.md) and [Distribution and pinning](../distribution.md).
- The public exact-pin contract is implemented in [`src/foundry_opt/distribution.py`](../../src/foundry_opt/distribution.py) and exercised by [`tests/test_distribution.py`](../../tests/test_distribution.py), [`tests/bootstrap/test_launchers.py`](../../tests/bootstrap/test_launchers.py), and [`tests/packaging/test_foundry_bootstrap_release.py`](../../tests/packaging/test_foundry_bootstrap_release.py).

## Supersedes / Superseded by

- Supersedes: None.
- Superseded by: [0011](0011-public-runtime-and-guided-bootstrap.md) as the default bootstrap distribution channel; exact pin verification and receipt binding remain explicit principles.
