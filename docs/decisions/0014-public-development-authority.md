# ADR 0014: Public development authority

## Status

Accepted

## Context

Bootstrap no longer benefits from split public and private repository authority. Maintainers and consumers need one public home for the runtime, documentation, ADRs, issues, and future development, while older private investigations and rollout evidence remain useful only as historical context.

## Decision

- This repository is the public development authority for the `foundry-opt` runtime, the `foundry-bootstrap` skill, repository documentation, architecture decisions, issues, and future feature work.
- Historical private evidence may be retained elsewhere, but it is archival only. It does not define the current architecture, distribute current runtime bits, or act as a second documentation authority.
- New repository-level architecture changes must be documented here through the append-only ADR sequence. Public docs, issues, and pull requests in this repository are the authoritative collaboration surface.
- Runtime packaging, release artifacts, branch-protection policy, and owner-facing guidance are maintained here so that the public repository remains the durable source of truth.

## Consequences

Benefits:

- One public source of truth reduces confusion about where architecture changes, docs, and runtime behavior now live.
- Supersession, status, and future design work become reviewable in the same repository that ships the code and skill.
- Historical private evidence can stay private without pretending to be a parallel product authority.

Tradeoffs:

- Maintainers must move the authoritative architecture narrative into public docs and ADRs instead of relying on private context.
- Some detailed acceptance evidence still needs generic public summaries because the underlying artifacts are not public.
- Public repository hygiene now matters for both runtime integrity and documentation authority.

## Alternatives considered

- **Keep split authority, with public code but private ADRs or issue history as the durable home** - rejected because it leaves future maintainers guessing which repository owns current truth.
- **Duplicate architecture decisions across multiple repositories** - rejected because append-only ADR history should have one authoritative lineage.
- **Make all historical private rollout evidence public** - rejected because some evidence remains sensitive or otherwise unsuitable for public publication.

## Evidence

- Repository authority is stated in the root [README](../../README.md) and [Documentation](../README.md).
- Public runtime source authority and exact release responsibilities are documented in [Distribution and pinning](../distribution.md) and [Recommended branch protection](../branch-protection.md).
- Owner-facing identity guidance lives in [Identity and RBAC](../identity-rbac.md).
- Plugin and packaging boundaries are exercised by [`tests/test_plugin_layout.py`](../../tests/test_plugin_layout.py) and [`tests/packaging/test_foundry_bootstrap_release.py`](../../tests/packaging/test_foundry_bootstrap_release.py).

## Supersedes / Superseded by

- Supersedes: [0011](0011-public-runtime-and-guided-bootstrap.md) for the transitional split-authority wording around where ADRs, docs, issues, and future development belong.
- Superseded by: None.
