# Evaluation onboarding

Evaluation onboarding turns a reviewed agent into an activated optimizer target: immutable
datasets, one default evaluator bundle, immutable development/validating definitions, an
activation smoke run, and a per-agent sidecar that is written only after that activation
succeeds.

## Command flow

```text
foundry-opt bootstrap evaluation inventory   # assess reusable assets, trace eligibility, split targets
foundry-opt bootstrap evaluation plan        # show the single composite action built from the reviewed contract
foundry-opt bootstrap evaluation apply       # run the staged onboarding machine (no repository mutation)
foundry-opt bootstrap evaluation activate    # receipt-bound atomic sidecar + registry activation
foundry-opt bootstrap evaluation status      # phase state, sidecar activation state, resume action
foundry-opt bootstrap evaluation inspect     # approved bounds, receipt finalization, persisted sidecar
foundry-opt bootstrap evaluation replace     # explicit replacement of an already active bundle
```

All commands emit stable JSON, accept an explicit repository root, never prompt, and exit
with typed codes (`20` config, `22` missing, `23` conflict, `24` stale, `25` apply).

**Exactly one human approval** covers the whole onboarding run: the evaluations phase
approval. A generated rubric is auto-adopted without a second prompt, but only because every
dynamic output must satisfy the pre-approved bounds and fail-closed gates first.

## One approval-bound composite action per agent

`BootstrapPlanInput.evaluations_phase.agents[].onboarding_contract` is the authoritative
`contract_version: 3` request. The plan factory turns each contract into exactly one action:

```text
evaluations:<repo-agent-id>:onboarding   kind=evaluation_onboarding
```

The contract carries only reviewable, deterministic inputs:

| Section | Content |
| --- | --- |
| `bounds` | target sample count, 10/5 minimums, 15-sample telemetry threshold, maximum generated samples, maximum evaluators, 100% safety pass rate, the required safety evaluator names, required headroom, allowed dataset types and provenance |
| `telemetry_probe` | prerequisite availability, useful sample count, window identifier, derived eligibility |
| `dataset_plan` | requested development/validating names and version, dataset type, connection, generation kind, deterministic generation job id, generation-context fingerprint, reviewed reuse candidates |
| `evaluator_plan` | requested evaluator name/version, deterministic rubric generation job id *or* one reviewed reuse candidate, the required built-in safety evaluator **names**, objective normalization and weight |
| `definition_plan` / `activation_plan` | requested definition names, model deployment, owned draft agent name/version |
| `sidecar_policy` | static sidecar content: roots, editable paths, runtime/protocol, Foundry binding, models, candidate bounds, decision policy, hard guardrails, deployment policy |
| `replacement` | previous bundle/sidecar lineage retained until an explicit replacement activates |

It deliberately carries **no** dynamic immutable identifiers: no dataset ids (except approved
reuse candidates), no evaluator version ids, no definition ids, no run ids, no generated
sample counts, no split hashes, and no scores.

## Staged provider state machine

`evaluation apply` executes the approved contract through ordered stages, recording each one
in provider state so a restart resumes instead of repeating work:

```text
inventory -> generation -> split -> evaluator -> definitions -> activation -> cleanup
```

- **inventory** — reuse reviewed immutable assets before generating anything.
- **generation** — create/resume the deterministic generation job. Trace output is accepted
  only at 15+ useful samples; below that the run fails closed and a partial trace dataset is
  never registered.
- **split** — deterministically split the case index (identifiers only, never row content),
  enforce about two-thirds/one-third with 10/5 minimums and zero overlap, and register the
  two immutable dataset versions.
- **evaluator** — reuse a suitable immutable evaluator, or generate the default rubric once,
  validate its structure, and record `auto_generated_unreviewed` provenance.
- **definitions** — create or adopt the immutable development/validating definitions that
  measure the objective evaluator and every required built-in safety evaluator.
- **activation** — submit both runs, read back per-criterion measurements, and gate on
  execution, measurable headroom, and a 1.0 pass rate for every configured safety evaluator.
- **cleanup** — always delete the owned draft, whether or not the gates passed.

## Real cloud evaluation APIs

Every stage that touches the service uses the current `AIProjectClient.get_openai_client().evals`
surface rather than a local approximation:

| Stage | Call | Shape |
| --- | --- | --- |
| definitions | `evals.create` | `data_source_config={"type": "azure_ai_source", "scenario": "synthetic_data_gen_preview"}` with one `TestingCriterionAzureAIEvaluator` (`{"type": "azure_ai_evaluator", "name", "evaluator_name", "evaluator_version", "data_mapping"}`) per approved evaluator |
| generation (synthetic) | `evals.runs.create` | `data_source.type = "azure_ai_synthetic_data_gen_preview"` with `item_generation_params(samples_count, prompt, model_deployment_name, output_dataset_name)` and `target={"type": "azure_ai_agent", "name", "version"}`; the run returns the immutable `output_dataset_id` that is then split |
| generation (traces) | `beta.datasets.create_generation_job` | agent/dataset sources, unchanged |
| activation | `evals.runs.create` | `TargetCompletionEvalRunDataSource` — `{"type": "azure_ai_target_completions", "source": {"type": "file_id", "id": <immutable split dataset id>}, "target": {"type": "azure_ai_agent", ...}}` |

