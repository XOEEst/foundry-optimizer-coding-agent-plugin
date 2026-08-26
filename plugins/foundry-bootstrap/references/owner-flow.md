# Owner flow

Invoke the skill from the repository root:

```text
Use /foundry-bootstrap to bootstrap this repository.
```

The coding agent first inspects the repository and relevant GitHub, Azure, and
Foundry resources without changing them. It then prepares proposed files in its
session staging area and shows one review containing:

1. discovered agents and their proposed enabled or disabled registration
2. existing contracts that will be migrated in place
3. exact repository diffs
4. exact patch SHA-256 and successful `git apply --check --index` result
5. cloud resources that exactly match and will be reused
6. missing cloud resources that will be created
7. conflicts that prevent safe progress
8. the exact local commit and `azd deploy` plan

The owner gives one combined approval. If the plan changes, the coding agent
shows a fresh exact diff and asks again.

## Expected result

- `.foundry-opt/registry.yaml` is version 2 and contains exact `foundry-opt`
  provenance from the published skill or its remotely fetchable source
  checkout.
- Each selected agent has a version 2 sidecar.
- Existing evaluation bundles and lineage remain unchanged; bootstrap creates no
  evaluation assets.
- `azure.yaml` describes or connects to the reviewed Foundry project and agent
  services.
- GitHub workflows use OIDC and the reviewed identity without static Azure
  credentials.
- The optimizer skill and runtime are installed from exact retained provenance.
- A local commit is created and deployed with `azd deploy`.
- No branch or tag is pushed.
- `.foundry-opt/bootstrap-report.md` records what happened and the actual tool
  versions.

If a step fails, the coding agent stops and reports completed, failed, and
pending work without deleting successful changes.
