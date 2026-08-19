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

Live optimize/deploy acceptance uses the enterprise-owned
[`microsoft-foundry/luffy-test-agent-repo-002`](https://github.com/microsoft-foundry/luffy-test-agent-repo-002)
agent code repository.

| Gate | Evidence |
| --- | --- |
| Public cutover | [PR #109](https://github.com/microsoft-foundry/luffy-test-agent-repo-002/pull/109) replaced the private cross-organization SSH fetch with exact public HTTPS and removed the obsolete deploy-key secret. |
| Runtime | Luffy pins `770ad878f0658e9368b042d9a7f6732e49ff0200`; the bootstrap Bash launcher discovered the legacy `agent/` root through the repository policy. |
| Setup | [Run 32285445511](https://github.com/microsoft-foundry/luffy-test-agent-repo-002/actions/runs/32285445511) installed and verified the public CLI and skill. |
| Validation | [Run 32285445523](https://github.com/microsoft-foundry/luffy-test-agent-repo-002/actions/runs/32285445523) passed the shared runtime suite and target contract. |
| Deployment | [Run 32280836699](https://github.com/microsoft-foundry/luffy-test-agent-repo-002/actions/runs/32280836699) reconciled existing version 15 with `advisory_safety=1.0`, `reconciled=true`, and `route_mutated=false`. |
| Optimize issue | [Issue #110](https://github.com/microsoft-foundry/luffy-test-agent-repo-002/issues/110) ran one baseline and exactly two changed candidates on public runtime `770ad878`. |
| Decision | Both candidates held `policy_coverage=0.5000` with delta `0.0000` and `advisory_safety=1.0000`; the final verdict was `no_winner`. |
| Repository result | [PR #113](https://github.com/microsoft-foundry/luffy-test-agent-repo-002/pull/113) closed with no files. No validating run, regular publication, or route mutation occurred. |
| Cleanup | All owned optimize drafts were removed; the retained agent version list contains regular versions only and latest remains 15. |

The Copilot workflow reports `cancelled` after the no-winner path closes its own
draft PR, but the broker had already persisted baseline, both candidate
results, and the final receipt-backed decision.

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
