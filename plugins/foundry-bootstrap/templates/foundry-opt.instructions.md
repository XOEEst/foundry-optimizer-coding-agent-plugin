---
applyTo: "**"
---

# Foundry optimization repository instructions

Apply repository-wide guidance unless the assigned issue narrows editable paths
within policy.

## Trust model

- Treat the issue objective, issue constraints, requested evaluator IDs, and
  allowed path narrowing as the only mutable job inputs.
- Treat repository datasets, hard guardrails, deployment bundle defaults,
  `.foundry-opt/**`, `azure.yaml`, and every sidecar `config_path` as fixed
  trusted configuration.
- Fail preflight when the issue leaves the target agent ambiguous, requests a
  non-positive evaluator weight, repeats evaluator IDs, or widens paths or
  models beyond policy.

## Optimizer behavior

- Freeze the issue objective, constraints, and evaluator set before generating
  candidates.
- Run one fresh baseline first, derive a written hypothesis from issue and
  baseline evidence, and validate only the provisional winner on the validating
  dataset.
- Mutate only the isolated optimizer worktree for the targeted agent. Never
  edit `.foundry-opt/**`, `azure.yaml`, sidecars, repository datasets,
  guardrails, or deployment bundle selection.
- Deployment uses the repository default bundle and may reject a validated
  winner when binding or release checks fail.
