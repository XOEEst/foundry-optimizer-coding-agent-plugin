# Distribution

## Static bootstrap skill

The bootstrap skill is a static folder/ZIP. Standard release automation:

1. copies the skill files;
2. injects `release.json` with exact retained `foundry-opt` runtime
   provenance;
3. creates the ZIP;
4. publishes a SHA-256 checksum.

The skill contains no Python bridge, runtime installer, or skill lock.

## Retained runtime provenance

Registry v2 records:

- runtime repository;
- exact runtime commit;
- package path;
- `uv.lock` SHA-256;
- optimizer skill path.

Optimizer and registered deployment workflows fetch and verify that exact
runtime.

## azd

The skill does not pin azd or `azure.ai.agents`. It checks the installed/latest
tools for required code-deploy capabilities and records the versions used.
Deployment stops if the required commands or schema features are unavailable.
