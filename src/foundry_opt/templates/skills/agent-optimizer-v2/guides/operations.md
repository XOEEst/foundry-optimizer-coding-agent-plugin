# Atomic operations

`guides/loop.md` decides *what* to do; the provider layer executes *how*. This
guide is the boundary between them: it names the atomic operations the loop
needs, and nothing else. It names verbs only. It never names an API, CLI, SDK,
service, or tool. The provider layer owns every implementation choice and
records its bindings per run (see `guides/tracking.md`).

Every operation is atomic and side-effect scoped. A provider **never** decides
ideas, mutations, promotion, winners, or termination — those belong to the
loop.

## Operation contract

| Operation | Purpose | Inputs | Outputs | Must not |
|---|---|---|---|---|
| `inspect_baseline` | Read the target without changing it | agent handle | agent definition, exact baseline identity, mutable and frozen surfaces, interfaces, invocation shape, dataset and evaluator handles | mutate anything; invent missing values |
| `open_candidate` | Create one isolated candidate subject from a named execution parent | execution parent identity | opaque candidate handle | mutate, evaluate, or share state with other candidates |
| `apply_mutation` | Apply one bounded change to the candidate | candidate handle, one mutation confined to allowed surfaces | mutated candidate handle | touch frozen properties or any surface the user did not allow |
| `freeze_candidate` | Fix the exact evaluated content and identity | candidate handle | immutable identity or hash with exact read-back | let evaluated content drift after freezing |
| `invoke_candidate` | Run the frozen candidate over a row set | frozen candidate handle, row set | per-row raw outputs and optional traces | reorder, drop, or synthesize rows |
| `evaluate` | Score outputs against the frozen evaluator | raw outputs, frozen evaluator contract, expected row set | per-row scores and aggregate over the complete expected denominator | select a winner; average only successful rows |
| `cleanup_candidate` | Dispose a candidate subject and temporary resources | candidate handle | released resources | run before the candidate's evidence is durable |
| `apply_winner` | Promote a frozen winner to the real agent | frozen winner identity | applied agent | run without explicit user approval |

`apply_winner` is optional and runs only when the user allowed it.

## Loop term to operation

| Loop / Tenzing term | Operation(s) |
|---|---|
| inspect repository and background | `inspect_baseline` |
| create a branch | `open_candidate` |
| implement an idea | `apply_mutation` |
| commit | `freeze_candidate` |
| evaluate on a split | `invoke_candidate` then `evaluate` |
| tear down a branch | `cleanup_candidate` |
| ship the result | `apply_winner` |

## Isolation is one operation, many mechanisms

`open_candidate`, `freeze_candidate`, and `cleanup_candidate` describe
isolation abstractly. The same contract is satisfied by a local Git worktree, a
hosted draft version, or an in-memory prompt snapshot, as long as each provides
isolation from other candidates, exact read-back, reproducibility, and cleanup.
The chosen mechanism for a run is recorded as the run's `isolation_mechanism`,
not fixed here.

For a **deployed** target, `freeze_candidate` is also where the exact frozen
content is deployed as the invocable subject — for example, a hosted draft
version is created at freeze time — so `invoke_candidate` has a running subject
to call. `open_candidate` only reserves isolation and does not yet produce an
invocable subject.

## Failure and confidentiality

- A provider failure is reported as an operation failure and stays distinct
  from candidate quality.
- Restricted handles — credentials, native resource identities, hidden rows —
  stay inside the operation boundary and never enter candidate reasoning or
  public reports.
