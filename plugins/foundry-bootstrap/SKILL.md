---
name: foundry-bootstrap
description: Establish the top-level bootstrap plugin boundary over the shared foundry_opt runtime.
---

# Foundry bootstrap

This folder defines the top-level bootstrap plugin boundary.

## Owner contract

- Treat this plugin as a thin client over shared `foundry_opt` runtime code.
- Treat `skill.lock.template.json` as a placeholder pin contract, not as an
  active runtime lock.
- Use `references/` for reviewed notes and source pointers.
- Use `templates/` for future owner-facing artifacts after the migration task
  lands.
- The checked-in `scripts/` content in this task documents intent only. Do not
  assume local launchers or setup workflow entrypoints exist in this folder
  yet.
