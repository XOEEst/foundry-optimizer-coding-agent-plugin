# Tenzing adapter mapping

The upstream Tenzing snapshot supplies the improvement discipline. The current
Foundry optimize-job runtime supplies the trusted request, worktree, evaluation,
decision, evidence, and review transaction.

The supported executable protocol is under `protocol/`. The snapshot remains
read-only provenance and must not be initialized inside a customer repository.

## Concept mapping

| Tenzing concept | Current Foundry optimize-job implementation |
| --- | --- |
| Establish an objective | Freeze `protocol/RUN_CONTRACT.md`: goal, target, mutation scope, scoring, data roles, termination, and provider |
| Establish a baseline | Evaluate the aligned baseline on training/search and confirmation |
| Observe | Read training evidence, current best, prior assessments, guardrails, lineage, and remaining budget |
| Form an idea | Diagnose one training failure pattern and write one falsifiable proposal with one primary mutation target |
| Independent exploration | Preallocate a diverse initial round whose candidates share the immutable baseline |
| Refinement | Create a candidate from one finalized execution parent |
| Synthesis | Start from one execution parent and reimplement contribution-tracked lessons from multiple earlier-round idea parents |
| Parallel climb | Execute independent candidate transactions concurrently, then wait for a deterministic batch barrier |
| Isolate an experiment | Use the CLI-provided detached candidate worktree |
| Evaluate the experiment | Calculate complete training task scores and split `avgScore` |
| Confirm a training improvement | Run a disjoint confirmation gate only after strict training improvement |
| Track experiment memory | Persist proposals, lineage, worktrees, hashes, scores, lessons, and cleanup state; project a redacted, idempotent candidate update to the original issue in cloud mode |
| Learn | Use authoritative training scores plus optional advisory evaluator evidence |
| Select the next climb | Compare a whole round with its frozen incumbent and promote at most one deterministically ranked candidate |
| Confirm the summit | Use final validation only for the confirmed provisional winner |
| Preserve the result | Apply only the deployable winning patch to the early Copilot pull request |
| Stop without improvement | Post final evidence and close the early pull request unchanged |

## Deliberate differences from upstream Tenzing

- The optimize job is issue-bounded rather than an open-ended autonomous run.
- Candidate budget and `next_action` control termination.
- Candidate worktrees replace one durable branch per experiment.
- The issue and trusted runtime state replace repository `results.tsv` as the
  authoritative cloud run record.
- Only the selected result reaches the one review pull request.
- Deployment remains a separate post-merge lifecycle.
- The validating contract is reserved for the provisional winner.
- Confirmation is a disjoint search gate and does not expose task-level evidence
  to reflection.
- The runtime currently persists one execution parent, not a multi-parent Git
  merge.
- Parallel rounds freeze membership and the incumbent before launch; candidate
  completion order never changes promotion or learning.
- Plateau counts candidates in sequential mode and closed rounds in parallel
  mode.

## Adapter boundaries

- `TARGET_PROVIDER_CONTRACT.md` separates target inputs, Tenzing strategy, and
  provider execution.
- `SCORE_AGGREGATION.md` defines task score and complete split `avgScore`.
- `EVALUATOR_EVIDENCE.md` separates canonical scores from advisory diagnostics.
- `EXPERIMENT_STATE.md` defines journal, lineage, worktree, resume, and cleanup
  behavior.
- `PARALLEL_ROUNDS.md` defines round snapshots, barriers, deterministic
  promotion, and multi-parent idea lineage.
- `RUNTIME_GAPS.md` identifies protocol features that are not yet enforced.

## Idea lineage versus Git ancestry

The current runtime has one `parent_id`, which is the execution parent used to
create the worktree.

A synthesis candidate may still use several idea parents:

```text
execution parent: candidate-a
idea parents:
  candidate-a | positive | retain its verified mechanism
  candidate-b | negative | avoid its observed regression
```

The coding agent starts from candidate A and reimplements the useful mechanism
or constraint learned from candidate B. Every idea parent must come from a
closed earlier round under the same run contract. Additional idea parents
belong in the proposal rationale and redacted evidence until the runtime gains
a structured lineage field.

Never:

- pass unsupported multi-parent CLI flags
- automatically merge candidate commits
- claim additional idea parents were persisted by the runtime
- publish a regular version
- change endpoint routing
- create child issues
- create one pull request per candidate
