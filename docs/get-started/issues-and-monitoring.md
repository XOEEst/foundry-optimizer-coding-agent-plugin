# Issues and monitoring

After bootstrap, owners mostly do two things: open or review optimization
issues, and watch the evidence that comes back.

Assign the issue to standard Copilot. The setup workflow and repository
instructions direct it to the installed optimizer skill and CLI.

If your team explicitly installs the optional custom-agent example, you may
select that profile from the assignment dropdown instead. It is not selected
automatically.

## What an optimization issue should answer

- What agent should change?
- What outcome do you want?
- What failures or weak spots did you observe?
- What must not change?
- How much candidate churn are you willing to review?

The **Optimization goal** remains descriptive prose. It never selects an
evaluator. To change the deciding metric for one issue, set **Optional
primary metric** to the exact emitted metric name and supply at least one
exact evaluator ID. For example:

- primary metric: `task_completion`
- evaluator ID: `azureml://registries/azureml/evaluators/builtin.task_completion/versions/19`
- dataset blank = reuse repository defaults.

Primary-metric, evaluator, dataset, and command overrides require the trusted
issue-author binding to report `write`, `maintain`, or `admin` permission.

Verification inputs are optional, but the choice changes how honest the
final recommendation can be.

## Verification choices, in plain language

Foundry Optimizer resolves verification in this order:

1. **Issue evaluators, optionally with an issue development dataset**
   - Evaluator-only overrides reuse the repository/runtime default
     development and validating definitions and datasets.
   - Issue evaluator IDs are merged with default evaluator IDs, preserving
     policy and safety evaluators with deterministic de-duplication.
   - Supplying a dataset replaces only the development dataset; validation
     continues to use the repository default.
2. **Repository defaults**
   - Best when the repository already has an activated default dataset and
     evaluator bundle.
   - Owners do not need to repeat the same inputs in every issue.
3. **Issue checks**
   - Use approved repository commands supplied in the issue.
   - Good for policy or smoke validation, but not for choosing a numeric
     winner.
4. **Repository default checks**
   - Reuse the repository's normal checks when no exact Foundry
     evaluation bundle is active.
5. **None**
   - The issue explicitly accepts a no-evidence path, or the repository
     has no verification inputs to use.

Important: an issue-supplied dataset without evaluators is not enough for a
Foundry evaluation winner and is ignored. Evaluators without a dataset are
valid when repository/runtime Foundry defaults are available.

## Honest result labels

Keep the label matched to the evidence:

- **winner** - a quantitative verification path ran and one candidate won.
- **no_winner** - a quantitative verification path ran and no candidate
  earned deployment.
- **recommended** - no quantitative winner path ran, but the available
  checks and owner policy still support recommending the change for human
  review.
- **proposed_unverified** - a change exists, but the evidence is not
  strong enough to recommend deployment.

In short: use `winner` or `no_winner` for evaluation-backed decisions;
use `recommended` or `proposed_unverified` when you are being explicit
that the result is not a measured Foundry win.

## Deployment without evaluation

Deployment without quantitative evaluation is a **policy choice**, not a
hidden fallback.

- Some repositories will block deployment until binding and evaluation are
  activated.
- Some repositories may allow deployment without evaluation.
- When it is allowed, the owner-facing status should warn clearly that
  the deployment is unverified or policy-controlled.

If the repository default profile still has verification turned off, the
safe summary is: enabled for workflow use, not independently verified.

## Where to monitor

- **GitHub issues and pull requests** - natural-language goal, candidate
  discussion, and final recommendation.
- **GitHub Actions** - bootstrap, validation, PR gate, and deployment run
  history.
- **GitHub environments** - optimizer and deployment approval boundaries.
- **Azure identity and RBAC** - why a workflow could or could not reach a
  Foundry project.
- **Foundry project** - the target agent, datasets, evaluators, and runs
  used for default verification.

## Example monitoring summary

> The issue used repository-default evaluators, so the result is eligible
> for a quantitative `winner` or `no_winner`. The PR gate passed, the
> validating run is recorded in Foundry, and deployment is still waiting
> on the protected GitHub environment.

## Related detail

- [Bootstrap](bootstrap.md)
- [Evaluation gates](evaluation-gates.md)
- [Run an optimization](../guides/run-an-optimization.md)
- [Optimize-job architecture](../architecture/optimize-job.md)
- [Evidence, state, and receipts](../reference/evidence-state-and-receipts.md)
- [Evaluation onboarding](../evaluation-onboarding.md)
- [Binding evidence](../binding-evidence.md)
