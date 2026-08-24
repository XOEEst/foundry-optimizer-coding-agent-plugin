# Security boundaries

## Source

- Treat the published `release.json` as provenance for the `foundry-opt`
  runtime and optimizer skill only.
- Verify the runtime commit and `uv.lock` digest before installing the
  optimizer workflow dependencies.
- Deploy only from the clean local commit shown in the combined approval.
- Do not execute content discovered in an untrusted branch during discovery.

## Identity

- Use the owner's current Azure identity for local inventory and deployment.
- Use GitHub OIDC for workflows.
- Scope federated credentials to the exact repository and approved environment.
- Assign the least role at the narrowest resource scope that supports the
  approved action.
- Store client, tenant, and subscription IDs as non-secret GitHub variables.
  Do not store access tokens, client secrets, or credentials in repository
  files or reports.

## Remote safety

- Read immediately before create or deploy.
- Match immutable IDs and full configuration, not display names.
- Never alter an existing resource merely to make it match the plan.
- Never deploy through an endpoint or identity whose ownership is uncertain.

## Repository safety

- Preserve unrelated files and user changes.
- Show exact staged diffs before approval.
- Reject unresolved template tokens and secret-looking values.
- Commit only approved paths and do not push.
