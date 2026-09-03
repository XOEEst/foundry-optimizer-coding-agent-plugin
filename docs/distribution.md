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

Optimizer and registered deployment workflows fetch and verify that exact
runtime.

Bootstrap also copies the optimizer skill directory from that runtime into the
customer repository at `.github/skills/foundry-agent-optimizer`. Copilot cloud
agent discovers the committed project skill before processing an issue; setup
verifies it rather than installing a new home-directory skill after startup.

Source-checkout bootstrap verifies compatibility by running the fetched
runtime's offline preflight against its bundled registry-v2 profile with
verification disabled. Legacy compatibility code does not make a runtime
incompatible with repositories that omit evaluation bundles.

## azd

The skill does not pin azd or `azure.ai.agents`. It checks the installed/latest
tools for required code-deploy capabilities and records the versions used.
Deployment stops if the required commands or schema features are unavailable.
