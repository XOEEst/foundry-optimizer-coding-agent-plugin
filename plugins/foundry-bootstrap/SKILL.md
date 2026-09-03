---
name: foundry-bootstrap
description: Incrementally bootstrap one or more repository agents from a user-confirmed folder scope into one shared Microsoft Foundry project, with one combined approval and standard tools.
---

# Foundry bootstrap

Use this static skill to prepare a repository for Foundry agent optimization and
hosted-agent deployment. The package contains guidance, editable templates, and
JSON schemas only. Do not look for or run a bundled bootstrap program.

## Non-negotiable behavior

- A repository containing only agent code is a valid input. Scan one
  user-confirmed folder scope per run, let the user select any recognized
  descendant agents, and generate or extend the registry, per-agent profiles,
  workflows, issue form, instructions, report, and deployment manifest.
- Perform discovery with read-only repository, Git, GitHub, Azure, and Foundry
  commands.
- Render every proposed repository file in the coding session's staging area.
  Do not modify the repository or a remote service during discovery.
- Commit the exact optimizer project skill under
  `.github/skills/foundry-agent-optimizer` so Copilot cloud agent discovers it
  before processing an issue. Do not rely on setup-time installation under the
  runner home directory.
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

Do not require or generate repository-global legacy optimizer policy or agent
metadata files. Registry v2 and each selected agent's sidecar are the complete
repository configuration interface.

## Mandatory onboarding group

Handle one onboarding scope per run. Before runtime resolution, cloud
inventory, classification, or proposal rendering:

1. Ask: **Which repository-relative folder should this run scan for agents?**
   The answer may be one agent root, a parent containing many agents, or `.` for
   the repository root.
2. Perform a read-only scan only within that confirmed scope. List every
   recognized deployable agent with a stable proposed ID, exact source root,
   package root, language/runtime, entry point, protocol, and recognition
   evidence.
3. Ask the user to confirm all recognized agents or list exact agents to
   exclude. Do not treat discovery as selection. Stop if the resulting subset
   is empty.
4. Ask: **Which one shared Microsoft Foundry project endpoint should the
   selected agents target?**

Existing registry entries, sidecars, `azure.yaml`, azd values, and Foundry
metadata may provide endpoint suggestions. If all selected existing agents
already use one endpoint, present it for confirmation. Suggestions and existing
bindings are not answers. Do not infer or silently select the folder, agent
subset, or endpoint, even when only one candidate or endpoint is found.

If the initial user prompt already states a folder scope or project endpoint,
retain it as a proposed answer but still show the recognized agent list and ask
the user to confirm the final scope, subset, and endpoint. Ask one question at a
time. Reject a folder outside the repository, a file instead of a directory, or
an endpoint that does not match the Foundry project endpoint format. Never ask
the user for an ARM resource ID.

All selected agents in one run share the confirmed project endpoint. If their
existing sidecars target different endpoints, require the user to unselect
agents or explicitly approve retargeting them to the one confirmed endpoint.

The confirmed scope, selected subset, and endpoint are session input, not
durable bootstrap state. A later run asks again and may scan another folder,
select another group, and use another project endpoint.

### Readable inventory and selection

Sort recognized agents deterministically by repository-relative source root and
assign session-only row numbers starting at `1`. Group the inventory by
immediate child folder, framework, and language/runtime. Show a compact group
summary in chat with each group's count and row range.

Write the complete inventory to these session-only artifacts:

- `foundry-bootstrap-agent-inventory.md` - full readable table
- `foundry-bootstrap-agent-inventory.csv` - the same rows for filtering

Each row includes its number, proposed stable ID, source root, package root,
manifest/service, language/runtime, entry point, protocol, optimizer readiness,
and recognition evidence. Show the artifact paths and the complete list in
pages when the user asks. Never add these session-only artifacts to the target
repository, patch, commit, or bootstrap report.

