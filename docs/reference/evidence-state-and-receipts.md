# Evidence, state, and receipts

Bootstrap has no operation state or machine receipt. Its owner-readable record
is `.foundry-opt/bootstrap-report.md`, and actual post-commit deployment
results are returned in the final skill response.

## Optimize-job state

Optimize jobs retain generation-hashed state so baseline, candidate,
validation, issue-comment, cleanup, and final-decision work can resume safely.

Logical issue records use stable identifiers:

- `foundry-opt-poc:<job_id>:baseline`
- `foundry-opt-poc:<job_id>:candidate:<candidate_id>`
- `foundry-opt-poc:<job_id>:final`

## Registered deployment receipts

Registered verification/publication commands emit deterministic JSON receipts
that include exact source fingerprints, verification status, published or
reconciled version, cleanup state, and route invariants.

These receipts are workflow artifacts, not bootstrap state and not committed
repository authority.
