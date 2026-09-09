---
name: foundry-agent-optimizer
description: Bootstrap a Microsoft Foundry agent repository or run one issue-driven, Tenzing-disciplined optimize job.
---

# Foundry agent optimizer

Use this skill for a guided local repository bootstrap or when an issue requests
an agent optimize job. Also use it when a local coding-agent session asks to
optimize a repository-defined agent through a Foundry deployment and evaluation
provider.

The default cloud entry uses standard Copilot with the managed repository
instructions and this installed skill. A repository custom agent is optional
and must be selected explicitly.

Choose the execution profile before starting:

- Issue-assigned cloud job: follow the issue-driven loop below.
- Local coding agent with a Foundry target: read
  `profiles/LOCAL_FOUNDRY.md`.

## Operating contract

- Treat `.foundry-opt/registry.yaml` as the enabled-agent registry.
- Treat each registry `config_path` as trusted agent-specific configuration.
- Treat `.foundry-opt/bootstrap.lock.json` as the generated managed ownership
  ledger; it is produced by repository apply and is never a rendered template.
- Legacy `.github/foundry-optimizer.yaml`, `.foundry/agent-metadata.yaml`, and
  `.github/foundry-opt.lock.yml` are migration inputs only and are not created
  by v1 bootstrap.
- Authenticate cloud jobs to Foundry with workload OIDC only. The local prompt
  adapter uses the developer's existing Azure CLI Entra session and never
  accepts stored client secrets.
- Use draft agent versions only. Never publish or change endpoint routing.
- Treat deployment as exact-source only: merge-time deployment must come from
  the reviewed repository source at the pinned commit.
- Use the broker-backed CLI for issue updates. Do not fall back to built-in
  GitHub comment tools.
- Freeze the selected agent, objective, verification contract, candidate
  budget, editable paths, models, exact runtime, and base commit before
  generating candidates.
- Freeze a reviewable run contract using `protocol/RUN_CONTRACT.md`. The target
  supplies source and evaluation inputs; it does not control Tenzing strategy.
- Freeze sequential or deterministic parallel-round execution. Parallel
  candidates share one observation snapshot and incumbent, use isolated
  transactions, and cannot influence siblings before the batch barrier.
- Freeze distinct training/search, confirmation, and final validating roles.
  If the repository supplies only train and validation, derive a deterministic
  confirmation partition from train and keep validation sealed.
- Enforce `protocol/DATA_ISOLATION.md`: expose row content, labels, and
  task-level diagnostics only for training/search. Never let candidate
  generation scan a combined dataset containing confirmation, validation, or
  reserved rows. Confirmation remains aggregate-only across all rounds.
- Freeze the evaluator normalization and weights. Calculate task scores and
  split-level `avgScore` exactly as specified in
  `protocol/SCORE_AGGREGATION.md`.
- Evaluate the baseline on training and confirmation once. Evaluate every
  implemented candidate on the same training split and evaluators.
- Run confirmation only when a candidate strictly improves training
  `avgScore`. Promote it only when confirmation also strictly improves over
  the current best confirmation `avgScore`.
- Use the validating dataset only for the provisional winner.
- Persist proposals, lineage, scores, lessons, worktree locations, and cleanup
  state according to `protocol/EXPERIMENT_STATE.md`.
- Keep evaluator scores authoritative and reasons/traces advisory according to
  `protocol/EVALUATOR_EVIDENCE.md`.
- Project the final evidence into an English-only detailed scorecard according
  to `protocol/SCORECARD.md`. A short main-change label never replaces the
  required per-candidate mutation idea, hypothesis, concrete change, idea
  contributions, expected mechanism, observed outcome, and decision.
- Keep redacted evidence in the original issue. Do not create child issues or
  candidate pull requests.
- GitHub creates one early draft pull request when the issue is assigned. Apply
  only the deployable winner to that branch, or close it unchanged when there is
  no winner.

## Issue-driven Tenzing loop

Use the executable discipline in:

- `protocol/TENZING_LOOP.md`
- `protocol/CANDIDATE_PROPOSAL.md`
- `protocol/LEARNING_RULES.md`
- `protocol/SCORE_AGGREGATION.md`
- `protocol/DATA_ISOLATION.md`
- `protocol/CONFIRMATION_GATE.md`
- `protocol/RUN_CONTRACT.md`
- `protocol/EXPERIMENT_STATE.md`
- `protocol/EVALUATOR_EVIDENCE.md`
- `protocol/SCORECARD.md`
- `protocol/TARGET_PROVIDER_CONTRACT.md`
- `protocol/PARALLEL_ROUNDS.md`
- `protocol/RUNTIME_GAPS.md`

The upstream snapshot remains reference material; these protocol files define
the supported optimize-job adaptation.

1. Read the issue, `.foundry-opt/registry.yaml`, the targeted sidecar, the
   managed repository instructions, and `.foundry-opt/bootstrap.lock.json`.
   Resolve exactly one enabled agent and the issue verification inputs.
   Partition evaluation data through the trusted adapter before inspecting
   task content, and expose only the training/search projection to candidate
   generation.
2. Run the repository preflight. Stop before any Foundry operation if the
   exact runtime, bootstrap receipt, OIDC identity, registry, sidecar,
   verification contract, or draft capability is unavailable. If
   `FOUNDRY_OPT_EXECUTABLE` is set, use that exact path when `foundry-opt` is
   not already on `PATH`.
