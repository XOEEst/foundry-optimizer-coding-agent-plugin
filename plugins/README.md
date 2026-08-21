# Plugins

This repository separates top-level plugin ownership from the shared
`foundry_opt` runtime package.

- `foundry-bootstrap/` defines the future bootstrap skill boundary for owners.
  This task only lays out the checked-in distribution surface. Runtime
  launchers and setup workflow entrypoints still live under `src/foundry_opt/`
  until a dependent migration task moves them.
- `foundry-agent-optimizer/` is the canonical issue-time optimizer skill
  folder. Setup workflows and bootstrap defaults install from this path, while
  legacy explicit pins and receipts that still name the former
  `src/foundry_opt/templates/skills/foundry-agent-optimizer/` location are
  compatibly resolved here.

Both skill folders are thin owner/runtime clients over shared `foundry_opt`
code. They do not vendor or duplicate the runtime package.
