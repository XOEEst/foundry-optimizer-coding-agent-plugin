# ADR 0008: Resumable state and minimal GitHub broker

## Status

Accepted

## Context

This ADR originated in the earlier pre-public optimizer lineage and is still reflected by the public compatibility runtime. Optimize jobs span issue parsing, candidate workspaces, evaluation, cleanup, evidence, and final projection, so they need trusted resumable state and the smallest useful GitHub write seam.

## Decision

Persist optimize-job state as trusted, atomic JSON with generation checks, digests, receipts, and runtime identity. Keep GitHub interaction behind a minimal broker seam that binds the exact issue and pull request, upserts redacted issue comments by stable markers, and performs only the narrow writes required by the optimize job.

The closed `issue.read` operation supplies the bound issue body when a Copilot
dynamic event contains repository and input metadata but no top-level issue.
Callers supply only a request ID and timeout; the broker uses its own credential
for exactly `GET /repos/{bound owner}/{bound repo}/issues/{bound number}`.
It checks the issue number, API/HTML/repository URLs, any returned repository
identity, and rejects pull-request-shaped responses. The typed `IssueReadReceipt`
contains the bound `repository_id`, `issue_number`, and nonblank, size-bounded,
token-redacted `body`; the caller independently compares the receipt identity
with its loaded binding. This is not an arbitrary GET or snapshot interface.

When the binding carries an `issue_author_id`, reads verify the fetched author's
immutable GitHub ID before returning the body. Legacy author-bearing bindings
without this ID instead require a case-insensitive match of the fetched author
login to the bound login. This preserves their compatibility, but cannot detect
login reuse; bindings with immutable IDs provide the stronger identity check.

## Consequences

Benefits:

- Optimize jobs can resume without repeating receipted work.
- Stable marker IDs make issue evidence idempotent and readable.
- Narrow broker scope reduces accidental GitHub side effects and keeps the controller implementation focused on decision flow.

Tradeoffs:

- The system must maintain sidecar, receipt, and digest compatibility over time.
- Broker availability becomes an explicit runtime dependency when evidence or closure work is required.
- Broker availability is also required to load the bound issue body from a dynamic event.
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
