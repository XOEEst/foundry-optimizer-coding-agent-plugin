# ADR 0016: Shared identity with environment federation

## Status

Accepted

## Context

Public bootstrap now defaults to one repository-wide Azure identity instead of the older separate-principal model. That simplifies repository onboarding, but it makes the remaining trust boundary explicit: GitHub environment federation constrains where tokens can be minted, not which Azure roles the shared principal carries once a token exists.

## Decision

- Bootstrap defaults to one repository-wide user-assigned managed identity, or an adopted equivalent identity, for reviewed bootstrap and deployment operations.
- The runtime creates or adopts separate exact GitHub OIDC federated credentials per reviewed environment. At minimum, the reviewed plan must carry two explicit subjects, one for the owner or Copilot environment and one for the deployment environment.
- Separate environment subjects constrain token issuance context, but they do not isolate Azure RBAC blast radius once the shared principal is in use. That residual risk is accepted and documented.
- Bootstrap never falls back to static Azure credentials, client secrets, certificates, or broad-role escalation. Only reviewed least-privilege role assignments and exact federated-credential claims are allowed.
- Planning, apply, verification, and rollback fail closed on ambiguous identity inventory, subject drift, role drift, or missing approved subjects.

## Consequences

Benefits:

- Repository owners review one identity by default instead of coordinating multiple principals.
- Environment-specific federation still keeps GitHub token issuance explicit and reviewable.
- Exact identity, subject, and role checks remain testable through the runtime and plan contracts.

Tradeoffs:

- One shared principal means Azure publication RBAC is shared across the approved environments.
- Environment federation is not equivalent to separate-principal isolation.
- Future migration back to split identities remains possible, but it is no longer the default bootstrap contract.

## Alternatives considered

- **Keep separate optimizer and deployment identities as the default** - rejected because the public bootstrap contract now optimizes for one repository-wide identity with explicit environment federation.
- **Use static credentials or client secrets as a fallback** - rejected because it violates the approved OIDC-only trust model.
- **Permit Owner, Contributor, or other broad-role fallback** - rejected because least-privilege review must fail closed rather than escalate.

## Evidence

- Default shared-identity policy, approved roles, and accepted residual risk in [Identity and RBAC](../identity-rbac.md).
- Input validation for exact identity references in [`src/foundry_opt/bootstrap/input_contracts.py`](../../src/foundry_opt/bootstrap/input_contracts.py).
- Exact two-subject planning and drift checks in [`src/foundry_opt/bootstrap/providers/azure.py`](../../src/foundry_opt/bootstrap/providers/azure.py) and [`tests/bootstrap/test_azure_provider.py`](../../tests/bootstrap/test_azure_provider.py).
- Owner-facing identity review in [`src/foundry_opt/bootstrap/connection_setup.py`](../../src/foundry_opt/bootstrap/connection_setup.py) and [`tests/bootstrap/test_connection_setup.py`](../../tests/bootstrap/test_connection_setup.py).
- Role-matrix and end-to-end bootstrap coverage in [`tests/bootstrap/test_azure_role_matrix.py`](../../tests/bootstrap/test_azure_role_matrix.py), [`tests/bootstrap/test_azure_identity_plan.py`](../../tests/bootstrap/test_azure_identity_plan.py), and [`tests/bootstrap/test_skill_one_click_e2e.py`](../../tests/bootstrap/test_skill_one_click_e2e.py).

## Supersedes / Superseded by

- Supersedes: [0004](0004-oidc-only-role-specific-identities.md) as the default bootstrap identity model while preserving OIDC-only authentication and refusal of static credentials.
- Superseded by: None.
