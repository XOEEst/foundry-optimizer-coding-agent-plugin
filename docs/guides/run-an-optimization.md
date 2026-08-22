# Run an optimization

This guide covers the current operator flow for one optimize issue.

Start with [Bootstrap](../get-started/bootstrap.md). That page is the onboarding authority for registering the repository, wiring OIDC, and enabling the managed workflows.

## 1. Open the current issue form

Use the managed issue form at [`src/foundry_opt/templates/customer-repo/.github/ISSUE_TEMPLATE/foundry-optimize-agent.yml`](../../src/foundry_opt/templates/customer-repo/.github/ISSUE_TEMPLATE/foundry-optimize-agent.yml).

The current public form asks for:

- one `repoAgentId` from `.foundry-opt/registry.yaml`, or an explicit Foundry target for migration cases
- the optimization goal
- observed failures or evidence
- constraints and guardrails
- changed candidates
- an optional narrower editable scope
- an optional primary metric
- optional exact evaluator IDs
- an optional exact verification dataset ID or URI
- optional verification commands
- an optional no-evidence acknowledgement

Notes about the current surface:

- The managed form is designed for standard Copilot and exact, reviewable inputs.
- Exact evaluator inputs are supported today. Friendly evaluator name or version pickers are not.
- Exact dataset IDs or URIs are supported today. Friendly dataset name or version pickers are not.
- Issue-supplied evaluator IDs, datasets, and verification commands require the trusted issue binding to show `write`, `maintain`, or `admin` permission for the issue author.
- The parser still accepts a hidden compatibility-only model filter from older issue bodies, but the current public form intentionally does not expose candidate-model selection.

## 2. Assign the issue to standard Copilot

Assign the completed issue to Copilot. The default public path is standard Copilot plus the installed optimizer skill and repository instructions.

If your repository also installs the optional custom-agent example, that profile can be chosen explicitly, but it is not selected automatically.

## 3. Understand verification precedence

For optimize jobs, verification resolves in this order:

1. issue evaluator IDs, optionally with an issue development dataset
2. repository default Foundry bundle
3. issue verification commands
4. repository default checks
5. no evidence

Important details:

- An issue dataset without issue evaluators is ignored for quantitative selection.
- Issue evaluators without an issue dataset are valid when repository defaults already provide the development and validating datasets.
- Issue evaluator IDs are merged with the repository-default evaluator bundle instead of replacing policy and safety evaluators.
- Repository checks can support a recommendation, but they do not create a measured Foundry winner.

See [Issues and monitoring](../get-started/issues-and-monitoring.md) for the same precedence in plain language.

## 4. Watch the visible evidence

The job records redacted issue comments with stable markers:

- baseline: `foundry-opt-poc:<job_id>:baseline`
- candidate: `foundry-opt-poc:<job_id>:candidate:<candidate_id>`
- final: `foundry-opt-poc:<job_id>:final`

Visible evidence includes the shared commit, base commit, selected verification mode, candidate lineage, hashes, evaluation links, metrics, guardrails, and the final label. Raw prompts, raw responses, traces, dataset rows, and credentials are intentionally excluded.

## 5. Know what the early PR means

The runtime expects one early draft pull request per optimize job when trusted issue context can bind it.

The important rule is timing:

- the PR can exist early
- the winner patch is not projected during candidate exploration
- projection happens in `job finish`

Finish behavior depends on the verification path:

- `winner` - validate the provisional winner, then project it to the early PR
- `recommended` - project the selected candidate after repository checks pass
- `proposed_unverified` - project the selected candidate only as an explicitly unverified proposal
- `no_winner` - close the early PR unchanged

A pull-request binding is therefore required for both projection and clean no-winner closure.

## 6. Read result labels honestly

| Label | Meaning |
| --- | --- |
| `winner` | A quantitative path ran and the provisional winner passed validation. |
| `no_winner` | A quantitative path ran and no candidate cleared the full bar. |
| `recommended` | Repository checks support recommending a change for human review, but there is no quantitative Foundry winner. |
| `proposed_unverified` | A candidate can be shown, but there is no approved evidence strong enough to recommend it as a measured win. |
| `platform_failure` | Platform or validation infrastructure failed; this is not a model score. |

Candidate comments can also use intermediate labels such as `keep`, `discard`, and `invalid` while the job is still in progress.

## 7. Resume instead of restarting blindly

Optimize jobs are resumable. Use the current state and receipts instead of duplicating work:

- `foundry-opt job status` to inspect state
- `foundry-opt job resume` to continue pending work safely

Baseline comments, candidate comments, cleanup, projection, and no-winner closure all use receipts so the runtime can replay incomplete work without duplicating completed writes.

## Related references

- [Issues and monitoring](../get-started/issues-and-monitoring.md)
- [CLI reference](../reference/cli.md)
- [Evidence, state, and receipts](../reference/evidence-state-and-receipts.md)
- [Optimize job](../architecture/optimize-job.md)
