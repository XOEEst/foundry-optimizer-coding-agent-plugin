# ADR 0007: Development/validation/evaluation split

## Status

Accepted

## Context

This ADR originated in the earlier pre-public optimizer lineage and still defines the compatibility optimizer evaluation flow. The optimizer needs enough evaluation depth to compare candidates without leaking final-decision signal into iterative search.

## Decision

Evaluate the fresh baseline once on the development split, then evaluate every implemented candidate on the development split against the fresh baseline and current best. Reserve the validating dataset for the provisional winner only. Treat platform failures as platform failures, not scores, and require hard guardrails in the final decision.

## Consequences

Benefits:

- Candidate ranking stays comparable because every candidate shares the same development reference frame.
- The validating dataset remains a higher-value final seam instead of becoming part of search noise.
- Decision logic stays explicit about when improvement, regression, and guardrail evidence matter.

Tradeoffs:

- The optimize job may end with no winner even after promising development results.
- Validating failures create extra cleanup and retry branches near the end of the workflow.
- Repositories that use the quantitative Foundry evaluation path must maintain
  both development and validating evaluation contracts in trusted metadata.

## Alternatives considered

- **Run the validating dataset for every candidate** - rejected because it weakens the final-evaluation seam and increases cost.
- **Choose a winner from baseline-only comparisons without current-best updates** - rejected because the implemented decision module compares each candidate in sequence against the current best.
- **Blend platform failures into candidate scoring** - rejected because infrastructure errors are operational failures, not model-quality evidence.

## Evidence

- Optimize-job contract in the root [README](../../README.md).
- Decision and controller logic in [`src/foundry_opt/poc/controller.py`](../../src/foundry_opt/poc/controller.py) and [`src/foundry_opt/poc/decision.py`](../../src/foundry_opt/poc/decision.py).
- Trusted optional evaluation split metadata in
  [`src/foundry_opt/templates/customer-repo/agent/.foundry/foundry-opt.yaml`](../../src/foundry_opt/templates/customer-repo/agent/.foundry/foundry-opt.yaml).
- Decision and validating-flow tests in [`tests/poc/test_controller.py`](../../tests/poc/test_controller.py), [`tests/poc/test_decision.py`](../../tests/poc/test_decision.py), and [`tests/poc/test_config.py`](../../tests/poc/test_config.py).

## Supersedes / Superseded by

- Supersedes: None.
- Superseded by: None.
