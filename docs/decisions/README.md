# Architecture decision records

This directory stores append-only architecture decision records (ADRs) for the public Foundry Optimizer plugin and bootstrap runtime.

ADRs 0001 through 0013 were migrated from an earlier private development lineage. Their original numbers and core decisions are preserved here, while evidence links and safety language are rewritten for the public plugin repository.

## Numbering

ADRs use zero-padded numeric prefixes in creation order: `0001`, `0002`, and so on. Numbers are never reused, even if a decision is later deprecated or superseded.

## Statuses

- **Proposed** - documented but not yet the accepted repository contract.
- **Accepted** - the implemented or otherwise approved repository contract.
- **Deprecated** - still historical evidence, but no longer recommended for new work.
- **Superseded** - replaced in whole or in its default role by a later ADR; keep the older record and link both ways.

## Update policy

This directory is append-only. Do not rewrite history or renumber records. Existing ADRs may only change to:

- correct factual errors,
- update **Status**,
- add or repair **Supersedes / Superseded by** links,
- improve public evidence links without changing the underlying decision, or
- redact unsafe or private references.

If the architecture changes, add a new ADR and supersede the older one rather than replacing it.

## Evidence expectations

Every ADR must be evidence-backed. Prefer relative links to durable public evidence such as:

- implementation modules,
- contract or behavior tests,
- templates and reviewed documentation, and
- public pull requests in this repository when they clarify the change.

If detailed acceptance evidence is retained privately, say so generically without naming a private repository or linking to private artifacts.

Never include local filesystem paths, personal identifiers, credentials, raw prompts or responses, traces, dataset rows, or private issue, run, or investigation URLs.

## Standard structure

Every ADR uses the same durable sections and section order:

1. Status
2. Context
3. Decision
4. Consequences
5. Alternatives considered
6. Evidence
7. Supersedes / Superseded by

## Index

| Number | Title | Status | Summary |
| --- | --- | --- | --- |
| [0001](0001-issue-driven-single-pr-workflow.md) | Issue-driven single PR workflow | Accepted | One optimize job starts from one issue and binds to one early same-repository pull request. |
| [0002](0002-commit-pinned-shared-implementation.md) | Commit-pinned shared implementation | Superseded | The original shared distribution channel used an exact commit pin and receipt-backed verification. |
| [0003](0003-repository-policy-and-issue-narrowing.md) | Repository policy and issue narrowing | Accepted | Trusted repository policy is the maximum contract and issue input may only narrow it. |
| [0004](0004-oidc-only-role-specific-identities.md) | OIDC-only role-specific identities | Superseded | The original Azure identity model used separate optimizer and deployment principals with OIDC only. |
| [0005](0005-draft-only-optimization-without-routing.md) | Draft-only optimization without routing | Accepted | Optimize jobs use owned draft versions and never mutate production routing. |
| [0006](0006-deterministic-exact-commit-packaging.md) | Deterministic exact-commit packaging | Accepted | Packaging is reproducible from immutable commits and later steps verify exact source bytes. |
| [0007](0007-development-validation-evaluation-split.md) | Development/validation/evaluation split | Accepted | Candidate search stays on the development split and reserves validating data for the provisional winner. |
| [0008](0008-resumable-state-and-minimal-github-broker.md) | Resumable state and minimal GitHub broker | Accepted | Trusted state makes optimize jobs resumable while GitHub writes stay narrow and redacted. |
| [0009](0009-separate-optimization-from-production-deployment.md) | Separate optimization from production deployment | Accepted | Issue-driven optimization and merge-time deployment remain distinct lifecycles. |
| [0010](0010-service-managed-latest-version-deployment.md) | Service-managed latest-version deployment | Accepted | Production deployment publishes exact code and relies on service-managed latest-version selection. |
| [0011](0011-public-runtime-and-guided-bootstrap.md) | Public runtime and guided bootstrap | Accepted | The first public bootstrap cut established a public runtime, exact-SHA launch, review phases, and namespaced managed files. |
| [0012](0012-receipt-bound-evaluation-activation.md) | Receipt-bound evaluation activation | Accepted | Evaluation onboarding is inventory-first, safety-gated, and only activates after receipt-bound finalization. |
| [0013](0013-immutable-github-oidc-and-enterprise-policy.md) | Immutable GitHub OIDC and enterprise-policy gating | Accepted | Bootstrap freezes GitHub's immutable OIDC subject and fails closed when tenant policy rejects the claim set. |
| [0014](0014-public-development-authority.md) | Public development authority | Accepted | This repository is the public authority for runtime, docs, ADRs, issues, and future development. |
| [0015](0015-skill-first-bootstrap-and-optional-verification.md) | Skill-first bootstrap and optional verification | Accepted | Standard Copilot plus `/foundry-bootstrap` is the default owner path; evaluation is optional and labels must match proof. |
| [0016](0016-shared-identity-with-environment-federation.md) | Shared identity with environment federation | Accepted | Bootstrap defaults to one repository-wide identity with exact per-environment OIDC federation and no static credentials. |
| [0017](0017-skill-only-bootstrap.md) | Skill-only bootstrap | Accepted | The static skill performs one-approved bootstrap directly through general tools; bootstrap runtime, state, receipts, and rollback are removed. |
