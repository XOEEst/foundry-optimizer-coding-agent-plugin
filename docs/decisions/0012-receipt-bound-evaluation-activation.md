# ADR 0012: Receipt-bound evaluation activation

## Status

Accepted

## Context

This ADR captured the first public evaluation-onboarding contract for bootstrap. It needed to reuse existing immutable evaluation assets when possible, fall back deterministically when evidence was thin, and refuse to point committed repository configuration at unproven cloud state.

## Decision

- Evaluation onboarding starts with inventory of existing datasets, evaluators, and definitions. Bootstrap reuses a suitable immutable bundle before generating any new asset.
- Trace-derived data is allowed only when inventory can supply at least 15 useful traces. Otherwise the operation falls back to synthetic-only data. Partial trace-generation outcomes are recorded in the receipt, but partial trace datasets are not configured as the active contract.
- Dataset preparation targets 30 total cases with an approximately 20-development / 10-validating split and hard minimums of 10 development and 5 validating cases. Deduplication, stable fingerprints, zero overlap, and grouped related cases remain part of the split contract.
- After data preparation, bootstrap reuses a suitable immutable evaluator when possible. If none is adequate, it may auto-generate one rubric exactly once during initial local bootstrap, after splitting and before activation. The generated evaluator is adopted only after structural validation, successful execution, measurable headroom, and full safety checks, and its provenance is recorded as `auto_generated_unreviewed`.
- Activation always includes the built-in Content Safety evaluator bundle with `required_pass_rate = 1.0`. Missing, incompatible, or unprovable safety behavior blocks activation.
- Dynamic cloud outputs stay in provider state and activation receipts bound to the exact plan hash, approved phase, and runtime SHA. Repository config is not flipped optimistically before those receipts exist.
- Sidecar and registry enablement is atomic and post-activation. `ready-unbound` roots stop before this transition; replacement validates and activates a candidate bundle before swapping committed references.

## Consequences

Benefits:

- Inventory-first reuse reduces avoidable dataset and evaluator churn.
- The 15-trace threshold and synthetic fallback avoid treating sparse trace history as trustworthy activation data.
- Receipt-bound activation keeps committed repository state aligned with one exact validated evaluation bundle.
- Atomic replacement prevents sidecars or registry state from drifting to a bundle that never actually activated.

Tradeoffs:

- Evaluation onboarding can stop with no activation when traces are too thin, safety is unavailable, or the generated rubric lacks measurable headroom.
- `ready-unbound` remains a deliberate two-step experience: scaffold first, deploy and bind later.
- Provider-state receipts become a required part of resumability because the most important outputs are immutable cloud IDs rather than local files.

## Alternatives considered

- **Always generate new datasets and evaluators** - rejected because bootstrap must reuse adequate immutable assets before creating more state.
- **Allow fewer than 15 traces or activate partial trace datasets** - rejected because those results are insufficient for the default evaluation contract.
- **Enable the sidecar or registry before activation completes** - rejected because committed config must not point at unproven cloud state.
- **Replace evaluator references in place even if the new bundle fails** - rejected because replacement must preserve the last known good bundle on failure.

## Evidence

- Public evaluation contract in [Evaluation onboarding](../evaluation-onboarding.md).
- Receipt-backed verification posture and labels in [Owner review interface](../owner-review.md) and [Evaluation gates](../get-started/evaluation-gates.md).
- Evaluation onboarding implementation in [`src/foundry_opt/bootstrap/evaluation/inventory.py`](../../src/foundry_opt/bootstrap/evaluation/inventory.py), [`src/foundry_opt/bootstrap/evaluation/core.py`](../../src/foundry_opt/bootstrap/evaluation/core.py), [`src/foundry_opt/bootstrap/evaluation/execution.py`](../../src/foundry_opt/bootstrap/evaluation/execution.py), and [`src/foundry_opt/bootstrap/evaluation/activation.py`](../../src/foundry_opt/bootstrap/evaluation/activation.py).
- Tests for inventory, dataset materialization, activation, resumability, and finalization in [`tests/bootstrap/test_evaluation_inventory.py`](../../tests/bootstrap/test_evaluation_inventory.py), [`tests/bootstrap/test_dataset_materialization.py`](../../tests/bootstrap/test_dataset_materialization.py), [`tests/bootstrap/test_activation_draft.py`](../../tests/bootstrap/test_activation_draft.py), [`tests/bootstrap/test_evaluation_onboarding_machine.py`](../../tests/bootstrap/test_evaluation_onboarding_machine.py), [`tests/bootstrap/test_evaluation_phase_flow.py`](../../tests/bootstrap/test_evaluation_phase_flow.py), and [`tests/bootstrap/test_plan_receipt.py`](../../tests/bootstrap/test_plan_receipt.py).
- Detailed original acceptance evidence is retained privately.

## Supersedes / Superseded by

- Supersedes: None.
- Superseded by: [0015](0015-skill-first-bootstrap-and-optional-verification.md) for the owner-facing rule that evaluation is optional and labels must match proof; receipt-bound activation, safety gates, and deterministic reuse rules remain current.
