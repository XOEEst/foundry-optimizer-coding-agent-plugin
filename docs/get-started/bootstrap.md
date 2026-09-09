# Bootstrap

Install the static `foundry-bootstrap` skill, open the target repository, and
say:

```text
Use /foundry-bootstrap to bootstrap this repository.
```

The target repository may contain only agent source code. Existing Foundry,
GitHub, Azure, evaluation, or optimizer metadata is optional; bootstrap
discovers what exists and generates registry v2 plus one profile for each
selected agent.

## Confirm one onboarding group

Each run begins by asking the owner to confirm:

1. one repository-relative folder to scan
2. all recognized descendant agents or the exact agents to exclude
3. one Microsoft Foundry project endpoint shared by the selected agents

The scan scope may be one agent root, a parent containing many agents, or the
repository root. Bootstrap lists every recognized deployable agent before the
owner confirms the subset. Repository evidence may suggest an endpoint, but
bootstrap never guesses the scope, subset, or endpoint.

For large inventories, bootstrap shows a grouped summary and writes complete
session-only Markdown and CSV inventories. Agents have short row numbers, so
owners can reply with `all`, `exclude 4,8-12`, or `only 2-20,31` instead of
copying long IDs.

Each inventory row includes optimizer readiness. Bootstrap consults the current
[Microsoft optimizer-readiness guide](https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/make-agent-optimizer-ready)
during the run. Selected agents that are not ready receive exact staged
readiness changes and validation in the combined plan; the skill does not embed
the guide's contents.

One run adds or reconciles only the selected group. Existing excluded registry
entries, sidecars, project bindings, and agent services remain unchanged.
Rerun `/foundry-bootstrap` to scan another folder or onboard another group,
including one targeting a different Foundry project.

A published archive is optional when developing from a source checkout. The
skill folder may remain local: `/skills reload` picks up instruction and
template edits. The skill separately reuses a compatible runtime commit from
its configured upstream, so only changes to shared runtime code require a push.

## Before approval

The skill performs read-only inspection:

- discovers agent roots and existing registry/profile/azure.yaml settings;
- inspects the confirmed scope, selected agents, and their one shared Foundry
  project;
- assesses optimizer readiness and stages required changes for selected agents;
- inspects GitHub Agents variables separately from Actions
  repository/environment variables, plus Azure identity/OIDC/RBAC;
- checks existing Python and NuGet package sources and whether approved proxy
  feeds are required;
- checks Git ignore rules for every required tracked bootstrap file and stages
  the smallest necessary `.gitignore` correction;
- renders one LF-normalized patch and verifies its exact bytes against both the
  Git index and worktree with `git apply --check --index`;
- classifies remote state as adopt, create, or conflict;
- stages proposed files in the session workspace;
- shows exact local diffs, remote actions, commit intent, and deployment plan.

Conflicting existing remote resources must be resolved before approval.

Repository branch protection is optional and its absence does not block
bootstrap. Deployment-environment branch restrictions are reviewed separately;
new environments are unrestricted unless the owner explicitly approves a
custom or protected-branch policy.

Draft-only optimizer rules are scoped to candidate evaluation and do not block
bootstrap from publishing a regular immutable version. Bootstrap compares those
rules with active merge/deployment workflows before deciding that repository
policy prohibits publication.

If direct public package feeds are unavailable, bootstrap can use
`https://packagefeedproxy.microsoft.io/pypi/simple` for Python and
`https://packagefeedproxy.microsoft.io/nuget/v3/index.json` for NuGet. The
selected sources and any persistent configuration changes are included in the
single approval; bootstrap never switches feeds silently.

## Cloud-agent variables

Copilot cloud sessions use **Settings > Secrets and variables > Agents >
Variables**, not the Actions `copilot` environment. Bootstrap configures and
reads back `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, and the optimizer client-ID
variable (normally `AZURE_OPTIMIZER_CLIENT_ID`) in the dedicated Agents store.
It preserves Actions variables needed by normal setup and deployment workflows.

Do not rely on job-level `env` or `environment` in `copilot-setup-steps.yml` to
inject cloud-session variables. A successful ordinary setup run or offline
preflight does not prove cloud-session readiness. If required Agents variables
cannot be verified, bootstrap reports the configuration as incomplete. Start a
fresh Copilot session after updating them.

See [GitHub's Agents configuration guide](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/customize-cloud-agent/configure-secrets-and-variables).

## Cloud-agent internet access

Bootstrap first reads the repository's firewall flags and custom allowlist
through `GET /repos/{owner}/{repo}/copilot/cloud-agent/configuration`. It does
not ask you to supply settings it can read automatically. Only relevant
API failures require help obtaining the repository configuration. Unavailable
inheritance and edit permission are recorded as unknown, not questions for you.
The approval includes exact additive repository rules for the Azure authority
(`login.microsoftonline.com` in Azure public cloud) and the hostname of the
confirmed Foundry project endpoint. It preserves existing rules and keeps the
firewall enabled. An explicit allowance may duplicate inherited coverage; you do
not need to investigate that redundancy. Approved UI-only changes may require
owner assistance under **Settings > Copilot > Internet access > Copilot cloud
agent**. Administrator help is requested only if a known restriction or an
actual application failure blocks the approved changes.

When manual changes are needed, bootstrap gives you a copy-ready **Allowlist
additions** block with the exact entries, why each is needed, and the target
repository. You do not need to work out the hostnames. Package-feed entries are
included only when required. After the combined approval, open **Custom
allowlist**, use **Add rule** for each listed entry, then **Save changes**.
Leave existing rules and the firewall enabled; bootstrap reads the saved
settings back. If no additions are needed, no manual action is requested.

Saved rules and successful setup steps do not prove connectivity from
agent-issued commands. The report tracks configuration separately from online
preflight in a fresh Copilot session, without starting evaluation runs. An
uncompleted administrator action is reported as a blocker, not successful setup.
See [GitHub's firewall guide](https://docs.github.com/en/copilot/how-tos/copilot-on-github/customize-copilot/customize-the-firewall).

## Approval

One approval covers:

- repository files for selected agents plus required shared-file updates;
- GitHub/Azure setup;
- `.foundry-opt/bootstrap-report.md`;
- local branch and commit creation;
- initial azd deployment for enabled agents.

There is no rollback. The skill states this before requesting approval.

If bootstrap must create a user-assigned managed identity, Azure generates its
client ID during creation. The approval names the exact identity ARM resource
and permits only that returned client ID to be inserted into
`registry.identity.client_id` and the approved Agents/Actions variable
destinations. Bootstrap validates and records the final patch hash without
requesting another approval.

## After approval

The skill:

1. applies the reviewed local changes;
2. validates registry, profiles, workflows, and `azure.yaml`;
3. rechecks remote drift;
4. adopts exact resources and creates missing resources;
5. records actual setup changes in the report;
6. creates a local exact commit;
7. capability-probes azd and the `azure.ai.agents` extension;
8. binds reused projects with `AZURE_AI_PROJECT_ID` in the selected azd
   environment;
9. runs `azd deploy <selected-service>` for each selected enabled agent;
10. returns deployment versions and resource links.

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
