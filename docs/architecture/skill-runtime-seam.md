# Skill-only bootstrap

Bootstrap has no Python orchestration runtime or CLI seam. The installed
`foundry-bootstrap` skill is the owner-facing module and uses general
repository, Git, GitHub, Azure, Foundry, and azd tools directly.

## Owner interface

The owner invokes `/foundry-bootstrap`, resolves any ambiguous agent or cloud
choices, reviews one combined plan, and gives one approval.

Before approval the skill performs read-only inspection and stages proposed
files in the session workspace. After approval it:

1. applies the reviewed repository changes;
2. adopts exact GitHub/Azure resources and creates missing resources;
3. writes `.foundry-opt/bootstrap-report.md`;
4. creates one local commit;
5. deploys enabled agents with `azd deploy`.

There is no operation ID, state machine, receipt, compensation, or rollback.
Conflicting existing remote resources stop the flow instead of being
overwritten.

## Retained runtime seam

Optimizer and registered deployment still use shared code:

- [`repository_contracts.py`](../../src/foundry_opt/repository_contracts.py)
- [`repository_selection.py`](../../src/foundry_opt/repository_selection.py)
- [`source_discovery.py`](../../src/foundry_opt/source_discovery.py)
- [`poc/runtime.py`](../../src/foundry_opt/poc/runtime.py)
- [`poc/deploy.py`](../../src/foundry_opt/poc/deploy.py)

These modules validate committed registry/profile contracts and exact-source
optimizer or deployment operations. They do not orchestrate bootstrap.

## Failure model

Bootstrap does not roll back. Local changes remain visible in Git and remote
resources already created remain in place. The report and final skill response
identify completed, failed, and pending actions so a rerun can re-inspect and
adopt exact existing state.

## Related architecture

- [System overview](system-overview.md)
- [Module map](module-map.md)
- [Trust model](trust-model.md)