Default selection is all recognized agents. Accept `all`, `exclude 4,8-12`, or
`only 2-20,31`; also accept exact proposed IDs in place of row numbers.
Validate every number, range, and ID against the unchanged inventory before
continuing. Reject ambiguous or unknown selectors and show the relevant group
and rows again. After parsing, show selected and excluded counts plus the
selected IDs, then ask the user to confirm the resulting subset before asking
for the shared endpoint.

### Optimizer readiness

During read-only inventory, consult the current Microsoft guide:

https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/make-agent-optimizer-ready

Do not embed or paraphrase the guide in this skill. Use it at runtime as the
external readiness authority and include only the link in owner-facing
inventory, approval, and report output.

Classify every recognized agent as `ready`, `not ready`, or `unknown`, with
concise repository evidence. For each selected not-ready agent, stage exact
optimizer-readiness changes in the session proposal, following its framework
and repository conventions, and include the smallest existing build or test
commands that validate those changes. These edits are part of the same patch,
combined approval, commit, and deployment plan.

Do not invent readiness changes when the guide is unavailable, the framework is
unsupported, or required behavior cannot be established from source. Mark the
agent `unknown` and require the user to exclude it or provide enough
information for a new plan. Readiness work must not create evaluation
datasets, evaluators, definitions, or runs.

## Policy scope and deployment versions

Interpret repository policy in its stated lifecycle and actor scope:

- Optimize-job draft-only rules do not prohibit bootstrap deployment. Rules
  about Copilot candidates, optimize jobs, temporary validation, early draft
  pull requests, or optimizer-owned versions govern optimization only.
- An existing regular-version deployment workflow and merge-time deployment
  documentation are affirmative evidence that repository deployment may
  publish regular versions.
- Bootstrap and deployment prohibitions apply only when they explicitly cover
  bootstrap, deployment, merge-time publication, or all agent publication and
  are not contradicted by another authoritative repository contract.

Do not promote a scoped optimizer rule into a repository-wide deployment
prohibition. Do not require a draft-capable `azd deploy` extension: the
supported `azd deploy` path publishes a regular immutable hosted-agent version.
If repository evidence is genuinely contradictory, report the exact files and
statements as a policy conflict; do not invent a missing draft-deployment
capability.

## GitHub branch-policy semantics

Repository branch protection and rulesets are optional. Their absence must not
block bootstrap. The generated deployment workflow already restricts its
trigger to the default branch and verifies the current default-branch tip
before publication.

GitHub deployment-environment branch policies are a separate feature. New
GitHub environments default to no deployment branch restriction unless the
user explicitly requests or approves one. Preserve an existing exact
environment policy; do not derive one merely because the repository has a
default branch.

When a custom environment policy is approved:

1. create or update the environment with
   `custom_branch_policies: true`
2. expect GitHub to return one `branch_policy` protection rule representing
   that enabled mode
3. create each approved branch or tag entry through the
   `deployment-branch-policies` endpoint
4. verify the mode and allowed-entry list separately

Do not expect `protection_rules` to remain empty after enabling custom mode.
The `branch_policy` protection rule is expected whenever that mode is enabled.
An empty custom-policy mode is partial, not a conflict: a fresh approved plan
may add the intended entries or disable custom mode. Never confuse repository
branch protection, environment policy mode, and environment allowed-branch
entries.

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
   - Execute the fetched runtime's real no-evaluation compatibility probe.
     Copy its bundled `src/foundry_opt/templates/customer-repo` into a fresh
     temporary directory, confirm the copy contains no repository-global
     legacy policy or metadata files, and initialize that directory as its own
     Git worktree. The fixture is the minimal supported registry v2 sidecar
     with `verification.mode: off` and no evaluation bundle:

     ```text
     git -C <fixture> init
     uv run --frozen --no-dev --project <package-root> foundry-opt preflight \
       --repository <fixture> --offline
     ```

     Use the approved Python feed fallback if dependency restoration cannot
     reach PyPI. A zero exit code is authoritative for this compatibility
     question. Do not infer incompatibility from legacy loaders, compatibility
     fields, old documentation, or the fact that `preflight` also supports
     older repository layouts.
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
- Record the confirmed scan scope, selected agent roots, and shared project
  endpoint as the sole onboarding target for this run.
