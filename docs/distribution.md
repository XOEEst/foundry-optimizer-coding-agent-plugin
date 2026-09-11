# Distribution

## Static bootstrap skill

The bootstrap skill is a static folder/ZIP. Standard release automation:

1. copies the skill files;
2. injects `release.json` with exact retained `foundry-opt` runtime
   provenance;
3. creates the ZIP;
4. publishes a SHA-256 checksum.

The skill contains no Python bridge, runtime installer, or skill lock.

For source development, a release archive is optional and the bootstrap skill
folder does not need to be pushed. When `release.json` contains placeholders,
the skill selects a compatible commit from its configured upstream or another
remote ancestor, then derives the package path, `uv.lock` digest, and optimizer
skill path from a clean fetch of that commit. Local instruction and template
edits take effect after `/skills reload`; only new shared runtime code needs a
remotely reachable commit.

## Retained runtime provenance

Registry v2 records:

- runtime repository;
- exact runtime commit;
- package path;
- `uv.lock` SHA-256;
- optimizer skill path.

Deployment, local execution, and ordinary Actions workflows use that exact
runtime. Existing registries without `github.copilot_runtime` retain pinned
Copilot execution as well.

New bootstrap output sets `github.copilot_runtime: main`. Each fresh Copilot
`dynamic` session fetches the configured runtime repository's `main`, resolves
it to one SHA, derives its lock digest, and installs it with frozen dependencies.
That session uses the same SHA for all job commands. A changed SHA cannot resume
old job state. A failed fetch or incompatible main stops setup, not a fallback.
This mode trusts future upstream code and dependency changes and is shown in
bootstrap approval. It follows the latest main tip, not the latest passing CI.

Main-mode repositories commit a stable project skill loader at
`.github/skills/foundry-agent-optimizer/SKILL.md`. It validates the selected
runtime and reads its exact optimizer instructions outside the customer
checkout. No tracked registry or skill files change at session startup.
Pinned mode instead commits the exact optimizer skill directory and retains
the recursive setup comparison. Neither mode relies on home-directory skill
installation. Changing the mode requires aligned registry, setup workflow, and
project-skill changes in a reviewed bootstrap patch.
A migration may require a one-time update of the recorded pin to a runtime
that understands the mode setting. Later main-tracking sessions do not rewrite
that pin.

Source-checkout bootstrap verifies compatibility by running the fetched
runtime's offline preflight against its bundled registry-v2 profile with
verification disabled. Legacy compatibility code does not make a runtime
incompatible with repositories that omit evaluation bundles.

## azd

The skill does not pin azd or `azure.ai.agents`. It checks the installed/latest
tools for required code-deploy capabilities and records the versions used.
Deployment stops if the required commands or schema features are unavailable.
