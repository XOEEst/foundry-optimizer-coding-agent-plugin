# Recommended branch protection

Enable these settings before allowing customer workflows to resolve floating
`main`:

- require a pull request before merging
- block direct pushes
- require CODEOWNERS review
- dismiss stale approvals
- require all conversations to be resolved
- require the complete Windows/Linux CI matrix
- require branches to be up to date before merging
- restrict bypass to explicitly approved maintainers
- enable secret scanning and dependency review
- disallow force pushes and branch deletion

Until these controls are active, use an explicit reviewed commit for every
privileged bootstrap, optimize, validation, and deployment workflow.

See [Distribution and pinning](distribution.md) and
[Trust model](architecture/trust-model.md) for the exact-commit contract that
applies before these protections are enabled.
