# Recovery

Bootstrap stores durable, non-secret operation state outside the customer
repository.

- Windows: `%APPDATA%\foundry-opt\bootstrap`
- Other platforms: `~/.foundry-opt/bootstrap`

## Resume after interruption

Invoke `/foundry-bootstrap` again from the same repository. `start` finds the
single active operation for that repository and returns its current review or
question. A completed or rolled-back operation is stale and does not prevent a
new operation. If more than one active operation exists, bootstrap fails closed
instead of choosing one.

Resume still requires the exact runtime repository and commit plus the exact
repository root, identity, branch, and commit recorded by the operation.

## Stale question

If another turn advanced the operation, the runner rejects the old question
ID. Refresh status and answer only the newly returned question.

## Failed provider operation

- A blocked Foundry target remains at target resolution. Correct the endpoint
  or agent name through the documented answer flags, or fix Azure access and
  retry the same reviewed target.
- Repository and connection operations retain redacted child receipts.
- A partial GitHub/Azure connection compensates only resources created by that
  operation.
- A failed local deployment retains completed-agent receipts and can resume the
  same exact approved plan. Published regular versions are not deleted.
- Operation-owned evaluation drafts are cleaned before a successful
  publication is reported.

## Rollback

Use rollback only when the skill offers it:

- `repository` restores reviewed preimages without deleting unrelated files.
- `connection` reverses operation-owned GitHub/Azure changes.
- `commit` restores the original branch and reviewed index/worktree snapshot.
- `deployment` is not offered because immutable regular versions are retained
  as audit history.

When several steps exist, the skill offers rollback in dependency order:
`commit`, then `connection`, then `repository`.

If runtime, repository identity, commit, profile, target, or provider
fingerprints drift, bootstrap fails closed and requires a fresh review.
