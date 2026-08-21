# Plugins

This repository separates top-level skill distribution from the shared
`foundry_opt` runtime package.

- `foundry-bootstrap/` is the downloadable first-time-owner skill. Owners
  invoke `/foundry-bootstrap`; the skill presents natural-language reviews and
  uses `scripts/bootstrap.py` internally to start, resume, answer, approve, or
  recover one durable `BootstrapRunner` operation.
- `foundry-agent-optimizer/` is the canonical issue-time optimizer skill folder.
  Setup workflows and bootstrap defaults install from this path, while legacy
  explicit pins and receipts that name the former
  `src/foundry_opt/templates/skills/foundry-agent-optimizer/` location are
  compatibly resolved here.

Both skill folders are thin owner/runtime clients over shared `foundry_opt`
code. They do not vendor or duplicate the runtime package.

The bootstrap skill is intentionally the only normal owner interface. The
existing `foundry-opt bootstrap ...` commands remain available for CI,
diagnostics, recovery, and older integrations.

Release packaging for `foundry-bootstrap/` is built after checkout with
`uv run python tools/build_foundry_bootstrap_skill.py`, which writes the
installable package directory plus `dist/foundry-bootstrap-skill.zip` and
`dist/foundry-bootstrap-skill.checksums.json`.

See [`foundry-bootstrap/references/owner-flow.md`](foundry-bootstrap/references/owner-flow.md)
for the owner experience and
[`foundry-bootstrap/references/recovery.md`](foundry-bootstrap/references/recovery.md)
for interruption handling.
