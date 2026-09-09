# Read-only discovery

Complete discovery before preparing a mutation plan. Start after the user
confirms one folder scope, then pause again for agent-subset confirmation
before resolving one shared Foundry project endpoint.

## Confirmed target scope

- Require the user to select one repository-relative scan scope. It may be an
  agent root, a parent containing many agents, or the repository root.
- Within that scope, recognize deployable agents from concrete evidence such
  as an `azure.ai.agent` service, agent manifest, or executable entry point plus
  dependency/runtime configuration. Do not classify nested skill folders,
  tests, clients, or infrastructure fixtures as agents without deployment
  evidence.
- List every recognized agent deterministically with its exact root and
  evidence, a session row number, and optimizer-readiness status.
- Group the chat summary by immediate child folder, framework, and
  language/runtime. Put the complete rows in session-only Markdown and CSV
  inventory artifacts.
- Accept `all`, `exclude` number/ranges, or `only` number/ranges, validate the
  expression against the unchanged inventory, and require confirmation of the
  resulting subset.
- Treat existing endpoints as suggestions and require one shared endpoint for
  the final selected subset.
- If selected agents are already registered, preserve their stable IDs and
  show their current endpoints before endpoint confirmation.

Assess readiness against
https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/make-agent-optimizer-ready.
Fetch the current guide during the run; do not copy its contents into the
skill. Record only `ready`, `not ready`, or `unknown`, concise source evidence,
and the guide link. Deeply inspect and plan remediation only for selected
not-ready agents.

## Repository and Git

- Resolve the repository root and canonical GitHub remote.
- Record the current branch, exact `HEAD`, default branch, worktree status, and
  existing untracked files.
- Read the existing registry, selected sidecars, `azure.yaml`, dependency
  files, workflows, instructions, and issue form.
- For each selected agent, locate entry points and determine source root,
  package root, runtime, dependency restoration, protocol, CPU/memory settings,
  model environment variable, and paths safe for optimizer edits.
- Detect shared source among selected and registered agents and keep all
  existing agent IDs stable.

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

For retained `foundry-opt` runtime compatibility, execute the exact fetched
runtime against its bundled registry-v2/no-evaluation customer template. Do
not classify compatibility by source inspection. A runtime may intentionally
support both legacy metadata and current registry/profile contracts.

If the isolated dependency restore fails, classify that as a package-feed
failure and apply the approved feed fallback. Only a completed preflight with a
nonzero exit is runtime incompatibility.

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
- each environment's deployment branch-policy mode and separate custom
  branch/tag entry list
- Actions environment and repository variables
- dedicated Agents repository variables and organization variables available to
  this repository
- existing deployment and Copilot setup workflows

Variable values that GitHub does not return must be treated as unknown, not as
an exact match or a missing variable. Resolve unknown state before approval.
Propose creation only for confirmed missing variables whose desired non-secret
values are established from Azure inventory.

### Agents versus Actions variables

