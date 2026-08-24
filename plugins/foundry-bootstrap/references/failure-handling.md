# Failure handling

Stop on the first failed mutation or verification. Do not attempt later
dependent work and do not delete or reverse successful work.

Update `.foundry-opt/bootstrap-report.md` and the owner response with:

- status: `partial` or `failed`
- actual `azd` and `azure.ai.agents` versions used
- base commit and current local commit
- files applied and whether they are committed
- remote resources reused
- remote resources created
- the failed command or API action and concise error
- pending approved actions that were not attempted
- current worktree status
- resource links and immutable IDs
- a safe next manual step, without claiming it was performed

Never retry a mutation against ambiguous live state. A later attempt begins
with a fresh read-only inventory and a new exact combined plan.
