# ADR 0008: Resumable state and minimal GitHub broker

## Status

Accepted

## Context

This ADR originated in the earlier pre-public optimizer lineage and is still reflected by the public compatibility runtime. Optimize jobs span issue parsing, candidate workspaces, evaluation, cleanup, evidence, and final projection, so they need trusted resumable state and the smallest useful GitHub write seam.

## Decision

Persist optimize-job state as trusted, atomic JSON with generation checks, digests, receipts, and runtime identity. Keep GitHub interaction behind a minimal broker seam that binds the exact issue and pull request, upserts redacted issue comments by stable markers, and performs only the narrow writes required by the optimize job.

## Consequences

Benefits:

- Optimize jobs can resume without repeating receipted work.
- Stable marker IDs make issue evidence idempotent and readable.
- Narrow broker scope reduces accidental GitHub side effects and keeps the controller implementation focused on decision flow.

Tradeoffs:

- The system must maintain sidecar, receipt, and digest compatibility over time.
- Broker availability becomes an explicit runtime dependency when evidence or closure work is required.
- The append-only evidence model is simpler than a richer dashboard, but less expressive for arbitrary reporting.

## Alternatives considered

- **Stateless reruns that recompute everything** - rejected because expensive external work and issue evidence need replay-safe receipts.
- **Direct GitHub API calls scattered across controller code** - rejected because a broker adapter gives better locality, redaction control, and exact binding checks.
- **Large GitHub write surface such as PR fan-out, rich dashboards, or trace uploads** - rejected because the repository deliberately keeps only redacted issue evidence in GitHub.

## Evidence

- State schema and compare-and-swap store in [`src/foundry_opt/poc/state.py`](../../src/foundry_opt/poc/state.py).
- Runtime wiring and sidecar management in [`src/foundry_opt/poc/runtime.py`](../../src/foundry_opt/poc/runtime.py).
- Minimal GitHub broker in [`src/foundry_opt/poc/github.py`](../../src/foundry_opt/poc/github.py).
- Resume, drift detection, and idempotent replay tests in [`tests/poc/test_state.py`](../../tests/poc/test_state.py), [`tests/poc/test_controller.py`](../../tests/poc/test_controller.py), [`tests/poc/test_runtime.py`](../../tests/poc/test_runtime.py), and [`tests/poc/test_github.py`](../../tests/poc/test_github.py).
- Broker CLI coverage in [`tests/poc/test_cli.py`](../../tests/poc/test_cli.py).

## Supersedes / Superseded by

- Supersedes: None.
- Superseded by: None.
