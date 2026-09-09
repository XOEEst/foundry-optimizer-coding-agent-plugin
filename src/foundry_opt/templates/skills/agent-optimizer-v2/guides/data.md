# Data and evaluation rules

## Data roles

- **Training dataset:** optimization input controlled by the Skill.
- **Search:** task-level rows, labels, scores, and diagnostics may be used for
  candidate reflection.
- **Confirmation:** selection evidence only; mutation-capable reasoning sees
  aggregate score, delta, and guardrail outcome.
- **Validation dataset:** a separate sealed source used only after the
  provisional winner is frozen.

When a separate confirmation dataset is provided, use all training rows for
search and all confirmation rows for confirmation. Otherwise deterministically
partition the training dataset and use every row exactly once across search and
confirmation. Never derive validation from training.

Partition hidden roles before mutation-capable reasoning reads their content.
Persist source fingerprints, row counts, role fingerprints, and a zero-overlap
check. Do not inspect combined source data after hidden roles have been
assigned.

## Baseline and scoring

Before candidate generation, freeze the evaluator identity/version, score
normalization, weights, primary metric, direction, hard guardrails, and expected
count for every split. Evaluate the exact baseline on complete search and
confirmation.

For each split, require exactly one complete result for every expected row.
Never average only successful or returned rows. Evaluator scores are
authoritative for selection; reasons and traces are advisory and may be used
only from search evidence.

Run confirmation only after strict search improvement. Promote only when the
candidate strictly improves the current confirmed best on both search and
confirmation and passes every hard guardrail. Ties are non-improvements.

## Validation and contamination

Open validation only after search terminates and the provisional winner is
immutable. Evaluate the exact baseline and frozen winner on the same complete
validation dataset. Validation never selects among multiple candidates.

If confirmation or validation task content, labels, expected outputs, per-task
scores, reasons, or traces reach mutation-capable reasoning, mark the split
contaminated. Replace it with fresh never-exposed evidence or report
`unverified`; never silently call contaminated evidence clean.
