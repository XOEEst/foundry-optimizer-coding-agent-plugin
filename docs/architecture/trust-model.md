# Trust model

## Trusted repository state

- `.foundry-opt/registry.yaml`
- each registered agent profile
- the exact Git commit used by optimizer or deployment
- exact runtime provenance recorded in registry v2

`.foundry-opt/bootstrap-report.md` is an audit summary, not authority.

## Skill-only bootstrap

The skill may inspect and mutate repository, GitHub, Azure, and Foundry state
only after showing one combined plan and receiving approval.

It adopts exact remote matches, creates missing resources, and stops on
conflicting existing resources. It does not overwrite conflicts, persist
credentials, compensate, or roll back.

## Identity

- Prefer an exact existing repository identity.
- Otherwise create a user-assigned managed identity.
- Use separate federated subjects for optimizer and production environments.
- Store only non-secret identity and resource identifiers.

## Exact source

- Initial skill deployment requires a clean reviewed local commit.
- Registered deployment requires the committed default-branch source.
- Optimize-job candidates remain inside allowed editable paths and owned
  drafts.
- No flow mutates an explicit Foundry version route.

## External seams

- GitHub CLI for environments and variables
- Azure CLI/tools for identity, federation, and RBAC
- Foundry tools for inventory
- azd for initial local source deployment
- [`poc/auth.py`](../../src/foundry_opt/poc/auth.py),
  [`poc/github.py`](../../src/foundry_opt/poc/github.py), and
  [`poc/foundry.py`](../../src/foundry_opt/poc/foundry.py) for retained
  optimizer/registered deployment runtime operations

## Sensitive data

Neither the report nor durable optimizer state may contain tokens, static
credentials, raw prompts, responses, traces, or dataset rows.
