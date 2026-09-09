# Learning rules

Derive strategy lessons only from the authoritative candidate assessment.

## Round learning

In parallel mode, wait for the complete round and confirmation barriers before
reflection.

Compare all assessed siblings against the same frozen incumbent. Do not let an
early completion become current best or influence a later sibling.

After the barrier:

- extract one candidate lesson per valid assessment
- record implementation-boundary lessons from invalid candidates
- keep platform failures operational rather than qualitative
- choose at most one promoted round winner
- create later candidates from any compatible earlier-round lessons

Every multi-parent child must identify each idea parent's `positive`,
`negative`, or `contrast` role and its exact contribution. A parent's score is
not inherited by the child.

## Current best

Record:

- which mechanism likely caused the measured improvement
- training and confirmation `avgScore` deltas from the prior best
- which focused cases improved
- whether any cost, latency, complexity, or maintainability tradeoff appeared
- the smallest useful next refinement

Do not declare a final winner before validating evaluation.

Do not use confirmation task-level failures as reflection evidence. Record only
its aggregate gate result and guardrails.

In parallel mode, only the deterministic round winner becomes current best.
Other confirmed candidates remain assessed evidence.

## Discarded

Record:

- whether the expected effect was absent, too small, or offset by regressions
- whether the implementation changed the intended behavior
- which part should not be repeated
- whether a narrower refinement or a different exploration direction is
  justified

A discarded candidate is evidence, not permission to revert trusted runtime
state or widen policy.

Distinguish:

- training rejection — training `avgScore` did not strictly improve
- confirmation rejection — training improved, but confirmation did not
  strictly improve or regressed

## Invalid

Learn only about:

- editable-path boundaries
- protected files
- source packaging requirements
- missing deployable changes
- invalid model or parent selection

Do not infer that the hypothesis itself was false.

## Platform failure

Authentication, deployment, evaluation transport, route drift, timeout, and
cleanup failures are operational failures.

Do not:

- assign a quality score
- mark the hypothesis as disproven
- compare it numerically with valid candidates

Follow the CLI retry, blocked, or terminal action.

## Validating failure

Record that development evidence did not generalize or did not satisfy final
guardrails.

Do not project the patch as a winner.

Final validating evidence must not become an idea parent or seed another search
round.

## Plateau

Sequential mode may count consecutive assessed candidates.

Parallel mode counts closed rounds that contain at least one valid assessment.
Only a promoted round winner resets the counter. A round containing only
invalid candidates or platform failures does not count as a quality
non-improvement.

## Redaction

Lessons may include:

- candidate ID
- hypothesis summary
- metric delta
- task-level evaluator scores and split `avgScore`
- confirmation aggregate score and gate outcome
- pass rate only as a secondary diagnostic
- focused improvement/regression counts
- guardrail names and outcomes
- changed-path count
- evaluation links

Lessons must not include raw prompts, responses, traces, dataset rows, tool
arguments, credentials, or evaluator source.
