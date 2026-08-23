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
- where an existing per-agent sidecar was found and its target, baseline model,
  deployment state, and verification defaults
- whether an existing repository OIDC identity and exact matching connection
  resources can be reused
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
   `start` resumes the repository's active operation. It creates a new
   operation only when no active operation remains.
3. Read only the `<<<FOUNDRY_BOOTSTRAP_OWNER_MARKDOWN>>>` section to the
   owner.
4. Keep the `<<<FOUNDRY_BOOTSTRAP_TURN>>>` envelope for yourself. Never paste
   or expose its raw JSON to the owner.
5. Inspect `next_question` before asking the owner:
   - for any question other than `foundry_target`, ask exactly
     `next_question.title` plus `next_question.details_markdown` and present
     any exact choices
   - for `foundry_target`, first resolve as many `required_fields` as possible
     with repository and Azure tools; do not expose the machine-only field
     names or ask the owner for an Azure resource ID
6. Resolve a `foundry_target` question in this order:
   - if `project_endpoint` is required, search the selected agent root and
     repository metadata, including existing profiles, `.foundry` metadata,
     `azure.yaml`, and azd environment values; ask the owner for the Foundry
     project endpoint only when no unique value can be established
   - if `agent_name` is required, search the same repository evidence; ask the
     owner only when no unique deployed agent name can be established
   - once the project endpoint is known, derive the exact Foundry account name
     from its hostname
   - if `account_resource_id` is required, use Azure resource lookup tools with
     the owner's current login, preferring Azure MCP or Azure Resource Graph,
     and query `Microsoft.CognitiveServices/accounts` for that exact name
   - accept an account only when one unique matching resource is found and its
     name matches the endpoint; pass its immutable ARM resource ID with
     `--account-resource-id "<resource-id>"`
   - if no unique account is visible, explain which endpoint/account was
     searched and ask the owner to correct the Azure tenant/subscription login
     or choose among the matching subscriptions, then repeat the lookup
   Collect every required field before invoking `answer`. Never ask the owner
   to discover or type an ARM resource ID.
7. Pass the owner response or tool-resolved target data back to the script
   instead of inventing deterministic validation, planning, or provider
   mutation logic:
   - choice question:
     `python scripts/bootstrap.py answer --operation-id <id> --question-id <question-id> --choice <value> [--choice <value> ...]`
   - free-text question:
     `python scripts/bootstrap.py answer --operation-id <id> --question-id <question-id> --response "<owner response>"`
   - Foundry target question:
     `python scripts/bootstrap.py answer --operation-id <id> --question-id <question-id> [--project-endpoint "<endpoint>"] [--agent-name "<name>"] [--account-resource-id "<resource-id>"]`
   - blocked Foundry project inventory after correcting data-plane access:
     `python scripts/bootstrap.py answer --operation-id <id> --question-id <question-id> --retry`
   Supply every Foundry target field named by the question. Never encode an
   owner answer as JSON.
8. When `available_actions` includes `approve`, request that exact approval
   from the owner and record it with
   `python scripts/bootstrap.py approve --operation-id <id> --step <repository|connection|commit|deployment> --actor "<owner>" --summary "<approved scope>"`.
9. Use `python scripts/bootstrap.py status --operation-id <id>` to resume
   after interruptions, refresh stale questions, or recover the current bridge
   state.
10. Use `python scripts/bootstrap.py rollback --operation-id <id> --step <repository|connection|commit|deployment>`
   only when the returned `available_actions` list includes that rollback step.
11. If `next_question` is `null`, do not invent a new question. Show the fresh
    owner markdown, stop on blocked/final states, or use the returned recovery
    actions.
12. Do not create or switch to a custom agent. Do not use another owner
    client. Keep target validation and classification, lifecycle state,
    planning, mutation, commit creation, deployment, receipts, and rollback in
    the runtime.
13. At final handoff, present the returned resource links grouped as GitHub,
    Azure, Foundry agents, and optional evaluation resources.

## References

- [Owner flow](references/owner-flow.md)
- [Recovery](references/recovery.md)
- [Security](references/security.md)
