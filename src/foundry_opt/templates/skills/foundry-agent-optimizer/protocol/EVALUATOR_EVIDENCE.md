# Evaluator evidence

Separate ranking evidence from reflection evidence.

## Authoritative evidence

- normalized evaluator scores
- task score
- split `avgScore`
- complete task count
- hard guardrail outcomes
- immutable evaluator and run references

Only authoritative evidence selects or rejects a candidate.

## Advisory evidence

- evaluator reason
- failed rule or constraint IDs
- expected-versus-actual summary
- compact tool trace
- one-line diagnosis
- failure category

Advisory evidence helps Tenzing propose the next experiment. It must not
override the canonical score.

## Score-only evaluators

An evaluator does not need a reason field.

When only scores are available, reflection may use training/search:

- task-level score deltas
- improved, regressed, and unchanged task IDs
- dataset-provided category or constraint metadata
- aggregate category scorecards

Do not inspect confirmation or final-validation task evidence during search.

## Hybrid evaluators

A hybrid evaluator should preserve deterministic scoring and add compact
diagnostic evidence. For tool agents, prefer:

- selected tool and redacted arguments
- tool success/error
- expected operation
- one-line diagnosis

Do not copy raw prompts, responses, datasets, credentials, or full traces into
Git or issue evidence.

## Category regression

When categories exist, report per-category `avgScore` and task counts.

Repository policy may require no regression or a bounded regression in
critical categories even when aggregate score improves.
