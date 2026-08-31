# Plugins

This repository separates top-level skill distribution from the shared
`foundry_opt` runtime package.

- `foundry-bootstrap/` is the downloadable first-time-owner skill. Owners
  invoke `/foundry-bootstrap`, confirm one folder scope, select any recognized
  descendant agents, and bind the group to one shared Foundry project endpoint.
  They can rerun it to onboard additional groups incrementally. The skill uses
  general repository, GitHub, Azure, Foundry, Git, and azd tools directly after
  one reviewed approval.
- `foundry-agent-optimizer/` is the canonical issue-time optimizer skill folder.
  Setup workflows install this skill from the exact runtime revision recorded
  in the repository registry.

The bootstrap skill is static and does not invoke a bootstrap runtime, state
machine, receipt, or rollback service. The optimizer skill remains a client of
the shared optimizer runtime.

The bootstrap skill is the only bootstrap interface. There is no bootstrap
command tree in the shared CLI.

Standard release automation packages the static skill folder, injects
`release.json`, and publishes the ZIP and checksum.

See [`foundry-bootstrap/references/owner-flow.md`](foundry-bootstrap/references/owner-flow.md)
for the owner experience and
[`foundry-bootstrap/references/recovery.md`](foundry-bootstrap/references/recovery.md)
for interruption handling.
