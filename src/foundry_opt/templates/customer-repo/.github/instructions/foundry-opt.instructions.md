---
applyTo: "**"
---

# Foundry optimization repository instructions

- Call each bounded unit of work an **optimize job**.
- Call the final-evaluation data the **validating dataset**.
- Treat `.github/foundry-optimizer.yaml` as the maximum repository policy.
- Treat `.foundry/agent-metadata.yaml` as trusted agent-specific configuration.
- Treat `.github/foundry-opt.lock.yml` as the exact shared skill and CLI revision.
- Issue choices may narrow policy and must never widen it.
- Use OIDC only. Never add static Azure credentials.
- Use Foundry draft versions only. Never publish or change endpoint routing.
- Evaluate the fresh baseline once, then evaluate every implemented candidate against the fresh baseline and current best.
- Use the validating dataset only for the provisional winner.
- Keep redacted evidence in the original issue.
- Candidate edits must stay inside the CLI-provided isolated workspace and allowed paths.
- Apply only the verified winner to the early Copilot pull request, or close it unchanged.
- The Tenzing snapshot installed with the skill is read-only.
