# ADR 0011: Public runtime and guided bootstrap

## Status

Accepted

## Context

This ADR captured the first public bootstrap cut. It moved bootstrap onto a public runtime authority without dropping the older trust seams around exact revisions, reviewed repository changes, OIDC-only cloud access, and redacted receipts. It also documented a transitional period in which runtime distribution had moved public before the repository authority and owner-interface story were fully split into later ADRs.

## Decision

- The public plugin repository is the runtime authority. The earlier private development lineage remains historical evidence rather than a second mutable runtime copy.
- Bootstrap runs locally through small PowerShell or Bash launchers and a guided skill that both call one deterministic `foundry-opt bootstrap` CLI. V1 has no hosted provisioning backend.
- Launchers resolve public `main` once per operation unless an explicit pin is supplied. They fetch that revision over HTTPS, verify the exact checkout plus `uv.lock`, record the resolved SHA, and require the same SHA for discovery, planning, and every approved apply phase. Resume reuses the recorded SHA.
- Repository, GitHub, Azure, and evaluation mutations are separate approval phases. Unapproved phases do not mutate anything.
- Mutable plans, provider state, and operation receipts live outside the customer repository or in workflow artifacts. Git records only non-secret desired configuration and ownership ledgers.
- Managed repository output is namespaced by default under `.foundry-opt/`, `<agent-root>/.foundry/`, and managed `.github/` paths, with semantic patching only at reserved integration points.
- V1 does not depend on a GitHub App or custom control plane beyond the reviewed skill and runtime.
- Until the documented branch protection is active for floating `main`, privileged workflows must use an explicit reviewed SHA rather than a floating branch reference.

## Consequences

Benefits:

- Public runtime authority removes the need to distribute bootstrap from a private implementation source.
- Exact-SHA plus `uv.lock` verification keeps the mutable public channel bounded to one reviewed runtime per operation.
- Separate approvals preserve customer review over repository, GitHub, Azure, and evaluation changes.
- Keeping mutable receipts and provider state outside Git avoids polluting customer repositories with transient or sensitive operational state.

Tradeoffs:

- Per-operation resolution from public `main` weakens customer review of runtime upgrades unless branch protection and rollback discipline are enforced.
- No hosted provisioning backend means bootstrap ergonomics still depend on a local launcher and guided skill.
- Semantic patching of reserved files still needs careful drift detection and explicit review when customer edits overlap managed steps.

Later ADRs refine this record: [0014](0014-public-development-authority.md) makes this repository the public home for ADRs and development, and [0015](0015-skill-first-bootstrap-and-optional-verification.md) restates the default owner interface and optional verification policy.

## Alternatives considered

- **Keep private distribution or a fixed customer-side repository pin as the permanent runtime authority** - rejected because bootstrap needed a clean public runtime with fresh public history. Exact explicit pins remain available when operators need a frozen reviewed revision.
- **Use a hosted GitHub App or other hidden control plane for V1** - rejected because the reviewed evidence did not show a way to provision repository files, GitHub environments, OIDC federation, and evaluation state without widening trust or hiding reviewed mutations.
- **Store mutable bootstrap status inside tracked repository files** - rejected because live provider state, partial failures, and compensation receipts need resumable storage outside customer-managed source control.

## Evidence

- Public owner guidance in the root [README](../../README.md) and [Bootstrap](../get-started/bootstrap.md).
- Exact-pin and branch-protection rules in [Distribution and pinning](../distribution.md) and [Recommended branch protection](../branch-protection.md).
- Managed-file namespacing and legacy-pin migration rules in [Managed files](../managed-files.md).
- Skill packaging and exact runtime provenance in [`src/foundry_opt/packaging/foundry_bootstrap_release.py`](../../src/foundry_opt/packaging/foundry_bootstrap_release.py), [`tests/packaging/test_foundry_bootstrap_release.py`](../../tests/packaging/test_foundry_bootstrap_release.py), and [`tests/test_plugin_layout.py`](../../tests/test_plugin_layout.py).
- Owner bridge documentation in [`plugins/foundry-bootstrap/SKILL.md`](../../plugins/foundry-bootstrap/SKILL.md) and [`plugins/foundry-bootstrap/scripts/README.md`](../../plugins/foundry-bootstrap/scripts/README.md).
- Detailed transition and pilot acceptance evidence is retained privately.

## Supersedes / Superseded by

- Supersedes: [0002](0002-commit-pinned-shared-implementation.md) as the default bootstrap distribution channel while preserving exact-SHA verification and optional explicit pins.
- Superseded by: [0014](0014-public-development-authority.md) for public repository authority over ADRs, docs, issues, and future development, and [0015](0015-skill-first-bootstrap-and-optional-verification.md) for the refined owner interface and verification-default wording.
