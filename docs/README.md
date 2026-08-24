# Documentation

This directory is the canonical documentation for the Foundry Optimizer
runtime, skills, repository contract, and architecture decisions.

## Start here

Repository owners should read:

1. [Overview](get-started/overview.md)
2. [Bootstrap](get-started/bootstrap.md)
3. [Issues and monitoring](get-started/issues-and-monitoring.md)
4. [Evaluation gates](get-started/evaluation-gates.md)

The bootstrap interface is `/foundry-bootstrap`. Bootstrap has no low-level
CLI or runtime state machine.

## Architecture

- [System overview](architecture/system-overview.md)
- [Module map](architecture/module-map.md)
- [Skill and runtime seam](architecture/skill-runtime-seam.md)
- [Optimize job](architecture/optimize-job.md)
- [Deployment](architecture/deployment.md)
- [Trust model](architecture/trust-model.md)

## Operator guides

- [Run an optimization](guides/run-an-optimization.md)
- [Operate deployments](guides/operate-deployments.md)

## Reference

- [CLI reference](reference/cli.md)
- [Repository contract](reference/repository-contract.md)
- [Evidence, state, and receipts](reference/evidence-state-and-receipts.md)
- [Managed files](managed-files.md)
- [Identity and RBAC](identity-rbac.md)
- [Distribution and pinning](distribution.md)
- [Recommended branch protection](branch-protection.md)

## Architecture decisions

[Architecture decision records](decisions/README.md) preserve the decision
lineage and identify which earlier choices have been superseded by the current
skill-first design.

## Normative and illustrative material

Architecture, guides, references, and accepted ADRs define the reusable
contract unless a page says otherwise.

[Retained bootstrap pilot](retained-pilot.md) is illustrative development
evidence. It does not override the normative documentation or current code.

## Documentation authority

Runtime, documentation, ADR, issue, and feature development happens in this
repository. Historical investigations and detailed rollout evidence may be
retained elsewhere, but they are not a second implementation or documentation
authority.
