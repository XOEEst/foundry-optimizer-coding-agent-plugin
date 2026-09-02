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
