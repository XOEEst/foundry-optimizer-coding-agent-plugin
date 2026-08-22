# ADR 0006: Deterministic exact-commit packaging

## Status

Accepted

## Context

This ADR originated in the earlier pre-public optimizer lineage and remains a core trust seam in both the compatibility optimizer and the public bootstrap runtime. Mutable checkout contents are too shallow a contract for retries, reruns, and deployment reconciliation.

## Decision

Package deployable source deterministically from an immutable Git commit and repository `source_root`. Ignore working-tree drift when building the package, record tree and ZIP hashes, and require later verification that downloaded draft or regular-version code bytes exactly match the packaged source ZIP.

## Consequences

Benefits:

- Packaging becomes reproducible across retries and separate lifecycle stages.
- Hash-based receipts give strong evidence for exact code identity at the package seam.
- Deployment reconciliation can compare the latest version to exact package bytes rather than a looser metadata heuristic.

Tradeoffs:

- Packaging code must manage work roots safely and validate exact commit identity.
- Any mismatch between packaged bytes and service-downloaded bytes becomes a hard failure.
- Operators cannot rely on uncommitted hotfixes in a checkout for optimize-job or deployment runs.

## Alternatives considered

- **Package the mutable working tree** - rejected because it breaks determinism and resumability.
- **Package the full repository instead of the trusted source root** - rejected because the repository distinguishes deployable source from broader checkout contents.
- **Trust version metadata without code-byte verification** - rejected because exact code identity is the real contract.

## Evidence

- Historical optimizer packaging remains implemented in [`src/foundry_opt/poc/source.py`](../../src/foundry_opt/poc/source.py), [`src/foundry_opt/poc/runtime.py`](../../src/foundry_opt/poc/runtime.py), and [`src/foundry_opt/poc/deploy.py`](../../src/foundry_opt/poc/deploy.py).
- Public deterministic bootstrap packaging is implemented in [`src/foundry_opt/packaging/deterministic_zip.py`](../../src/foundry_opt/packaging/deterministic_zip.py) and [`src/foundry_opt/packaging/foundry_bootstrap_release.py`](../../src/foundry_opt/packaging/foundry_bootstrap_release.py).
- Deterministic packaging tests live in [`tests/poc/test_source.py`](../../tests/poc/test_source.py), [`tests/poc/test_deploy.py`](../../tests/poc/test_deploy.py), [`tests/packaging/test_deterministic_zip.py`](../../tests/packaging/test_deterministic_zip.py), and [`tests/packaging/test_foundry_bootstrap_release.py`](../../tests/packaging/test_foundry_bootstrap_release.py).

## Supersedes / Superseded by

- Supersedes: None.
- Superseded by: None.
