# Evaluation data isolation

Protect confirmation and final-validation evidence from candidate generation.
Disjoint scoring subsets are not sufficient when the coding agent can inspect
their rows, labels, expected outputs, or per-task evaluation results.

## Roles and visibility

Freeze every evaluation row into exactly one role before candidate generation:

| Role | Candidate-generation visibility | Purpose |
|---|---|---|
| Training/search | Row content, labels, and task-level evaluation evidence allowed | Observation, diagnosis, and mutation design |
| Confirmation | Aggregate `avgScore`, delta, guardrails, and provider reference only | Promotion gate |
| Final validation | No evidence until the winner and all mutations are frozen | Final generalization estimate |
| Unused/reserved | No visibility | Future clean split or replacement evidence |

A trusted partition/evaluation adapter may read hidden rows to execute its
role. The coding agent, proposal generator, reflection loop, and any subagent
that can influence a mutation must not receive them.

## Partition before inspection

When one source file contains rows for multiple roles:

1. partition it deterministically in a trusted adapter before candidate
   generation
2. freeze role counts, row-set fingerprints, and zero-overlap evidence
3. expose only the training/search projection to the mutation loop
4. keep confirmation, final-validation, unused, and reserved rows behind the
   evaluation adapter

Do not search, print, summarize, sample, or run ad hoc analysis over the
combined source file after it has been assigned hidden roles. This includes
`rg`, file views, notebooks, scripts, and subagent prompts. A hash partition is
not a holdout boundary if the mutation loop can still read the original file.

Do not use hidden row content in the partition key exposed to the mutation
loop. The trusted adapter may use frozen row IDs or content hashes internally,
but it must reveal only role fingerprints and counts.

## Confirmation remains hidden across rounds

Confirmation is part of candidate selection, so its aggregate outcome may
enter strategy state. Its task contents, row IDs, labels, expected outputs,
responses, tool traces, per-task scores, evaluator reasons, and failure
categories must never influence a later candidate in the same run.

Do not inspect confirmation output-item artifacts after a round and then
continue optimization. Persist them in a restricted evidence sink for audit;
project only aggregate gate evidence into the candidate journal and scorecard.

## Final validation is one-way

Run final validation only after:

- candidate generation has terminated
- the provisional winner's mutation and hashes are frozen
- no further mutation is permitted in the run

After final-validation evidence is opened, the run may report or apply the
frozen winner, but it must not resume search. Any subsequent mutation requires
a new run with fresh confirmation and validation evidence.

## Contamination handling

Treat a hidden split as contaminated when mutation-capable context receives any
of its row content, labels, expected outputs, per-task results, reasons, traces,
or derived policy summaries.

On contamination:

1. stop using that split as holdout evidence
2. persist an incident containing the exposed role, exposure type, time,
   affected candidates, and discovery point
3. do not describe the split as hidden, holdout, sealed, or confirmation-clean
4. replace it with a deterministically frozen split drawn only from
   never-exposed reserved rows, then restart every affected comparison,
   or report the limitation and omit the compromised gate
5. if final validation is contaminated before the winner is frozen, obtain a
   fresh validation dataset or report no verified winner

Aggregate confirmation scores disclosed according to this protocol are not
contamination. Task-level confirmation evidence is contamination.

## Required receipts

Persist:

- role counts and row-set fingerprints
- zero-overlap result
- mutation-loop visibility allowlist
- confirmation aggregate-only projection receipt
- winner-freeze receipt created before final validation
- any contamination and replacement receipts

The final scorecard must state whether search, confirmation, validation, and
reserved-row isolation remained intact. Never silently downgrade a compromised
split into valid evidence.

