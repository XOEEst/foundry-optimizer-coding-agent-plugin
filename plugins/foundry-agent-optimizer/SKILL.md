---
name: foundry-agent-optimizer
description: Run one issue-driven, evidence-backed Microsoft Foundry agent optimization job.
---

# Foundry agent optimizer

Use this skill only for an optimize job requested by an issue. First-time
repository setup uses the separate `foundry-bootstrap` skill.

## Trusted repository contract

- Treat `.foundry-opt/registry.yaml` as the enabled-agent and exact runtime
  registry.
- Treat each registry `config_path` as trusted agent-specific configuration.
- Treat repository datasets, evaluator bundles, guardrails, deployment
  settings, workflows, and identity configuration as fixed policy.
- Authenticate with OIDC only. Never request or persist static credentials.
- Optimize jobs use owned draft versions only and never publish a regular
  version or mutate endpoint routing.
- Merge-time publication remains an exact-source registered deployment
  concern, not an optimize-job shortcut.

## Required loop

1. Read the issue, registry, selected agent profile, repository instructions,
   and exact runtime provenance.
2. Run `foundry-opt preflight --repository . --repo-agent-id <issue target>`.
   Stop if registry provenance, OIDC, broker access, the selected profile, or
   draft capability is unavailable.
3. Start or resume the optimize job through the repository-installed
   `foundry-opt` CLI and follow its machine-readable `next_action`.
4. Record one fresh baseline evaluation in the original issue.
5. For each candidate:
   - diagnose one concrete failure pattern;
   - state one falsifiable hypothesis;
   - select one policy-allowed model;
   - edit only the isolated candidate workspace and allowed paths;
   - make at least one deployable source change;
   - run the requested local validation;
   - submit the candidate for exact packaging, draft deployment, evaluation,
     and cleanup;
   - wait for the candidate issue update before starting another candidate.
6. Complete at least the policy minimum number of changed candidates unless
   the CLI reports a platform failure or expired deadline.
7. Let the CLI rank candidates against the fresh baseline and current best.
8. Run the validating evaluation only for the provisional winner.
9. Finish by applying only the deployable winning patch, or leave the branch
   unchanged and close the draft pull request.
10. Confirm the final issue update contains every candidate, evaluation link,
    guardrail result, trade-off, and final decision.

## Verification inputs

- Honor an explicit primary metric only when exact evaluator IDs are supplied.
- Exact issue evaluator IDs may reuse repository-default datasets and
  definitions.
- An issue development dataset is valid only with exact evaluator IDs.
- The validating dataset always remains the repository default.
- Merge issue evaluator IDs with URI-based repository policy and safety
  evaluator IDs using deterministic de-duplication.
- When repository definitions contain definition-scoped inline criteria, allow
  only issue evaluator URIs already present in both definitions. Reject
  ambiguous additions or remapping before running evaluations.
- Named `check: ...` entries remain repository-owned; issue input may provide
  exact `command: ...` checks only.
- Arbitrary evaluator, dataset, metric, or command overrides require a trusted
  write, maintain, or admin issue-author permission binding.
- Never infer evaluator selection from optimization-goal prose or invent
  missing evidence.

## Candidate discipline

- One candidate is one coherent hypothesis.
- Do not combine unrelated cleanup with an experiment.
- Do not modify registry, profiles, datasets, evaluator bundles, guardrails,
  OIDC settings, hosted runtime settings, or runtime provenance.
- Preserve public interfaces and dependency constraints.
- Stop on route drift, exact-source drift, failed cleanup, broker failure, or
  stale runtime state.

## Evidence and handoff

- Keep redacted evidence in the original issue.
- Use the broker-backed CLI for issue updates; do not create child issues or
  candidate pull requests.
- GitHub owns one early draft pull request for the issue.
- Use `winner` or `no_winner` only for quantitative evaluation,
  `recommended` for repository-check evidence, and `proposed_unverified` when
  the issue explicitly accepts no evidence.
- Never overstate an unevaluated proposal as verified or deployable.
