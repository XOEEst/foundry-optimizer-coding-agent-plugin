# Frozen run contract

Resolve and freeze this contract before baseline work.

## Identity

- repository identity
- exact base commit
- selected agent ID
- target kind: prompt or hosted
- provider and provider/runtime version
- route fingerprint

## Optimization scope

- objective and primary metric
- mutation allowlist
- allowed models
- candidate minimum and hard cap
- plateau termination rule
- one configured worktree root outside the active checkout

## Execution topology

- execution mode: `sequential` or `parallel_rounds`
- initial and later round widths
- maximum concurrent candidate transactions
- provider deployment and evaluation concurrency limits
- deterministic candidate ID allocation
- deterministic round-promotion ranking
- whether plateau counts candidates or closed rounds

Sequential execution uses a round width of one. Parallel execution must follow
`PARALLEL_ROUNDS.md`; do not infer concurrency from provider latency or task
completion order.

The target repository supplies the editable surfaces. Tenzing chooses
candidates only inside them.

## Evaluation

- training/search rows and fingerprint
- confirmation rows and fingerprint
- final validation rows and fingerprint
- unused or reserved rows and fingerprint
- zero overlap between roles
- mutation-loop visibility allowlist
- confirmation aggregate-only projection policy
- final-validation winner-freeze requirement
- evaluator IDs, versions, order, normalization, and weights
- complete expected task counts
- hard guardrails and category regression rules

## Output

- evidence sinks
- review destination
- scorecard schema
- cleanup policy
- whether a validating baseline comparison was explicitly requested

## Immutability

Hash the normalized contract.

Resume must reuse the same hash. Issue or local input may narrow repository
policy before freezing, but no candidate or later input may widen or replace
the contract.

If any required field is ambiguous, stop before deployment or evaluation.

Partition and exposure must follow `DATA_ISOLATION.md`. Fingerprints and
disjoint IDs do not make a split clean if mutation-capable context can inspect
its content or task-level evidence.
