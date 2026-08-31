# Editable templates

Render these files in the coding session's staging area and patch existing
customer files rather than replacing unrelated content.

Each run renders one user-confirmed onboarding target. Append or reconcile only
that target's registry entry, sidecar, project binding, and agent service.
Preserve every other registered agent and service unchanged.

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
sidecar and `azure.ai.agent` service only for the selected agent. Reuse an
existing `azure.ai.project` service when its endpoint exactly matches the
confirmed endpoint. Remove template comments that do not describe the rendered
repository.

Registry and sidecar output must pass the bundled schemas. `azure.yaml` must
pass the installed `azure.ai.agents` extension's validation/capability checks.
No rendered file may contain an unresolved token or a credential.
