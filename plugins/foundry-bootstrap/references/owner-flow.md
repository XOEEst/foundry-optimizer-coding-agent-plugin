# Owner flow

Invoke the skill from the repository root:

```text
Use /foundry-bootstrap to bootstrap this repository.
```

Each run onboards one agent folder. Before broad discovery, the coding agent
asks the owner to confirm:

1. one exact repository-relative agent source folder
2. one exact Microsoft Foundry project endpoint for that folder

Repository and cloud evidence may be shown as suggestions, but the coding agent
does not select either value. If both are supplied in the initial prompt, the
owner confirms the pair before discovery continues.

The coding agent then inspects only that onboarding target plus shared
repository and identity configuration. It prepares proposed files in its
session staging area and shows one review containing:

1. the confirmed folder and endpoint
2. selected-agent registration and deployment state
3. existing registry entries and services that remain unchanged
4. exact repository diffs
5. exact patch SHA-256 and successful `git apply --check --index` result
6. cloud resources that exactly match and will be reused
7. missing cloud resources that will be created
8. conflicts that prevent safe progress
9. the exact local commit and selected-service `azd deploy` plan

The owner gives one combined approval. If the plan changes, the coding agent
shows a fresh exact diff and asks again.

## Expected result

- `.foundry-opt/registry.yaml` is version 2 and contains exact `foundry-opt`
  provenance from the published skill or its remotely fetchable source
  checkout.
- The selected agent has a version 2 sidecar; existing sidecars remain
  unchanged.
- Existing evaluation bundles and lineage remain unchanged; bootstrap creates no
  evaluation assets.
- `azure.yaml` connects the selected agent to the confirmed Foundry project
  while preserving other project and agent services.
- GitHub workflows use OIDC and the reviewed identity without static Azure
  credentials.
- The optimizer skill and runtime are installed from exact retained provenance.
- A local commit is created and deployed with `azd deploy`.
- No branch or tag is pushed.
- `.foundry-opt/bootstrap-report.md` records what happened and the actual tool
  versions.

Rerun `/foundry-bootstrap` to onboard another folder, including a folder that
targets a different Foundry project.

If a step fails, the coding agent stops and reports completed, failed, and
pending work without deleting successful changes.
