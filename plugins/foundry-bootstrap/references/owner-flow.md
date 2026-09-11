# Owner flow

Invoke the skill from the repository root:

```text
Use /foundry-bootstrap to bootstrap this repository.
```

Each run scans one folder scope and can onboard one or many descendant agents
into one shared Foundry project. Before cloud discovery, the coding agent:

1. asks the owner to confirm one repository-relative scan scope
2. shows a grouped summary and writes the complete numbered inventory to
   session-only Markdown and CSV artifacts
3. asks the owner for `all`, an `exclude` number/range expression, or an `only`
   number/range expression, then confirms the resulting subset
4. asks the owner to confirm one Microsoft Foundry project endpoint shared by
   the selected agents

Repository and cloud evidence may be shown as suggestions, but the coding agent
does not select the scope, agent subset, or endpoint. A parent folder is valid;
the owner is never forced to choose one descendant before seeing the discovered
list.

The coding agent then inspects only the selected agents plus shared
repository and identity configuration. It prepares proposed files in its
session staging area and shows one review containing:

1. the confirmed scope, selected and excluded agents, and shared endpoint
2. selected-agent registration, optimizer readiness, remediation, and
   deployment states
3. existing registry entries and services that remain unchanged
4. exact repository diffs
5. exact patch SHA-256 and successful `git apply --check --index` result
6. cloud resources that exactly match and will be reused
7. missing cloud resources that will be created, plus the Copilot runtime mode
   and any change from pinned execution to tracking the runtime's main branch
8. conflicts that prevent safe progress
9. the exact local commit and ordered selected-service `azd deploy` plan

When the plan creates a user-assigned managed identity, the review shows the
static patch without `identity.client_id`, the exact approved identity ARM
resource ID, and the sole late-binding rule. After creation, only the returned
client ID may be added to the registry and approved Agents/Actions variable
destinations without another approval; the final patch hash is recorded before
apply.

The review distinguishes repository-level Agents variables for Copilot cloud
sessions from Actions variables used by normal workflows. Bootstrap verifies
the tenant, subscription, and optimizer client ID in the Agents store before
marking cloud-agent configuration complete, while preserving Actions values.

The inventory and approval link to the current optimizer-readiness guide:
https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/make-agent-optimizer-ready
The skill does not copy that guide into its own instructions.

The owner gives one combined approval. If the plan changes, the coding agent
shows a fresh exact diff and asks again.

## Manual allowlist changes

The review and report must show **Allowlist additions** with the repository,
exact entries in a copy-ready block, each entry's purpose, and who must save
them. The owner does not need to derive hostnames or investigate inherited
rules. Existing coverage is listed separately; package-feed entries appear only
when required by the approved cloud-session configuration.

Entries are proposed, not permission to act, until the combined approval.
After approval, if automated saving is unavailable, the coding agent says so
and directs the owner to **Settings > Copilot > Internet access > Copilot cloud
agent > Custom allowlist**. Add each approved entry with **Add rule**, then
**Save changes**, preserving existing rules and leaving the firewall enabled.
The coding agent reads the saved configuration back after the owner's
confirmation. If there are no additions, no manual action is requested.

## Expected result

- `.foundry-opt/registry.yaml` is version 2 and contains exact `foundry-opt`
  provenance from the published skill or its remotely fetchable source
  checkout.
- Every selected agent has a version 2 sidecar; existing unselected sidecars
  remain unchanged.
- Existing evaluation bundles and lineage remain unchanged; bootstrap creates no
  evaluation assets.
- `azure.yaml` connects all selected agents to the confirmed shared Foundry
  project while preserving other project and agent services.
- GitHub workflows use OIDC and the reviewed identity without static Azure
  credentials.
- Main-tracking Copilot sessions resolve the latest upstream main once and load
  the matching runtime skill through a committed stable loader. The actual
  runtime SHA stays fixed within the session; deployment retains its saved pin.
- Existing pinned repositories keep their exact-copy behavior unless a mode
  migration is approved.
- A local commit is created and deployed with `azd deploy`.
- No branch or tag is pushed.
- `.foundry-opt/bootstrap-report.md` records what happened and the actual tool
  versions.

Rerun `/foundry-bootstrap` to scan another folder scope or onboard another
subset, including a group targeting a different Foundry project.

If a step fails, the coding agent stops and reports completed, failed, and
pending work without deleting successful changes.
