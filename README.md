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
repository commits `foundry-agent-optimizer` as an exact project skill under
`.github/skills`, while the Copilot setup workflow verifies it against the
pinned runtime and launches the issue broker. Bootstrap also creates the
optimizer issue form, Copilot environment, OIDC identity, and required workflow
configuration. Repository owners do not install the optimizer skill manually.

Then open the agent repository in Copilot CLI and say:

```text
Use the /foundry-bootstrap skill to bootstrap this repository.
```

The skill first asks the owner to confirm one folder to scan. It lists every
recognized deployable agent below that folder, lets the owner confirm all or
exclude some, and confirms one Foundry project endpoint shared by the selected
group. It then prepares one combined review covering their repository changes,
GitHub OIDC, Azure access, local commit, and deployments. After approval, it
creates the reviewed local commit and deploys only the selected services
without pushing it. Rerun the skill to onboard another group or project.

Large inventories are grouped and written to session-only Markdown and CSV
files with numbered rows, supporting selections such as `all`,
`exclude 4,8-12`, or `only 2-20,31`. Bootstrap also checks each selected
agent's optimizer readiness against the current
[Microsoft guide](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/make-agent-optimizer-ready)
and includes required readiness changes in the reviewed plan.

Evaluation is optional. Owners can start with no evidence, repository checks,
or a later issue that supplies a dataset and evaluators.

## Owner decisions

The single combined approval covers:

- Which folder should be scanned, which recognized agents should be selected,
  and which shared Foundry project endpoint should they use?
- Should each selected agent be registered disabled or enabled?
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
