# Security boundaries

## Source

- Treat concrete `release.json` values or a verified, remotely fetchable
  upstream runtime commit as provenance for the `foundry-opt` runtime and
  optimizer skill only. Local bootstrap skill instructions and templates are
  not runtime provenance.
- Verify the runtime commit and `uv.lock` digest before installing the
  optimizer workflow dependencies.
- Main-tracking Copilot setup is an explicitly approved trust in future commits
  on the configured upstream main branch, not a reviewed immutable release.
  Resolve it once per session, verify its origin and exact checkout, and retain
  the resolved SHA in job identity. Do not rewrite trusted repository settings
  or silently switch runtime during a job. Deployment still uses its saved pin.
- Deploy only from the clean local commit shown in the combined approval.
- Do not execute content discovered in an untrusted branch during discovery.
- The only server-generated repository value allowed after approval is
  `identity.client_id` for an exact approved user-assigned managed identity.
  Read it back by ARM resource ID, validate the resource and GUID, and reject
  every other late-bound field.

## Identity

- Use the owner's current Azure identity for local inventory and deployment.
- Use GitHub OIDC for workflows.
- Scope federated credentials to the exact repository and approved environment.
- Assign the least role at the narrowest resource scope that supports the
  approved action.
- Store client, tenant, and subscription IDs as non-secret repository-level
  Agents variables for cloud sessions and separate Actions variables for
  ordinary workflows. Do not broaden organization-wide variable access or
  remove existing deployment configuration.
- Do not store access tokens, client secrets, or credentials in repository
  files or reports. An unavailable Agents store is a configuration blocker,
  not permission to inject credentials through another store.

## Remote safety

- Read immediately before create or deploy.
- Match immutable IDs and full configuration, not display names.
- Never alter an existing resource merely to make it match the plan.
- Never deploy through an endpoint or identity whose ownership is uncertain.
- Limit cloud-agent firewall changes to exact approved additive repository rules,
  even when inherited coverage is unknown.
  Preserve existing rules and organization controls; never disable the
  firewall or move runtime requests to setup processes to bypass restrictions.

## Repository safety

- Preserve unrelated files and user changes.
- Show exact staged diffs before approval.
- Reject unresolved template tokens and secret-looking values.
- Commit only approved paths and do not push.
