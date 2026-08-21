# Foundry Optimizer Coding Agent

Foundry Optimizer helps repository owners bootstrap a GitHub repository,
connect it to Azure and Foundry, and run issue-driven optimization for
Foundry agents.

## First-time owners start here

- [Overview](docs/get-started/overview.md)
- [Bootstrap](docs/get-started/bootstrap.md)
- [Issues and monitoring](docs/get-started/issues-and-monitoring.md)
- [Evaluation gates](docs/get-started/evaluation-gates.md)

These guides explain the owner decisions first. Use the advanced
references once you need the detailed contracts.

## What this repository covers

- repository discovery and bootstrap
- GitHub environments and Actions for optimization and deployment
- Azure OIDC identity and least-privilege RBAC
- Foundry evaluation onboarding, datasets, evaluators, and runs
- issue-driven optimization with explicit receipts and deployment gates

## Status

Pre-release. Until the recommended branch protection policy is enabled,
treat privileged bootstrap, optimization, validation, and deployment
workflows as reviewed-commit operations rather than floating `main`.

See [Distribution and pinning](docs/distribution.md) and
[Recommended branch protection](docs/branch-protection.md).

## Advanced references

- [Identity and RBAC](docs/identity-rbac.md)
- [Binding evidence](docs/binding-evidence.md)
- [Managed files](docs/managed-files.md)
- [Evaluation onboarding](docs/evaluation-onboarding.md)
- [Recommended branch protection](docs/branch-protection.md)
- [Distribution and pinning](docs/distribution.md)
- [Owner review interface](docs/owner-review.md)

## License

[MIT](LICENSE)
