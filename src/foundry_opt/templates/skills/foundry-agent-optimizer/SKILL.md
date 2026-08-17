---
name: foundry-agent-optimizer
description: Bootstrap a Microsoft Foundry agent repository or run one issue-driven, evidence-backed optimize job.
---

# Foundry agent optimizer

Use this skill for a guided local repository bootstrap or when an issue requests
an agent optimize job.

## Operating contract

- Treat `.foundry-opt/registry.yaml` as the enabled-agent registry.
- Treat each registry `config_path` as trusted agent-specific configuration.
- Treat `.foundry-opt/bootstrap.lock.json` as managed ownership state.
- Legacy `.github/foundry-optimizer.yaml`, `.foundry/agent-metadata.yaml`, and
  `.github/foundry-opt.lock.yml` are migration inputs only.
- Authenticate to Foundry with OIDC only.
- Use draft agent versions only. Never publish or change endpoint routing.
- Use the broker-backed CLI for issue updates. Do not fall back to built-in
  GitHub comment tools.
- Evaluate the repository baseline once, then evaluate every implemented
  candidate with the same development dataset and evaluators.
- Use the validating dataset only for the provisional winner.
- Keep redacted evidence in the original issue. Do not create child issues or
  candidate pull requests.
- GitHub creates one early draft pull request when the issue is assigned. Apply
  only the verified winner to that branch, or close it unchanged when there is
  no winner.

## Required loop

1. Read the issue, repository policy, agent metadata, and shared revision pin.
2. Run the repository preflight. Stop before any Foundry operation if the
   bootstrap receipt, OIDC identity, policy, metadata, or draft capability is
   unavailable. If `FOUNDRY_OPT_EXECUTABLE` is set, use that exact path when
   `foundry-opt` is not already on `PATH`.
3. Start or resume the optimize job through the repository-installed
   `foundry-opt` CLI and follow its machine-readable `next_action`.
4. Record the fresh baseline evaluation in the original issue.
5. For each candidate:
   - diagnose one concrete failure pattern
   - state one falsifiable hypothesis
   - select one allowed model
   - edit only the isolated workspace and allowed paths
   - make at least one deployable source change
   - run the requested local validation
   - submit the candidate to the CLI for packaging, draft deployment, and
     evaluation
   - wait for the candidate issue update before starting another candidate
6. Complete at least the policy minimum number of changed candidates unless the
   CLI reports a platform failure or an expired job deadline.
7. Let the CLI rank candidates against the fresh baseline and current best.
8. Run the validating evaluation only for the provisional winner.
9. Finish the optimize job:
   - apply only the verified winning patch to the Copilot branch, or
   - leave the branch unchanged and close the draft pull request
10. Confirm the final issue update contains every candidate, every Foundry
    evaluation link, guardrail results, tradeoffs, and the final decision.

## Bootstrap UX

1. Run `foundry-opt bootstrap discover` with the verified runtime provenance and
   repository root.
2. Explicitly select agents and prepare a reviewed `BootstrapPlanInput`.
3. Run `foundry-opt bootstrap plan --plan-input ...`; offline plans contain only
   the repository phase.
4. Show exact repository diffs and GitHub/Azure/evaluation action summaries.
5. Create one approval record for one phase and run
   `foundry-opt bootstrap apply --phase ... --approval-file ... --plan-input ...`.
6. Stop on stale plan/SHA drift or any failed/compensation-required receipt.
7. Use `status` and receipt-bound `rollback` to resume safely.
8. Auto-adopt a generated rubric only after structural, execution, headroom,
   activation, cleanup, and 100% Content Safety gates pass.
9. Use `bootstrap evaluation inspect|replace` for provenance and explicit
   replacement. There is no rubric editor in v1.
10. Explain that issue objectives guide mutations, issue-supplied weighted
    evaluators select optimize winners, and repository defaults govern
    merge-time deployment.

## Candidate discipline

- One candidate is one coherent hypothesis.
- Do not combine unrelated cleanup with an experiment.
- Do not change datasets, evaluators, decision rules, OIDC settings, hosted
  runtime settings, or the shared revision pin.
- Do not copy raw prompts, responses, dataset rows, tool arguments, credentials,
  or traces into GitHub.
- A failed authentication, deployment, or evaluation is a platform failure, not
  a candidate score.
- Prefer no winner over an unsupported or ambiguous improvement.

## Tenzing reference

The snapshot under `references/tenzing/` is read-only reference material.
Follow `references/ADAPTER_MAPPING.md`; do not initialize or modify the upstream
snapshot. Attribution is in `references/TENZING_ATTRIBUTION.md`.
