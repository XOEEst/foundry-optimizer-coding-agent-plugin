# Read-only discovery

Complete discovery before preparing a mutation plan.

## Repository and Git

- Resolve the repository root and canonical GitHub remote.
- Record the current branch, exact `HEAD`, default branch, worktree status, and
  existing untracked files.
- Read existing registry, sidecars, `azure.yaml`, dependency files, workflows,
  instructions, issue forms, and agent metadata.
- Locate agent entry points and determine source root, package root, runtime,
  dependency restoration, protocol, CPU/memory settings, model environment
  variable, and paths safe for optimizer edits.
- Detect shared source between agents and keep stable existing agent IDs.

Do not use file writes, Git index changes, branch changes, package installation,
or formatting commands during this phase.

## Tool capability probes

Run non-mutating probes and capture their output:

```text
git --version
gh --version
az version
az account show
azd version
azd ext list
azd ai agent version
azd ai agent --help
```

If `azd` or `azure.ai.agents` is absent or lacks the commands required by the
rendered `azure.yaml`, plan an official-channel install or upgrade for the
combined approval. The release metadata never pins these tools.

Use the standard extension commands after approval:

```text
azd ext install azure.ai.agents
azd ext upgrade azure.ai.agents
```

## GitHub inventory

Use `gh` read operations to inspect:

- repository owner/name and immutable IDs
- default branch and branch protection or rulesets
- existing `copilot` and deployment environments
- environment and repository variables
- existing deployment and Copilot setup workflows

Variable values that GitHub does not return must be treated as unknown, not as
an exact match. Ask for approval to set a missing/unknown variable only when its
desired non-secret value is established from Azure inventory.

## Azure and Foundry inventory

Use the current authenticated tenant and subscription. Record them explicitly.
Inspect:

- Foundry accounts and projects
- project endpoints and immutable ARM resource IDs
- model deployments
- hosted agents and versions
- user-assigned identities or application registrations
- federated identity credentials
- role assignments at their exact scopes

Prefer immutable IDs and service-returned endpoints over names inferred from
text. If the requested account or project is visible in more than one
subscription, stop and require an unambiguous subscription selection before
planning.

## Classification

For each resource needed by the desired repository:

- **exact** - immutable identity, type, scope, and relevant configuration match
- **missing** - no resource occupies the approved identity/name/scope
- **conflict** - a resource occupies the identity/name/scope but differs
- **unknown** - permissions or API limitations prevent a conclusive read

Only exact and missing resources may appear in an approval plan. A conflict or
unknown result blocks mutation.
