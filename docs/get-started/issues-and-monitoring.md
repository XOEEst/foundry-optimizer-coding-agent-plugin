# Issues and monitoring

After bootstrap, owners mostly do two things: open or review optimization
issues, and watch the evidence that comes back.

## What an optimization issue should answer

- What agent should change?
- What outcome do you want?
- What failures or weak spots did you observe?
- What must not change?
- How much candidate churn are you willing to review?

Verification inputs are optional, but the choice changes how honest the
final recommendation can be.

## Verification choices, in plain language

Foundry Optimizer resolves verification in this order:

1. **Issue dataset plus issue evaluators**
   - Best when an owner wants one exact Foundry dataset and one exact set
     of evaluator IDs.
   - This is the strongest issue-level quantitative path.
2. **Repository defaults**
   - Best when the repository already has an activated default dataset and
     evaluator bundle.
   - Owners do not need to repeat the same inputs in every issue.
3. **Issue checks**
   - Use repository commands or checks named in the issue.
   - Good for policy or smoke validation, but not for choosing a numeric
     winner.
4. **Repository default checks**
   - Reuse the repository's normal checks when no exact Foundry
     evaluation bundle is active.
5. **None**
   - The issue explicitly accepts a no-evidence path, or the repository
     has no verification inputs to use.

Important: an issue-supplied dataset without evaluators, or evaluators
without a dataset, is not enough for a Foundry evaluation winner. Those
partial inputs are ignored and should be corrected by the owner.

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
- [Evaluation onboarding](../evaluation-onboarding.md)
- [Binding evidence](../binding-evidence.md)
