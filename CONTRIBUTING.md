# Contributing

The project is publicly usable, but code contributions are currently limited to
approved maintainers while the bootstrap contract is stabilized.

You may open an issue for reproducible bugs or documentation gaps. Do not
include credentials, customer data, prompts, responses, traces, or evaluation
dataset rows.

Maintainer changes must:

- use a focused pull request
- preserve backward compatibility or document the intentional break
- include tests for observable behavior
- pass Windows and Linux validation
- retain all third-party license and attribution files

Documentation and architecture changes must:

- keep [`docs/README.md`](docs/README.md) as the navigation authority
- update or add an ADR when a durable repository-level decision changes
- preserve the ADR number and supersession history instead of rewriting an
  earlier decision
- keep private rollout evidence, customer identifiers, local paths, prompts,
  responses, traces, and dataset rows out of the public tree
- pass `uv run python tools/check_docs.py`
