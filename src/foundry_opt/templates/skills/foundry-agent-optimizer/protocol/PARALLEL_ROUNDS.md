# Parallel rounds and multi-parent idea lineage

Parallelism is optional. Sequential execution is the same protocol with a
round width of one.

Use parallel rounds to explore several independent hypotheses from one frozen
observation snapshot. Do not let task completion order change lineage,
promotion, learning, or termination.

## Frozen controls

Record in the run contract:

- execution mode: `sequential` or `parallel_rounds`
- initial round width and later round width
- maximum concurrent candidate transactions
- provider deployment and evaluation concurrency limits
- candidate minimum and hard cap
- round-based plateau rule
- deterministic round-promotion ranking

Recommended round-promotion ranking after guardrails:

1. highest confirmation `avgScore`
2. highest training `avgScore`
3. lowest preallocated candidate ID

The provider may execute below the requested concurrency when its declared
limit is lower. It must not change round membership, comparison scores, or
ranking because of completion order.

## Round snapshot

Before launching a round:

1. freeze the incumbent candidate and its training and confirmation scores
2. allocate every candidate ID and slot
3. freeze the execution parent and idea parents for every candidate
4. write every proposal before reading any sibling result
5. reserve one isolated worktree and owned draft identity per candidate

Initial-round candidates normally use the immutable baseline as their
execution parent and test deliberately different hypotheses.

Later-round candidates may start from the baseline or any finalized candidate
allowed by policy. They are still compared with the same frozen round
incumbent.

Candidates in one round must not depend on, refine, or cite another candidate
from that round.

## Parallel execution

Candidate worktrees, source verification, draft deployment, and training
evaluation may run concurrently when the provider declares those capabilities.

Every candidate must retain independent:

- worktree and patch hash
- source or prompt-definition hash
- draft ownership reference
- evaluation references
- status and cleanup receipt

Do not share mutable worktrees, draft versions, output files, or candidate
state between concurrent transactions.

## Batch barrier

Close the round only after every allocated candidate reaches an assessed,
invalid, or terminal platform-failure state and all required receipts are
durable.

Do not:

- update current best when the first candidate finishes
- derive a child proposal from an early sibling result
- increment the plateau counter from a partial round
- clean a draft before its required evidence is durable

Unresolved provider failures block the barrier. A round containing no valid
candidate assessment does not count as a quality non-improvement.

## Confirmation and promotion

Compare every valid training result with the incumbent frozen at round start.
Every candidate that strictly beats the incumbent training `avgScore` is
eligible for confirmation.

Eligible confirmations may run concurrently. Expose only their aggregate gate
results to Tenzing.

After the confirmation barrier:

1. reject candidates that do not strictly beat the frozen incumbent on both
   training and confirmation
2. reject candidates that fail a hard guardrail
3. rank the remaining candidates using the frozen promotion key
4. promote at most one round winner
5. preserve all assessed candidates as learning evidence

A completion-order winner is invalid.

## Multi-parent idea lineage

Each candidate has exactly one execution parent and zero or more idea parents.

The execution parent determines the source checkout and Git ancestry. Idea
parents record assessed experiments whose mechanisms or lessons shaped the new
hypothesis.

Each idea-parent entry must record:

- candidate ID
- evidence role: `positive`, `negative`, or `contrast`
- the exact mechanism retained, avoided, or compared

Rules:

- all idea parents must belong to the same run-contract hash
- all idea parents must come from closed earlier rounds
- the execution parent must be included as an idea parent unless it is the
  immutable baseline
- duplicate, unknown, same-round, forward, and cyclic references are invalid
- a discarded candidate may contribute a bounded positive or negative lesson
- an invalid candidate may contribute only implementation-boundary knowledge
- a platform failure contributes no quality conclusion

Construct the child as one new patch on its execution parent. Do not create an
automatic Git merge or treat parent scores as the child's evidence. The child
must run its own complete training and confirmation path.

## Learning and termination

Reflection begins only after the full round and confirmation barriers close.
It may compare training evidence across all assessed candidates and use only
aggregate confirmation outcomes.

In parallel mode:

- candidate minimum and hard cap still count individual candidates
- plateau counts closed rounds with at least one valid assessment
- only a promoted round winner resets the plateau
- ties are non-improvements
- final validation still runs once for the confirmed provisional winner

