# Security

## Trusted runtime

- The skill lock identifies the exact runtime repository, commit, package path,
  and lock digest.
- Installer scripts fetch and verify that exact revision before execution.
- Resume refuses a different runtime revision.

## Explicit mutation approvals

The runner separates four mutation seams:

1. repository files
2. GitHub-to-Azure connection
3. local Git commit
4. Foundry deployment

An approval is bound to the current immutable state generation and reviewed
plan. Stale or mismatched approvals are rejected.

## Identity

- Local deployment uses the owner's current Azure CLI/default credential.
- Future GitHub workflows use OIDC and the committed Azure client ID.
- Static Azure credentials are not stored in the repository or operation
  state.
- Foundry User access is scoped to reviewed projects.

## Exact source

- Deployment requires a clean, reviewed local commit.
- Packaging reads source from that Git object, not dirty working-tree bytes.
- Downloaded Foundry code is compared with the exact uploaded archive.
- Publication never mutates an explicit version route.

## Private state

Operation state contains reviewed decisions, hashes, redacted receipts, and
resource identifiers. It must not contain access tokens, static credentials,
raw customer datasets, evaluator source, or archive bytes.
