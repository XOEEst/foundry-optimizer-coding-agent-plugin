# Read-only discovery

Complete discovery before preparing a mutation plan.

## Repository and Git

- Resolve the repository root and canonical GitHub remote.
- Record the current branch, exact `HEAD`, default branch, worktree status, and
  existing untracked files.
- Read existing registry, sidecars, `azure.yaml`, dependency files, workflows,
  instructions, issue forms, and agent metadata.
- Locate agent entry points and determine source root, package root, runtime,
  dependency restoration, protocol, CPU/memory settings, model environment
  variable, and paths safe for optimizer edits.
- Detect shared source between agents and keep stable existing agent IDs.

Do not use file writes, Git index changes, branch changes, package installation,
or formatting commands during this phase.

## Deployment policy scope

Inventory every statement about drafts, regular versions, publication, and
routing. Record its source and scope:

- candidate or optimize-job execution
- bootstrap
- merge-time or production deployment
- explicitly repository-wide

Do not treat Copilot/optimizer draft-only instructions as deployment policy.
Inspect active workflows and deployment documentation. A workflow that names
and executes regular-version publication establishes regular deployment intent
unless an equally explicit deployment-wide prohibition supersedes it.

If scoped optimization rules and regular deployment coexist, preserve both:
optimizer jobs remain draft-only, while bootstrap uses regular `azd deploy`.
Only an unresolved contradiction between equally authoritative deployment
contracts is a blocker.

## Ignore rules

Check every path the plan may create or update:

```text
git check-ignore -v --no-index -- <path>...
```

Include `.foundry-opt/registry.yaml`,
`.foundry-opt/bootstrap-report.md`, every agent sidecar, `azure.yaml`, and the
GitHub workflow, instruction, and issue-form destinations. Record whether each
match comes from the repository `.gitignore`, `.git/info/exclude`, or a global
excludes file.

When a repository `.gitignore` rule blocks a required tracked file, stage the
smallest clear `.gitignore` correction with the other proposed files. Prefer
removing an obsolete directory-wide rule; otherwise unignore the required
parent directory and exact tracked files. Preserve unrelated ignore behavior.
Do not use `git add -f` to conceal an unresolved repository rule.

Local or global exclude rules cannot be fixed by a repository patch. Include
their exact correction in the approval plan or stop until the owner corrects
them. Re-run the command against the rendered paths before approval and after
applying the approved changes.

Treat an untracked `.foundry-opt/bootstrap.lock.json` as removable only when its
contents and surrounding evidence identify retired bootstrap tooling. Never
delete `.foundry-opt/registry.yaml`, `.foundry-opt/bootstrap-report.md`, or an
unknown file as legacy metadata.

## Tool capability probes

Run non-mutating probes and capture their output:

```text
git --version
gh --version
az version
az account show
azd version
azd ext list
azd ai agent version
azd ai agent --help
```

If `azd` or `azure.ai.agents` is absent or lacks the commands required by the
rendered `azure.yaml`, plan an official-channel install or upgrade for the
combined approval. The release metadata never pins these tools.

Use the standard extension commands after approval:

```text
azd ext install azure.ai.agents
azd ext upgrade azure.ai.agents
```

## Package feed inventory

Identify Python and NuGet restore commands plus existing `pyproject.toml`,
`uv.toml`, pip configuration, `NuGet.Config`, private sources, credentials, and
package-source mapping. Probe required sources without installing packages.

Prefer an existing approved repository configuration. When direct public
access is unavailable, use these replacement public sources:

```text
https://packagefeedproxy.microsoft.io/pypi/simple
https://packagefeedproxy.microsoft.io/nuget/v3/index.json
```

Use `UV_DEFAULT_INDEX` or `PIP_INDEX_URL` for a temporary Python restore.
Replace the unreachable public NuGet source without removing approved private
sources or source mapping. Do not use the Python proxy as an extra index beside
public PyPI, and do not write feed credentials into repository files.

Record the selected source and whether it requires an approved repository or
workflow change. An inaccessible or authentication-blocked required source is
`unknown` and blocks mutation until access is established.

## GitHub inventory

Use `gh` read operations to inspect:

- repository owner/name and immutable IDs
- default branch and branch protection or rulesets
- existing `copilot` and deployment environments
- environment and repository variables
- existing deployment and Copilot setup workflows

Variable values that GitHub does not return must be treated as unknown, not as
an exact match. Ask for approval to set a missing/unknown variable only when its
desired non-secret value is established from Azure inventory.

## Azure and Foundry inventory

Use the current authenticated tenant and subscription. Record them explicitly.
Inspect:

- Foundry accounts and projects
- project endpoints and immutable ARM resource IDs
- model deployments
- hosted agents and versions
- user-assigned identities or application registrations
- federated identity credentials
- role assignments at their exact scopes

Prefer immutable IDs and service-returned endpoints over names inferred from
text. If the requested account or project is visible in more than one
subscription, stop and require an unambiguous subscription selection before
planning.

## Classification

For each resource needed by the desired repository:

- **exact** - immutable identity, type, scope, and relevant configuration match
- **missing** - no resource occupies the approved identity/name/scope
- **conflict** - a resource occupies the identity/name/scope but differs
- **unknown** - permissions or API limitations prevent a conclusive read

Only exact and missing resources may appear in an approval plan. A conflict or
unknown result blocks mutation.
