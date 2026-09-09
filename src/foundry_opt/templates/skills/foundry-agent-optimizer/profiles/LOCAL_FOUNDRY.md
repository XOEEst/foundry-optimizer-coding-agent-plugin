# Local Tenzing loop with a Foundry target

Use this profile when the coding agent and Tenzing loop run locally while the
target is an existing Microsoft Foundry prompt or hosted agent.

This profile has a strict lifecycle:

```text
aligned active standard baseline
-> frozen local Tenzing candidate round
-> isolated worktrees and owned Foundry drafts
-> bounded parallel exact-source verification and training evaluation
-> batch barrier and conditional confirmation gate
-> provisional winner
-> final validating evaluation
-> exact winner materialization
-> draft cleanup
```

Optimization never creates a regular numeric version and never changes
routing.

## Responsibility split

The local coding agent owns:

- observation
- failure diagnosis
- candidate proposal
- worktree edits
- lessons
- round membership, hypotheses, and idea lineage
- selection of the next bounded experiment round

The local `foundry-opt` adapter owns:

- preflight and frozen run identity
- candidate worktree policy
- deterministic source packaging
- owned draft creation
- exact downloaded-source verification
- training, confirmation, and final validating evaluations
- bounded concurrent candidate execution and batch barriers when declared
- route-drift detection
- cleanup receipts
- exact winner patch verification and materialization

A repository-specific target adapter may supply agent source, deployment
metadata, datasets, and evaluator references. It must not create regular
candidate versions or generate the hill-climbing candidates.

## Target kinds

### Prompt agent

The aligned standard baseline is an active numeric prompt-agent version.

Freeze and fingerprint:

- model deployment
- instructions
- temperature and top-p when configured
- tools and tool descriptions
- target metadata relevant to evaluation

Each candidate worktree changes only policy-approved prompt-agent source such
as `instructions.md`, tool descriptions, or reviewed agent configuration.

The adapter creates an owned prompt-agent draft from the exact candidate
definition. It must require a `draft-*` version, read the definition back, and
verify the model/instructions/tools fingerprint before evaluation.

Prompt candidates do not require Docker, ACR, container startup, or source ZIP
packaging.

### Hosted agent

The aligned standard baseline is an active numeric hosted-agent version.

Freeze and fingerprint the exact source/package content and hosted runtime
definition. Candidate drafts use deterministic source packaging, returned-code
verification, and the fixed hosted runtime contract.

## 1. Preflight

Run preflight before baseline evaluation or candidate creation.

Freeze:

- repository and exact base commit
- selected agent sidecar or reviewed local target contract
- active regular baseline agent name and version
- local-source and deployed-source fingerprints
- route fingerprint
- source/package root
- editable paths
- hosted runtime definition
- baseline and allowed models
- fixed training/search, confirmation, and final validating contracts
- evaluator normalization, weights, and complete split sample counts
- objective metric, regression rules, and hard guardrails
- candidate budget
- execution mode, round widths, concurrency limits, and promotion ranking
- local review destination
- target kind: `prompt` or `hosted`

Require:

- the baseline version exists, is active, and is a regular numeric version
- baseline deployed content is aligned with the exact local base commit
- authentication can read the baseline and create/delete drafts
- training, confirmation, and final validating definitions are immutable and
  usable
- training/search and confirmation tasks are disjoint
- final validating tasks remain sealed during search
- the trusted adapter exposes only training/search task content and labels to
  candidate generation; combined source datasets containing confirmation,
  validation, or reserved rows are not mutation-loop inputs
- the route is captured without mutation
- the active checkout is not the candidate or review destination

Fail closed on unknown/diverged source, ambiguous agent selection, missing
evaluation assets, insufficient permissions, or route drift.

## 2. Evaluate the standard baseline

Evaluate the existing aligned regular baseline version as a read-only control.

Do not redeploy the baseline merely to start the loop. Record:

- agent name and numeric version
- source and package fingerprints
- training and confirmation evaluation IDs and run IDs
- training and confirmation `avgScore`, complete sample counts, and guardrails
- route fingerprint

The baseline evaluation adapter may target a regular version. Candidate
evaluation must remain draft-only.

## 3. Observe and plan a round

Read bounded baseline and prior-candidate evidence.

Read task-level rows and diagnostics only from the optimizer-visible
training/search projection. Do not inspect the original combined dataset,
confirmation output items, validation rows, or reserved rows. Follow
`protocol/DATA_ISOLATION.md`.

Use `protocol/CANDIDATE_PROPOSAL.md` and `protocol/PARALLEL_ROUNDS.md` to
preallocate the round and create every local proposal from one frozen
observation snapshot. The Foundry provider must not generate or rewrite the
proposals.

Initial-round candidates normally share the baseline execution parent and test
different hypotheses. Later candidates may combine contribution-tracked
lessons from multiple closed earlier-round idea parents while retaining one
execution parent.

## 4. Isolate and implement the round

