# Bootstrap

This is the shortest owner path from an unprepared repository to a
usable Foundry Optimizer setup.

The default path uses standard Copilot with the managed repository
instructions and installed skill. Bootstrap does not add or select a custom
agent. Normal owners should use the `/foundry-bootstrap` flow; the
`foundry-opt bootstrap ...` commands below remain as an advanced
compatibility interface for automation and reviewed source checkouts.

## Decide these things first

- Which repository folders are real agents?
- Which agent should be enabled first if you do not want every
  discovered candidate managed at once?
- Which GitHub environments will hold optimizer access and deployment
  access?
- Will you adopt an existing Azure identity, or let bootstrap create the
  reviewed one?
- Do you want optional default verification now, or later?

## Quick path

1. **Choose agents**
   - Run the discovery review and confirm the selected stable IDs.
   - Stop if the roots or package boundaries look wrong.
2. **Apply repository setup**
   - Register the selected agents.
   - Add the managed owner files, including the registry, sidecar, issue
     form, and workflow scaffolding.
3. **Approve the combined GitHub-to-Azure connection**
   - Plan once.
   - Approve once.
   - Apply once.
4. **Optionally activate verification**
   - Reuse or create the repository's default Foundry datasets,
     evaluators, definitions, and activation runs.
5. **Check status**
   - Confirm whether the selected agent is only enabled, fully verified,
     or already deployable.

## Why the connection approval is combined

Owners approve one connection plan because the GitHub and Azure pieces
are one trust boundary:

- GitHub environments and Actions request OIDC tokens.
- Azure identity and RBAC decide what those tokens can do.

You should review them together, not as unrelated steps. Internal child
receipts may still record environment creation, OIDC subject updates,
variables, or role assignments, but those receipts stay implementation
detail. Owners do **not** need to approve each child change separately
or hand-author approval JSON.

## Advanced compatibility command path

- `foundry-opt bootstrap review discovery`
- `foundry-opt bootstrap review plan`
- `foundry-opt bootstrap connect plan`
- `foundry-opt bootstrap connect approve` or `foundry-opt bootstrap connect apply --approve`
- `foundry-opt bootstrap review status`

Add the evaluation commands only when you want repository-default
verification:

- `foundry-opt bootstrap evaluation plan`
- `foundry-opt bootstrap evaluation apply`
- `foundry-opt bootstrap evaluation activate`

Compatibility policy: keep this CLI tree quiet and stable for existing
automation. Future retirement or breaking changes should be announced in
docs and release notes instead of adding runtime deprecation warnings.

## Example owner summaries

**Discovery**

> Selected `chat-agent` at `src/chat-agent`. Source and package roots
> match the reviewed layout. One extra sample app was discovered but not
> selected.

**Connection plan**

> Create GitHub environment `copilot`, update GitHub environment
> `foundry-production`, adopt Entra application `foundry-owner-review`,
> and grant reviewed Foundry User access on the target project.

**Status**

> Connection is complete and rollback-ready. Repository defaults for
> verification are not activated yet, so deployment remains policy-driven
> rather than evidence-backed.

**Resources**

> GitHub now contains the managed environments and Actions. Azure now
> contains the reviewed identity and RBAC. Foundry still points to the
> existing project and agent until evaluation onboarding creates default
> datasets, evaluators, and runs.

## Related detail

- [Overview](overview.md)
- [Issues and monitoring](issues-and-monitoring.md)
- [Evaluation onboarding](../evaluation-onboarding.md)
- [Identity and RBAC](../identity-rbac.md)
- [Managed files](../managed-files.md)
