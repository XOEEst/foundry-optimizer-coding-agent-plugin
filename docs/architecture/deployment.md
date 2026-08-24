# Deployment

Deployment has two entry paths.

## Skill-local first deployment

After the owner approves the combined bootstrap plan, the skill:

1. writes or updates `azure.yaml`;
2. applies repository and GitHub/Azure setup changes;
3. writes the bootstrap report;
4. creates an exact local commit;
5. verifies clean `HEAD`;
6. capability-probes the installed/latest `azd` and `azure.ai.agents`
   extension;
7. runs `azd deploy <service>` for each enabled agent.

Foundry source or reconciliation inconsistencies are warnings, not blockers.
The approved repository source is authoritative. Repeated azd deployment may
create another immutable version.

## Registered deployment

The main-branch workflow uses:

- [`repository_selection.py`](../../src/foundry_opt/repository_selection.py)
  to select enabled agents;
- [`poc/deploy.py`](../../src/foundry_opt/poc/deploy.py) to verify and publish
  an exact committed source;
- [`poc/source.py`](../../src/foundry_opt/poc/source.py) and
  [`packaging/`](../../src/foundry_opt/packaging/) for exact packaging.

Registered deployment validates committed registry/profile bytes and registry
v2 runtime provenance. It no longer requires a bootstrap lock or receipt.

## Stable invariants

- never deploy dirty working-tree source;
- preserve explicit Foundry route configuration;
- verify uploaded and downloaded source bytes where the registered deployment
  runtime supports it;
- keep verification warnings honest;
- retain immutable versions as audit history.
