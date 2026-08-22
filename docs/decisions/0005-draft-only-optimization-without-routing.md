# ADR 0005: Draft-only optimization without routing

## Status

Accepted

## Context

This ADR originated in the earlier pre-public optimizer lineage and remains implemented by the public compatibility runtime. An optimize job needs realistic Foundry evaluation but must not disturb production traffic or publish unreviewed code.

## Decision

Optimize jobs use Foundry draft versions only. The runtime captures the route fingerprint, evaluates the fresh baseline and each implemented candidate on owned drafts, cleans those drafts up exactly, and refuses route drift. The optimize job never publishes a regular version and never mutates endpoint routing.

## Consequences

Benefits:

- Candidate evaluation stays close to production behavior without changing production selection.
- Draft ownership and cleanup create a clear operational boundary around temporary resources.
- Route drift detection protects the optimize job from reasoning over a moving production target.

Tradeoffs:

- Draft lifecycle management adds cleanup and retry paths.
- Platform failures around draft creation, activation, evaluation, or deletion are explicit terminal conditions rather than hidden score effects.
- The optimize job cannot serve as a production deployment shortcut.

## Alternatives considered

- **Publish candidate regular versions during optimization** - rejected because optimize jobs are not production release machinery.
- **Mutate production routing to test candidates** - rejected because the approved contract avoids routing changes entirely.
- **Skip draft ownership and rely on ambient service state** - rejected because exact cleanup and trusted evidence need owned resources.

## Evidence

- Contract in the root [README](../../README.md).
- Foundry operations and route capture in [`src/foundry_opt/poc/runtime.py`](../../src/foundry_opt/poc/runtime.py) and [`src/foundry_opt/poc/foundry.py`](../../src/foundry_opt/poc/foundry.py).
- Controller behavior around candidate and validating cleanup in [`src/foundry_opt/poc/controller.py`](../../src/foundry_opt/poc/controller.py).
- Cleanup, retry, and route-drift tests in [`tests/poc/test_controller.py`](../../tests/poc/test_controller.py), [`tests/poc/test_runtime.py`](../../tests/poc/test_runtime.py), and [`tests/poc/test_foundry.py`](../../tests/poc/test_foundry.py).

## Supersedes / Superseded by

- Supersedes: None.
- Superseded by: None.
