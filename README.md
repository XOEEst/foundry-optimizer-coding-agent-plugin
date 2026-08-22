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

Install the `foundry-bootstrap` skill, open the agent repository in your coding
agent, and say:

```text
Use /foundry-bootstrap to bootstrap this repository.
```

The skill shows one plain-language review or question at a time. It discovers
agent folders, records which agents should be registered and enabled, resolves
each enabled agent's Foundry project endpoint and agent name, prepares GitHub
OIDC and Azure access, creates a reviewed local commit, and asks separately
before deploying that exact commit.

Evaluation is optional. Owners can start with no evidence, repository checks,
or a later issue that supplies a dataset and evaluators.

## Owner decisions

Owners review only these decisions:

- Which discovered agents should be ignored, registered disabled, or
  registered enabled?
- Which Foundry project endpoint and agent name should each enabled agent use?
- Should the reviewed repository changes be applied?
- Should GitHub environments be connected to the reviewed Azure identity?
- Should verification be configured now, deferred, or skipped?
- Should the exact local bootstrap commit be created?
- Should that exact commit be deployed now with the current Azure login?

The final summary links to the GitHub repository and environments, Azure
identity and role assignments, Foundry projects and agents, and any configured
datasets or evaluators.

## Compatibility policy

- Use `/foundry-bootstrap` for normal owner bootstraps.
- Keep `foundry-opt bootstrap ...` names, arguments, exit codes, JSON
  payloads, receipts, and workflows stable while compatibility is
  retained.
- Record future retirement or breaking changes in docs and release
  notes instead of emitting runtime warning noise in CI.

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
- [Binding evidence](docs/binding-evidence.md)
- [Managed files](docs/managed-files.md)
- [Evaluation onboarding](docs/evaluation-onboarding.md)
- [Recommended branch protection](docs/branch-protection.md)
- [Distribution and pinning](docs/distribution.md)
- [Owner review interface](docs/owner-review.md)

## License

[MIT](LICENSE)
