# Foundry bootstrap report

- Status: `planned`
- Repository: `__GITHUB_REPOSITORY_SLUG__`
- Base commit: `__BASE_COMMIT__`
- Local bootstrap commit: `pending`
- Generated at: `__GENERATED_AT_UTC__`

## Onboarding target

- Scan scope: `__AGENT_SCAN_SCOPE__`
- Selected agents: `__SELECTED_AGENT_COUNT__`
- Excluded agents: `__EXCLUDED_AGENT_COUNT__`
- Shared Foundry project endpoint: `__FOUNDRY_PROJECT_ENDPOINT__`
- Inventory Markdown: `__SESSION_INVENTORY_MARKDOWN_PATH__` (session-only)
- Inventory CSV: `__SESSION_INVENTORY_CSV_PATH__` (session-only)
- Selection expression: `__AGENT_SELECTION_EXPRESSION__`

## Optimizer readiness

- Guide: https://learn.microsoft.com/en-us/azure/foundry/agents/how-to/make-agent-optimizer-ready

| Repository agent ID | Status | Planned or completed remediation |
| --- | --- | --- |
| `__REPO_AGENT_ID__` | `__OPTIMIZER_READINESS__` | `__OPTIMIZER_REMEDIATION_OR_NONE__` |

## Tool versions

- Git: `__GIT_VERSION__`
- GitHub CLI: `__GH_VERSION__`
- Azure CLI: `__AZ_VERSION__`
- Azure Developer CLI: `__AZD_VERSION__`
- `azure.ai.agents` extension: `__AZURE_AI_AGENTS_VERSION__`

## Package feeds

- Python: `__PYTHON_PACKAGE_INDEX_OR_NOT_USED__`
- NuGet: `__NUGET_PACKAGE_SOURCE_OR_NOT_USED__`

## Runtime provenance

- Repository: `__FOUNDRY_OPT_REPOSITORY__`
- Commit: `__FOUNDRY_OPT_COMMIT__`
- Package path: `__FOUNDRY_OPT_PACKAGE_PATH__`
- `uv.lock` SHA-256: `__FOUNDRY_OPT_UV_LOCK_SHA256__`
- Optimizer skill path: `__FOUNDRY_OPT_OPTIMIZER_SKILL_PATH__`

## Repository patch

- Static patch SHA-256: `__STATIC_PATCH_SHA256__`
- Identity client-ID late binding: `__IDENTITY_LATE_BINDING_OR_NONE__`
- Final patch SHA-256: `__FINAL_PATCH_SHA256__`
- Created identity ARM ID: `__CREATED_IDENTITY_RESOURCE_ID_OR_NONE__`
- Created identity client ID: `__CREATED_IDENTITY_CLIENT_ID_OR_NONE__`
- Created identity principal ID: `__CREATED_IDENTITY_PRINCIPAL_ID_OR_NONE__`

## Agent registry

| Repository agent ID | Root | Run scope | Registration | Deployment | Foundry target |
| --- | --- | --- | --- | --- | --- |
| `__REPO_AGENT_ID__` | `__AGENT_ROOT__` | selected | enabled | pending | `__FOUNDRY_PROJECT_ENDPOINT__/agents/__FOUNDRY_AGENT_NAME__` |
| `__EXCLUDED_REPO_AGENT_ID_OR_NONE__` | `__EXCLUDED_AGENT_ROOT_OR_NONE__` | excluded | unchanged | not attempted | unchanged |
| `__EXISTING_REPO_AGENT_ID_OR_NONE__` | `__EXISTING_AGENT_ROOT_OR_NONE__` | preserved | unchanged | unchanged | unchanged |

## Reused resources

- `__REUSED_RESOURCE_OR_NONE__`

## GitHub deployment branch policy

- Repository branch protection: `__REPOSITORY_BRANCH_PROTECTION_OR_NONE__`
- Copilot environment mode: `__COPILOT_ENVIRONMENT_BRANCH_MODE__`
- Deployment environment mode: `__DEPLOYMENT_ENVIRONMENT_BRANCH_MODE__`
- Deployment environment allowed entries: `__DEPLOYMENT_BRANCH_ENTRIES_OR_NONE__`

## GitHub variables

Record actual names, non-secret values, and per-store read-back results for all
required variables. Replace the example Actions scope with the approved
repository or environment scope, and include inherited Agents values with their
verified effective scope when reused. Do not record secrets.

| Store and scope | Variables | Read-back result |
| --- | --- | --- |
| Agents / repository | Tenant ID, subscription ID, configured optimizer client-ID variable | pending |
| Actions / environment | Existing setup and deployment variables, preserved or created as approved | pending |

## Cloud-agent internet access

Record observed repository settings, exact required destinations, their purpose,
known coverage, approved repository additions, and saved-rule evidence.
Use the confirmed project's hostname, not a sample endpoint. Preserve existing
rules. Record unavailable inheritance and edit permission as `unknown`, without
requesting an owner investigation or treating them as pending prerequisites.

- Repository configuration API outcome and returned firewall fields: `pending`
- Inherited coverage: `unknown` (not a prerequisite for additive repository rules)
- Repository rule-edit permission: `unknown` (resolve when applying approved changes)
- Known organization restrictions, if any: `not observed`
- Required destinations and known coverage: `pending`
- Approved additions and saved-rule evidence: `pending`
- Blocked approved action and required owner/administrator help: `none`
- Fresh cloud-session online preflight: `not attempted`

Saved rules and successful setup steps are not evidence of cloud-session Azure
access. Record the session link and actual result only after online preflight.

### Allowlist additions

- Repository: `__GITHUB_REPOSITORY_SLUG__`
- Where: **Settings > Copilot > Internet access > Copilot cloud agent > Custom allowlist**
- Status: `Proposed - do not apply yet`
- Who must save the entries: `pending`

Render only the exact additions as a copy-ready text block, one entry per line.
Use actual resolved values, not placeholders. Explain each entry's purpose
outside the block and list already-covered entries separately. Include package
hosts only when required. If nothing needs adding, replace the action/status
with **No allowlist additions required**.

After approval, for manual saving, tell the owner to use **Add rule** for each
entry, then **Save changes**, preserving existing rules and keeping the firewall
enabled. Record awaiting manual save or confirmed saved based on API read-back;
an owner confirmation alone does not establish saved configuration.

## Created resources

- `__CREATED_RESOURCE_OR_NONE__`

## Repository files

- `__APPLIED_FILE_OR_NONE__`

## Deployment

- azd environment: `__AZD_ENVIRONMENT__`
- Foundry project ARM ID: `__FOUNDRY_PROJECT_RESOURCE_ID__`
- commands: `azd deploy <selected-service>` for each selected enabled agent
- deployed commit: `pending`
- result: `pending`
- links: `pending`

## Completed work

- `__COMPLETED_ITEM_OR_NONE__`

## Failed work

- `none`

## Pending work

- `__PENDING_ITEM_OR_NONE__`
