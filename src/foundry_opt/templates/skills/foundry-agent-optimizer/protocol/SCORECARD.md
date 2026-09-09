# Final scorecard

The canonical scorecard is the durable, reviewable projection of the frozen run
contract, candidate journal, provider receipts, and final decision. Generate it
only from persisted evidence after the search and validation barriers close.
Do not reconstruct scores or lineage from memory.

## Language

Write every canonical scorecard artifact in English only. This includes
headings, column names, mutation ideas, hypotheses, explanations, decisions,
incident notes, and machine-readable descriptive values.

Candidate IDs, provider IDs, file paths, metric names, and exact source-defined
names remain unchanged. Do not copy prompts, responses, dataset rows, or other
possibly non-English evaluation content into the scorecard. If the user asks
for a localized summary, provide it separately; never localize or overwrite the
canonical scorecard.

The English-only rule applies to:

- `SCORECARD.md`
- `scorecard.json`
- `results.tsv`
- scorecard sections projected into an issue, pull request, or final run report

## Required identity and contract

Start with:

- experiment or run ID
- provider evaluation ID
- evaluator names and pinned versions
- final winner or `no_winner`
- frozen mutation scope
- candidate budget and round widths
- requested and effective evaluation concurrency
- primary metric and deterministic promotion rule
- frozen training/search, confirmation, and validation task counts
- data-isolation status for search, confirmation, validation, and reserved rows

State that `avgScore` is authoritative and pass rate is diagnostic unless the
frozen objective says otherwise.

## Required mutation idea ledger

Include one row for every allocated candidate, including invalid and rejected
candidates. A one-line "main change" is not enough. Each row must contain:

| Field | Required meaning |
|---|---|
| Candidate and round | Stable candidate ID and frozen round |
| Execution parent | The exact source/deployment parent actually evaluated |
| Mutation idea | A concise name for the strategy being tested |
| Hypothesis | Why this change was expected to improve the frozen objective |
| Concrete change | What changed in the allowed mutation surface, at behavioral detail rather than a vague label |
| Idea parents | Closed earlier candidates that contributed an idea, or `None` |
| Exact contribution | The specific positive, negative, or contrast lesson taken from each idea parent |
| Expected mechanism | How the concrete change should affect target behavior or evaluator outcomes |
| Observed outcome | Search and, when triggered, confirmation result relative to the round incumbent |
| Decision | Promoted, rejected, invalid, platform failure, or not confirmed, with the reason |

Round-one candidates must identify independent baseline hypotheses. Later
candidates must distinguish their one execution parent from any additional
idea parents. Never infer an idea contribution merely because two instructions
look similar; use the frozen proposal rationale and closed-round lessons.

## Required search scorecard

Show the baseline and every candidate:

| Candidate | Round | Execution parent | `avgScore` | Delta vs baseline | Delta vs round incumbent | Passed / Failed / Error | Pass rate | Decision |
|---|---:|---|---:|---:|---:|---:|---:|---|

Include the main change only as a short scan aid; the mutation idea ledger is
the authoritative explanation.

## Required confirmation scorecard

Show the frozen confirmation baseline and every candidate eligible for
confirmation:

| Candidate | Round | `avgScore` | Delta vs baseline | Delta vs round incumbent | Passed / Failed / Error | Pass rate | Decision |
|---|---:|---:|---:|---:|---:|---:|---|

Also list search-gate failures as `Not run` so absence cannot be confused with
missing evidence.

## Required final validation scorecard

Show the provisional winner's sealed validation result. When the run contract
requests a validating baseline comparison, show both:

| Candidate | `avgScore` | Delta vs baseline | Passed / Failed / Error | Pass rate | Pass-rate delta |
|---|---:|---:|---:|---:|---:|

Report absolute and relative `avgScore` improvement. Do not present validation
as a new search or use it to select among candidates.

## Required lineage and barriers

Include or link a Mermaid candidate DAG with:

- solid execution-parent edges
- dashed idea-parent edges labeled with exact contributions
- round boundaries
- promoted incumbents
- candidates not confirmed
- the final validation edge

Add a round table containing frozen incumbent, membership, confirmation
eligibility, promoted candidate, and barrier state. The DAG and table must
represent actual execution, not the intended plan.

## Required provider evidence

List every valid provider run with candidate, split, immutable run ID, and
report URL. Link the durable output-item artifact for each run when available.

Add an incidents and exclusions section for incomplete, retried, canceled, or
invalid runs. Record why each excluded run was inadmissible and identify the
replacement evidence. Excluded attempts must never appear as candidate scores.

Also record every data-exposure incident. Identify the compromised role,
exposure type, affected candidates, whether a never-exposed replacement was
used, and whether the result may still be called clean holdout evidence.

## Required artifact and application status

End with:

- canonical scorecard path
- machine-readable scorecard path
- tabular results path
- lineage DAG path
- output-item evidence directory
- winning mutation artifact or patch
- whether the winner was applied to the target source
- cleanup status for operation-owned drafts, deployments, and worktrees

## Machine-readable projections

`scorecard.json` must preserve the same identity, contract, mutation ideas,
lineage, split evidence, decisions, incidents, and final application status as
the Markdown scorecard.

`results.tsv` must contain one row per valid candidate/split result with stable
IDs, parent, score, deltas, counts, pass rate, provider run ID, and decision.
Do not put invalid attempts in `results.tsv`; keep them in the incidents list.

Before finalizing, verify that:

1. every allocated candidate appears in the mutation ledger and search table
2. every idea parent has an exact contribution and comes from a closed earlier round
3. every score has a durable provider receipt and complete frozen denominator
4. Markdown, JSON, TSV, DAG, and experiment state agree on the winner and lineage
5. all scorecard prose and descriptive values are English
6. the target source application and cleanup status are explicit
7. no hidden split is described as sealed, holdout, or confirmation-clean when
   its task-level evidence reached mutation-capable context
