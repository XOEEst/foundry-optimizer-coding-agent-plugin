# agent-optimizer-v2

`agent-optimizer-v2` is a provider-neutral Agent Skill for improving an agent
through controlled, measurable experiments. It inspects an exact baseline,
proposes a plan, evaluates isolated candidates with the pinned Tenzing loop,
and reports a winner only when the configured evidence gates pass.

The Skill never starts an evaluation or mutation until you approve its complete
optimization plan. It also never applies a winning candidate to the real agent
without a separate explicit approval.

## Install the Skill

Copy this entire directory, without changing its name or internal layout, to a
location supported by your coding agent:

- `.github/skills/agent-optimizer-v2/` for one repository
- `~/.copilot/skills/agent-optimizer-v2/` for personal use across repositories

Keep `SKILL.md`, `guides/`, `references/`, and `templates/` together. The
`SKILL.md` file is the entrypoint; this README is user documentation.

The runtime must also provide bindings for the atomic operations defined in
`guides/operations.md`. Provider bindings choose how to inspect, isolate,
mutate, invoke, evaluate, clean up, and optionally apply candidates. They do
not choose optimization ideas or winners.

## Before You Start

Prepare or identify:

- the agent location or identity
- the surfaces that may be changed, such as instructions or tool descriptions
- a training dataset
- a separate validation dataset for a verified winner
- an evaluator, primary metric, direction, and any guardrails
- an execution provider that can bind the required atomic operations

The Skill can discover read-only configuration when a field is `auto`, but it
does not invent missing values or widen mutation permissions. If no clean,
separate validation dataset is available, the best possible result is
`unverified`, not `winner`.

## Run with Natural Language

Invoke `/agent-optimizer-v2` in chat, or ask the coding agent to optimize an
agent in natural language. Include the target and mutation boundary explicitly.

For example:

```text
/agent-optimizer-v2 Optimize the agent at ./agents/support-agent.
You may change only its instructions and tool descriptions. Use the configured
training, confirmation, and validation datasets, maximize task_success, try at
most 6 candidates, and do not apply the winner automatically.
```

The Skill may also be selected automatically when your request matches the
description in `SKILL.md`.

## Run with YAML

Start from `templates/target.yaml`, edit the user-level fields, and provide the
file in your request:

```yaml
agent:
  location: ./agents/support-agent
  kind: auto

mutation:
  allowed:
    - instruction
    - tool_description

evaluation:
  train_dataset: ./eval/train.jsonl
  confirmation_dataset: ./eval/confirmation.jsonl
  validation_dataset: ./eval/validation.jsonl
  evaluator: task-success-v1
  primary_metric: task_success
  direction: maximize
  guardrails: []

split:
  strategy: deterministic
  seed: "0"
  search: auto
  confirmation: auto

budget:
  candidates: 6
  plateau_rounds: auto

apply_winner: false
```

Then invoke the Skill:

```text
/agent-optimizer-v2 Optimize the agent using ./optimizer-target.yaml.
```

Relative paths are resolved from the YAML file's directory. For inline YAML,
they are resolved from the agent root. You may combine YAML with natural
language; the latest explicit natural-language value overrides YAML for
non-permission fields. Mutation permissions always use the narrowest explicit
scope.

## What Happens During a Run

1. The Skill reads its loop, operation, data, tracking, and scorecard guides.
2. It inspects the target, datasets, evaluator, provider bindings, and exact
   baseline without changing anything.
3. It presents a complete pre-run plan, including unresolved values and
   limitations.
4. You correct or explicitly approve the plan.
5. The Skill evaluates the baseline and isolated candidates, persists every
   durable transition, and completes every opened Tenzing cycle.
6. It freezes one provisional winner before opening the sealed validation
   dataset.
7. It cleans temporary resources and writes durable run state, a scorecard, and
   a result following the templates in `templates/`.
8. If a verified winner exists and applying it was allowed, the Skill asks for
   explicit approval before changing the real agent.

Do not approve a plan that leaves required values hidden behind `auto`. The
displayed plan must contain each discovered value or list it as an open
question.

## Outcomes

Every completed run ends with one of these outcomes:

- `winner`: a candidate passed search, confirmation, and separate validation
- `no_winner`: no candidate passed the required improvement gates
- `unverified`: a provisional improvement exists but clean validation evidence
  is unavailable
- `blocked`: required data, operations, permissions, or valid evidence are
  unavailable

See `SKILL.md` for the authoritative execution contract and
`references/tenzing/climb.md` for the normative optimization loop.