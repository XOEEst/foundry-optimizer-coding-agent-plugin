# Experiment tracking

Persist enough state to resume the run without reconstructing it from memory or
mutable provider resources.

For a local Git agent, write public run memory only to:

```text
<agent-root>/experiment_tracking/runs/<run-id>/
  plan.md
  run-state.yaml
  result.yaml
  SCORECARD.md
  results.tsv
  CANDIDATE_DAG.md
  ideas/
  candidates/
  incidents/
```

Keep `experiment_tracking/` Git-ignored. Write it through the primary checkout,
never through a candidate worktree. Keep candidate worktrees outside the active
checkout and remove them only after their evidence is durable.

Persist:

- approved plan and resolved target
- provider binding: the binding reference (the provider artifact outside the
  Skill), the isolation mechanism, and the operation-to-provider map for this
  run
- dataset/evaluator fingerprints and expected counts
- baseline scores
- complete idea cycles
- candidate ID, hypothesis, execution parent, idea contributions, changed
  surfaces, immutable hash, isolation subject (worktree path or draft version
  identity), provider status, scores, decision, and lesson
- provisional winner and validation transition
- contamination/platform incidents
- resource and worktree cleanup status

Each candidate's isolation subject may be a local Git worktree, a hosted draft
version, or a prompt snapshot; record which mechanism was used and its durable,
non-secret handle. Native resource identities and credentials stay in the
restricted store.

Restricted evidence such as credentials, hidden rows, per-task confirmation or
validation output, and provider-native handles must live outside the public
journal.

For non-Git providers, use an equivalent durable store. On resume, reuse the
approved plan and completed receipts; never repeat a completed side effect.
