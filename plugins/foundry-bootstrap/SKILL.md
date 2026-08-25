---
name: foundry-bootstrap
description: Bootstrap repository agents for Microsoft Foundry with read-only discovery, one combined approval, static templates, and standard Git, GitHub, Azure, and azd tools.
---

# Foundry bootstrap

Use this static skill to prepare a repository for Foundry agent optimization and
hosted-agent deployment. The package contains guidance, editable templates, and
JSON schemas only. Do not look for or run a bundled bootstrap program.

## Non-negotiable behavior

- Perform discovery with read-only repository, Git, GitHub, Azure, and Foundry
  commands.
- Render every proposed repository file in the coding session's staging area.
  Do not modify the repository or a remote service during discovery.
- Present one combined approval request containing the exact repository diffs,
  exact remote resources to reuse or create, local commit plan, and deployment
  plan.
- After approval, apply only that reviewed plan. If discovery changes the plan,
  render a new exact diff and request a new combined approval.
- Reuse exact matching cloud resources. Create missing resources only after
  approval. Stop on any name, scope, identity, endpoint, or configuration
  conflict; never replace or modify a conflicting remote resource.
- Create an exact local commit before deployment. Never push.
- Deploy the clean committed tree with `azd deploy`.
- If any mutation fails, stop immediately. Leave successful local and remote
  changes in place and document completed, failed, and pending work.

Do not introduce bootstrap programs, durable bootstrap metadata, opaque
identifiers, generated evidence files, automatic reversal behavior, or
evaluation onboarding.

## Inputs

1. Resolve exact retained-runtime provenance:
   - When [release.json](release.json) contains concrete values, use its
     repository, commit, package path, `uv.lock` digest, and optimizer skill
     path.
   - When it contains `__...__` placeholders, resolve the retained runtime
     independently from the bootstrap skill files. The bootstrap skill
     directory itself may remain local, include unpushed instruction or
     template edits, and be refreshed with `/skills reload`.
   - Prefer the skill repository's configured upstream ref as the runtime
     candidate. If no upstream exists, select the nearest remote-ref tip that
     is an ancestor of the local checkout. Require the candidate to contain the
     package, `uv.lock`, registry v2 contract, and optimizer skill used by the
     rendered workflows. Do not require the skill checkout's `HEAD` to be
     remotely reachable.
   - A published archive is not required for a source checkout. Prove that a
     clean temporary checkout can fetch the compatible runtime commit before
     using it:

     ```text
     git init <temporary-directory>
     git -C <temporary-directory> remote add origin <repository>
     git -C <temporary-directory> fetch --depth=1 origin <commit>
     ```

     Require `FETCH_HEAD` to equal the selected runtime commit. Compute the
     package path, `uv.lock` SHA-256, and optimizer skill path from that fetched
     tree, not from unpushed local files. A pushed runtime branch or tag is
     sufficient; a GitHub Release is optional.
   - If no compatible runtime commit is remotely fetchable, stop with the
     inspected repository and refs plus the instruction for the plugin
     maintainer to publish a compatible runtime. Do not ask the owner to supply
     runtime provenance or choose among provenance sources.
2. Read all files under [references](references/).
3. Use files under [templates](templates/) as editable starting points, not as
   blind replacements.
4. Validate registry and sidecar output against [schemas](schemas/).

`release.json` intentionally does not select an Azure Developer CLI version or
an `azure.ai.agents` extension version.

## Approved package-feed fallback

Direct public package feeds can be unreachable in restricted environments.
During discovery, inspect existing package-source configuration and probe the
sources needed by the repository without restoring or installing packages.

When direct PyPI or NuGet access is unavailable, or repository policy requires
an approved proxy, plan these replacement public sources:

- Python: `https://packagefeedproxy.microsoft.io/pypi/simple`
- NuGet: `https://packagefeedproxy.microsoft.io/nuget/v3/index.json`

For Python, set the proxy as the default source with `UV_DEFAULT_INDEX` or
`PIP_INDEX_URL`; do not add it as an extra index alongside public PyPI. For
NuGet, replace the unreachable public NuGet source while preserving approved
private sources and package-source mapping. Never overwrite existing feed
configuration blindly or commit credentials.

Show the selected sources and every persistent configuration change in the
combined approval. If a source fails after approval and using the proxy was not
approved, stop and prepare a new exact plan rather than switching silently.
Record the sources actually used in the bootstrap report.

## Required process

### 1. Establish a read-only baseline

- Confirm the repository root, current branch, `HEAD`, worktree status, remotes,
  default branch, and GitHub repository identity.
- Resolve and verify retained-runtime provenance from `release.json` or the
  skill's source checkout before rendering repository contracts.
- Stop before planning if unrelated local changes overlap a proposed file or
  prevent an exact clean deployment commit.
- Run `git check-ignore -v --no-index` for every planned registry, report,
  sidecar, workflow, instruction, issue-form, and `azure.yaml` destination.
  Record the exact ignore rule and its source.
