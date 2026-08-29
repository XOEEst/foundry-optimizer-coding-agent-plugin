# Foundry Optimizer Coding Agent

Foundry Optimizer gives repository owners one guided path to register,
connect, and deploy Foundry agents without reading bootstrap JSON or learning
the low-level command tree.

## First-time owners start here

- [Overview](docs/get-started/overview.md)
- [Bootstrap](docs/get-started/bootstrap.md)
- [Issues and monitoring](docs/get-started/issues-and-monitoring.md)
- [Evaluation gates](docs/get-started/evaluation-gates.md)
- [Complete documentation index](docs/README.md)

These guides explain the owner decisions first. Use the advanced
references once you need the detailed contracts.

## Install bootstrap from this checkout

Repository owners install only the bootstrap skill. From this repository root:

```powershell
copilot skill add ".\plugins\foundry-bootstrap"
```

Do not install a generated package under `dist`. If `foundry-bootstrap` was
previously registered from another directory, use
`/skills info foundry-bootstrap` to find that directory, remove it with
`copilot skill remove "<old-directory>"`, and rerun the add command above.

In an active Copilot CLI session, load and verify the registration:

```text
/skills reload
/skills info foundry-bootstrap
```

After pulling, switching branches, or editing the skill, `/skills reload` is
enough. Re-add the directory only when the repository checkout path changes.
Start a new conversation if the skill was already invoked before reloading.

Bootstrap still prepares the complete optimization experience. The generated
Copilot setup workflow automatically installs `foundry-agent-optimizer` from
the exact runtime revision, and bootstrap creates the optimizer issue form,
Copilot environment, OIDC identity, and required workflow configuration.
Repository owners do not install the optimizer skill manually.

Then open the agent repository in Copilot CLI and say:

```text
Use the /foundry-bootstrap skill to bootstrap this repository.
```

The skill discovers agent folders, records which agents should be registered
and enabled, resolves each enabled agent's Foundry project endpoint and agent
name, and prepares one combined review covering repository changes, GitHub
OIDC, Azure access, the local commit, and deployment. After approval, it creates
the reviewed local commit and deploys that exact commit without pushing it.

Evaluation is optional. Owners can start with no evidence, repository checks,
or a later issue that supplies a dataset and evaluators.

## Owner decisions

The single combined approval covers:

- Which discovered agents should be ignored, registered disabled, or
  registered enabled?
- Which Foundry project endpoint and agent name should each enabled agent use?
- Which exact repository files and cloud resources will be reused or created?
- Which local commit and deployment command will be executed?

The final summary links to the GitHub repository and environments, Azure
identity and role assignments, Foundry projects and agents, and any configured
datasets or evaluators.

## Bootstrap interface

- Use `/foundry-bootstrap` for repository bootstrap.
- Bootstrap is skill-only; there is no bootstrap CLI, state service, receipt,
  or rollback runtime.
- Optimizer and registered deployment commands remain available.

## What this repository covers

- repository discovery and bootstrap
- GitHub environments and Actions for optimization and deployment
- Azure OIDC identity and least-privilege RBAC
- optional Foundry evaluation onboarding, datasets, evaluators, and runs
- issue-driven optimization with explicit receipts and deployment gates

## Status

Pre-release. Bootstrap deploys only from an exact reviewed local commit. A
later main-branch workflow reconciles matching source, package, profile,
registry, and target fingerprints instead of publishing a duplicate version
because merge changed the commit SHA.

See [Distribution and pinning](docs/distribution.md) and
[Recommended branch protection](docs/branch-protection.md).

## Advanced references

- [System architecture](docs/architecture/system-overview.md)
- [Skill and runtime seam](docs/architecture/skill-runtime-seam.md)
- [Architecture decisions](docs/decisions/README.md)
- [Repository contract](docs/reference/repository-contract.md)
- [CLI reference](docs/reference/cli.md)
- [Evidence, state, and receipts](docs/reference/evidence-state-and-receipts.md)
- [Identity and RBAC](docs/identity-rbac.md)
- [Managed files](docs/managed-files.md)
- [Recommended branch protection](docs/branch-protection.md)
- [Distribution and pinning](docs/distribution.md)

## License

[MIT](LICENSE)
