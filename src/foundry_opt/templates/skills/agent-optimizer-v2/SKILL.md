---
name: agent-optimizer-v2
description: Accept a YAML or natural-language agent target, present an optimization plan, and run the pinned Tenzing loop with user-bounded mutations.
---

# Agent optimizer

Use this Skill when a user wants to optimize an agent. `SKILL.md` is the
entrypoint. The pinned Tenzing loop in `references/tenzing/climb.md` is
normative; `guides/loop.md` only translates its experiment terms to agent
operations.

## Package layout

- `SKILL.md` — this entrypoint.
- `guides/` — the Skill's own execution rules, read on demand:
  `loop.md`, `operations.md`, `data.md`, `plan.md`, `tracking.md`,
  `scorecard.md`.
- `references/tenzing/` — the pinned, read-only Tenzing source.
- `templates/` — user input (`target.yaml`) and run output examples
  (`run-state.yaml`, `result.yaml`).

## Accepted input

Accept:

- a YAML object or YAML file following `templates/target.yaml`
- natural-language instructions containing some or all of the same fields
- a mixture, where the user's latest explicit value overrides YAML for
  non-permission fields

Resolve relative YAML paths from the YAML file's directory, or from the agent
root for inline YAML. `auto` permits read-only discovery from trusted target
configuration; it does not permit invention. Mutation permissions always use
the narrowest explicit scope and are never widened implicitly.

Resolve:

- agent location or identity
- allowed mutation surfaces
- training dataset and separate validation dataset
- optional separate confirmation dataset
- evaluator, metric, direction, normalization, and guardrails
- split settings, candidate budget, and termination settings
- whether an approved winner may be applied

Ask only for required values that cannot be discovered safely. A clean,
separate validation dataset is required to report a verified `winner`.

## Required execution

1. Read `references/tenzing/climb.md`, `guides/loop.md`, `guides/operations.md`,
   `guides/data.md`, `guides/tracking.md`, and `guides/scorecard.md`.
2. Understand the target read-only: inspect the agent, exact baseline,
   interfaces, mutable and frozen surfaces, invocation shape, datasets, and
   evaluator.
3. Resolve the provider operations needed for this target. The atomic
   operations the loop requires are defined in `guides/operations.md`.
   Providers own API, CLI, SDK, service, and tool implementation choices.
   This Skill must not hardcode them. Providers perform atomic operations only
   and never choose ideas, promotion, winners, or termination. Record the
   chosen isolation mechanism and operation bindings in run state.
4. Build the pre-run plan using `guides/plan.md`.
5. **Before baseline evaluation, candidate creation, deployment, or any other
   optimization side effect, show the complete plan to the user and wait for
   explicit approval.** Apply requested corrections and show the revised plan
   again before proceeding.
6. After approval, initialize durable run state according to
   `guides/tracking.md`.
7. Partition and protect data according to `guides/data.md`.
8. Evaluate the exact baseline on complete search and confirmation splits.
9. Run the Tenzing cycle in `guides/loop.md`: two or three persisted ideas per cycle,
   one isolated candidate per idea, complete evaluation before reflection,
   strict search and confirmation promotion, and complete every opened cycle.
10. Freeze a provisional winner before opening the separate sealed validation
    dataset. Never mutate after validation opens.
11. Persist state after every durable transition and resume without repeating
    completed deployment, evaluation, or cleanup operations.
12. Clean temporary resources, generate the scorecard defined by
    `guides/scorecard.md`, and write `templates/result.yaml`. Applying a winner
    requires explicit user approval.

## Required guarantees

- Every training row is used: all rows are search when a separate confirmation
  dataset exists; otherwise every row belongs to exactly one deterministic
  search or confirmation partition.
- Validation is a separate dataset and is never derived from training.
- Search task-level evidence may guide mutations; confirmation remains
  aggregate-only; validation remains winner-frozen-only.
- Scores use the frozen evaluator contract and complete expected denominator.
  Partial, duplicate, missing, or failed results never become a score.
- Provider failures remain distinct from candidate quality.
- Hidden-evidence contamination invalidates the affected gate.
- `experiment_tracking` is durable loop memory, not candidate source.
- Credentials, hidden rows, and provider-native handles never enter candidate
  reasoning or public reports.
- Terminal outcomes are `winner`, `no_winner`, `unverified`, or `blocked`.
