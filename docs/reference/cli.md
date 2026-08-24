# CLI reference

Bootstrap is skill-only and has no `foundry-opt bootstrap ...` command tree.

## Top-level commands

- `foundry-opt version`
- `foundry-opt validate-config`
- `foundry-opt preflight`

Validation and preflight read registry v2 runtime provenance directly. They do
not require a bootstrap receipt.

## Optimize-job commands

- `foundry-opt issue parse`
- `foundry-opt broker launch`
- `foundry-opt broker bind-pr`
- `foundry-opt job start`
- `foundry-opt job status`
- `foundry-opt job handoff`
- `foundry-opt job complete`
- `foundry-opt job finish`
- `foundry-opt job resume`
- `foundry-opt acceptance smoke`

## Registered deployment commands

- `foundry-opt deploy plan`
- `foundry-opt deploy verify-registered`
- `foundry-opt deploy publish-registered`

Initial local bootstrap deployment is performed by the skill with azd rather
than a `foundry-opt` command.

## Output

Runtime and workflow commands emit deterministic JSON except `version`, which
prints plain text.
