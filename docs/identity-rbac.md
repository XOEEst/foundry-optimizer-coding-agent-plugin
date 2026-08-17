# Identity and RBAC

Bootstrap uses one repository-wide user-assigned managed identity by default, with separate
federated credentials per GitHub environment. It never creates static credentials and never
assigns a privileged fallback role.

## Approved least-privilege role matrix

`foundry-opt bootstrap plan` accepts only the Azure built-in role definitions below. Anything
else fails closed with `role_definition_id is not in the approved allow-list`. Each approved
assignment must also carry an alias that starts with the role slug, so plans, actions, and
receipts name the role they actually grant.

| Slug | Role | Role definition GUID | Scope kind | Why it is required |
| --- | --- | --- | --- | --- |
| `foundry-user` | Foundry User | `53ca6127-db72-4b80-b1b0-d745d6d5456d` | foundry | project read plus Cognitive Services data actions for draft agent, dataset, evaluator, definition, and run operations |
| `foundry-project-runtime-user` | Foundry Project Runtime User | `142bfaed-a13f-4c2d-bed2-6db62c4a1009` | foundry | project runtime data-plane access used by hosted agent execution during evaluation and deployment verification |
| `foundry-agent-consumer` | Foundry Agent Consumer | `eed3b665-ab3a-47b6-8f48-c9382fb1dad6` | foundry | invoke an existing agent version without publication or routing authority |
| `monitoring-reader` | Monitoring Reader | `43d0d8ad-25c7-4714-9337-8ba259a9fe05` | telemetry | read Application Insights telemetry when trace-derived dataset generation is modeled |
| `log-analytics-reader` | Log Analytics Reader | `73c42c96-874c-492b-b04d-ab87d138a893` | telemetry | query the Log Analytics workspace backing Application Insights trace availability probes |

Telemetry roles are optional: plan them only when trace-derived dataset generation is actually
modeled for an agent. Synthetic-only onboarding needs neither.

## Explicitly refused roles

These are rejected by the plan input contract before any Azure call, and independently by the
ARM provider:

| Role | Role definition GUID |
| --- | --- |
| Owner | `8e3af657-a8ff-443c-a75c-2fe8c4bcb635` |
| Contributor | `b24988ac-6180-42a0-ab88-20f7382dd24c` |
| Azure AI Project Manager | `eadc314b-1a2d-4efa-be10-5d325db5065e` |

There is no broad-role fallback path: an operation that cannot be expressed with the approved
matrix must be re-reviewed, not escalated.

## Scope rules

- Subscription-wide and management-group scopes are refused.
- Every approved scope must stay inside the reviewed subscription and resource group.
- Prefer the narrowest scope that works: the Foundry project, then the account, then the
  Application Insights or Log Analytics resource for telemetry roles.
- Rollback removes only role assignments this operation created; adopted assignments are
  never deleted.

## Identity naming

`AzureIdentityInput.existing_resource_id` must target
`.../providers/Microsoft.ManagedIdentity/userAssignedIdentities/<name>`, and the planned
identity name is always derived from that id — including when `create_if_missing` is true,
where the id is the reviewed creation target. Plans, receipts, and provider state therefore
name the exact resource that Azure returns, with no placeholder. Adopted Entra applications
have no ARM resource id, so their exact client id is used as the identity label.

## Pilot policy

The retained development pilot assigns **project-scoped `Foundry User` only**. The remaining
approved roles stay available for reviewed rollouts that need hosted runtime execution,
agent invocation, or telemetry probes, but they are not part of the pilot baseline.

## Accepted residual risk

One shared principal means the Copilot-session token carries the same Azure publication RBAC
as deployment. Separate OIDC subjects constrain token issuance contexts, not Azure role blast
radius, and CLI draft-only enforcement is not principal isolation. Future migration to
separate identities is preserved.
