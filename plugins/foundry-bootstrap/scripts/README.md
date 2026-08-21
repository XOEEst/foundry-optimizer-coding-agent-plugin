# Scripts

This directory is the canonical checked-in home for the reviewed owner bridge
and runtime install/verification launchers:

- `bootstrap.py` — the only owner client over `BootstrapRunner`. It translates
  `start`, `answer`, `approve`, `status`, and `rollback` requests into direct
  runner calls, prints owner markdown separately from the machine envelope, and
  uses the reviewed runtime contract when it must self-install the runtime.

These subcommands are transport between the skill and runner. Owner
documentation should say "Use `/foundry-bootstrap`", not teach this command
surface.

- `install-runtime.ps1`
- `install-runtime.sh`

Both launchers accept an exact reviewed runtime contract either from explicit
arguments or from a materialized `skill.lock.json` that follows the
`skill.lock.template.json` field names.

The legacy `src/foundry_opt/bootstrap/launch-bootstrap.*` files remain thin
source-checkout compatibility wrappers that delegate here.
