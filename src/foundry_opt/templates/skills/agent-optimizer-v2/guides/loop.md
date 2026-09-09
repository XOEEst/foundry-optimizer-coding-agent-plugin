# Tenzing agent execution guide

`references/tenzing/climb.md` remains the optimization methodology. This guide
only translates its software-experiment vocabulary into agent operations.

| Tenzing term | Agent execution |
|---|---|
| repository and background | Agent definition, instruction, tools, interfaces, exact baseline, training dataset, separate validation dataset, and evaluator |
| editable area | Only the mutation surfaces explicitly allowed by the user |
| branch | An isolated candidate copy, draft, deployment, or snapshot |
| implement an idea | Apply one bounded mutation to one candidate |
| evaluate | Invoke the candidate on the frozen split and score every expected row |
| commit | Freeze the exact candidate content and identity that was evaluated |
| result table | Persist candidate, parent, idea lineage, scores, outcome, and lesson |
| continue | Generate or finish ideas according to Tenzing and the frozen budget |

The translation is operational, not a second methodology. A local code agent
may use a Git worktree; a prompt agent may use an in-memory prompt snapshot; a
hosted agent may use a temporary draft deployment. They are equivalent when
they provide isolation, exact read-back, reproducibility, and cleanup. The
atomic operations behind these mechanisms are defined in
`guides/operations.md`; each run records which mechanism and provider bindings
it used.

## Local Git experiment tracking

When candidate isolation is implemented with local Git worktrees:

- create each candidate worktree from its frozen execution parent
- keep worktrees outside the active agent checkout
- write durable run memory only to
  `<agent-root>/experiment_tracking/runs/<run-id>/` in the primary checkout
- keep `experiment_tracking/` Git-ignored and never copy it into candidate
  worktrees
- persist idea cycles, candidate metadata, changed paths, hashes, scores,
  reflections, lineage, incidents, validation state, and cleanup state
- remove disposable candidate worktrees only after their evidence is durable
- resume from the journal instead of reconstructing state from mutable
  worktrees

For non-Git providers, use an equivalent durable run store with the same
logical information. The storage mechanism may change; the experiment history
must not disappear.

## Cycle execution

1. Refresh the objective, allowed mutations, baseline, prior results, and
   permitted search evidence.
2. Generate two or three distinct ideas and persist them as one cycle.
3. For each idea, sequentially:
   - choose the execution parent
   - create an isolated candidate
   - implement only that idea
   - freeze the evaluated candidate
   - run complete search evaluation
   - record the result and reflection
   - run aggregate-only confirmation when the search gate passes
4. Finish every idea in the cycle.
5. Reflect across the completed cycle before opening another cycle.
6. Stop on the frozen candidate budget or another explicit termination rule.
7. If a provisional winner exists, freeze it before opening validation.

The training dataset supplies search and confirmation. Search provides
task-level learning evidence; confirmation provides only an aggregate promotion
gate. The validation dataset is a separate sealed source used only after the
winner is frozen.

Candidate creation, deployment, invocation, evaluation, and cleanup can each use different
available tools. Tool selection must not change the loop above.
