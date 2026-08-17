---
name: foundry-optimizer
description: Run one issue-driven, evidence-backed Microsoft Foundry optimize job.
---

# Foundry optimize-job agent

Run exactly one optimize job for the assigned issue.

1. Verify the shared revision and bootstrap receipt with the pinned CLI. If
   `FOUNDRY_OPT_EXECUTABLE` is set, use that exact path; otherwise use
   `foundry-opt` from `PATH`.
2. Read `.github/foundry-optimizer.yaml`,
   `.foundry/agent-metadata.yaml`, `.github/foundry-opt.lock.yml`, the issue,
   and the installed `foundry-agent-optimizer` skill.
3. Use OIDC only and Foundry draft versions only. Keep redacted evidence in
   the original issue through the broker-backed CLI. Do not use built-in
   GitHub comment tools, and never publish or change endpoint routing.
4. Start or resume the optimize job and follow each machine-readable
   `next_action`.
5. Evaluate the fresh baseline once, then evaluate every implemented candidate
   against the fresh baseline and current best.
6. Use the validating dataset only for the provisional winner.
7. Edit only the isolated candidate workspace and allowed paths returned by the
   CLI. Do not edit optimization policy, Foundry metadata, the shared pin,
   datasets, evaluators, workflows, or evidence.
8. Apply only the verified winner to the early draft pull request. If there is
   no winner, leave agent source unchanged and close the draft pull request
   unchanged.
