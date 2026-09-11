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
- Commit the optimizer project skill entry point under
  `.github/skills/foundry-agent-optimizer` so Copilot cloud agent discovers it
  before processing an issue. For main-tracking sessions use the stable runtime
  loader; for pinned sessions use the exact runtime skill copy. Do not rely on
  setup-time installation under the runner home directory.
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
   - For main-tracking setup, also require the fetched runtime to accept
     `github.copilot_runtime: main` in the staged registry. Fetch the configured
     runtime repository's actual `refs/heads/main` during discovery and confirm
     that it supports session-resolved provenance and the loader contract. An
     older compatible offline-only runtime is not sufficient for this mode.
2. Read all files under [references](references/).
3. Use files under [templates](templates/) as editable starting points, not as
   blind replacements.
4. Validate registry and sidecar output against [schemas](schemas/).

`release.json` intentionally does not select an Azure Developer CLI version or
an `azure.ai.agents` extension version.

## Runtime selection at Copilot startup

For newly bootstrapped repositories, configure `github.copilot_runtime: main`.
Include that choice in the combined approval: each fresh Copilot cloud session
will trust the latest commit on the configured runtime repository's `main`,
including future code, dependency, and optimizer-skill changes without a
customer-repository repin. This does not mean the latest successful CI run.
Preserve an existing explicit pinned choice unless its change is approved;
an omitted setting remains `pinned` for backwards compatibility.
For an older generated setup with no mode, propose the main-mode migration in
the combined approval, including its loader and workflow changes. Ensure the
retained pin can parse the new setting; include a one-time compatible repin
when necessary so ordinary Actions and deployment do not use an older parser.

Setup fetches `refs/heads/main` once per fresh `dynamic` session into a separate
runner-temporary checkout, detaches at the resolved SHA, and derives the
`uv.lock` digest from that same tree. Install with `uv sync --frozen`. Record
the actual SHA in setup output and `FOUNDRY_OPT_RUNTIME_SHA`; export the exact
checkout, package, and skill paths. Never cache away this branch-resolution
step, fetch again during job commands, or silently fall back when `main` is
unavailable or incompatible.

Keep `distribution.pin` and its lock digest as recorded immutable provenance
for deployment and ordinary Actions/local execution. Do not rewrite the
customer registry, project skill, Git index, or commits at session startup.
Runtime validation resolves the approved session provenance in memory. Job
state records the selected SHA and rejects resuming with a different runtime
commit; an existing session stays on its original SHA even if `main` advances.

For main mode, commit [optimizer-runtime-skill.md](templates/optimizer-runtime-skill.md)
as `.github/skills/foundry-agent-optimizer/SKILL.md`. This stable loader validates
the runtime checkout, reads the optimizer skill from its verified source path,
and follows that revision's instructions and references. Remove only a previous
exact skill copy's files as part of the approved loader migration. Do not replace
tracked skill files during setup or rely on a home-directory skill install.

For pinned mode, copy the exact optimizer skill from the verified runtime
checkout and set the setup template's `runtime_mode="pinned"`. The workflow mode
and registry setting must agree. The recorded pin, lock digest, and recursive
skill comparison remain authoritative in that mode. Existing deployments do
not opt into main tracking when Copilot does.

## GitHub Agents variables

