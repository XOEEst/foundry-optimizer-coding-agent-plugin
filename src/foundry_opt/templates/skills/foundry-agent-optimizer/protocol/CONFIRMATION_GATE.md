# Confirmation gate

Use a confirmation split to decide whether a training improvement is reliable
enough to become the next current best.

Confirmation is part of search selection. It is not the final validating
dataset.

## Data roles

Preferred:

```text
training/search
confirmation
final validation
```

If a repository supplies only train and validation:

1. deterministically partition train into disjoint search and confirmation
   subsets in a trusted adapter before any mutation-capable process inspects
   row content
2. freeze row IDs and fingerprints
3. expose only the search projection to candidate generation
4. keep confirmation behind an aggregate-only evaluation interface
5. keep the repository validation split sealed for the final winner

Do not use the same tasks for training and confirmation.
Do not inspect or search the combined train source after partitioning when it
contains confirmation or reserved rows. Follow `DATA_ISOLATION.md`.

If no separate confirmation data can be created, repeated use of the existing
validation split makes it confirmation data. The run then has no final
holdout and must report that limitation explicitly.

## Baseline

Evaluate the baseline once on both training and confirmation.

Record:

- baseline training `avgScore`
- baseline confirmation `avgScore`
- complete sample counts
- evaluator contract and guardrails

Do not expose confirmation task-level evidence to reflection.

## Candidate gate

First run training/search evaluation.

If:

```text
candidateTrainingAvgScore <= currentBestTrainingAvgScore
```

discard the candidate without running confirmation.

If training strictly improves, run the frozen confirmation contract.

Promote only when:

```text
candidateConfirmationAvgScore > currentBestConfirmationAvgScore
```

and all confirmation guardrails pass.

Compare with the current best, not only the original baseline.

## Parallel-round gate

For a parallel round, freeze the incumbent and both comparison scores before
launch. Compare every sibling with that same incumbent; do not update current
best when an early result arrives.

Run confirmation for every valid candidate whose training `avgScore` strictly
beats the frozen incumbent. Eligible confirmations may run concurrently.

After all confirmation results are durable:

1. retain only candidates that strictly beat the incumbent on both splits and
   pass every hard guardrail
2. rank them using the promotion key frozen in the run contract
3. promote at most one round winner

Completion order must never break a tie or determine promotion.

## Failure handling

- Training tie or regression: non-improvement.
- Confirmation tie or regression: non-improvement.
- Confirmation guardrail failure: non-improvement.
- Authentication, transport, missing output, or incomplete scoring:
  platform failure; retry according to policy and do not infer candidate
  quality.

Only a candidate promoted on both training and confirmation resets the
candidate plateau in sequential mode. In parallel mode, only a promoted round
winner resets the round plateau.

## Evidence exposure

The reflection loop may use:

- training task scores
- training failure categories
- training evaluator reasons and redacted diagnostics

The reflection loop must not use:

- confirmation task contents
- confirmation task IDs
- confirmation per-task scores
- confirmation evaluator reasons or traces

Record only:

- confirmation aggregate `avgScore`
- confirmation delta from current best
- guardrail outcome
- evaluation reference

This prevents confirmation from becoming another training set.

These restrictions persist across rounds. A closed-round confirmation result
does not authorize inspection of its task-level evidence for later mutations.
If that evidence is exposed, mark the split contaminated and replace it from
never-exposed reserved rows or report that the run has no clean confirmation
gate.
