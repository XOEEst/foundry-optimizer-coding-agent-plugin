# ADR 0015: Skill-first bootstrap and optional verification

## Status

Accepted

## Context

Public bootstrap needs one normal owner interface that works with standard Copilot plus installed skills, not a custom control plane or raw JSON workflow. The skill and runtime also need a crisp responsibility boundary so adaptive discovery stays conversational while deterministic validation, mutation, receipts, and rollback stay machine-enforced.

## Decision

- Standard Copilot plus installed skills is the default owner experience. Do not require or select a custom agent by default. `/foundry-bootstrap` is the normal bootstrap interface for repository owners.
- The owner bridge exposes only `start`, `answer`, `approve`, `status`, and `rollback`.
- Adaptive discovery, question reduction, and contextual repository or Azure lookup belong in the skill. Deterministic validation, lifecycle state, mutations, receipts, compensation, and rollback remain in the runtime.
- Repository, connection, commit, and deployment remain explicit approval seams. Evaluation is optional: owners may configure it now, defer it, use repository checks, or proceed with a visible unverified warning when policy allows.
- Evidence and status labels must match proof. No-evidence or claim-only paths must be surfaced as unverified warnings rather than presented as verified or evaluated.
- The compatibility CLI surface remains available for automation and reviewed source checkouts, but human guidance should default to `/foundry-bootstrap`.

## Consequences

Benefits:

- Owners get one short, human-first path instead of learning the low-level command tree.
- The bridge contract stays narrow enough to keep mutation and rollback logic out of the skill.
- Optional verification supports fast bootstrap while still preserving explicit proof boundaries and warnings.
- Stable labels reduce the risk of overstating what the repository has actually proven.

Tradeoffs:

- The skill must stay disciplined and avoid re-implementing runtime behavior.
- Optional verification requires visible warnings and careful policy language so convenience does not look like proof.
- Bridge protocol stability constrains future refactoring because automation and downloaded skills depend on it.

## Alternatives considered

- **Make a custom agent or hidden backend the default owner path** - rejected because standard Copilot plus skills keeps the trust surface smaller and more inspectable.
- **Teach owners the raw bootstrap JSON and low-level CLI by default** - rejected because the normal path should be conversational and review-oriented.
- **Let the skill perform validation, mutation, or rollback directly** - rejected because those responsibilities need deterministic runtime checks and receipts.
- **Require evaluation for every bootstrap before registration or deployment** - rejected because owners need an explicit no-evidence path when policy allows it.

## Evidence

- Default owner workflow in the root [README](../../README.md), [Bootstrap](../get-started/bootstrap.md), and [`plugins/foundry-bootstrap/references/owner-flow.md`](../../plugins/foundry-bootstrap/references/owner-flow.md).
- Bridge operations and boundary rules in [`plugins/foundry-bootstrap/SKILL.md`](../../plugins/foundry-bootstrap/SKILL.md), [`plugins/foundry-bootstrap/scripts/README.md`](../../plugins/foundry-bootstrap/scripts/README.md), and [`plugins/foundry-bootstrap/scripts/bootstrap.py`](../../plugins/foundry-bootstrap/scripts/bootstrap.py).
- Runtime owner-review and state handling in [`src/foundry_opt/bootstrap/runner.py`](../../src/foundry_opt/bootstrap/runner.py) and [`src/foundry_opt/bootstrap/owner_review.py`](../../src/foundry_opt/bootstrap/owner_review.py).
- Optional-verification and proof-label behavior in [Evaluation gates](../get-started/evaluation-gates.md), [Issues and monitoring](../get-started/issues-and-monitoring.md), and [Owner review interface](../owner-review.md).
- End-to-end bridge coverage in [`tests/bootstrap/test_skill_bridge.py`](../../tests/bootstrap/test_skill_bridge.py), [`tests/bootstrap/test_skill_one_click_e2e.py`](../../tests/bootstrap/test_skill_one_click_e2e.py), [`tests/bootstrap/test_owner_review.py`](../../tests/bootstrap/test_owner_review.py), and [`tests/bootstrap/test_workflow_integration.py`](../../tests/bootstrap/test_workflow_integration.py).

## Supersedes / Superseded by

- Supersedes: [0011](0011-public-runtime-and-guided-bootstrap.md) for the default owner interface and skill-runtime responsibility split, and [0012](0012-receipt-bound-evaluation-activation.md) for the owner-facing verification-default and proof-label wording.
- Superseded by: None.
