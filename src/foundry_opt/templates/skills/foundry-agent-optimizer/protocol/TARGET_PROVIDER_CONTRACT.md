# Target and provider contract

Do not confuse the target with the optimization loop.

## Target

The target agent repository supplies:

- source or prompt definition
- editable allowlist
- target kind
- baseline agent/version
- runtime and deployment metadata
- training, confirmation, and final-validation data
- evaluators, primary metric, and guardrails

The target does not:

- generate Tenzing ideas
- choose lineage
- control termination
- select the winner

## Tenzing

Tenzing owns:

- observation
- diagnosis and hypothesis
- primary mutation target
- candidate sequence and round membership
- lessons and lineage
- current-best promotion
- termination

## Provider

The provider executes:

- baseline observation/evaluation
- candidate draft creation
- exact source or definition verification
- evaluation
- bounded concurrent candidate execution when declared
- durable round-barrier inputs and receipts when declared
- route checks
- cleanup
- evidence references

### Prompt target

Verify the exact model, instructions, tools, and generation configuration.
Candidate versions must be owned prompt drafts.

### Hosted target

Verify deterministic source/package identity and hosted runtime definition.
Candidate versions must be owned hosted drafts.

## Capability declaration

Providers must declare whether they support:

- prompt drafts
- hosted source drafts
- exact read-back verification
- route fingerprinting
- training, confirmation, and final validation
- maximum concurrent drafts, deployments, and evaluations
- parallel candidate scheduling and deterministic batch barriers
- resumable state
- cleanup receipts
- local review materialization
- GitHub broker/projection

Missing capabilities are blockers or explicit assurance limitations, never
success-shaped defaults.

A provider limit of one is valid sequential execution. Do not claim parallel
round support merely because independent API calls could be launched
concurrently.
