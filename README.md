# Foundry Optimizer Coding Agent

`foundry-opt` provides a guided bootstrap and issue-driven optimization
workflow for Microsoft Foundry agent repositories.

This public repository is the authoritative reviewed runtime. It provides:

- local `discover -> plan -> apply` repository bootstrap
- multi-agent registry and per-agent configuration
- OIDC-only GitHub and Azure setup
- evaluation dataset and evaluator onboarding with a receipt-bound sidecar activation
- Copilot optimize-job instructions and workflows
- exact-commit merge-time Foundry deployment

The architecture and implementation plan are tracked in the private design
repository until the public documentation is complete.

## Status

Pre-release. Do not use floating `main` for privileged workflows until the
documented branch-protection policy is enabled. Generated customer templates
currently pin reviewed runtime `770ad878f0658e9368b042d9a7f6732e49ff0200`.

The retained development pilot completed repository bootstrap, evaluation
activation, validation, and Copilot setup. GitHub-hosted Foundry publication is
blocked by the selected Microsoft tenant's enterprise-claim policy; no static
credential fallback is provided.

## Distribution trust

New bootstrap operations resolve one exact runtime commit and retain it for the
entire operation. Resumed optimize jobs reuse the commit recorded at job start.

See:

- [Distribution and pinning](docs/distribution.md)
- [Recommended branch protection](docs/branch-protection.md)
- [Managed files](docs/managed-files.md)
- [Identity and RBAC](docs/identity-rbac.md)
- [Binding evidence](docs/binding-evidence.md)
- [Evaluation onboarding](docs/evaluation-onboarding.md)
- [Retained pilot status](docs/retained-pilot.md)

## License

[MIT](LICENSE)
