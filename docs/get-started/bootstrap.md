# Bootstrap

Install the static `foundry-bootstrap` skill, open the target repository, and
say:

```text
Use /foundry-bootstrap to bootstrap this repository.
```

## Before approval

The skill performs read-only inspection:

- discovers agent roots and existing registry/profile/azure.yaml settings;
- resolves Foundry projects and existing deployed agents;
- inspects GitHub environments/variables and Azure identity/OIDC/RBAC;
- checks existing Python and NuGet package sources and whether approved proxy
  feeds are required;
- checks Git ignore rules for every required tracked bootstrap file and stages
  the smallest necessary `.gitignore` correction;
- classifies remote state as adopt, create, or conflict;
- stages proposed files in the session workspace;
- shows exact local diffs, remote actions, commit intent, and deployment plan.

Conflicting existing remote resources must be resolved before approval.

If direct public package feeds are unavailable, bootstrap can use
`https://packagefeedproxy.microsoft.io/pypi/simple` for Python and
`https://packagefeedproxy.microsoft.io/nuget/v3/index.json` for NuGet. The
selected sources and any persistent configuration changes are included in the
single approval; bootstrap never switches feeds silently.

## Approval

One approval covers:

- repository files;
- GitHub/Azure setup;
- `.foundry-opt/bootstrap-report.md`;
- local branch and commit creation;
- initial azd deployment for enabled agents.

There is no rollback. The skill states this before requesting approval.

## After approval

The skill:

1. applies the reviewed local changes;
2. validates registry, profiles, workflows, and `azure.yaml`;
3. rechecks remote drift;
4. adopts exact resources and creates missing resources;
5. records actual setup changes in the report;
6. creates a local exact commit;
7. capability-probes azd and the `azure.ai.agents` extension;
8. runs `azd deploy <service>`;
9. returns deployment versions and resource links.

The skill does not push or merge.

## Existing repositories

Registry v1 and existing profiles migrate in place. Stable agent IDs, custom
paths, Foundry targets, identity, workflow settings, and evaluation bundles
are preserved. Legacy locks, journals, receipts, and `.foundry-proposed`
siblings are removed after approval and recorded in the report. Bootstrap does
not use `git add -f` to conceal ignored repository contracts.

## Evaluation

Bootstrap preserves an existing default evaluation bundle but does not create
datasets or evaluators. New evaluation onboarding is deferred to a future
dedicated skill.
