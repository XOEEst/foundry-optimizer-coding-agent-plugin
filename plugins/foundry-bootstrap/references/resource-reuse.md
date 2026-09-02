# Exact cloud resource reuse

Remote resources are either reused exactly, created because they are missing,
or treated as blockers.

Scope each run to the user-confirmed folder scope, selected agent subset, and
shared project endpoint. Existing resources for excluded or previously
registered agents are inventory context only and must not be changed.

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
  environment name and approved deployment branch mode. Repository branch
  protection is independent and may be absent.

## Missing resources

The combined approval must identify every missing resource by deterministic
name and scope and state which command or API will create it. Immediately
before creation, repeat the read and confirm it is still missing.

For a newly approved user-assigned managed identity, omit
`identity.client_id` from the static repository patch. After creation, read the
identity back by its exact approved ARM resource ID. Its generated client ID may
be added only to the declared registry field and used for the approved GitHub
variable after all immutable resource properties match. No other repository
field may be late-bound.

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

An environment with custom policy mode enabled and no allowed entries is
partial state rather than a conflicting resource. A new approved plan may add
the intended entries or disable custom mode.

## Deployment

For an exact existing agent, `azd deploy` may publish a new immutable version.
It must not redirect or replace a different named agent. For a missing approved
agent, `azd deploy` creates the first version from the exact local commit. Run
`azd deploy <selected-service>` once per selected enabled agent in the approved
order and leave every other service untouched.
