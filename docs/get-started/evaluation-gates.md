# Evaluation gates

Treat pull request evaluation and main-branch deployment as separate owner
controls.

Evaluation is optional during bootstrap. You may register, enable, commit, and
deploy an agent without a dataset or evaluator bundle. That path is recorded as
unverified and emits a visible warning; it is never reported as evaluated.

## Example workflows stay inactive until copied

The repository now ships parseable examples under `examples/github-actions/`.
They do **not** activate merely by existing there. A workflow runs only after
you copy it into `.github/workflows/`.

## Template 1: opt-in PR gate

- Source: `examples/github-actions/foundry-opt-pr-evaluation.yml`
- Active path: `.github/workflows/foundry-opt-pr-evaluation.yml`

Use this when you want a pull request check to verify the exact PR head
commit with `foundry-opt deploy verify-registered` before merge.

### Copy and configure

1. Copy `examples/github-actions/foundry-opt-pr-evaluation.yml` to
   `.github/workflows/foundry-opt-pr-evaluation.yml`.
2. Set `FOUNDRY_OPT_DEFAULT_REPO_AGENT_ID` to the exact enabled
   `repoAgentId` from `.foundry-opt/registry.yaml`, or require the
   `workflow_dispatch` input for multi-agent repositories.
3. Keep the job `environment` aligned with the committed deployment
   environment from the selected sidecar (typically `foundry-production`).
4. Keep `FOUNDRY_OPT_DEPLOYMENT_CLIENT_ID`, `AZURE_TENANT_ID`, and `GH_TOKEN`
   available exactly as shown. `verify-registered` needs `GH_TOKEN` whenever
   the selected profile uses named repository checks.
5. Leave the example on `pull_request` plus `workflow_dispatch`. The example
   checks out `github.event.pull_request.head.sha` and exports that same exact
   source to `GITHUB_SHA` before calling `verify-registered`, so the
   verification receipt is bound to the reviewed commit rather than the merge
   ref.
6. The example resolves the shared CLI from the committed
   `.foundry-opt/registry.yaml` plus `.foundry-opt/bootstrap.lock.json`
   contract, verifies that pin, uploads the verification receipt, and writes a
   GitHub step summary.
7. Add the workflow as a required branch-protection status only after you
   trust the signal.

### Expected outcomes

- `foundry_evaluation` - required Foundry evaluation ran and passed.
- `repository_checks` - optional repository checks fallback passed.
- `none` / `unverified` - the policy allowed a no-evidence path; the workflow
  stays green but emits `WARNING: Unverified deployment permitted`.
- Any required-check or guardrail failure returns a non-zero exit and fails the
  PR gate after uploading the receipt.

## Template 2: main-branch deployment gate

- Source: `examples/github-actions/foundry-opt-main-deployment-gate.yml`
- Active path: `.github/workflows/foundry-opt-main-deployment-gate.yml`

Use this when you want to verify the exact main-branch commit first and call
`foundry-opt deploy publish-registered` only after the selected verification
gate succeeds.

### Copy and configure

1. Copy `examples/github-actions/foundry-opt-main-deployment-gate.yml` to
   `.github/workflows/foundry-opt-main-deployment-gate.yml`.
2. Protect the `foundry-production` GitHub environment (or your committed
   deployment environment equivalent) before allowing routine publication.
3. Set `FOUNDRY_OPT_DEFAULT_REPO_AGENT_ID` to the exact enabled
   `repoAgentId`, or require the workflow input if multiple agents can be
   published from the same repository.
4. Keep `FOUNDRY_OPT_DEPLOYMENT_CLIENT_ID`, `AZURE_TENANT_ID`, and
   `GH_TOKEN` available exactly as shown. `publish-registered` still performs
   the default-branch freshness check before creating a regular version.
5. Preserve the exact-source pattern from the example: check out the exact
   commit, export that same SHA before both `verify-registered` and
   `publish-registered`, and stop before publish if verification fails.
6. Review the step summary after each run. The example explicitly calls out:
   - `required Foundry evaluation`
   - `optional repository checks fallback`
   - `no-evidence / off path permitted by policy`
   - `WARNING: Unverified deployment permitted`
7. If bootstrap already deployed the same content locally, the workflow
   compares source, package, profile, registry, and target fingerprints. A
   complete match is a reconciled no-op even when the merge commit SHA differs
   from the local bootstrap commit.

## Recommended owner stance

- It is acceptable to skip evaluation when first bootstrapping a repository.
- Start with the PR gate as informational when you are ready to add evidence.
- Turn on the main deployment gate before routine publication.
- Keep `none` / `unverified` publication paths rare, deliberate, and visibly
  warned.
- Reuse repository-default evaluation assets when you want stable, repeatable
  signals.

## Related detail

- [Issues and monitoring](issues-and-monitoring.md)
- [Run an optimization](../guides/run-an-optimization.md)
- [Operate deployments](../guides/operate-deployments.md)
- [Evaluation onboarding](../evaluation-onboarding.md)
- [Recommended branch protection](../branch-protection.md)
- [Identity and RBAC](../identity-rbac.md)
- [Distribution and pinning](../distribution.md)
