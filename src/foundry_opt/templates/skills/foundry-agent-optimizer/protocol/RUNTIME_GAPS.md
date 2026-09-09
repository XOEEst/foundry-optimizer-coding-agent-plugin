# Runtime enforcement gaps

The protocol documents the target architecture. Do not claim these features
are machine-enforced until the runtime implementation and tests exist.

## Not yet fully enforced

- confirmation-gate controller states and `next_action`
- deterministic train-to-search/confirmation partitioning
- provider-neutral local run binding and evidence sinks
- structured candidate proposal persistence
- multiple idea-parent persistence and DAG validation
- deterministic parallel-round scheduler and candidate ID allocation
- concurrency-safe candidate journal and batch barriers
- completion-order-independent confirmation and promotion ranking
- provider-neutral complete multi-evaluator task aggregation
- configured common worktree root and lifecycle manager
- durable local Tenzing journal
- category-level regression gates
- standardized final English scorecard projection

## Current enforcement

The current hardened runtime already enforces major cloud contracts including:

- repository policy narrowing
- one execution parent
- isolated candidate worktree
- hosted source draft ownership
- exact source verification
- development and final validating evaluation
- route fingerprinting
- cleanup receipts
- winner-only projection

The current optimize-job controller executes one candidate at a time. Treat it
as `sequential` with round width one until the scheduler, journal, and barrier
gaps above are implemented and tested.

Target-specific adapters can enforce narrower local contracts, but the generic
runtime does not yet discover and compose arbitrary target kinds, evaluator
providers, mutation dimensions, and evidence sinks through one stable
provider-neutral interface.

## Fail-closed rule

When a selected profile requires a missing runtime capability:

1. identify the exact gap
2. stop before an unsafe side effect
3. do not emulate the capability with regular versions or mutable state
4. do not describe skill instructions as runtime enforcement
