# Repository contract

## Authority

1. `.foundry-opt/registry.yaml`
2. each registered agent profile
3. reviewed issue input that only narrows repository policy

## Core files

| File | Role |
| --- | --- |
| `.foundry-opt/registry.yaml` | Runtime provenance, GitHub settings, identity, and enabled-agent registry |
| `<agent-root>/.foundry/foundry-opt.yaml` | Agent source/runtime/model/verification/deployment policy |
| `.foundry-opt/bootstrap-report.md` | Owner-readable bootstrap audit summary; never deployment authority |
| `azure.yaml` | azd source-deployment service definitions |
| `.github/workflows/copilot-setup-steps.yml` | Exact optimizer runtime and broker setup |
| `.github/workflows/foundry-opt-deploy.yml` | Registered exact-source deployment |
| `.github/instructions/foundry-opt.instructions.md` | Repository trust instructions |
| `.github/ISSUE_TEMPLATE/foundry-optimize-agent.yml` | Optimize-job issue contract |

The validation workflow is optional.

## Registry v2

Registry v2 records exact runtime provenance:

- repository and commit;
- package path;
- `uv.lock` SHA-256;
- optimizer skill path.

It also records GitHub environment names, client-ID variable, identity, and
stable agent entries.

## Agent profiles

Profiles remain schema version 2 and contain source/package/editable paths,
runtime/protocol settings, Foundry target, model policy, guardrails,
deployment settings, repository checks, and any existing default evaluation
bundle.

Evaluation bundles may preserve exact definition-scoped inline criterion IDs.
When development and validating definitions use different IDs, the bundle
stores `development_evaluator_ids` and `validating_evaluator_ids` separately;
immutable evaluator URIs remain unchanged.

Bootstrap preserves existing bundles but does not generate new evaluation
assets.

## Skill ownership

There is no ownership ledger, managed manifest, journal, receipt, semantic
patch engine, or `.foundry-proposed` workflow. The skill shows exact proposed
diffs before approval and applies those reviewed changes directly.

## Migration

The skill migrates registry v1 and existing profiles in place, preserving
stable IDs, target configuration, identity, workflow settings, and evaluation
bundles. After approval it removes legacy bootstrap locks, journals, receipts,
and proposed siblings and records those removals in the report.
