---
name: foundry-bootstrap
description: Guide a first-time owner through the downloadable bootstrap start/resume/approval loop over the shared foundry_opt runtime.
---

# Foundry bootstrap

Use this skill for the first-time owner bootstrap loop. This downloadable skill
is the only owner client over `BootstrapRunner`.

## Owner experience

Keep every owner turn short and concrete:

- what agents were discovered and where
- which agents will be ignored, registered disabled, or registered enabled
- the Foundry project endpoint and agent name for each enabled agent
- the GitHub environments and Azure identity/OIDC/RBAC connection
- optional dataset, evaluator, or repository-check verification
- the exact local commit and deployment action
- links to all relevant resources

Evaluation is optional. Do not block registration or enablement because a
dataset or evaluator is absent. A no-evidence deployment must retain the
runner's explicit warning.

Never ask the owner to inspect raw JSON, construct hashes, write approval
files, or learn the low-level bootstrap CLI.

## Required owner loop

1. Keep the materialized `skill.lock.json` beside this skill so
   downloaded `bootstrap.py` can always install and re-execute through the
   exact reviewed runtime. Ambient `foundry_opt` packages are ignored.
2. Invoke this skill script:
   - downloaded skill: `python scripts/bootstrap.py start --repository .`
   - source checkout of this repository:
     `python plugins/foundry-bootstrap/scripts/bootstrap.py start --repository .`
3. Read only the `<<<FOUNDRY_BOOTSTRAP_OWNER_MARKDOWN>>>` section to the
   owner.
4. Keep the `<<<FOUNDRY_BOOTSTRAP_TURN>>>` envelope for yourself. Never paste
   or expose its raw JSON to the owner.
5. If `next_question` is present, ask the owner exactly
   `next_question.title` plus `next_question.details_markdown`. If
   `next_question.choices` are present, present those exact choices.
6. Pass the owner response back to the script instead of inventing planning or
   provider logic:
   - choice question:
     `python scripts/bootstrap.py answer --operation-id <id> --question-id <question-id> --choice <value> [--choice <value> ...]`
   - free-text question:
     `python scripts/bootstrap.py answer --operation-id <id> --question-id <question-id> --response "<owner response>"`
7. When `available_actions` includes `approve`, request that exact approval
   from the owner and record it with
   `python scripts/bootstrap.py approve --operation-id <id> --step <repository|connection|commit|deployment> --actor "<owner>" --summary "<approved scope>"`.
8. Use `python scripts/bootstrap.py status --operation-id <id>` to resume
   after interruptions, refresh stale questions, or recover the current bridge
   state.
9. Use `python scripts/bootstrap.py rollback --operation-id <id> --step <repository|connection|commit|deployment>`
   only when the returned `available_actions` list includes that rollback step.
10. If `next_question` is `null`, do not invent a new question. Show the fresh
    owner markdown, stop on blocked/final states, or use the returned recovery
    actions.
11. Do not create or switch to a custom agent. Do not use another owner
    client. Do not implement Foundry target resolution, planning, commit
    creation, or deployment logic in this skill.
12. At final handoff, present the returned resource links grouped as GitHub,
    Azure, Foundry agents, and optional evaluation resources.

## References

- [Owner flow](references/owner-flow.md)
- [Recovery](references/recovery.md)
- [Security](references/security.md)
