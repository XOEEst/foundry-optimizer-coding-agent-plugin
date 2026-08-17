# Foundry Optimizer Coding Agent

`foundry-opt` provides a guided bootstrap and issue-driven optimization
workflow for Microsoft Foundry agent repositories.

This public repository is being initialized from the reviewed generic runtime.
The first supported release will provide:

- local `discover -> plan -> apply` repository bootstrap
- multi-agent registry and per-agent configuration
- OIDC-only GitHub and Azure setup
- evaluation dataset and evaluator onboarding
- Copilot optimize-job instructions and workflows
- exact-commit merge-time Foundry deployment

The architecture and implementation plan are tracked in the private design
repository until the public documentation is complete.

## Status

Pre-release. Do not use floating `main` for privileged workflows until the
documented branch-protection policy is enabled. Pin an exact reviewed commit.

## License

[MIT](LICENSE)
