# Editable templates

Render these files in the coding session's staging area and patch existing
customer files rather than replacing unrelated content.

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
sidecar and `azure.ai.agent` service for each selected agent. Remove template
comments that do not describe the rendered repository.

Registry and sidecar output must pass the bundled schemas. `azure.yaml` must
pass the installed `azure.ai.agents` extension's validation/capability checks.
No rendered file may contain an unresolved token or a credential.
