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
   - Choose `ignore`, `register disabled`, or `register enabled`.
2. **Foundry targets**
   - Confirm a Foundry project endpoint and agent name for each enabled agent.
   - Values already proven by a profile, metadata, `azure.yaml`, azd, or
     binding evidence are reused.
3. **Repository changes**
   - Registry, profiles, instructions, issue form, workflows, preserved files,
     and conflicts.
4. **GitHub-to-Azure connection**
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
