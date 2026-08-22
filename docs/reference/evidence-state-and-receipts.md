# Evidence, state, and receipts

This page describes the durable records that make bootstrap, optimize jobs, and deployment resumable, auditable, and redacted.

## Redaction first

Public evidence is intentionally summarized.

GitHub-visible evidence and persisted state must not include raw prompts, raw responses, traces, dataset rows, credentials, tokens, or other secret-bearing payloads. The runtime stores identifiers, hashes, approvals, bounded summaries, and receipt IDs instead.

## Optimize-job state

Optimize jobs persist trusted state under the configured job state root.

The main records are:

- `optimize-job-poc-state.json`
- `optimize-job-poc-issue-request.json`
- per-candidate artifacts under the job artifact root

`JobIdentity` binds the state to one job ID, repository, issue number, shared commit, base commit, source root, route fingerprint, candidate budget, and runtime digests.

The state store adds:

- `content_sha256`
- a monotonic `generation`
- compare-and-swap writes
- atomic sibling-file replacement

That is the core resumability contract for `foundry-opt job status` and `foundry-opt job resume`.

## Optimize-job receipts

| Receipt | Purpose |
| --- | --- |
| `IssueCommentReceipt` | proves that one baseline, candidate, or final issue comment was already upserted for a stable marker |
| `CleanupReceipt` | proves that one exact draft cleanup already completed |
| `ProjectionReceipt` | proves that one selected candidate patch was already projected to the early PR checkout |
| `ClosureReceipt` | proves that the early PR was already closed for `no_winner` or `platform_failure` |

Stable issue markers are:

- `foundry-opt-poc:<job_id>:baseline`
- `foundry-opt-poc:<job_id>:candidate:<candidate_id>`
- `foundry-opt-poc:<job_id>:final`

These receipts are why an interrupted job can resume without duplicating comments, projections, or cleanup.

## Bootstrap runner state

`/foundry-bootstrap` stores owner-facing lifecycle state in `BootstrapRunnerStateEnvelope`.

That state tracks:

- the current lifecycle stage
- repository and runtime bindings
- the selected discovery plan
- owner answers and approvals
- reviewed Foundry targets
- registration intents and verification choices
- child references for applied repository, connection, commit, and deployment work
- handler context needed for safe resume

Like optimize-job state, it is generation-hashed and compare-and-swap updated.

The runner also keeps a repository index so the current active bootstrap operation can be rediscovered safely.

## Bootstrap operation state and phase receipts

The lower-level bootstrap operation state stores the exact multi-phase plan and its receipts.

Key persisted items are:

- the canonical `plan_hash`
- reviewed `ApprovalRecord` entries
- `PhaseReceipt` entries for `repository`, `github`, `azure`, and `evaluations`
- before and after fingerprints
- redacted `error_info` and `resume_info`
- evaluator replacement lineage when evaluation activation is in play

A phase receipt binds the exact plan hash, phase-plan hash, provider receipt, approval hash, recorded fingerprints, and bounded summary.

## Evaluation activation receipts

Evaluation activation is receipt-bound on purpose.

The sidecar mutation stores an `ActivationBinding` that points back to:

- the bootstrap operation ID
- the exact plan hash
- the approval hash
- the evaluations phase receipt hash
- the runtime commit

That binding is what allows later review to prove that an activated verification bundle really came from the reviewed evaluations phase.

## Connection state and receipts

The combined GitHub and Azure connection step has its own state and receipt model.

It stores:

- a composite `ConnectionPlan`
- one owner `ConnectionApproval`
- per-phase receipts for the `github` and `azure` child phases
- a `ConnectionStatus`
- a final `ConnectionReceipt`

Owners approve once, but the runtime still preserves the child phase lineage needed for diagnostics and rollback.

## Local commit state and receipt

The reviewed bootstrap commit step stores its own local state and receipt.

The durable local-commit receipt records:

- the repository and runtime identity
- the reviewed repository plan hash and review hash
- the approval hash
- the base commit, branch name, commit SHA, and tree SHA
- the committed managed paths
- the registry digest
- per-profile and per-agent source and package digests

The local-commit state also keeps rollback snapshots so the reviewed branch, index, and managed files can be restored if the owner rolls the commit step back.

## Local deployment state and receipt

Local bootstrap deployment has its own resumable state machine.

The local deployment plan is tied to:

- the bootstrap operation ID
- the exact reviewed commit SHA
- the commit receipt hash
- one or more reviewed agent deployment plans

Each per-agent deployment receipt records:

- previous and published version
- source tree and source ZIP digests
- package, profile, registry, and target digests
- verification mode and verification status
- an optional evaluation link
- `draft_cleanup_complete: true`
- `route_mutated: false`
- `latest_verified: true`

A full `LocalDeploymentReceipt` collects the per-agent receipts under one approval hash.

## Registered deployment verification and publication receipts

The registry-managed deploy path has two receipt shapes:

- `DeploymentVerificationReceipt` from `foundry-opt deploy verify-registered`
- `DeploymentReceipt` from `foundry-opt deploy publish-registered`

The verification receipt proves exact-source verification without publication. The publication receipt adds:

- `previous_version`
- `published_version`
- `reconciled`
- `verification`
- `evaluation_link` and guardrails when Foundry evaluation ran

Registered deployment reconciliation metadata includes the full fingerprint set:

- `repoAgentId`
- source fingerprint
- package fingerprint
- profile fingerprint
- registry fingerprint
- target fingerprint

That metadata is what lets a later workflow run treat identical content as a no-op even when the merge commit SHA changed.

## Resumability rules

Resumability is always identity-bound.

- optimize jobs revalidate the job identity and replay only incomplete receipted work
- bootstrap apply, connection apply, local commit, and local deployment all use generation-checked state
- registered deployment can reconcile unchanged exact content instead of publishing a duplicate version
- superseded deployment runs return structured status instead of forcing stale publication

## Rollback ownership

Rollback is intentionally narrow.

- bootstrap runner rollback walks the recorded child ownership chain in reverse apply order: commit, then connection, then repository
- evaluation activation rollback is governed by the reviewed phase and activation binding
- local commit has explicit rollback snapshots
- optimize-job evidence, projection, and closure are idempotent, but they are not a general rollback surface
- published Foundry versions are not automatically rolled back by the bootstrap or deployment runtime

In short: receipts make replay safe; they do not imply that every successful mutation has a symmetric remote undo path.

## Related references

- [Run an optimization](../guides/run-an-optimization.md)
- [Operate deployments](../guides/operate-deployments.md)
- [Bootstrap](../get-started/bootstrap.md)
- [CLI reference](cli.md)
- [Repository contract](repository-contract.md)
