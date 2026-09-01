# Editable templates

Render these files in the coding session's staging area and patch existing
customer files rather than replacing unrelated content.

Each run renders one user-confirmed folder scope, selected agent subset, and
shared project endpoint. Append or reconcile only those agents' registry
entries, sidecars, shared project binding, and agent services. Preserve every
other registered agent and service unchanged.

The Markdown/CSV discovery inventory is session-only and is not a customer
repository template. Optimizer-readiness code changes for selected agents are
ordinary reviewed patch content.

| Template | Customer repository destination |
| --- | --- |
| `registry.yaml` | `.foundry-opt/registry.yaml` |
| `sidecar.yaml` | `<agent-root>/.foundry/foundry-opt.yaml` |
| `azure.yaml` | `azure.yaml` |
| `foundry-opt-deploy.yml` | `.github/workflows/foundry-opt-deploy.yml` |
| `copilot-setup-steps.yml` | `.github/workflows/copilot-setup-steps.yml` |
| `foundry-opt.instructions.md` | `.github/instructions/foundry-opt.instructions.md` |
| `foundry-optimize-agent.yml` | `.github/ISSUE_TEMPLATE/foundry-optimize-agent.yml` |
| `bootstrap-report.md` | `.foundry-opt/bootstrap-report.md` |

Replace every `__TOKEN__` from discovered and approved values. Duplicate the
sidecar and `azure.ai.agent` service for each selected agent. Reuse one existing
`azure.ai.project` service when its endpoint exactly matches the confirmed
shared endpoint. Remove template comments that do not describe the rendered
repository.

For a newly approved user-assigned managed identity, remove the
`identity.client_id` line from the static registry proposal instead of leaving
its token unresolved. After Azure creates and the skill verifies that exact
identity, add the concrete returned client ID to the final registry patch.

Registry and sidecar output must pass the bundled schemas. `azure.yaml` must
pass the installed `azure.ai.agents` extension's validation/capability checks.
No rendered file may contain an unresolved token or a credential.