Copilot cloud agent receives configuration from the dedicated **Agents**
secrets and variables store, not from GitHub Actions variables in the
`copilot` environment. The settings page is
**Settings > Secrets and variables > Agents > Variables**.
Follow [GitHub's configuration guide](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/customize-cloud-agent/configure-secrets-and-variables).

Include these non-secret repository-level Agents variables in bootstrap:

| Variable | Approved source |
| --- | --- |
| `AZURE_TENANT_ID` | Verified tenant of the optimizer identity |
| `AZURE_SUBSCRIPTION_ID` | Verified subscription of the selected Foundry project |
| `AZURE_OPTIMIZER_CLIENT_ID` | Verified optimizer identity client ID; use the exact name in `github.client_id_variable` if customized |

Inventory Agents variables separately from Actions repository/environment
variables. Do not assume automatic migration from the `copilot` environment.
Keep existing Actions variables for ordinary setup and deployment workflows;
do not move or delete them, or substitute the deployment identity for the
optimizer identity. Include any additional required cloud-session configuration,
such as an approved package-feed URL, in the same store-specific review.

Use the Agents REST API through `gh api`, as described in
[Discovery](references/discovery.md). A successful `gh variable list --env
copilot` proves only Actions configuration, not Agents configuration.
Show each destination store, repository/environment scope, variable name, and
non-secret value or approved client-ID binding in the combined approval.
Create missing Agents variables only after approval, reuse exact matches, and
stop on conflicting or unreadable values rather than overwriting them.

Read back each required Agents variable at its recorded scope after
configuration and compare its effective value with the approved value. Reuse
verified inherited matches without changing organization-wide settings. If the
API is unavailable or permissions prevent verification, report that blocker and
the settings-page path; do not treat Actions values as a fallback or declare the
cloud environment ready.

In `copilot-setup-steps.yml`, job-level `env` and `environment` are not supported
cloud-agent customization fields. Keep `environment` only when needed for the
workflow's ordinary Actions runs; do not rely on it to inject cloud-session
variables. Agents variables are exposed directly to the cloud process.
Offline preflight or a successful ordinary Actions setup run does not prove
that the cloud session received them. After changing Agents variables, use a
fresh Copilot session; keep existing OIDC credentials and broker setup intact.

## Copilot cloud-agent internet access

Treat outbound network access as a separate bootstrap prerequisite from Agents
variables, OIDC, and successful setup. Before approval, automatically query
`GET /repos/{owner}/{repo}/copilot/cloud-agent/configuration` through `gh api`,
using the command and API version in [Discovery](references/discovery.md).
Do this before requesting screenshots or manual settings evidence. Retain the
returned firewall flags and custom rules as repository configuration evidence.
The settings UI is **Settings > Copilot > Internet access**, in the **Copilot
cloud agent** section, not code review.
Follow [GitHub's firewall guide](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/customize-the-firewall).

Derive the required destinations from the confirmed Azure and agent bindings:

- the verified Azure authority hostname (`login.microsoftonline.com` for Azure
  public cloud)
- the hostname parsed from the confirmed Foundry project endpoint, without its
  scheme or `/api/projects/...` path; never copy a sample account hostname
- additional package, storage, or service destinations only when evidence shows
  that the selected agent's cloud-session commands require them

Reuse coverage established by known matching rules or the enabled recommended
allowlist's documented entries. Otherwise propose an explicit, narrowly scoped
repository rule for each required destination. Show the observed repository
custom allowlist plus the exact deduplicated additions in the combined approval.
An explicit rule may duplicate unknown inherited coverage; it is still bounded
by the approved destinations. Do not claim a host is blocked merely because it
is absent from the returned custom list.
Preserve unrelated rules and rules for previously onboarded projects on re-entry.
Keep the firewall enabled; do not add broad wildcard rules, disable network
controls, or move runtime Azure calls into setup steps to bypass the firewall.
If the firewall is already disabled, report the existing deviation rather than
silently changing it or treating unrestricted access as an allowlist match.

Do not ask the owner to repeat settings already returned by the API. It does
not separately expose inherited organization rules or permission to add
repository rules. Record unavailable inheritance and edit permission as
`unknown`, not `none` or `allowed`. Do not ask the owner to investigate inherited
rules or confirm edit permission during discovery. Unknown inheritance or edit
permission does not block approval of exact additive repository rules when the
repository configuration is readable. This narrow exception does not permit
overwriting unreadable repository settings or ignoring known organization
restrictions. A failed API read still requires a conclusive repository
configuration read before approving its changes, not an organization-policy
questionnaire.

For approved writes, use a documented write API only if available; the GET
endpoint does not establish write support. Otherwise use **Custom allowlist >
Add rule > Save changes**, with owner assistance if tools cannot access the
page. Do not invent a write API or use Actions/Agents variables as a replacement
for Internet access settings. If organization policy forbids repository custom
rules, identify the required organization-administrator action; do not change
organization policy implicitly.

After approval, re-read the repository configuration, preserve existing rules,
and add only approved entries not already covered. Verify the saved repository
rules and firewall flags through the read API without requiring an inheritance
inventory. Request owner assistance only to perform approved UI-only changes;
request administrator help only when a known restriction or an actual attempt
blocks application. Never probe write permission by mutating during discovery.
If an approved change cannot be applied, report it as pending and stop rather
than declaring setup complete.
On re-entry, reuse completed rules and resume the remaining approved work.

Keep saved firewall configuration separate from cloud-runtime connectivity
evidence. Local deployment, offline preflight, and even online requests in
setup steps do not prove access from agent-issued commands: the cloud firewall
does not cover setup processes in the same way. At handoff, direct the owner to
start a fresh Copilot session and run online
`foundry-opt preflight --repository . --repo-agent-id <selected-id>` before
optimization. Record this as `not attempted` until actual session evidence is
available; do not start optimization jobs or evaluation runs merely to test
network access. Report DNS/firewall blocks separately from Azure authentication
or RBAC failures.

### Allowlist owner handoff

When additions are needed, show **Allowlist additions** in the combined approval
and bootstrap report. Repeat the same approved entries when manual action is
needed; do not ask the owner to derive them. Include:

- the target repository URL and **Settings > Copilot > Internet access >
  Copilot cloud agent > Custom allowlist**
- a copy-ready text block containing only the exact entries to add, one per
  line, with no placeholders, bullets, or explanatory text inside the block
- each entry's purpose outside the block: Azure sign-in, access to the confirmed
  Foundry project, or an evidenced cloud-session dependency; include
  `packagefeedproxy.microsoft.io` only when the approved cloud-session package
  source requires it
- who must act and the current status: proposed, awaiting manual save, or
  confirmed saved; state explicitly when available tools can read settings but
  cannot save them

Use actual resolved hostnames, never an example account name or the full
Foundry project URL as a domain entry. Omit entries with established coverage
from the additions block and list them separately as already covered. If no
additions remain, say **No allowlist additions required** and skip manual work.

Before approval, label the entries **Proposed - do not apply yet**. After
approval, if manual action is needed, instruct the owner to add each approved
entry separately with **Add rule**, then click **Save changes**. Preserve
existing rules and keep the firewall enabled. Ask the owner to confirm saving,
then re-query the configuration API; confirmation alone is not saved-rule
evidence. Keep an unsaved or blocked action pending rather than marking it done.

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
- Independently inventory the dedicated Agents variable store and compare the
  required optimizer values with the selected identity and project.
- Read repository cloud-agent Internet access settings and derive the required
  destinations from the confirmed project and Azure authority. Record
  unavailable inheritance and edit permission as unknown without prompting.
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
- create the main-mode project skill loader, or copy the exact optimizer skill
  from the verified runtime checkout to `.github/skills/foundry-agent-optimizer`
  for pinned mode, preserving every file and byte
- create or patch `.github/instructions/foundry-opt.instructions.md`
- create or patch
  `.github/ISSUE_TEMPLATE/foundry-optimize-agent.yml`
- create or patch `.foundry-opt/bootstrap-report.md`
- remove `.foundry-opt/bootstrap.lock.json` only when discovery confirms it is
  untracked metadata from retired bootstrap tooling

Preserve unrelated content in existing files. Never silently replace an
existing workflow, environment, identity, Foundry target, or agent definition.
Treat `.github/skills/foundry-agent-optimizer` as either the stable loader or an
exact runtime-derived directory according to the approved mode. For pinned
mode, compare it recursively with the verified source. Replace only that skill
directory during an approved mode change.
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
- the materialized values for the exact approved GitHub client-ID variable
  destinations in Agents and Actions; the approval must name each store and
  scope explicitly

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
- Copilot runtime mode, its upstream repository and trust implications, the
  retained deployment pin, and the project-skill loader or exact-copy diff
- exact staged file diffs
- static patch SHA-256 and the successful index-aware preflight
- any approved managed-identity client-ID late-binding rule, including its
  exact resource ID and sole allowed registry field
- exact GitHub, Azure, and Foundry resources to reuse
- required Agents variables and separate Actions variables, with exact
  destinations, names, values, and any approved identity client-ID binding
- observed cloud-agent firewall settings, existing repository rules, exact
  additive repository allowlist rules and their purpose, unknown inheritance
  or edit permission, and any already-known administrator restriction
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
   Configure the required repository-level Agents variables separately from
   Actions variables, and read back each store-specific value before marking
   cloud-agent configuration complete.
   Apply approved cloud-agent allowlist additions without replacing existing
   rules, and verify saved repository rules and firewall flags. Unknown
   inheritance is not a read-back prerequisite. Stop and report pending work
   if the approved additions cannot be applied, including any administrator
   action required by an actual permission failure or known restriction.
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
    repository firewall configuration, unknown inheritance where applicable,
    separate cloud-session preflight evidence,
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
