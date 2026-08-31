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
7. missing cloud resources that will be created
8. conflicts that prevent safe progress
9. the exact local commit and ordered selected-service `azd deploy` plan

The inventory and approval link to the current optimizer-readiness guide:
https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/make-agent-optimizer-ready
The skill does not copy that guide into its own instructions.

The owner gives one combined approval. If the plan changes, the coding agent
shows a fresh exact diff and asks again.

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
- The optimizer skill and runtime are installed from exact retained provenance.
- A local commit is created and deployed with `azd deploy`.
- No branch or tag is pushed.
- `.foundry-opt/bootstrap-report.md` records what happened and the actual tool
  versions.

Rerun `/foundry-bootstrap` to scan another folder scope or onboard another
subset, including a group targeting a different Foundry project.

If a step fails, the coding agent stops and reports completed, failed, and
pending work without deleting successful changes.
