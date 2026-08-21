# Recovery

Bootstrap stores durable, non-secret operation state outside the customer
repository.

- Windows: `%APPDATA%\foundry-opt\bootstrap`
- Other platforms: `~/.foundry-opt/bootstrap`

## Resume after interruption

Invoke `/foundry-bootstrap` again. The skill uses the stored operation ID,
reloads the exact runtime pin, validates repository and commit drift, and shows
the current review or question.

## Stale question

If another turn advanced the operation, the runner rejects the old question
ID. Refresh status and answer only the newly returned question.

## Failed provider operation

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

If runtime, repository identity, commit, profile, target, or provider
fingerprints drift, bootstrap fails closed and requires a fresh review.
