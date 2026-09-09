# Score aggregation

Use this contract for baseline, candidate, and validating evaluation results.

## Freeze the scoring contract

Before baseline evaluation, freeze:

- exact dataset split and expected sample count
- split role: training/search, confirmation, or final validating
- evaluator IDs and immutable versions
- evaluator score ranges and normalization
- evaluator weights and order
- primary objective
- hard guardrails

Do not change this contract between baseline and candidates.

## Task score

For task `i`, normalize every objective evaluator score to `[0, 1]`.

With equal weights:

```text
taskScore_i = sum(evaluatorScores_i) / evaluatorCount
```

With frozen normalized weights:

```text
taskScore_i = sum(weight_j * evaluatorScore_i_j)
```

Weights must be finite, positive before normalization, and sum to `1` after
normalization.

Hard guardrails remain pass/fail gates unless the frozen objective explicitly
includes them. Do not silently average a safety guardrail into the objective.

Evaluator reasons, diagnoses, and trace summaries are advisory evidence. They
help reflection but do not replace evaluator scores.

## Candidate split avgScore

For a split containing exactly `N` tasks:

```text
candidateAvgScore = sum(taskScore_i for i in 1..N) / N
```

This is the primary split-level score used by Tenzing.

Requirements:

- retrieve all output-item pages
- verify exactly `N` unique task results
- do not drop failed, errored, skipped, or missing tasks from the denominator
- do not average only successful or returned items

If the frozen contract cannot produce a valid task score for every task, mark
the run incomplete or platform-failed according to policy. Never return a
success-shaped average over a partial subset.

## Training, confirmation, and validating roles

Apply the visibility boundary in `DATA_ISOLATION.md`. Only training/search
task content, labels, and task-level evidence may inform mutation design.
Confirmation is aggregate-only for the entire run, including later rounds.
Final validation remains inaccessible until search has terminated and the
winner mutation is frozen.

Training/search `avgScore` is used for:

- baseline comparison
- deciding whether a candidate is eligible for confirmation
- training task-level failure analysis

Confirmation `avgScore` is used for:

- promotion to current best
- confirmation-regression rejection
- strict-improvement and plateau termination

A candidate becomes current best only when:

```text
candidateTrainingAvgScore > currentBestTrainingAvgScore
and
candidateConfirmationAvgScore > currentBestConfirmationAvgScore
```

Ties on either split are non-improvements.

Only the provisional winner runs the validating split by default.

The final optimization headline is:

```text
winnerValidatingAvgScore
```

The final report should show:

1. the detailed mutation idea ledger required by `SCORECARD.md`
2. training scorecard for baseline and every candidate
3. confirmation score for baseline and every triggered candidate
4. provisional winner training and confirmation `avgScore`
5. winner final validating `avgScore` as the final score
6. validating sample count and guardrails
7. provider evidence, incidents, actual lineage, cleanup, and application status

Write the canonical scorecard and all of its projections in English only.

Pass rate and passed/failed counts are useful diagnostics, but they are not the
primary score unless the frozen objective explicitly defines pass rate.

If there is no provisional winner, report `no_winner` and no validating score.

Run a final-validating baseline only when policy or the user explicitly
requests a post-search comparison. Do not expose final validation results
during candidate search.

Unused or reserved training rows are hidden evidence, not extra reflection
data. They may replace a contaminated split only if they were never exposed to
mutation-capable context.