Create one worktree per candidate from its selected execution parent. Never
share a mutable worktree between parallel candidates.

For every candidate, edit only allowed paths and finalize:

- candidate ID
- round ID and preallocated slot
- execution parent
- contribution-tracked idea parents
- proposal
- changed paths
- candidate commit or patch hash
- source tree hash
- deterministic source ZIP hash for hosted agents, or prompt-definition hash
  for prompt agents

## 5. Create an owned draft for every candidate

Use `foundry-opt` draft operations or an adapter with the same contract:

- capture the current route fingerprint
- upload the exact hosted candidate source or prompt candidate definition
- require the returned version to start with `draft-`
- persist the ownership token and draft reference before evaluation
- wait for active status
- download/read back the draft source or definition
- verify exact ZIP/content identity or prompt-definition identity
- verify the route remains unchanged

Independent candidate transactions may run concurrently only when the adapter
declares deterministic scheduling, durable per-candidate ownership, and a
batch barrier. Otherwise use a round width of one.

If Foundry returns a numeric regular version, stop immediately. It is not a
candidate.

Do not use a target's normal create/update deployment API for candidate
deployment when that API creates regular numeric versions.

## 6. Run training evaluation and close the barrier

Run the fixed training/search contract against every verified draft.

Normalize:

- every evaluator score for every task
- task score using the frozen normalized weights or equal-weight mean
- candidate training `avgScore` across the complete training split
- focused improvements and regressions
- guardrails
- report URL
- total/pass/fail/error counts

A provider or transport failure is not a candidate score.

The number of scored tasks must equal the frozen training sample count.
Missing output items, pagination loss, or errored tasks make the evaluation
incomplete; never average only the returned subset.

Wait until every allocated candidate has a durable assessed, invalid, or
terminal platform-failure state. Do not update current best or learn from a
partial round.

## 7. Run confirmation conditionally

Read `protocol/CONFIRMATION_GATE.md`.

Compare every candidate with the incumbent frozen at round start. Run
confirmation only when training `avgScore` strictly exceeds that incumbent's
training `avgScore`.

Eligible confirmations may run concurrently. After the confirmation barrier,
promote at most one candidate using the deterministic ranking frozen in the
run contract. Every promoted candidate must also strictly exceed the frozen
incumbent confirmation `avgScore` and pass all guardrails.

Do not expose confirmation task-level evidence to the coding agent. Only the
aggregate gate result may enter strategy memory.

## 8. Learn and continue

Use `protocol/LEARNING_RULES.md`.

The local coding agent derives lessons for the complete round and only then
proposes the next round.

Clean non-winning drafts after their evidence and cleanup reference are safely
persisted. The current best confirmed draft may be retained until final
validation.

## 9. Validate the provisional winner

Before validating:

- rerun preflight checks that can drift
- verify route fingerprint is unchanged
- verify candidate draft ownership and exact source identity
- verify the provisional winner patch hash
- verify the final validating contract matches the frozen run identity

Run the final validating dataset only for the provisional winner.

Persist the frozen winner mutation and hashes before opening validation. Once
validation evidence is opened, do not generate another candidate in this run.

Require final guardrails and regression policy.

Compute the winner's final validating `avgScore` over the complete validating
split. This validating `avgScore` is the final reported optimization score.

## 10. Final winner gate

Before materialization, require:

- completed training evaluation
- completed confirmation gate
- completed validating evaluation
- complete validating task count and final validating `avgScore`
- final decision is `winner`
- draft source or definition still matches the finalized candidate
- route fingerprint remains unchanged
- candidate patch matches the evaluated candidate
- review destination still points to the original base commit

Apply only that exact patch to a dedicated local review worktree.

Do not publish the draft or create a regular version.

## 11. Cleanup

Delete every operation-owned draft, including the final winner draft after
winner materialization.

Persist cleanup receipts. Fail visibly when cleanup is incomplete.

Retain only redacted evaluation references, hashes, decisions, and the local
review worktree.

## Target-provider binding

Treat every concrete agent, source layout, mutable field set, dataset,
evaluator, and invocation mechanism as target-provider configuration rather
than hill-climbing strategy.

Require the trusted target adapter to expose:

- one aligned active numeric baseline
- target kind and immutable runtime/model/tool definition
- explicit editable paths and mutation dimensions
- fixed training, confirmation, and sealed validation inputs
- evaluator identities, normalization, weights, and complete sample counts
- draft create, exact readback, invoke/evaluate, route-check, and cleanup
  capabilities

The generic loop must work for instruction, tool-description, configuration,
or source mutations when the run contract allows them. It must not assume an
agent name, repository layout, evaluator type, benchmark, or mutation
dimension.

## Current implementation boundary

The runtime may provide target-specific adapters, but their supported agent
kinds, evaluators, and mutation dimensions are capabilities discovered during
preflight, not behavior encoded in this skill.

Stop after preflight when the selected target adapter cannot satisfy the frozen
generic contract. Never fall back to regular versions and call the result a
local Tenzing optimize job.
