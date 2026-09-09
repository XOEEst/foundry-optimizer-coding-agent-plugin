# Experiment state, lineage, and worktrees

## Worktree root

Place all candidate worktrees for one run under one configured root outside the
active repository checkout:

```text
<state-root>/<run-id>/worktrees/
  round-001/
    candidate-001/
    candidate-002/
  round-002/
    candidate-003/
  review/
```

Do not scatter worktrees beside the repository or across unrelated folders.

## Candidate state

Persist:

- candidate ID and status
- round ID, preallocated slot, and frozen round-incumbent reference
- proposal and primary mutation target
- execution parent
- multiple idea parents with evidence role and exact contribution
- worktree path
- changed paths and patch hash
- source tree/ZIP or prompt-definition hash
- training evaluation
- confirmation gate result when triggered
- final validation result for the winner
- lessons
- draft and cleanup references
- data-role fingerprints and mutation-loop visibility allowlist
- confirmation aggregate-only projection receipt
- winner-freeze receipt created before final validation
- contamination and replacement receipts when applicable

## Lineage

Use one execution parent to create deterministic Git ancestry.

Use zero or more idea parents to record which assessed experiments influenced
the proposal. Each idea parent records a candidate ID, `positive`, `negative`,
or `contrast` evidence role, and the exact contribution. The execution parent
must be one of the idea parents when it is not the baseline.

Idea parents must be from closed earlier rounds under the same run-contract
hash. Reject unknown, duplicate, same-round, forward, and cyclic parents.

A child is a new patch on one execution parent. Do not create a multi-parent
Git merge or inherit parent scores.

## Round state and barrier

Persist:

- round ID and status
- frozen incumbent and comparison scores
- ordered candidate membership
- requested and effective concurrency
- candidate terminal states
- confirmation-eligible and confirmed candidate IDs
- deterministic ranking inputs
- promoted candidate or no-promotion result
- barrier-open and barrier-closed receipts

Allocate round membership before launch. Serialize journal transitions or use
atomic compare-and-swap so concurrent candidate completions cannot overwrite
state.

Current best, lessons, cleanup, and plateau state may change only after the
round barrier closes.

## Resume

Resume from persisted state rather than rereading mutable worktree assumptions.
Do not repeat a receipted deployment, evaluation, projection, or cleanup.

Provider failures remain distinct from candidate scores.

Data contamination also remains distinct from candidate quality. A score from
a contaminated confirmation or validation split may be retained as diagnostic
history, but it cannot be projected as clean holdout evidence. Follow
`DATA_ISOLATION.md` before resuming or finalizing.

Resume an open round with its original membership, incumbent, IDs, and ranking
rule. Never add a replacement candidate to an already-open round.

## Cleanup

- clean non-winning drafts after evidence is durable
- retain only the current confirmed best draft when final validation still
  requires it
- delete the winner draft after exact review materialization
- remove disposable rejected worktrees according to retention policy
- never delete the active checkout or configured worktree root

## Journal projection

A local Tenzing journal may project this state into JSON, `results.tsv`, a
round timeline, a Mermaid idea-lineage DAG, and a final Markdown scorecard. It
is strategy memory, not a substitute for provider receipts. Follow
`SCORECARD.md` for the mandatory schema and English-only language rule.
