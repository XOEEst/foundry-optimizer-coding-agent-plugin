# Distribution and pinning

This repository is the source authority for `foundry-opt`.

## Pre-release policy

Until the recommended branch protection is enabled, privileged workflows must
use an explicit reviewed commit. Do not execute floating `main` with GitHub OIDC
or Foundry publication permissions.

Every operation must:

1. resolve or receive one exact commit
2. fetch that commit over public HTTPS
3. verify the checkout commit
4. verify the expected `uv.lock` SHA-256
5. record the runtime commit in its receipt
6. reuse the recorded commit when resuming

Customer templates currently demonstrate the exact-pin compatibility contract.
The guided bootstrap will add a channel option while preserving explicit pins.

## Rollback

Rollback means selecting a previously reviewed commit, not rewriting public
history. Record the known-good commit and lock hash in the customer repository
or bootstrap receipt before starting a privileged operation.
