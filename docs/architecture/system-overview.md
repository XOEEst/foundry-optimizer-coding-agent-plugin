# System overview

Foundry Optimizer has three lifecycles:

1. skill-only repository bootstrap;
2. issue-driven optimization;
3. exact-source deployment.

## Bootstrap

The owner invokes [`/foundry-bootstrap`](../../plugins/foundry-bootstrap/).
The skill performs read-only discovery, shows one combined plan, receives one
approval, applies repository and GitHub/Azure setup, writes the bootstrap
report, creates a local commit, and deploys with azd.

Bootstrap has no shared Python runtime, command tree, operation state, receipt,
or rollback.

## Optimize job

The [`foundry-agent-optimizer`](../../plugins/foundry-agent-optimizer/) skill
uses [`poc/runtime.py`](../../src/foundry_opt/poc/runtime.py) for exact
registry provenance, draft evaluation, candidate state, and issue handoff.

## Deployment

Initial local deployment uses azd from the reviewed bootstrap commit.
Registered main-branch deployment uses
[`poc/deploy.py`](../../src/foundry_opt/poc/deploy.py) and the committed
registry/profile contract.

## Shared contract

Optimizer and registered deployment share:

- [`repository_contracts.py`](../../src/foundry_opt/repository_contracts.py)
- [`repository_selection.py`](../../src/foundry_opt/repository_selection.py)
- [`source_discovery.py`](../../src/foundry_opt/source_discovery.py)

The registry and agent profiles are trusted configuration. The bootstrap
report is owner-readable history, not deployment authority.
