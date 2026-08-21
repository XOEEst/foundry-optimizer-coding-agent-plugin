# Scripts

This directory is the canonical checked-in home for the reviewed runtime
install and verification launchers:

- `install-runtime.ps1`
- `install-runtime.sh`

Both launchers accept an exact reviewed runtime contract either from explicit
arguments or from a materialized `skill.lock.json` that follows the
`skill.lock.template.json` field names.

The legacy `src/foundry_opt/bootstrap/launch-bootstrap.*` files remain thin
source-checkout compatibility wrappers that delegate here.
