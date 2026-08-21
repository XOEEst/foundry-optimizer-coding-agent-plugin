# Evaluation gates

Treat pull request evaluation and main-branch deployment as two separate
owner controls.

## Why two gates

- A **pull request gate** helps you learn whether a proposed change is
  worth merging.
- A **deployment gate** helps you decide whether the reviewed main-branch
  change is allowed to publish.

They often reuse the same GitHub environments, Azure identity, Foundry
project, datasets, evaluators, and runs, but they answer different owner
questions.

## Template 1: opt-in PR gate

Use this workflow concept when you want a pull request to say:

- which agent changed
- what verification source it used
- whether the outcome is `winner`, `no_winner`, `recommended`, or
  `proposed_unverified`
- what the owner should review next

**Conceptual copy and enable steps**

1. Copy the PR gate template into `.github/workflows/` when the template
   is available.
2. Point it at the repository's selected agent IDs and managed profile.
3. Configure it to run on the PR events or manual trigger you want.
4. Make it publish a visible summary back to the pull request.
5. If you want it to block merges, add it to branch protection as a
   required status after you trust the signal.

## Template 2: main-branch deployment gate

Use this workflow concept when you want main-branch deployment to say:

- which exact reviewed commit is being published
- whether binding and verification requirements are satisfied
- whether deployment is fully verified or only policy-allowed
- which GitHub environment approval and Foundry target are involved

**Conceptual copy and enable steps**

1. Copy the deployment template into `.github/workflows/` when the
   template is available.
2. Bind it to the protected deployment environment created during
   bootstrap.
3. Require the reviewed branch and exact runtime pin policy.
4. Surface an explicit warning whenever policy allows deployment without
   evaluation.
5. Make sure owners can see the final link back to the GitHub run, Azure
   identity context, and Foundry target.

## Recommended owner stance

- Start with the PR gate in an informational mode.
- Turn on the deployment gate before you allow routine publication.
- Reuse repository-default evaluation assets when you want stable,
  repeatable signals.
- Keep no-evidence deployment paths rare, deliberate, and visibly warned.

## Related detail

- [Issues and monitoring](issues-and-monitoring.md)
- [Evaluation onboarding](../evaluation-onboarding.md)
- [Recommended branch protection](../branch-protection.md)
- [Identity and RBAC](../identity-rbac.md)
- [Distribution and pinning](../distribution.md)
