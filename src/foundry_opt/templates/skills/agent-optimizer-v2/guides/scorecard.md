# Final scorecard

Generate `SCORECARD.md`, `results.tsv`, and `CANDIDATE_DAG.md` from persisted
run evidence. Do not reconstruct scores or lineage from memory.

## Run summary

Include:

- run ID and terminal outcome
- agent and exact baseline
- allowed mutations and frozen properties
- datasets, split counts, fingerprints, and isolation status
- evaluator, primary metric, direction, normalization, and guardrails
- candidate budget and actual candidates evaluated
- provisional winner, validation result, application status, and cleanup

## Idea and candidate ledger

Include every allocated candidate, including invalid or failed candidates:

| Candidate | Cycle | Execution parent | Idea | Hypothesis | Concrete change | Idea contributions | Expected mechanism | Outcome | Decision |
|---|---:|---|---|---|---|---|---|---|---|

Do not replace the hypothesis and concrete change with a vague “main change.”

## Search results

| Candidate | Cycle | Search score | Delta vs baseline | Delta vs incumbent | Complete count | Decision |
|---|---:|---:|---:|---:|---:|---|

## Confirmation results

Include every candidate that reached confirmation and list search-gate failures
as `Not run`.

| Candidate | Confirmation score | Delta vs baseline | Delta vs incumbent | Guardrails | Decision |
|---|---:|---:|---:|---|---|

## Validation

Show the frozen provisional winner and exact baseline on the same separate
validation dataset:

| Subject | Validation score | Complete count | Guardrails |
|---|---:|---:|---|

Validation is final evidence, not another search round.

## Lineage, incidents, and artifacts

Include:

- a Mermaid DAG with execution-parent edges and labeled idea contributions
- incomplete, retried, failed, excluded, or contaminated evaluations
- provider references without credentials or hidden evidence
- run-state, tabular result, winning artifact, and experiment-tracking paths
- cleanup status for every temporary candidate resource

The scorecard and `templates/result.yaml` must agree on the winner and terminal
outcome:

- `winner`: strict search and confirmation improvement plus clean validation
- `no_winner`: complete quantitative run with no promoted candidate
- `unverified`: missing or contaminated required evidence
- `blocked`: platform, credential, target, or data failure prevented completion
