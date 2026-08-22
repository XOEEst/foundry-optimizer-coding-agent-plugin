# Bootstrap

This is the one-click owner path from an agent repository to registered,
OIDC-connected, and optionally deployed Foundry agents.

The default path uses standard Copilot with the managed repository
instructions and installed skill. Bootstrap does not add or select a custom
agent. Normal owners should use the `/foundry-bootstrap` flow; the
`foundry-opt bootstrap ...` commands below remain as an advanced
compatibility interface for automation and reviewed source checkouts.

## Before you start

- Open the Git repository that owns the agent source.
- Authenticate GitHub CLI for the repository operations you intend to approve.
- Authenticate Azure CLI to a tenant and subscription that can access the
  target Foundry project.
- Keep unrelated working-tree changes out of the bootstrap commit. Bootstrap
  preserves customer files but refuses to hide unrelated dirty paths inside
  its reviewed commit.
- Have the Foundry project endpoint available when repository metadata cannot
  identify it. The skill resolves the backing Azure account with the current
  login; owners do not construct or enter an ARM resource ID.

An existing agent, dataset, or evaluator is not required. Agents may be
registered disabled, verification may be deferred, and the first regular
version may be created by the separately approved deployment step.

## Start

Install the released skill folder or add a local checkout:

```text
copilot skill add <path-or-release>/foundry-bootstrap
```

Then open the repository in your coding agent and say:

```text
Use /foundry-bootstrap to bootstrap this repository.
```

The skill resumes an interrupted operation automatically when durable state is
present.

## Guided steps

1. **Discovery**
   - See each candidate's folder, source root, package root, readiness, and
     current Foundry binding state.
   - Choose `ignore`, `register disabled`, or `register enabled`.
2. **Foundry targets**
   - For each enabled agent, reuse the Foundry project endpoint and agent name
     from repository metadata, or ask only when either value is missing.
   - Bootstrap reuses trusted profile, metadata, `azure.yaml`, azd, or binding
     evidence values and asks only for missing information.
   - The coding agent uses Azure tools and the current Azure login to resolve
     the backing account resource. The owner is prompted only when no unique
     account can be found or Azure access needs correction.
3. **Repository approval**
   - Review registry entries, profiles, instructions, issue forms, workflows,
     added files, updated files, preserved files, and conflicts.
   - One approval applies only that reviewed repository plan.
4. **Connection approval**
   - Review GitHub environments and variables together with the Azure identity,
     OIDC subjects, and Foundry User role assignments.
   - One approval applies the complete GitHub-to-Azure connection.
5. **Verification choice**
   - Configure Foundry datasets and evaluators now.
   - Defer them to a GitHub issue.
   - Use repository checks.
   - Start with no evidence and an explicit warning.
6. **Commit approval**
   - Review the exact changed paths and commit message.
   - Bootstrap creates a dedicated local branch and commit. It does not push or
     merge.
7. **Deployment approval**
   - Review the exact commit, Foundry targets, verification mode, warnings, and
     version action.
   - A separate approval deploys with the current local Azure login.
8. **Resource links**
   - Open the returned GitHub, Azure identity/RBAC, Foundry project/agent, and
     optional dataset/evaluator links.

Owners never need to open internal plan JSON, construct hashes, or author
approval files.

## Why connection approval is combined

GitHub requests OIDC tokens; Azure identity and RBAC determine what those
tokens can do. They are one trust decision, so the owner reviews them once.
Separate child receipts remain available for rollback and diagnostics.

## Deployment behavior

- Deployment always packages the exact approved local commit.
- A new Foundry agent name receives its first regular immutable version.
- An existing aligned agent reconciles identical code or publishes a new
  version.
- An existing diverged agent is deployable only with a visible warning.
- No explicit version route is set or changed.
- Operation-owned evaluation drafts are cleaned before publication completes.
- After merge, the main workflow compares source, package, profile, registry,
  and target fingerprints. Matching content records a reconciled no-op even if
  merge changed the commit SHA.

## Evaluation can wait

Datasets and evaluators are not prerequisites for registration, enablement, or
bootstrap deployment. Start without them when speed matters. Add a GitHub issue
later that names the desired dataset and evaluators, then enable the provided
PR or main-branch gate template.

## Advanced compatibility interface

- `foundry-opt bootstrap review discovery`
- `foundry-opt bootstrap review plan`
- `foundry-opt bootstrap connect plan`
- `foundry-opt bootstrap connect approve` or `foundry-opt bootstrap connect apply --approve`
- `foundry-opt bootstrap review status`

Use evaluation commands only when you intentionally want repository-default
verification:

- `foundry-opt bootstrap evaluation plan`
- `foundry-opt bootstrap evaluation apply`
- `foundry-opt bootstrap evaluation activate`

Compatibility policy: keep this CLI tree quiet and stable for existing
automation. Future retirement or breaking changes should be announced in
docs and release notes instead of adding runtime deprecation warnings.

## Example owner summaries

**Discovery**

> Selected `chat-agent` at `src/chat-agent`. Source and package roots
> match the reviewed layout. One extra sample app was discovered but not
> selected.

**Connection plan**

> Create GitHub environment `copilot`, update GitHub environment
> `foundry-production`, adopt Entra application `foundry-owner-review`,
> and grant reviewed Foundry User access on the target project.

**Deployment review**

> Deploy `chat-agent` from commit `abc123...` to the reviewed Foundry project.
> No dataset or evaluator is configured, so the deployment will be marked
> unverified. No route mutation is planned.

**Resources**

> GitHub contains the managed environments and Actions. Azure contains the
> reviewed identity and RBAC. Foundry contains the published agent version.
> Dataset and evaluator links are omitted because verification was deferred.

## Related detail

- [Overview](overview.md)
- [Issues and monitoring](issues-and-monitoring.md)
- [Skill and runtime seam](../architecture/skill-runtime-seam.md)
- [Repository contract](../reference/repository-contract.md)
- [Evidence, state, and receipts](../reference/evidence-state-and-receipts.md)
- [Evaluation onboarding](../evaluation-onboarding.md)
- [Identity and RBAC](../identity-rbac.md)
- [Managed files](../managed-files.md)
- [Skill owner flow](../../plugins/foundry-bootstrap/references/owner-flow.md)
- [Recovery](../../plugins/foundry-bootstrap/references/recovery.md)
