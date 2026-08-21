# Plugins

This repository separates top-level plugin ownership from the shared
`foundry_opt` runtime package.

- `foundry-bootstrap/` defines the future bootstrap skill boundary for owners.
  This task only lays out the checked-in distribution surface. Runtime
  launchers and setup workflow entrypoints still live under `src/foundry_opt/`
  until a dependent migration task moves them.
- `foundry-agent-optimizer/` reserves the future home for the existing
  optimizer skill. The discoverable skill still lives under
  `src/foundry_opt/templates/skills/foundry-agent-optimizer/` in this task.

Both skill folders are thin owner/runtime clients over shared `foundry_opt`
code. They do not vendor or duplicate the runtime package.
