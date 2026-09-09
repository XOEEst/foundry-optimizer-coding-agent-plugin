# Tenzing optimize-job loop

This protocol adapts Tenzing's evidence-driven improvement loop to sequential
or deterministic parallel rounds over the current issue-driven `foundry-opt`
runtime.

The CLI remains authoritative for policy, worktrees, evaluation, decisions,
receipts, GitHub evidence, and terminal actions. The coding agent supplies
repository reasoning and candidate edits.

## Loop

### 1. Resolve

Before proposing a candidate, freeze:

- the selected enabled repository agent
- the exact base commit and runtime
- issue goal and constraints
- training/search, confirmation, and final validating data roles
- evaluators, checks, or acknowledged no-evidence mode
- candidate budget
- editable paths
- allowed models
- hard guardrails
- sequential or parallel-round execution settings
- deterministic round-promotion ranking

Stop if the target or verification contract is ambiguous.

### 2. Establish the baseline

Start or resume the job and evaluate one fresh baseline on the frozen training
and confirmation splits.

Do not generate candidate edits before baseline evidence exists.

### 3. Observe

At the start of each round, inspect:

- baseline training and confirmation scores
- current best candidate
- prior candidate proposals and assessments
- focused improvements and regressions
- guardrail failures
- changed paths
- remaining budget
- current machine-readable `next_action`

Ignore raw prompts, responses, traces, dataset rows, and credentials.

Freeze one observation snapshot and incumbent for the whole round.

### 4. Propose

Use `CANDIDATE_PROPOSAL.md` and `PARALLEL_ROUNDS.md`.

One proposal must contain one failure pattern, one falsifiable hypothesis, one
expected effect, one allowed model, and one execution parent.

Do not bundle unrelated cleanup or multiple independent hypotheses.

Write every proposal in a parallel round before reading any sibling result.
Initial candidates should explore deliberately different directions. Later
synthesis candidates may use multiple contribution-tracked idea parents from
closed earlier rounds while retaining exactly one execution parent.

### 5. Hand off and implement

Ask the CLI for each isolated candidate workspace.

Edit only policy-approved paths. Run the requested local validation and make at
least one deployable source change.

When the selected runtime declares parallel-round support, independent
candidate transactions may run concurrently up to the frozen provider limits.
Otherwise use a round width of one; do not emulate concurrency outside the
runtime contract.

### 6. Evaluate

Complete every candidate in the round through the CLI.

Treat the returned assessment as authoritative. Do not substitute a local
impression for the frozen verification contract.

Use `SCORE_AGGREGATION.md`. Verify that every expected training task has a
complete task score before calculating candidate `avgScore`. Do not use pass
rate as a substitute.

If the candidate strictly improves current-best training `avgScore`, run the
confirmation gate in `CONFIRMATION_GATE.md`. Do not promote it from training
evidence alone.

In parallel mode, compare every candidate with the incumbent frozen at round
start. Wait for the full training and confirmation barriers, then select at
most one winner using the frozen deterministic ranking. Never promote by
completion order.

### 7. Learn

Use `LEARNING_RULES.md`.

Write one concise, redacted lesson for every assessed candidate after the
round barrier and before proposing the next round.

Use task-level evidence only from the training/search split. Confirmation is a
selection gate; do not expose its task contents, task IDs, reasons, or traces to
the reflection loop.

### 8. Continue or finish

Follow `next_action` exactly:

```text
handoff-candidate
complete-candidate
finish
blocked
terminal
```

Do not create an extra candidate after budget exhaustion. Do not finish while a
candidate is incomplete. Report an exact blocker rather than manufacturing a
result.

In parallel mode, plateau counts closed rounds with at least one valid
assessment. Only a promoted round winner resets it.

### 9. Validate and materialize

After termination, let the CLI run final validation only for the current best
candidate that passed confirmation.

Apply only the deployable winning patch to the early pull request. If there is
no winner, leave the branch unchanged and close it.

The final human-facing headline score is the winner's final validating
`avgScore`. Training and confirmation scores remain in the candidate scorecard
as search and promotion evidence.

Optimization never publishes a regular version or changes routing.