- Resolve and verify retained-runtime provenance from `release.json` or the
  skill's source checkout before rendering repository contracts.
- Stop before planning if unrelated local changes overlap a proposed file or
  prevent an exact clean deployment commit.
- Run `git check-ignore -v --no-index` for every planned registry, report,
  sidecar, workflow, instruction, issue-form, and `azure.yaml` destination.
  Record the exact ignore rule and its source.
- Inspect each selected agent's entry points, dependency files, protocols,
  model environment variables, editable paths, and existing sidecar. Inventory
  shared registry, `azure.yaml`, workflows, instructions, and issue forms only
  as needed to extend them safely.
- Fetch the optimizer-readiness guide once, record its URL, classify every
  recognized agent in the inventory, and prepare remediation only for selected
  not-ready agents.
- Summarize other candidate or registered folders but do not classify, edit,
  retarget, enable, disable, or deploy them in this run.
- Classify every draft/regular-version statement by lifecycle: optimizer,
  bootstrap, merge-time deployment, or repository-wide publication. Compare
  prose with active deployment workflow behavior before declaring a conflict.
- Inventory GitHub environments and variables plus the Azure identities,
  federated credentials, and role assignments needed by the selected agents.
- Record repository branch protection separately from each environment's
  deployment policy mode and allowed branch/tag entries.
- Resolve the confirmed endpoint to exactly one Foundry project, then inventory
  only that project's account, ARM resource ID, model deployments, and matching
  deployed agents. If it does not resolve uniquely, ask the user to correct the
  endpoint or Azure login; never substitute a different project.
- Retain the selected project's full ARM resource ID as
  `AZURE_AI_PROJECT_ID` in the deployment plan. An endpoint alone is
  insufficient for an existing-project `azd deploy`.
- Probe `azd version`, `azd ext list`, `azd ai agent version`, and
  `azd ai agent --help`. Use the installed tools when the required commands are
  available. Otherwise include installation or upgrade from the official
  channel in the approval plan. Do not pin either tool in repository files.
- Inventory Python and NuGet source configuration and determine whether direct
  public feeds or the approved proxy sources will be used.
- Record the actual `azd` and `azure.ai.agents` versions ultimately used.

Follow [Discovery](references/discovery.md) and
[Resource reuse](references/resource-reuse.md).

### 2. Classify selected agents and migrate their contracts

For each user-selected agent, propose one of:

- registered but disabled
- registered and enabled
- not onboarded because that recognized agent is unsupported or the user stops

If an agent is already registered, preserve its stable agent ID and config
path. Treat the user-confirmed endpoint as an explicit reconciliation target:
an exact endpoint is reused; a different endpoint is shown as a retargeting
change and never applied silently.

Create or migrate only the selected agents' registry entries and sidecars.
Preserve every unselected registry entry and sidecar byte-for-byte, including
its enabled state, target binding, policy, hard guardrails, evaluation bundle,
and lineage. Do not create datasets, evaluators, evaluation definitions, or
evaluation runs. Follow [Migration](references/migration.md).

### 3. Render the exact proposed repository

In the session staging area:

- patch `.gitignore` when a repository rule ignores a required tracked file
- create or patch `.foundry-opt/registry.yaml` only for selected entries
- create or patch only each
  `<selected-agent-source-folder>/.foundry/foundry-opt.yaml`
- create or patch `azure.yaml`
- create or patch `.github/workflows/foundry-opt-deploy.yml`
- create or patch `.github/workflows/copilot-setup-steps.yml`
- copy the exact optimizer skill from the verified runtime checkout to
  `.github/skills/foundry-agent-optimizer`, preserving every file and byte
