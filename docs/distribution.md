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

Customer templates and generated bootstrap plans use this exact-pin contract.
The current reviewed customer runtime is
`260ca61b2cde7e9b9f03a81bf880c0453f9e8981`; later upgrades must select a new
reviewed commit explicitly and refresh the managed lock.

## Rollback

Rollback means selecting a previously reviewed commit, not rewriting public
history. Record the known-good commit and lock hash in the customer repository
or bootstrap receipt before starting a privileged operation.
