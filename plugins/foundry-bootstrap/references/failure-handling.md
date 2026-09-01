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

If patch preflight or apply fails, record the patch SHA-256, base commit, exact
`git apply` command, and concise stderr. Do not retry with a regenerated patch
under the existing approval. A corrected LF patch requires fresh validation
and a new combined approval.

If an approved identity was created but client-ID materialization or final
patch validation fails, leave the identity unchanged. Record its ARM resource
ID, client ID, principal ID, static patch hash, failed final patch hash or
validation, and pending repository/GitHub work.