- create or patch `.github/instructions/foundry-opt.instructions.md`
- create or patch
  `.github/ISSUE_TEMPLATE/foundry-optimize-agent.yml`
- create or patch `.foundry-opt/bootstrap-report.md`
- remove `.foundry-opt/bootstrap.lock.json` only when discovery confirms it is
  untracked metadata from retired bootstrap tooling

Preserve unrelated content in existing files. Never silently replace an
existing workflow, environment, identity, Foundry target, or agent definition.
Treat `.github/skills/foundry-agent-optimizer` as an exact runtime-derived
directory: compare it recursively with the verified source and replace only
that skill directory when updating its pinned runtime.
Validate YAML, validate registry and sidecars with the bundled schemas, and
search the staged tree for unresolved `__TOKEN__` values and secrets.
Re-run `git check-ignore -v --no-index` against every planned tracked path after
rendering. A reviewed `.gitignore` correction must make each path addable;
`git add -f` is not a substitute for resolving the repository contract.

Render generated YAML, Markdown, and other text as UTF-8 without BOM and LF
line endings. Build tracked-file context from Git index blobs rather than
platform-normalized worktree bytes. Respect an explicit `.gitattributes`
binary or `-text` rule instead of converting that path.

Create one immutable static patch artifact for all reviewed tracked changes.
Use `a/` and `b/` repository-relative paths, LF patch control lines, and no
absolute staging paths. Before approval, from the clean target repository run:

```text
git apply --check --index --whitespace=error-all <patch>
```

Calculate and retain the SHA-256 of the exact patch bytes. Do not request
approval unless that exact command succeeds against the recorded base `HEAD`
and index. If it fails, correct the staged representation, regenerate the
patch, and rerun every validation. Never substitute a worktree-only
`git apply --check`.

#### Bounded late-binding exception

When no reusable identity exists and the approved plan creates a
user-assigned managed identity, its ARM resource ID is deterministic but Azure
generates its client ID only during creation. This is the only bounded
late-binding exception to the exact static patch rule.

Before approval:

- include the exact identity type, name, subscription, resource group,
  location, and full ARM resource ID in the remote plan
- include that exact resource ID and identity kind in the registry
- omit `identity.client_id` from the static patch; do not use a placeholder
- show the static patch SHA-256
- state that the final patch may differ only by `identity.client_id`, whose
  value must come from the exact approved identity resource

The combined approval explicitly approves this deterministic substitution and
does not require a second approval. It does not authorize late binding of any
other repository value.

After approval, create only that missing identity first. Read it back by its
exact ARM resource ID and require matching subscription, resource group, name,
location, tenant, and type plus nonempty GUID-form client and principal IDs.
Insert the returned client ID into:

- `.foundry-opt/registry.yaml` at `identity.client_id`
- the materialized value for the exact approved GitHub client-ID variable

Generate the final patch from the unchanged static proposal plus that single
registry field. Verify that no other path or value changed, validate all
schemas, rerun the secret/token scan and index-aware `git apply --check`, and
record the final patch SHA-256. Stop on any mismatch; leave the created identity
in place and report the partial state.

Patch `azure.yaml` to connect to an exact existing Foundry project or to declare
the approved missing project and selected agent services. Reuse one existing
project service when its endpoint exactly matches the confirmed shared
endpoint, and make each selected agent service depend on it. Keep every other
project and agent service unchanged. Use source-code or container settings that
match each selected agent.

### 4. Request one combined approval

Show:

- the user-confirmed scan scope, selected and excluded agents, and shared
  Foundry project endpoint
- the inventory artifact paths, selection expression, and final selected IDs
- optimizer readiness for every selected agent, the Microsoft guide link, and
  exact staged remediation and validation for each not-ready agent
- each selected agent classification and all existing entries that remain
  unchanged
