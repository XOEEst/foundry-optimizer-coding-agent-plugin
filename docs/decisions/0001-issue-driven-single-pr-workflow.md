# ADR 0001: Issue-driven single PR workflow

## Status

Accepted

## Context

This ADR originated in the earlier pre-public optimizer lineage and is preserved here because the public plugin still carries the compatibility optimizer modules and tests. The workflow boundary remains the same: one issue states the problem, one controller state tracks progress, and one early pull request is the only patch projection target.

## Decision

Use an issue-driven workflow with one optimize job per issue and one early same-repository pull request binding. The optimize job starts from the trusted issue form, records one baseline, evaluates bounded candidates, writes redacted issue evidence, and either applies the verified winner to the early pull request or closes that pull request unchanged when there is no winner.

## Consequences

Benefits:

- The controller, evidence, and projection seams all share one durable job identity.
- Reviewers get one pull request with bounded diff locality instead of fan-out across many branches or repositories.
- Resumption logic stays simpler because there is one authoritative state machine and one projection target.

Tradeoffs:

- The workflow gives up candidate-by-candidate PR review in exchange for tighter control and simpler state.
- The optimize job depends on an exact early pull request binding for no-winner closure and verified winner projection.
- The design is intentionally narrower than a general orchestration system and does not optimize multiple issues at once.

## Alternatives considered

- **Candidate PR fan-out** - rejected because it would increase GitHub write surface, reduce locality, and complicate ranking, cleanup, and final selection.
- **Late PR creation only after winner selection** - rejected because the approved workflow wants one early pull request that accumulates verified outcome, not a hidden patch queue.
- **Out-of-band patch artifacts without a bound PR** - rejected because maintainers need a normal repository review seam and exact repository binding.

## Evidence

- Workflow contract in the root [README](../../README.md).
- Controller lifecycle and terminal outcomes in [`src/foundry_opt/poc/controller.py`](../../src/foundry_opt/poc/controller.py).
- Exact issue-form parsing in [`src/foundry_opt/poc/issue.py`](../../src/foundry_opt/poc/issue.py) and [`tests/poc/test_issue.py`](../../tests/poc/test_issue.py).
- Pull request binding and same-repository checks in [`src/foundry_opt/poc/github.py`](../../src/foundry_opt/poc/github.py) and [`tests/poc/test_github.py`](../../tests/poc/test_github.py).
- Resume and finish behavior in [`tests/poc/test_cli.py`](../../tests/poc/test_cli.py).
- Detailed original acceptance evidence is retained privately.

## Supersedes / Superseded by

- Supersedes: None.
- Superseded by: None.
