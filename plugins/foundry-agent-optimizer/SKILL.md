---
name: foundry-agent-optimizer
description: Bootstrap a Microsoft Foundry agent repository or run one issue-driven, evidence-backed optimize job.
---

# Foundry agent optimizer

Use this skill for a guided local repository bootstrap or when an issue requests
an agent optimize job.

## Operating contract

- Treat `.foundry-opt/registry.yaml` as the enabled-agent registry.
- Treat each registry `config_path` as trusted agent-specific configuration.
- Treat `.foundry-opt/bootstrap.lock.json` as the generated managed ownership
  ledger; it is produced by repository apply and is never a rendered template.
- Legacy `.github/foundry-optimizer.yaml`, `.foundry/agent-metadata.yaml`, and
  `.github/foundry-opt.lock.yml` are migration inputs only and are not created
  by v1 bootstrap.
- Authenticate to Foundry with OIDC only.
- Use draft agent versions only. Never publish or change endpoint routing.
- Treat deployment as exact-source only: merge-time deployment must come from
  the reviewed repository source at the pinned commit.
- Use the broker-backed CLI for issue updates. Do not fall back to built-in
  GitHub comment tools.
- Evaluate the repository baseline once, then evaluate every implemented
  candidate with the same development dataset and evaluators.
- Use the validating dataset only for the provisional winner.
- Keep redacted evidence in the original issue. Do not create child issues or
  candidate pull requests.
- GitHub creates one early draft pull request when the issue is assigned. Apply
  only the deployable winner to that branch, or close it unchanged when there is
  no winner.

## Required loop

1. Read the issue, repository policy, agent metadata, and shared revision pin.
2. Run the repository preflight. Stop before any Foundry operation if the
   bootstrap receipt, OIDC identity, policy, metadata, or draft capability is
   unavailable. If `FOUNDRY_OPT_EXECUTABLE` is set, use that exact path when
   `foundry-opt` is not already on `PATH`.
3. Start or resume the optimize job through the repository-installed
   `foundry-opt` CLI and follow its machine-readable `next_action`.
4. Record the fresh baseline evaluation in the original issue.
5. For each candidate:
   - diagnose one concrete failure pattern
   - state one falsifiable hypothesis
   - select one allowed model
   - edit only the isolated workspace and allowed paths
   - make at least one deployable source change
   - run the requested local validation
   - submit the candidate to the CLI for packaging, draft deployment, and
     evaluation
   - wait for the candidate issue update before starting another candidate
6. Complete at least the policy minimum number of changed candidates unless the
   CLI reports a platform failure or an expired job deadline.
7. Let the CLI rank candidates against the fresh baseline and current best.
8. Run the validating evaluation only for the provisional winner.
9. Finish the optimize job:
   - apply only the deployable winning patch that satisfies the repository
     verification policy, or
   - leave the branch unchanged and close the draft pull request
10. Confirm the final issue update contains every candidate, every Foundry
    evaluation link, guardrail results, tradeoffs, and the final decision.
11. When the issue supplies verification inputs, honor them exactly: either an
    exact Foundry verification dataset with exact evaluator IDs, exact
    repository commands, or an explicit acknowledged no-evidence fallback.
    Named `check: ...` entries are repository-owned and stay reserved for
    trusted PR/deployment verification profiles. Never widen issue-supplied
    inputs, never invent missing evidence, and require a trusted
    write/maintain/admin issue-author permission binding before honoring
    arbitrary evaluator, dataset, or command overrides.

## Bootstrap for first-time owners

Bootstrap prepares a repository for managed Foundry ownership: it discovers
candidate agents, writes the reviewed managed files, connects GitHub to Azure
with OIDC, optionally onboards evaluation assets, and finishes with resource
links. It never changes runtime code, publishes a regular version, mutates
endpoint routing, stores secrets, copies raw evaluation content into GitHub, or
deploys from anything other than the reviewed exact source.

### Default owner flow

- Keep bootstrap short, natural-language, and bullet-based for owners.
- Default to `foundry-opt bootstrap review ...`, `foundry-opt bootstrap connect
  ...`, and `foundry-opt bootstrap resources ...` summaries. Do not paste raw
  JSON into owner-facing updates unless you are debugging or implementing.
- Decision 1 — choose agents: run `foundry-opt bootstrap review discovery ...`.
  Tell the owner exactly which discovered `repoAgentId` values would become
  registered in `.foundry-opt/registry.yaml`, which would stay out of scope,
  and which have blockers. Approval here means “register this reviewed agent
  set and leave the rest untouched for now.”
- Decision 2 — review repository setup: run `foundry-opt bootstrap review plan
  ...`. Tell the owner exactly which managed repository files will be added or
  updated, which selected agents will start registered only versus registered
  and enabled, which OIDC subjects and RBAC assignments are planned, and any
  deployment warnings. Approval here means “apply this reviewed repository
  setup at the exact runtime pin, with these managed files, these agent states,
  and this exact-source deployment policy.”
