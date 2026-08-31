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

## Agent registry

| Repository agent ID | Root | Run scope | Registration | Deployment | Foundry target |
| --- | --- | --- | --- | --- | --- |
| `__REPO_AGENT_ID__` | `__AGENT_ROOT__` | selected | enabled | pending | `__FOUNDRY_PROJECT_ENDPOINT__/agents/__FOUNDRY_AGENT_NAME__` |
| `__EXCLUDED_REPO_AGENT_ID_OR_NONE__` | `__EXCLUDED_AGENT_ROOT_OR_NONE__` | excluded | unchanged | not attempted | unchanged |
| `__EXISTING_REPO_AGENT_ID_OR_NONE__` | `__EXISTING_AGENT_ROOT_OR_NONE__` | preserved | unchanged | unchanged | unchanged |

## Reused resources

- `__REUSED_RESOURCE_OR_NONE__`

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
