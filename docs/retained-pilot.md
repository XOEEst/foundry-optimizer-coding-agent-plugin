# Retained bootstrap pilot

The public synthetic pilot repository is
[`XOEEst/foundry-bootstrap-pilot`](https://github.com/XOEEst/foundry-bootstrap-pilot).
It retains one active aligned root, one inactive bound-unknown fixture, and one
inactive ready-unbound fixture.

## Completed evidence

| Gate | Evidence |
| --- | --- |
| Runtime | Customer files pin `6f6e5249356b4680184cd4b3376b60c33b2fa4fb`; `uv.lock` SHA-256 is `74d7bb534c53e71a61ce197f3d5fa3169f2413373c2e42617280e78e83d6c681`. |
| Repository | Registry, managed lock, issue form, Copilot agent/instruction, validation/deployment workflows, and receipt-derived sidecar are committed. Existing customer instructions, skill, and setup steps remain present. |
| Evaluation | Synthetic-only onboarding produced a deterministic 20/10 split, one auto-generated unreviewed objective evaluator, and all five required safety evaluators. Activation atomically enabled only `travel-approver-live`. |
| Live agent | `foundry-opt-bootstrap-pilot-aligned:4` remains the latest routed version. No failed deployment created a new version or changed its route. |
| Validation | Pilot run [`32258753039`](https://github.com/XOEEst/foundry-bootstrap-pilot/actions/runs/32258753039) passed. |
| Copilot setup | Pilot run [`32258753085`](https://github.com/XOEEst/foundry-bootstrap-pilot/actions/runs/32258753085) passed, including pinned `uv` fallback in the preserved customer workflow. |
| Deployment preflight | Run [`32258753043`](https://github.com/XOEEst/foundry-bootstrap-pilot/actions/runs/32258753043) selected only the enabled root and froze the exact source, registry hash, sidecar hash, objective hash, and repository-default evaluator ID. |

## Synthetic repository boundary

The deployment run stopped before any Foundry request with structured
`status=blocked`. Microsoft Entra returned `AADSTS7002381`: this tenant accepts
GitHub federation only when the OIDC token carries an `enterprise` claim equal
to `microsoft`, `github`, or `microsoftopensource`. The personal `XOEEst`
repository's token has an empty enterprise claim.

The repository and federated credential already use GitHub's exact immutable
subject:

```text
repo:XOEEst@18523445/foundry-bootstrap-pilot@1337678711:environment:foundry-production
```

Changing the subject cannot add the missing enterprise claim. That repository
therefore remains bootstrap-shape and evaluation-activation evidence only. The
runtime does not fall back to static Azure credentials or broaden RBAC.

## Live enterprise acceptance

Live optimize/deploy acceptance uses a separate private enterprise-owned agent
code repository. Retained evidence confirms:

- exact public HTTPS runtime cutover with the private deploy key removed
- successful public CLI/skill setup and target-contract validation
- merge-deployment reconciliation of the existing regular version with safety
  at 1.0 and no route mutation
- one fresh baseline and exactly two changed candidates
- both candidates discarded at zero aggregate delta with safety at 1.0
- final `no_winner`, no validating run, and an unchanged closed draft PR
- all owned optimize drafts removed and the existing regular version retained

The Copilot workflow reports `cancelled` after the no-winner path closes its own
draft PR, but the broker had already persisted baseline, both candidate
results, and the final receipt-backed decision.

A subsequent bounded issue proved the complete winner path on the same private
enterprise-owned repository:

- fresh baseline `0.5000`
- exactly two candidates; winner development score `1.0000`
- validating score `0.8889`, safety `1.0000`, zero focused regressions
- reviewed PR containing only the evaluated winner
- automatic exact-merge deployment from previous version 15 to active version
  16
- deployment draft cleanup, latest-version verification, and no route mutation
- managed-session invocation confirmed the new behavior; the verification
  session was deleted

## Retained resources and cost posture

- public pilot repository and public runtime repository: no paid GitHub Actions
  minutes for the retained runs
- one user-assigned managed identity, two federated credentials, and one
  project-scoped Foundry User assignment: retained, with no direct idle charge
- hosted agent version 4, two datasets, one custom evaluator, five built-in
  safety bindings, two evaluation definitions, and retained evaluation runs:
  retained for inspection
- no new Foundry account, project, or model deployment was created
- the bounded generation/evaluation work stayed within the approved 30-case,
  one-generation-job, one-evaluator-job, and retry/deadline limits

The personal synthetic repository still requires a compatible tenant or
enterprise owner for its own publication, but it is no longer a blocker for
the completed customer bootstrap acceptance.