Use [the Agents variables REST API](https://docs.github.com/en/rest/agents/variables)
with `gh api` and the supported `X-GitHub-Api-Version: 2026-03-10` header:

```text
GET repos/{owner}/{repo}/agents/variables
GET repos/{owner}/{repo}/agents/organization-variables
GET repos/{owner}/{repo}/agents/variables/{name}
POST repos/{owner}/{repo}/agents/variables
```

Paginate list results. POST creates a missing repository variable with `name`
and `value` fields and is allowed only after the combined approval. Read back
each created variable with GET; do not mistake an Actions variable with the
same name for an Agents variable.

Inventory `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, and the client-ID variable
named in `registry.github.client_id_variable` (normally
`AZURE_OPTIMIZER_CLIENT_ID`). Resolve their values from the approved optimizer
identity and Foundry project, never from an unrelated deployment identity.
Repository Agents values override organization Agents values. Reuse a matching
inherited value only when its effective value and repository access are
verified; do not change organization-wide configuration for this bootstrap.
Record any same-name secret shadowing as unknown rather than exposing a secret
or claiming the effective value matches.

An empty successful list is missing configuration. Authentication, permission,
and API-availability errors are unknown state, not evidence that a store is
empty. Stop and point to **Settings > Secrets and variables > Agents >
Variables** if the required values cannot be established. Preserve existing
Actions variables used by normal workflows. Do not rely on setup job-level
`env` or `environment` to supply cloud-agent variables.

No repository branch protection is a valid exact state. For environments,
distinguish `deployment_branch_policy` from `protection_rules` and from
`GET .../deployment-branch-policies`. Enabling custom mode itself produces a
`branch_policy` protection rule even before any allowed entry exists.

### Cloud-agent firewall inventory

First call the documented
[repository cloud-agent configuration API](https://docs.github.com/en/rest/copilot/copilot-cloud-agent-management#get-copilot-cloud-agent-configuration-for-a-repository)
with the verified repository owner/name. This is a read-only public-preview
endpoint; do not defer discovery to the owner without attempting it.

```powershell
gh api --method GET -H "Accept: application/vnd.github+json" `
  -H "X-GitHub-Api-Version: 2026-03-10" `
  "repos/{owner}/{repo}/copilot/cloud-agent/configuration" `
  --jq '{is_firewall_enabled, is_firewall_recommended_allowlist_enabled, custom_allowlist}'
```

Record those three fields and the query outcome. Project only firewall fields;
do not dump unrelated MCP configuration, which can contain credentials.
Preserve boolean `false` and empty arrays as returned values, but treat missing,
null, or malformed fields as unknown. A `401`, `403`, `404`, or server error
does not mean the firewall is disabled or the allowlist is empty; report the
actual failure before requesting narrowly scoped settings evidence.

The response does not separately expose inherited organization rules or
whether repository custom additions are permitted. Do not infer either from an
empty `custom_allowlist`, or assume a host absent there is blocked: recommended
or inherited rules may cover it. Record unavailable inheritance and edit
permission as `unknown`. Do not ask for inherited rules, matching organization
rules, or permission confirmation during discovery. A valid empty
`custom_allowlist` with unknown inheritance is enough to plan exact repository
additions without an owner question about inheritance.

For each required hostname derived by the skill's Internet access rules, record
its purpose and any known coverage. Reuse known matching rules; otherwise
propose an explicit repository allowance, even if it may duplicate unknown
inherited coverage. Include the Azure authority and the confirmed Foundry
endpoint hostname. Keep the existing custom list intact and show only exact,
deduplicated additions; do not require proof that a host is currently blocked.
Do not interpret the recommended-allowlist flag alone as coverage for every
required host: match its documented entries.

An unknown edit permission is not a reason to query the owner before approval
or to probe it with a write. Apply approved additions through an available
supported write surface; if no automated write surface exists, request the
owner's help with the approved UI action, not inheritance discovery. Escalate
only a known restriction or actual application failure to an administrator,
preserving completed work. Never change organization policy implicitly.
Re-query the repository API after approved changes. Saved repository rules and
flags establish configuration without an inherited-rule inventory; actual
cloud-session online preflight evidence must still be recorded separately.

## Azure and Foundry inventory

Use the current authenticated tenant and subscription. Record them explicitly.
For the confirmed shared endpoint, inspect:

- its Foundry account and project
- the selected project's full ARM resource ID as well as its endpoint
- project endpoints and immutable ARM resource IDs
- model deployments
- hosted agents and versions that could match each selected agent
- user-assigned identities or application registrations
- federated identity credentials
- role assignments at their exact scopes

Prefer immutable IDs and service-returned endpoints over names inferred from
text. If the requested account or project is visible in more than one
subscription, stop and require an unambiguous subscription selection before
planning.

## Classification

For each resource needed by the selected onboarding group:

- **exact** - immutable identity, type, scope, and relevant configuration match
- **missing** - no resource occupies the approved identity/name/scope
- **conflict** - a resource occupies the identity/name/scope but differs
- **unknown** - permissions or API limitations prevent a conclusive read

Only exact and missing resources may appear in an approval plan. A conflict or
unknown result blocks mutation.

For additive repository firewall rules, unknown inheritance and edit
permission are context, not an unknown target resource: a successful
repository configuration read establishes the current list to preserve and the
exact additions to approve. Do not block that plan or query the owner for
inheritance. This exception does not apply to unreadable repository settings,
unknown Azure resources, or known prohibitions.

For an existing project used by an `azure.ai.agent` service, plan
`AZURE_AI_PROJECT_ID=<full-project-ARM-resource-ID>` in the selected azd
environment. Verify the resource with an Azure management-plane read. Do not
infer that `endpoint:` in `azure.yaml` removes this azd environment
requirement.
