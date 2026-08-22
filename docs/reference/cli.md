# CLI reference

This page documents the current public command tree from [`src/foundry_opt/cli.py`](../../src/foundry_opt/cli.py) and [`src/foundry_opt/bootstrap/cli.py`](../../src/foundry_opt/bootstrap/cli.py).

Normal owners should use `/foundry-bootstrap`. The `foundry-opt ...` tree remains the advanced compatibility, workflow, and automation surface.

Hidden internal commands are omitted here. For example, `foundry-opt broker serve` is intentionally not part of the normal public operator surface.

## Stable behavior classes

| Class | Meaning |
| --- | --- |
| read-only | validate, inspect, or render without changing trusted or remote state |
| owner-state | store reviewed bootstrap answers, approvals, or resumable local state |
| repository-mutating | change managed repository files, branches, commits, or ownership ledgers |
| GitHub-writing | upsert issue comments, discover or close the exact PR, or bind trusted broker state |
| cloud-mutating | change reviewed GitHub environments, Azure identity/RBAC, or Foundry resources |
| draft-mutating | create, evaluate, or delete temporary Foundry drafts |
| projection | project a selected patch to the early PR checkout |
| regular-publication | publish or reconcile a regular immutable Foundry version |

## Normal owner path vs compatibility CLI

- `/foundry-bootstrap` is the normal owner path.
- `foundry-opt bootstrap ...` is the frozen compatibility tree for automation, reviewed source checkouts, and recovery.
- `foundry-opt job ...` and `foundry-opt broker ...` are the optimize-job runtime surface.
- `foundry-opt deploy plan`, `foundry-opt deploy verify-registered`, and `foundry-opt deploy publish-registered` are the registry-managed deployment surface.
- `foundry-opt validate-config`, `foundry-opt preflight`, `foundry-opt deploy preflight`, and `foundry-opt deploy publish` remain legacy single-agent compatibility commands.

## Top-level commands

- `foundry-opt version` - print the package version. Read-only.
- `foundry-opt validate-config` - validate the legacy single-agent repository contract and optional shared checkout. Read-only.
- `foundry-opt preflight` - validate bootstrap, metadata, OIDC, broker, and state-root prerequisites for the legacy single-agent runtime. Read-only.

## Bootstrap compatibility tree

Core bootstrap commands:

- `foundry-opt bootstrap verify` - verify the exact runtime checkout against either the committed registry pin or a legacy shared pin, then write a bootstrap receipt. Owner-state.
- `foundry-opt bootstrap discover` - discover repository agents, capture fingerprints, and persist reviewed discovery state. Owner-state.
- `foundry-opt bootstrap binding-evidence` - observe deployed immutable agent versions and write a reviewable binding-evidence file. Read-only.
- `foundry-opt bootstrap plan` - build the exact multi-phase bootstrap plan from reviewed inputs. Owner-state.
- `foundry-opt bootstrap status` - report persisted bootstrap operation status. Read-only.
- `foundry-opt bootstrap diff` - diff two saved bootstrap plans. Read-only.
- `foundry-opt bootstrap apply` - apply one approved bootstrap phase against the recorded exact plan. Owner-state; repository-mutating or cloud-mutating depending on the selected phase.
- `foundry-opt bootstrap rollback` - roll back one applied bootstrap phase when that phase supports rollback. Owner-state; repository-mutating or cloud-mutating depending on the selected phase.
- `foundry-opt bootstrap resources` - render the owner-facing resource links from state, a plan, or a reviewed plan input. Read-only.

Owner review subcommands:

- `foundry-opt bootstrap review discovery` - render a human-friendly discovery summary from state or a discovery file. Read-only.
- `foundry-opt bootstrap review plan` - render a human-friendly plan summary from state or a plan file. Read-only.
- `foundry-opt bootstrap review status` - render a human-friendly bootstrap status summary. Read-only.

Composite connection subcommands:

