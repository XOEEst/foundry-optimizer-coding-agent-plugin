# System overview

Foundry Optimizer has three lifecycles:

1. skill-only repository bootstrap;
2. issue-driven optimization;
3. exact-source deployment.

## Bootstrap

The owner invokes [`/foundry-bootstrap`](../../plugins/foundry-bootstrap/).
Each run begins with owner confirmation of one folder scope. The skill lists
every recognized descendant agent, lets the owner select the onboarding subset,
and confirms one Foundry project endpoint shared by that group. It performs
group-scoped read-only discovery, shows one combined plan, receives one
approval, extends repository and GitHub/Azure setup without changing other
agents, writes the bootstrap report, creates a local commit, and deploys only
the selected services with azd.

Large inventories use grouped summaries plus session-only Markdown/CSV rows and
number/range selection. The same discovery classifies optimizer readiness and
adds required readiness edits for selected agents to the reviewed patch.

Owners rerun the same skill to incrementally onboard another group or project.

Bootstrap has no shared Python runtime, command tree, operation state, receipt,
or rollback.

## Optimize job

The [`foundry-agent-optimizer`](../../plugins/foundry-agent-optimizer/) skill
uses [`poc/runtime.py`](../../src/foundry_opt/poc/runtime.py) for exact
registry provenance, draft evaluation, candidate state, and issue handoff.
It selects the issue's registry agent and projects that agent's sidecar into
runtime contracts in memory. No repository-global legacy policy or metadata
file is required.

The skill is committed as a repository project skill before a cloud session
starts. Copilot setup verifies its exact pinned bytes and launches the broker
and trusted state paths; it does not install the skill into the runner home
directory at setup time.

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
- [`poc/registry_runtime.py`](../../src/foundry_opt/poc/registry_runtime.py)

The registry and agent profiles are trusted configuration. The bootstrap
report is owner-readable history, not deployment authority.
