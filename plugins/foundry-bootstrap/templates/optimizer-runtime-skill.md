---
name: foundry-agent-optimizer
description: Load the Foundry optimizer skill from this session's verified runtime.
---

# Foundry optimizer runtime loader

Use this project skill only for an issue-driven optimize job. First-time
repository setup uses `foundry-bootstrap`.

Before any source edit, run:

```sh
foundry-opt validate-config --repository . --shared-checkout "$FOUNDRY_OPT_SHARED_ROOT"
```

Require successful validation and a concrete `optimizer_skill_source` in the
JSON result. Read `SKILL.md` under that returned directory and follow it for
the rest of the job. Resolve its relative references within that runtime skill
directory, not this loader's directory.

The setup workflow selects one exact runtime commit for the session. Do not
fetch another revision, modify the repository registry, or replace this loader
with a runtime skill copy. Do not commit session runtime files.

If setup variables, validation, or the runtime skill are unavailable, stop
without editing. Do not fall back to an older skill, manually optimize the
baseline, or treat an unverified proposal as measured evidence.
