# Bootstrap-managed files

The skill shows exact proposed diffs before its single approval. It then
applies those changes directly; there is no ownership ledger or proposed-file
sibling mechanism.

## Required core

- `.foundry-opt/registry.yaml`
- `<agent-root>/.foundry/foundry-opt.yaml`
- `.foundry-opt/bootstrap-report.md`
- `azure.yaml`
- `.github/workflows/copilot-setup-steps.yml`
- `.github/workflows/foundry-opt-deploy.yml`
- `.github/instructions/foundry-opt.instructions.md`
- `.github/ISSUE_TEMPLATE/foundry-optimize-agent.yml`

The validation workflow is optional.

Existing unrelated files and unrelated dirty paths are preserved and are not
staged into the bootstrap commit.

## Removed legacy artifacts

The skill proposes removal of:

- `.foundry-opt/bootstrap.lock.json`
- bootstrap journals and receipts
- `.foundry-proposed` siblings
- legacy shared pin files no longer used by the registry v2 flow

Approved removals are recorded in the bootstrap report.
