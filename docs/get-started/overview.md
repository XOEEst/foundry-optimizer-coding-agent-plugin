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

## What owners actually decide

- Which discovered folders are real agents?
- Which should be ignored, registered disabled, or registered enabled?
- What Foundry project endpoint and agent name should each enabled agent use?
- Which Azure identity should GitHub OIDC use?
- Should verification be configured now, deferred to an issue, replaced with
  repository checks, or skipped?
- Should the reviewed local commit be deployed now?

Registration and enablement do not require a dataset or evaluator bundle.
Verification can be added later without repeating repository bootstrap.

## Quick path for a first repository

1. Install and invoke `/foundry-bootstrap`.
2. Select discovered agents and their enabled state.
3. Confirm the Foundry endpoint and agent name for each enabled agent.
4. Approve repository setup and the combined GitHub-to-Azure connection.
5. Choose optional verification.
6. Approve the exact local commit.
7. Separately approve immediate deployment from that commit.
8. Open the returned GitHub, Azure, and Foundry links.

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

> Repository, GitHub, and Azure setup is complete. Evaluation is deferred, so
> deployment will carry an explicit no-evidence warning instead of pretending
> the agent was evaluated.

**Final resource-link summary**

> Review the GitHub environments and Actions, the Azure identity and role
> assignments, and each Foundry project and agent. Dataset and evaluator links
> appear only when verification was configured.

## Advanced references

- [Documentation index](../README.md)
- [System architecture](../architecture/system-overview.md)
- [Skill and runtime seam](../architecture/skill-runtime-seam.md)
- [Architecture decisions](../decisions/README.md)
- [Repository contract](../reference/repository-contract.md)
- [Identity and RBAC](../identity-rbac.md)
- [Managed files](../managed-files.md)
- [Recommended branch protection](../branch-protection.md)
- [Distribution and pinning](../distribution.md)