- actual tool versions already present and any approved install/upgrade action
- exact Python and NuGet sources and any persistent source-configuration change
- exact optimizer project-skill source, destination, and recursive diff
- exact staged file diffs
- static patch SHA-256 and the successful index-aware preflight
- any approved managed-identity client-ID late-binding rule, including its
  exact resource ID and sole allowed registry field
- exact GitHub, Azure, and Foundry resources to reuse
- for each GitHub environment, one explicit deployment branch mode:
  unrestricted, protected branches, or a custom allowed-entry list
- exact missing resources to create, including names, types, scopes, regions,
  OIDC subjects, roles, and deterministic resource IDs where available
- preserved evaluation bundles and the statement that no evaluation assets
  will be created
- exact branch, base commit, paths, commit message, and intended commit
- exact `azd` environment, selected services, confirmed shared project
  endpoint, and ordered `azd deploy <selected-service>` commands
- exact `AZURE_AI_PROJECT_ID` full ARM resource ID and every azd environment
  value required by the selected services

Ask for one explicit approval of the entire plan. A partial answer is not
approval.

### 5. Apply only the approved plan

1. Install or upgrade `azd` or `azure.ai.agents` only if the approved capability
   plan requires it, then record the resulting versions.
2. Confirm the base `HEAD`, clean worktree, static patch SHA-256, and Git index
   still match the approved review. Rerun
   `git apply --check --index --whitespace=error-all <static-patch>`.
3. Re-query the approved identity. Reuse an exact match. If it is still missing
   and the approved plan includes managed-identity late binding, create only
   that identity, read it back by exact ARM resource ID, and materialize the
   approved client ID field and GitHub variable value.
4. Produce the final patch. Require it to equal the static patch unless the
   approved late-binding rule applies, in which case the final patch may differ
   only by `identity.client_id`. Record the final patch SHA-256, rerun all
   validation, then apply it with:

   ```text
   git apply --index --whitespace=error-all <final-patch>
   ```

   Do not make any other generated-value substitution. Rerun repository
   validation against the staged result.
5. Re-query every remaining remote resource immediately before mutation. Reuse
   an exact match, create a still-missing resource, and stop on drift or
   conflict.
6. Configure GitHub environments and non-secret variables, federated
   credentials, and least-privilege role assignments from the approved plan.
   For custom environment branch policies, enable the mode, create every
   approved entry, and then verify both surfaces. Do not store credentials in
   the repository.
7. Create the approved local commit containing only approved paths.
8. Verify `HEAD` is that commit and the deployment worktree is clean.
9. Select the approved azd environment. For every existing Foundry project,
   set `AZURE_AI_PROJECT_ID` to its verified full ARM resource ID and set the
   approved endpoint, subscription, location, resource group, and referenced
   model values required by `azure.yaml`. Verify the binding before running
   `azd deploy`:

   ```text
   azd env get-value AZURE_AI_PROJECT_ID
   ```

   Require an exact match with the approved project ID. Run `azd provision`
   only for approved missing resources declared by `azure.yaml`; otherwise
   skip it.
10. For each selected enabled agent, run
   `azd deploy <selected-service>` from the exact local commit in the approved
   order. Stop on the first failure and do not deploy remaining selected or
   unselected services. Never treat an endpoint-only azd environment as
   sufficient for an agent service that depends on an existing
   `azure.ai.project` service.
11. Verify resulting resource identities and links without changing
   unapproved settings.
12. Complete `.foundry-opt/bootstrap-report.md` with versions, commit, reused
    and created resources, scan scope, selected and excluded agents, shared
    endpoint, per-agent deployment results, all previously onboarded entries,
    and remaining work.

At successful handoff, tell the user to rerun `/foundry-bootstrap` for another
folder scope. Every later run repeats scope and subset confirmation, resolves
one shared endpoint, and extends the existing repository contract instead of
replacing it.

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