- Use the owner-facing terms consistently:
  - `registered` — the agent is listed in `.foundry-opt/registry.yaml`
  - `enabled` — the reviewed registry/profile intends the agent to participate
  - `verified` — reviewed evidence or receipt-backed verification is attached
  - `deployable` — policy currently allows exact-source deployment
- Verification is optional during bootstrap. Make the choice explicit:
  - `now` — review binding evidence and attach verification immediately
  - `later` — finish bootstrap now and add reviewed verification later
  - `skip` — continue without evidence when policy allows it
- If verification is deferred or skipped, say so plainly. Unverified deployment
  may still be allowed by policy, but it must be reported as a warning rather
  than presented as verified.
- Decision 3 — connect GitHub to Azure: use `foundry-opt bootstrap connect
  plan ...`, then `foundry-opt bootstrap connect approve ...` or
  `foundry-opt bootstrap connect apply --approve ...`. Tell the owner exactly
  what they approve: the shown GitHub environments, variables, and branch
  policy; the shown Azure identity create/adopt action; exactly two reviewed
  federated OIDC subjects; and the reviewed RBAC assignments. This is one
  combined connection approval with internal child receipts, not separate owner
  approvals for GitHub and Azure.
- When optimize-job verification inputs are discussed with owners, use the same
  plain-language model: repository defaults, exact issue-supplied Foundry
  evaluators plus dataset, exact issue-supplied repository commands, or an
  explicit no-evidence acknowledgement when policy allows it. Named
  `check: ...` entries stay repository-owned and apply only to trusted
  deployment/PR verification flows.
- End every successful bootstrap handoff with `foundry-opt bootstrap resources
  ...` and share the final GitHub, Azure, and Foundry links for the reviewed
  registered/enabled/verified/deployable state.

## Advanced and recovery

- Low-level inputs such as `bootstrap discover`, reviewed `BootstrapPlanInput`,
  selection roots, binding-evidence files, operation state, hashes, approval
  records, and receipt internals belong here, not in the default owner flow.
- Run `foundry-opt bootstrap discover` with the verified runtime provenance and
  repository root. Its discovery roots, `sourceFingerprint`, and
  `packageFingerprint` values are the authoritative local digests for reviewed
  binding evidence.
- Preserve discovery `root` as `discovery_root`. Use the managed agent
  directory as `root`; a repository-root discovery (`discovery_root: "."`)
  must switch to its concrete `sourceRoot` for managed bootstrap paths.
- To classify a real deployed baseline, use
  `foundry-opt bootstrap binding-evidence --plan-input ... --output ...` and
  pass the reviewed evidence back through `discover --binding-evidence` or the
  plan input (never both). Without content fingerprints an agent stays
  `bound-unknown`; metadata alone can never make it `bound-aligned`.
- `foundry-opt bootstrap plan --plan-input ...` is the low-level plan builder;
  offline plans include only the repository phase.
- Stop on stale runtime/SHA drift, plan drift, or failed/compensation-required
  receipts. Use `foundry-opt bootstrap review status ...`,
  `foundry-opt bootstrap connect status ...`, and receipt-bound rollback only
  for recovery.
- Evaluation onboarding remains ordered and approval-bound:
  `bootstrap evaluation inventory`, `bootstrap evaluation plan`,
  `bootstrap evaluation apply`, then `bootstrap evaluation activate`.
  `activate` finalizes the same single evaluations approval, is idempotent, and
  preserves the reviewed enabled state while attaching receipt-backed
  verification lineage.
- Keep evaluation internals in recovery/debug detail only: immutable dataset,
  evaluator, definition, run, and finalization ids come from receipts, not the
  approved plan; raw rows, prompts, traces, and secrets never leave Foundry.
- Report `ready-unbound` agents as scaffolded but disabled. They can be
  registered, but they are not verified or deployable until alignment is proven.
- Use `bootstrap evaluation inspect|status|replace` only for approved bounds,
  resume state, receipt finalization, or explicit replacement recovery.

## Candidate discipline

- One candidate is one coherent hypothesis.
- Do not combine unrelated cleanup with an experiment.
- Do not change datasets, evaluators, decision rules, OIDC settings, hosted
  runtime settings, or the shared revision pin.
- Do not copy raw prompts, responses, dataset rows, tool arguments, credentials,
  or traces into GitHub.
- A failed authentication, deployment, or evaluation is a platform failure, not
  a candidate score.
- Prefer no winner over an unsupported or ambiguous improvement.

## Tenzing reference

The snapshot under `references/tenzing/` is read-only reference material.
Follow `references/ADAPTER_MAPPING.md`; do not initialize or modify the upstream
snapshot. Attribution is in `references/TENZING_ATTRIBUTION.md`.
