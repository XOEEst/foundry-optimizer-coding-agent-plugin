# Tenzing adapter mapping

The Tenzing snapshot supplies the improvement discipline. Repository policy,
the optimize-job issue, and the `foundry-opt` CLI supply the bounded Foundry
implementation.

| Tenzing pattern | Foundry optimize-job adaptation |
| --- | --- |
| Establish an objective | Read the issue goal, decision metric, constraints, and guardrails |
| Establish a baseline | Package the immutable base commit, create a draft, and evaluate it once |
| Form a hypothesis | Name one failure pattern and one expected measurable improvement |
| Isolate an experiment | Use a detached candidate worktree with an explicit parent |
| Evaluate the experiment | Deploy a draft and run the fixed development evaluation contract |
| Track experiment memory | Post one redacted, idempotent candidate update to the original issue |
| Select the next climb | Compare with the fresh baseline and current best |
| Confirm the summit | Use the validating dataset only for the provisional winner |
| Preserve the result | Apply only the winning patch to the early Copilot pull request |
| Stop without improvement | Post the evidence and close the early pull request unchanged |

The adapter never publishes a regular version, changes endpoint routing, creates
child issues, or creates one pull request per candidate.
