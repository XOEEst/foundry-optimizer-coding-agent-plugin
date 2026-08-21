# Overview

Foundry Optimizer gives a repository owner one plain-language path from
"we have an agent repo" to "we can safely optimize and deploy it."

## What it connects

- **GitHub repository** - where owners review changes, issues, pull
  requests, environments, and Actions.
- **Azure identity and RBAC** - the OIDC-backed permission layer that
  lets GitHub act without static secrets.
- **Foundry project** - where your agents, datasets, evaluators, and
  evaluation runs live.
- **Foundry Optimizer** - the workflow that discovers agents, records
  approvals, and keeps deployment decisions explicit.

The default flow uses standard Copilot. The setup workflow installs the
reviewed CLI and skill, and repository instructions tell Copilot how to use
them. Bootstrap does not require or automatically select a custom agent.
An optional specialist profile is available under `examples/custom-agents/`
for teams that deliberately want another agent choice.

If you are new here, read these in order:

1. [Bootstrap](bootstrap.md)
2. [Issues and monitoring](issues-and-monitoring.md)
3. [Evaluation gates](evaluation-gates.md)

## The owner lifecycle in five words

- **Discovered** - the tool found an agent-shaped directory and can tell
  you what it thinks the agent root, source root, and package root are.
- **Registered** - the repository now records that agent in
  `.foundry-opt/registry.yaml`.
- **Enabled** - that registered agent is selected for workflow use.
  Enabled means "eligible to operate," not "ready to deploy."
- **Verified** - the repository has approved evidence for deployment or
  an activated default evaluation bundle that later runs can reuse.
- **Deployable** - policy, binding, and verification gates all allow a
  deployment workflow to publish the reviewed agent.

An agent can be discovered but not registered, registered but disabled,
or enabled but still not deployable.

## Quick path for a first repository

1. Choose the agent or agents you actually want the optimizer to own.
2. Apply repository bootstrap so the registry, sidecar, issue form, and
   workflows exist in GitHub.
3. Approve one combined GitHub-to-Azure connection plan.
4. Optionally activate default verification so future issues can use the
   repository's Foundry dataset and evaluator bundle.
5. Start taking optimization issues and use the status summaries to see
   what is blocked, verified, or deployable.

## What good owner summaries sound like

**Discovery summary**

> Found 2 agent candidates. Selected `chat-agent` because it has the
> reviewed package root and existing profile. `playground-agent` stays
> discovered only and will not be managed yet.

**Plan summary**

> Bootstrap will register `chat-agent`, create the optimizer issue form
> and workflows, adopt the existing Azure identity, and connect GitHub
> environments `copilot` and `foundry-production` to the same client ID.

**Status summary**

> Repository, GitHub, and Azure phases are applied. Evaluation
> activation is still pending, so the agent is enabled for workflow use
> but not yet verified as deployable.

**Final resource-link summary**

> Review the GitHub environments and Actions run history in the
> repository, the Azure identity and role assignments in the reviewed
> subscription, and the Foundry project page for the agent, datasets,
> evaluators, and evaluation runs created during onboarding.

## Advanced references

- [Identity and RBAC](../identity-rbac.md)
- [Binding evidence](../binding-evidence.md)
- [Managed files](../managed-files.md)
- [Evaluation onboarding](../evaluation-onboarding.md)
- [Recommended branch protection](../branch-protection.md)
- [Distribution and pinning](../distribution.md)