- Inventory candidate agent roots, entry points, dependency files, protocols,
  model environment variables, editable paths, existing `azure.yaml`, registry,
  sidecars, workflows, instructions, and issue forms.
- Inventory GitHub environments and variables, Azure identities and federated
  credentials, role assignments, Foundry accounts/projects/model deployments,
  and deployed agents using read-only commands.
- Probe `azd version`, `azd ext list`, `azd ai agent version`, and
  `azd ai agent --help`. Use the installed tools when the required commands are
  available. Otherwise include installation or upgrade from the official
  channel in the approval plan. Do not pin either tool in repository files.
- Inventory Python and NuGet source configuration and determine whether direct
  public feeds or the approved proxy sources will be used.
- Record the actual `azd` and `azure.ai.agents` versions ultimately used.

Follow [Discovery](references/discovery.md) and
[Resource reuse](references/resource-reuse.md).

### 2. Classify agents and migrate contracts

For each discovered agent, propose one of:

- ignored
- registered but disabled
- registered and enabled

Migrate an existing v1 registry or sidecar in place. Preserve its agent IDs,
paths, target bindings, policy, hard guardrails, and complete evaluation bundle
and lineage. Do not create datasets, evaluators, evaluation definitions, or
evaluation runs. Follow [Migration](references/migration.md).

### 3. Render the exact proposed repository

In the session staging area:

- patch `.gitignore` when a repository rule ignores a required tracked file
- create or patch `.foundry-opt/registry.yaml`
- create or patch each `<agent-root>/.foundry/foundry-opt.yaml`
- create or patch `azure.yaml`
- create or patch `.github/workflows/foundry-opt-deploy.yml`
- create or patch `.github/workflows/copilot-setup-steps.yml`
- create or patch `.github/instructions/foundry-opt.instructions.md`
- create or patch
  `.github/ISSUE_TEMPLATE/foundry-optimize-agent.yml`
- create or patch `.foundry-opt/bootstrap-report.md`
- remove `.foundry-opt/bootstrap.lock.json` only when discovery confirms it is
  untracked metadata from retired bootstrap tooling

Preserve unrelated content in existing files. Never silently replace an
existing workflow, environment, identity, Foundry target, or agent definition.
Validate YAML, validate registry and sidecars with the bundled schemas, and
search the staged tree for unresolved `__TOKEN__` values and secrets.
Re-run `git check-ignore -v --no-index` against every planned tracked path after
rendering. A reviewed `.gitignore` correction must make each path addable;
`git add -f` is not a substitute for resolving the repository contract.

Patch `azure.yaml` to connect to an exact existing Foundry project or to declare
the approved missing project and agent services. Keep existing unrelated
services. Use `azure.ai.agent` services and source-code or container settings
that match each discovered agent.

### 4. Request one combined approval

Show:

- discovery and agent classifications
- actual tool versions already present and any approved install/upgrade action
- exact Python and NuGet sources and any persistent source-configuration change
- exact staged file diffs
- exact GitHub, Azure, and Foundry resources to reuse
- exact missing resources to create, including names, types, scopes, regions,
  OIDC subjects, roles, and deterministic resource IDs where available
- preserved evaluation bundles and the statement that no evaluation assets
  will be created
- exact branch, base commit, paths, commit message, and intended commit
- exact `azd` environment, services, project endpoints, and deployment command

Ask for one explicit approval of the entire plan. A partial answer is not
approval.

### 5. Apply only the approved plan

1. Install or upgrade `azd` or `azure.ai.agents` only if the approved capability
   plan requires it, then record the resulting versions.
2. Apply the staged repository files byte-for-byte and rerun validation.
3. Re-query every remote resource immediately before mutation. Reuse an exact
   match, create a still-missing resource, and stop on drift or conflict.
4. Configure GitHub environments and non-secret variables, Azure identity,
   federated credentials, and least-privilege role assignments from the
   approved plan. Do not store credentials in the repository.
5. Create the approved local commit containing only approved paths.
6. Verify `HEAD` is that commit and the deployment worktree is clean.
7. Select the approved azd environment. Run `azd provision` only for approved
   missing resources declared by `azure.yaml`; otherwise skip it.
8. Run the approved `azd deploy` command from the exact local commit.
9. Verify resulting resource identities and links without changing
   unapproved settings.
10. Complete `.foundry-opt/bootstrap-report.md` with versions, commit, reused
    and created resources, deployments, and remaining work.

### 6. Stop safely on failure

Do not undo successful work. Do not continue to a dependent step. Update the
report and owner response with:

- last successful step
- completed local and remote changes
- failed command or API action and concise error
- pending actions that were not attempted
- current local commit and worktree status
- links or immutable IDs for resources that now exist

Follow [Failure handling](references/failure-handling.md).

## Templates and schemas

See [Template map](templates/README.md) for destination paths and editing rules.
The schemas describe repository contracts, not the `azure.yaml` provider
surface. Validate `azure.yaml` with the installed `azure.ai.agents` capability
before approval and again before deployment.