Every criterion binds an immutable evaluator id and maps `query` to `{{item.query}}` and
`response` to `{{sample.output_text}}`, so the service performs the scoring and the adapter
only reads back per-criterion `passed`/`failed`/`errored` counts. No Python-grader passthrough
remains on the onboarding path; the pre-v3 granular `evaluation_definition`/`activation_run`
action kinds keep their structural audit behaviour purely so older plans can be resumed and
rolled back.

Honest caveat: `azure_ai_synthetic_data_gen_preview` has no typed dict in
`azure-ai-projects` 2.4.0b/`openai` 2.53.0, so its body is built as an explicitly validated
mapping. The literal `item_generation_params.type = "synthetic_data_gen"` is taken from the
documented preview shape and has not been confirmed against a live run.

## Required built-in safety bundle

Live Foundry projects publish individual built-in safety evaluators from the shared registry
with immutable versioned ids, for example:

```text
azureml://registries/azureml/evaluators/builtin.violence/versions/3
azureml://registries/azureml/evaluators/builtin.self_harm/versions/3
azureml://registries/azureml/evaluators/builtin.indirect_attack/versions/3
```

A dev-project capability probe found 43 built-ins and **no** aggregate
`azureai://built-in/evaluators/content_safety`, so the guardrail is a bundle resolved by
canonical name rather than a fabricated aggregate id:

| Canonical name | Required |
| --- | --- |
| `violence` | yes |
| `sexual` | yes |
| `self_harm` | yes |
| `hate_unfairness` | yes |
| `indirect_attack` | yes |
| `protected_material` | optional; still enforced at 100% when the project publishes it |

Rules:

- The approved contract requests safety evaluators **by name**; their immutable registry ids
  are discovered at apply time and recorded in the receipt finalization.
- Name matching accepts the plain name, the `builtin.`-prefixed catalog name, and the id path
  segment, so `violence` and `builtin.violence` resolve identically.
- A project that cannot supply every required evaluator fails closed and activation is
  blocked; there is no advisory substitute.
- Every configured safety evaluator must be measured in both activation phases and pass at
  exactly `1.0`. A single sub-rate blocks activation and rolls back created resources.
- The sidecar must declare every required safety evaluator as a `required` hard guardrail at
  `required_pass_rate: 1.0`.
- The legacy aggregate `content_safety` id is honored **only** when a project actually returns
  it, in which case it covers the whole bundle.

Every immutable identifier the stages discover is written to the receipt and provider state as
an `EvaluationFinalization`, sealed with its own `finalization_hash`, and re-verified against
the approved contract bounds before it is accepted.

Two narrow adapter seams are explicit and fail closed when unavailable: the dataset case index
(identifiers only) and split materialization (an injected writer that streams selected case
identifiers into a new blob and returns its URI). Neither ever returns row content.

## Receipt-bound activation

The repository phase never writes `<agent-root>/.foundry/foundry-opt.yaml`. After a successful
evaluations phase, `evaluation activate` verifies:

1. the applied evaluations phase receipt and its single approval record,
2. the parent plan hash, the evaluations phase plan hash, and the provider receipt hash,
3. the exact recorded runtime SHA,
4. that the reviewed plan input rebuilds the approved composite actions exactly, and
5. that every recorded finalization satisfies its approved contract and gates.

It then derives the sidecar from static policy plus receipt-recorded immutable ids, writes it
atomically with `evaluation_lineage.activation_binding` (operation, plan, approval, receipt,
runtime SHA, and the finalization binding hash), enables the activated agents in
`.foundry-opt/registry.yaml`, and advances `.foundry-opt/bootstrap.lock.json`.

Agents that fail any gate keep their previous sidecar and default evaluator bundle. An
explicit replacement additionally requires the reviewed previous sidecar digest and previous
bundle objective hash to match what is on disk, so a failed replacement always retains the old
contract.

## Registry and deployment semantics

`.foundry-opt/registry.yaml` records desired enabled configuration, not live status. Only
agents with a successful activation become `enabled: true`. `bound-diverged` and
`bound-unknown` agents may optimize drafts, but their sidecar keeps
`deployment.require_aligned_binding: true` and `deployment.enabled: false`, so merge-time
deployment stays blocked until alignment is proven. `ready-unbound` and `not-ready` agents
stop before onboarding: they plan no action, receive no sidecar, and stay disabled.

Alignment is proven with reviewed binding evidence — see
[Binding evidence](binding-evidence.md). When the plan input carries evidence,
`bootstrap evaluation plan` re-derives each classification and refuses a contract that claims
a different one.
