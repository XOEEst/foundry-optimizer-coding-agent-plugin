# Owner flow

Invoke the installed skill from the repository root:

```text
Use /foundry-bootstrap to bootstrap this repository.
```

The skill presents one review or question at a time. It does not show internal
JSON, hashes, provider state, or receipt payloads during the normal path.

## What you review

1. **Discovered agents**
   - Folder, source root, package root, readiness, and current binding state.
   - When an existing sidecar is found, its repository-relative path, Foundry
     target, baseline model, deployment state, and verification defaults.
   - Choose `ignore`, `register disabled`, or `register enabled`.
2. **Foundry targets**
   - Detect the Foundry project endpoint and agent name from repository
     metadata when possible; ask the owner only for values that remain missing.
   - Values already proven by a profile, metadata, `azure.yaml`, azd, or
     binding evidence are reused.
   - The coding agent resolves the backing Azure account with Azure tools and
     the owner's current login. Owners are not asked to discover ARM resource
     IDs.
   - If no unique Azure account is visible, the owner is prompted to correct
     the tenant/subscription login or choose the matching subscription.
     Bootstrap does not advance until the enabled target is resolved.
   - The skill submits the complete resolved target through `answer`; the
     runtime validates the endpoint, agent name, account match, and Foundry
     project access before continuing.
3. **Repository changes**
   - Registry, profiles, instructions, issue form, workflows, preserved files,
     and conflicts.
4. **GitHub-to-Azure connection**
   - Reuse the repository registry's existing identity when it still matches
     the live Azure resource.
   - Adopt exact matching OIDC credentials, role assignments, GitHub
     environments, and variables instead of recreating them.
   - GitHub environments and variables.
   - Azure identity, OIDC subjects, and project-scoped Foundry User roles.
5. **Verification**
   - Foundry dataset/evaluators now, defer to an issue, repository checks, or
     no evidence.
6. **Local commit**
   - Exact paths, base commit, branch, and commit message.
7. **Local deployment**
   - Exact commit, project, agent name, target state, verification mode,
     warnings, and version action.

Repository, connection, commit, and deployment mutations each require their
own explicit approval.

## What happens at the end

- The selected agents are registered; only enabled agents are deployment
  candidates.
- Reviewed target data is stored in each v2 profile.
- GitHub OIDC uses the reviewed Azure identity without static credentials.
- Bootstrap creates a local branch and exact commit; it does not push or merge.
- Approved enabled agents are deployed from that exact commit with the current
  local Azure identity.
- The final response links to relevant GitHub, Azure, Foundry agent, dataset,
  evaluator, and evaluation-run resources.

## Evaluation is optional

No dataset or evaluator is required to register, enable, or initially deploy an
agent. A no-evidence deployment carries an explicit warning. Add evaluation
later through an issue and activate the provided workflow template when the
signal is ready.
