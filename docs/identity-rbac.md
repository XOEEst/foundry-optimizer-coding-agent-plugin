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

The two object identifiers have different meanings:

- For a user-assigned managed identity, `existing_object_id` is the managed identity principal
  object ID and is planned as `principal_id`.
- For an adopted Entra application, `existing_object_id` is the **application registration**
  object ID and is planned as `object_id`. The provider uses that ID for Microsoft Graph
  federated-credential operations, then resolves the application's service principal object ID
  separately for Azure RBAC.

Do not substitute the service principal object ID for the application object ID in an
`entra_application` plan input.

## GitHub OIDC subjects

The registry records the exact GitHub `sub` prefix used by both runtime
validation and Azure federation. It accepts the legacy name-based form
`repo:<owner>/<repository>` and GitHub's immutable form
`repo:<owner>@<owner-id>/<repository>@<repository-id>`.

Repositories created, renamed, or transferred after July 15, 2026 can emit the
immutable form. Bootstrap must inventory GitHub's OIDC settings, freeze the
reported prefix in the reviewed plan, and create one exact environment subject
per configured GitHub environment. It must not silently retain a mutable
name-based credential when GitHub emits an immutable subject.

The reviewed federated-credential actions are authoritative through planning and apply. The
Azure provider must consume those exact subjects; it never reconstructs them from a mutable
`owner/repository` name. Before approval, confirm the Azure action summary contains exactly two
subjects and that both begin with the reviewed registry `oidc_subject_prefix`.

For adopted Entra applications, the credential's display `name` is not its identity. Bootstrap
inventories the application's federated credentials and adopts one unique exact
issuer/subject/audience match even when it has a legacy human-readable name. The deterministic
subject hash is used only as the name for a newly created credential. A duplicate match, a
same-subject credential with different issuer/audience, or a deterministic-name collision fails
closed before mutation. Graph deletion and rollback use the credential object ID returned by
inventory.

Some Microsoft Entra tenants additionally require the GitHub OIDC token's
`enterprise` claim to be `microsoft`, `github`, or `microsoftopensource`.
Personal repositories emit an empty enterprise claim and cannot satisfy that
tenant policy, even with a correct immutable subject. Bootstrap stops at the
OIDC boundary in that case. The supported remedies are a qualifying
enterprise-owned repository or a development tenant/project whose federation
policy accepts the repository; static Azure credentials are not a fallback.

## Pilot policy

The retained development pilot assigns **project-scoped `Foundry User` only**.
Repository bootstrap and evaluation activation succeeded. GitHub-hosted
publication is retained as a fail-closed tenant-policy result because the
personal pilot repository has no qualifying enterprise claim.

## Accepted residual risk

One shared principal means the Copilot-session token carries the same Azure publication RBAC
as deployment. Separate OIDC subjects constrain token issuance contexts, not Azure role blast
radius, and CLI draft-only enforcement is not principal isolation. Future migration to
separate identities is preserved.

Bootstrap adds or adopts only the reviewed least-privilege assignments. It does not silently
remove broader role assignments that already exist on the identity; owners should review those
legacy assignments separately before considering the identity least-privilege.

## Related architecture

- [Trust model](architecture/trust-model.md)
- [Repository contract](reference/repository-contract.md)
- [ADR 0013: Immutable GitHub OIDC and enterprise-policy gating](decisions/0013-immutable-github-oidc-and-enterprise-policy.md)
- [ADR 0016: Shared identity with environment federation](decisions/0016-shared-identity-with-environment-federation.md)
