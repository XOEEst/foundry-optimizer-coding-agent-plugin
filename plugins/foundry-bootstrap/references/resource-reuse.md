# Exact cloud resource reuse

Remote resources are either reused exactly, created because they are missing,
or treated as blockers.

Scope each run to the user-confirmed source folder and project endpoint.
Existing resources for other registered folders are inventory context only and
must not be changed.

## Exact match

Names alone are insufficient. Confirm the resource type, immutable identifier,
tenant/subscription, resource group or project, endpoint, and relevant
configuration.

Examples:

- A Foundry project matches only when its endpoint is the user-confirmed
  endpoint and its backing account ARM ID matches the selected sidecar.
- A deployed agent matches only when it is in the reviewed project and has the
  reviewed agent name.
- A managed identity matches only when its resource ID, tenant, and client ID
  match.
- A federated credential matches only when issuer, audience, subject, and
  parent identity match.
- A role assignment matches only when principal, role definition, and scope
  match.
- A GitHub environment matches only in the intended repository with the exact
  environment name and compatible protection settings.

## Missing resources

The combined approval must identify every missing resource by deterministic
name and scope and state which command or API will create it. Immediately
before creation, repeat the read and confirm it is still missing.

Generated values such as a new identity's client ID may be copied only to the
approved GitHub variable after the create response is checked against the
approved resource ID.

## Conflicts and insufficient access

Stop when:

- the desired name is occupied by a different resource
- an endpoint resolves to a different account or project
- an OIDC subject is attached to another identity
- a role assignment is broader or otherwise different
- an existing workflow or `azure.yaml` service has incompatible behavior
- permissions prevent a conclusive inventory

Do not delete, rename, replace, repurpose, or edit the conflicting resource.
Document the blocker for the owner.

## Deployment

For an exact existing agent, `azd deploy` may publish a new immutable version.
It must not redirect or replace a different named agent. For a missing approved
agent, `azd deploy` creates the first version from the exact local commit. Run
`azd deploy <selected-service>` and leave every other service untouched.