- `foundry-opt bootstrap connect plan` - preview the combined GitHub and Azure connection plan. Read-only.
- `foundry-opt bootstrap connect approve` - create and bind the exact owner approval for the connection plan. Owner-state.
- `foundry-opt bootstrap connect apply` - apply the approved combined connection step. Owner-state, GitHub-writing, cloud-mutating.
- `foundry-opt bootstrap connect status` - inspect the combined connection status. Read-only.
- `foundry-opt bootstrap connect rollback` - roll back the combined connection step when approval and child receipts allow it. Owner-state, GitHub-writing, cloud-mutating.

Evaluation onboarding subcommands:

- `foundry-opt bootstrap evaluation inventory` - inspect reusable datasets, evaluators, and trace prerequisites. Read-only.
- `foundry-opt bootstrap evaluation plan` - build the reviewed evaluations action set and execution contracts. Read-only.
- `foundry-opt bootstrap evaluation apply` - run the staged evaluations phase without yet mutating the sidecar. Owner-state, cloud-mutating, draft-mutating.
- `foundry-opt bootstrap evaluation activate` - atomically persist the reviewed verification bundle, lineage, and managed lock updates. Owner-state, repository-mutating.
- `foundry-opt bootstrap evaluation status` - show phase state and sidecar activation state. Read-only.
- `foundry-opt bootstrap evaluation inspect` - inspect persisted bundle lineage, contracts, and finalization details. Read-only.
- `foundry-opt bootstrap evaluation replace` - validate an explicit reviewed bundle replacement before planning and activation. Read-only.

## Optimize-job support commands

- `foundry-opt issue parse` - parse the optimize issue and optionally prove it narrows policy. Read-only.
- `foundry-opt broker launch` - create the trusted issue binding and launch the detached GitHub broker. GitHub-writing.
- `foundry-opt broker bind-pr` - discover and bind the exact early pull request through the broker. GitHub-writing.

Optimize-job runtime commands:

- `foundry-opt job start` - initialize trusted job state, record the issue request, run the fresh baseline, and record baseline evidence. Draft-mutating, GitHub-writing.
- `foundry-opt job status` - load trusted optimize-job state and emit deterministic JSON. Read-only.
- `foundry-opt job handoff` - create one isolated candidate workspace under the trusted job root. Owner-state.
- `foundry-opt job complete` - finalize one candidate, evaluate it, and record candidate evidence. Draft-mutating, GitHub-writing.
- `foundry-opt job finish` - run final validation when needed, project the selected candidate or close the PR unchanged, write final evidence, and finish cleanup. Draft-mutating, GitHub-writing, projection.
- `foundry-opt job resume` - continue incomplete receipted optimize-job work after identity and state checks. Depends on pending work.
- `foundry-opt acceptance smoke` - run the protected draft create, evaluate, and cleanup smoke path. Draft-mutating.

## Deployment commands

Legacy single-agent deployment compatibility:

- `foundry-opt deploy preflight` - validate the exact merge commit and legacy single-agent deployment prerequisites. Read-only.
- `foundry-opt deploy publish` - validate one exact source ZIP as a draft, then publish a regular version for the legacy single-agent contract. Draft-mutating, regular-publication.

Registry-managed deployment commands:

- `foundry-opt deploy plan` - build the registry-bound deployment plan for one changed enabled agent. Read-only.
- `foundry-opt deploy verify-registered` - run the selected deployment verification gate without creating a regular version. Draft-mutating for Foundry evaluation, otherwise verification-only.
- `foundry-opt deploy publish-registered` - evaluate and publish one exact registry-selected commit, or reconcile if the latest regular version already matches. Draft-mutating, regular-publication.

## Output conventions

- `foundry-opt version` prints plain text.
- Most runtime and workflow commands emit deterministic JSON.
- Owner-facing review and connection commands render plain text by default and support `--markdown` or `--json`.

## Related references

- [Bootstrap](../get-started/bootstrap.md)
- [Run an optimization](../guides/run-an-optimization.md)
- [Operate deployments](../guides/operate-deployments.md)
- [Repository contract](repository-contract.md)
- [Evidence, state, and receipts](evidence-state-and-receipts.md)
