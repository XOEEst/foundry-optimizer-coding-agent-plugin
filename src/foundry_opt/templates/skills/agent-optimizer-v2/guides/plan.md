# Pre-run optimization plan

Present this plan to the user before baseline evaluation or any optimization
side effect. Do not start until the user explicitly approves it.

```markdown
## Optimization plan

| Item | Resolved value |
|---|---|
| Agent | identity, location, and kind |
| Exact baseline | deployed/local version and immutable identity |
| Allowed mutations | explicit mutable surfaces |
| Frozen properties | model, interfaces, tools/code, or other protected state |
| Training dataset | source, row count, and fingerprint |
| Search split | source or deterministic split rule and expected count |
| Confirmation split | source or deterministic split rule and expected count |
| Validation dataset | separate source, expected count, and sealed status |
| Evaluator | identity/version and required inputs |
| Objective | primary metric, direction, normalization, and guardrails |
| Candidate budget | maximum candidates and termination rule |
| Execution | sequential Tenzing cycles with 2-3 ideas per cycle |
| Tracking | run ID and experiment_tracking location or equivalent store |
| Apply winner | yes/no; still requires explicit approval before application |

### Planned gates

1. Freeze and evaluate the exact baseline on search and confirmation.
2. Evaluate each candidate on complete search.
3. Run aggregate-only confirmation only after strict search improvement.
4. Promote only after strict improvement on both search and confirmation.
5. Freeze one provisional winner.
6. Open validation once and compare the baseline and winner on the same
   separate validation dataset.

### Open questions or limitations

- List unresolved fields, unavailable operations, missing clean evidence, or
  assumptions.
```

Do not hide an unresolved value behind `auto` in the displayed plan. Replace it
with the discovered value or list it as an open question. If no clean separate
validation dataset exists, state that the run can produce only `unverified`.