3. Start or resume the optimize job through the repository-installed
   `foundry-opt` CLI and follow its machine-readable `next_action`.
4. Record the fresh baseline evaluation in the original issue.
5. Execute candidate rounds:
   - use a round width of one unless the selected runtime explicitly declares
     parallel-round scheduling, durable per-candidate state, and batch barriers
   - freeze the round incumbent, membership, candidate IDs, proposals,
     execution parents, and idea parents before launching any candidate
   - generate diverse initial hypotheses from the immutable baseline
   - for later synthesis, use one execution parent plus contribution-tracked
     idea parents from closed earlier rounds
   - edit only isolated workspaces and allowed paths
   - make at least one deployable source change per candidate
   - run the requested local validation
   - submit independent candidate transactions up to the frozen concurrency
     limits
   - wait for the complete training and confirmation barriers
   - select at most one round winner using the frozen deterministic ranking
   - derive redacted lessons for the complete round before proposing another
     round
6. Complete at least the policy minimum number of changed candidates unless the
   CLI reports a platform failure or an expired job deadline.
7. Let the CLI rank candidates against the fresh baseline and the incumbent
   frozen at round start. A training improvement is provisional until it
   passes `protocol/CONFIRMATION_GATE.md`.
8. Promote at most one candidate after the round barrier, and only when both
   its training and confirmation `avgScore` strictly improve over the frozen
   incumbent. A tie on either split is a non-improvement. Completion order must
   not affect promotion.
9. Run the final validating evaluation only for the provisional winner after
   search termination and a durable winner-freeze receipt. Opening final
   validation is a one-way transition; do not resume mutation afterward.
10. Finish the optimize job:
   - apply only the deployable winning patch that satisfies the repository
     verification policy, or
   - leave the branch unchanged and close the draft pull request
11. Generate the canonical scorecard using `protocol/SCORECARD.md`. Confirm it
    contains every candidate's detailed mutation idea and lineage, training
    `avgScore`, triggered confirmation `avgScore`, every Foundry evaluation
    link, guardrail results, tradeoffs, incidents, cleanup and application
    status, and the final decision. Write the canonical Markdown, JSON, TSV,
    DAG labels, and any scorecard projection in English only. When a winner
    exists, the headline final score is the winner's final validating
    `avgScore`; pass rate is diagnostic only unless policy explicitly defines
    it as the primary metric.
12. Use honest terminal labels:
    - `winner` or `no_winner` only when a quantitative verification path ran
    - `recommended` when approved repository checks support human review
    - `proposed_unverified` when evidence is insufficient for a recommendation
13. When the issue supplies verification inputs, honor them exactly: either an
    exact Foundry verification dataset with exact evaluator IDs, exact
    repository commands, or an explicit acknowledged no-evidence fallback.
    Named `check: ...` entries are repository-owned and stay reserved for
    trusted PR/deployment verification profiles. Never widen issue-supplied
    inputs, never invent missing evidence, and require a trusted
    write/maintain/admin issue-author permission binding before honoring
    arbitrary evaluator, dataset, or command overrides.

### Current lineage boundary

The current runtime creates each worktree from exactly one execution parent:

- no parent means the immutable baseline
- one parent means that finalized candidate commit

Tenzing synthesis may draw lessons from several assessed candidates, but the
current CLI persists only one execution parent. Record additional idea parents
with a `positive`, `negative`, or `contrast` role and their exact contribution
in the proposal rationale and issue evidence. They must come from closed
earlier rounds under the same run contract. Do not pass unsupported
multi-parent flags, create an automatic Git merge, inherit parent scores, or
claim that idea lineage was stored by the runtime.

The current optimize-job controller also runs one candidate at a time. Treat
it as sequential rounds of width one until native candidate allocation,
concurrency-safe state, and batch barriers are implemented and tested.

### Current execution profile

The hardened managed optimize-job profile is:

```text
standard Copilot cloud coding host
-> installed foundry-agent-optimizer skill
-> repository-pinned foundry-opt runtime
-> Microsoft Foundry hosted agent
```

The supported local provider profile is:

```text
local coding agent
-> installed foundry-agent-optimizer skill
-> local Tenzing observe/propose/learn loop
-> repository-defined Foundry deployment/evaluation adapter
-> Microsoft Foundry prompt or hosted agent
```

For a repository-defined target, reuse only the source, deployment, dataset,
evaluator, and optimization metadata exposed by its trusted target adapter.
Do not submit a service-owned optimization job and do not create regular
candidate versions. The local coding agent must generate and implement the
hill-climbing candidates; `foundry-opt` draft, exact-definition/source, route,
evaluation, and cleanup primitives must execute each candidate transaction.

Read `protocol/RUNTIME_GAPS.md` before claiming that a documented protocol
feature is machine-enforced.

An `azd` optimization provider and a local target-agent provider still require
separate runtime contracts. Do not simulate them from skill instructions alone.

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
- An invalid candidate teaches only about implementation or policy boundaries;
  it does not disprove the hypothesis.
- A discarded candidate must produce a concise lesson before another candidate
  is proposed.
- Prefer no winner over an unsupported or ambiguous improvement.

## Tenzing reference

The snapshot under `references/tenzing/` is read-only reference material.
Do not run its `INIT.md`, create its branch-per-experiment layout, or modify the
snapshot inside an optimize job. Follow `references/ADAPTER_MAPPING.md` and the
supported files under `protocol/`. Attribution is in
`references/TENZING_ATTRIBUTION.md`.
