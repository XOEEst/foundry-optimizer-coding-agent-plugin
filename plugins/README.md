# Plugins

This repository separates top-level skill distribution from the shared
`foundry_opt` runtime package.

- `foundry-bootstrap/` is the owner bootstrap skill boundary. Its canonical
  runtime install and verification scripts live under `scripts/`; the old
  `src/foundry_opt/bootstrap/launch-bootstrap.*` paths remain thin
  source-checkout compatibility wrappers.
- `foundry-agent-optimizer/` is the canonical issue-time optimizer skill folder.
  Setup workflows and bootstrap defaults install from this path, while legacy
  explicit pins and receipts that name the former
  `src/foundry_opt/templates/skills/foundry-agent-optimizer/` location are
  compatibly resolved here.

Both skill folders are thin owner/runtime clients over shared `foundry_opt`
code. They do not vendor or duplicate the runtime package.
