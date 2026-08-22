# Operate deployments

Deployment is a separate lifecycle from optimize jobs. The current public runtime supports two operator paths:

| Path | Normal entry point | Exact source |
| --- | --- | --- |
| Local bootstrap deployment | the reviewed bootstrap deployment approval | the exact local commit created during bootstrap |
| Registered deployment | `.github/workflows/foundry-opt-deploy.yml` or the registered deploy CLI | the exact selected default-branch commit |

Neither path mutates an explicit Foundry route. Deployment requires service-managed latest behavior and records `route_mutated: false` in its receipts.

## 1. Local bootstrap deployment

Local bootstrap deployment is the owner-reviewed path described in [Bootstrap](../get-started/bootstrap.md).

What it does:

- deploys only from the exact reviewed local commit
- uses the current local Azure identity
- works per enabled agent and per reviewed Foundry target
- records a per-agent receipt with the previous and published versions
- keeps draft cleanup and latest-version verification inside the shared deployment implementation

This is not a dirty-working-tree deploy. The local deployment plan is bound to the local commit receipt hash and the reviewed commit SHA.

## 2. Registered deployment on the default branch

The managed workflow at [`src/foundry_opt/templates/customer-repo/.github/workflows/foundry-opt-deploy.yml`](../../src/foundry_opt/templates/customer-repo/.github/workflows/foundry-opt-deploy.yml) is the normal repository path after bootstrap.

It does three important things before publication:

1. require the current default-branch tip
2. discover which enabled agents are affected by the exact changed paths
3. freeze a deployment plan from `.foundry-opt/registry.yaml` plus the selected v2 sidecar

Shared-contract changes under `.foundry-opt/` or `.github/workflows/` expand to every eligible enabled agent.

## 3. Verification modes

Deployment verification comes from the selected v2 profile.

| Mode | Meaning |
| --- | --- |
| `foundry_evaluation` | Verify the exact source ZIP by creating a draft, downloading it back, and running the approved Foundry evaluation plus hard guardrails. |
| `repository_checks` | Run trusted repository commands or named checks instead of a quantitative Foundry winner path. |
| `none` | Publish only when the profile policy allows a visible unverified path. |

Gate policy matters:

- `require_foundry_evaluation` blocks fallback modes.
- `allow_repository_checks` permits repository checks but not a no-evidence publish.
- `allow_no_evidence` permits an explicit unverified deployment when neither a usable bundle nor trusted checks exist.

## 4. Fingerprints and reconciliation

Registered deployment does not rely on the merge SHA alone. It freezes and later compares a full fingerprint set:

- `source_fingerprint`
- `package_fingerprint`
- `profile_fingerprint`
- `registry_fingerprint`
- `target_fingerprint`
- `repoAgentId`

These values become the reconciliation metadata. If the latest regular version already matches the exact ZIP and the full reconciliation metadata, the run records `reconciled: true` instead of publishing a duplicate version.

This is why a merge commit with the same packaged bytes can still become a clean no-op.

## 5. Freshness checks

Publication is freshness-checked twice:

- the workflow requires the exact current default-branch tip before deployment starts
- `publish-registered` checks again before regular publication

If the branch moved, the run returns structured `superseded` output instead of forcing an out-of-date publish.

`verify-registered` is different: it is verification-only and can run from pull-request context against the exact PR head commit.

## 6. Exact-source verification flow

For Foundry evaluation, deployment follows this exact-source sequence:

1. package the exact commit and selected source root
2. create a draft
3. wait for the draft to become active
4. download the draft code
5. prove that the downloaded ZIP exactly matches the packaged ZIP
6. run the configured deployment verification
7. clean up the draft
8. reconcile or publish one regular immutable version
9. verify that the resulting regular version is latest

The same exact ZIP rule applies to local bootstrap deployment and registered deployment.

## 7. Understand failure boundaries

### Pre-publication failures

These stop before a regular version is created:

- registry or sidecar selection problems
- OIDC or client-id mismatches
- route-mode failures
- exact-source packaging failures
- draft activation or download mismatches
- repository-check failures
- Foundry guardrail failures
- freshness or superseded checks

### Post-publication failures

These happen after the service already created a regular version:

- the downloaded regular version bytes do not match the packaged ZIP
- the published version cannot be confirmed as latest
- later latest-version verification fails

In that case the CLI reports a blocked result that still names the created version. There is no automatic route rollback or version rollback contract in this workflow.

## 8. Where to look after a run

Registered deployment writes operator-facing details to:

- the GitHub Actions run result
- the step summary
- the JSON receipt written by `--receipt`

The summary includes the verification mode, verification status, warning text for unverified publication, and the full deployment receipt.

## Related references

- [Evaluation gates](../get-started/evaluation-gates.md)
- [CLI reference](../reference/cli.md)
- [Evidence, state, and receipts](../reference/evidence-state-and-receipts.md)
- [Deployment](../architecture/deployment.md)
