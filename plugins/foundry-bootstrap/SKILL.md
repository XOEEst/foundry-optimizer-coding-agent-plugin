---
name: foundry-bootstrap
description: Establish the top-level bootstrap plugin boundary over the shared foundry_opt runtime.
---

# Foundry bootstrap

This folder defines the top-level bootstrap plugin boundary.

## Owner contract

- Treat this plugin as a thin client over shared `foundry_opt` runtime code.
- Treat `skill.lock.template.json` as the canonical field contract for reviewed
  runtime pins (`runtime_repository`, `runtime_commit`, `uv_lock_sha256`, and
  `package_path`).
- Use `references/` for reviewed notes and source pointers.
- Use `templates/` for future owner-facing artifacts after the migration task
  lands.
- Use `scripts/install-runtime.ps1` and `scripts/install-runtime.sh` as the
  canonical runtime install and verification entrypoints.
- The legacy `src/foundry_opt/bootstrap/launch-bootstrap.*` files remain thin
  source-checkout compatibility wrappers over the canonical scripts.
