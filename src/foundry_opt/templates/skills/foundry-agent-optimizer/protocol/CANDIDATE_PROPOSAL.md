zhege yous# Candidate proposal

Use this structure before every candidate handoff.

```text
Round:
Candidate slot:
Experiment kind:
Diagnosis:
Hypothesis:
Expected measurable effect:
Primary mutation target:
Execution parent:
Idea parents:
Model:
Implementation boundary:
```

## Fields

### Round and candidate slot

Use the preallocated round ID and candidate slot from the frozen batch. A
candidate must not choose its ID after observing sibling completion.

### Experiment kind

Choose one:

- `exploration` - start a new direction from the immutable baseline
- `refinement` - improve one assessed candidate
- `synthesis` - combine lessons from multiple assessed candidates

### Diagnosis

Name one concrete failure pattern supported by issue or evaluation evidence.

### Hypothesis

State one falsifiable claim about why the proposed source change should improve
the frozen objective.

Keep it concise enough for the current runtime's candidate hypothesis field.

### Expected measurable effect

Identify the primary metric, focused cases, regression count, guardrail, or
approved check that should change.

Do not promise an effect the frozen verification contract cannot measure.

### Primary mutation target

Choose one main surface:

- `instruction`
- `tool_description`
- `model`
- `workflow`
- another explicitly allowlisted target

A candidate may contain tightly coupled supporting edits, but it must test one
primary mechanism. Do not combine unrelated prompt, tool, model, workflow, and
cleanup changes.

The target repository declares what is editable. The proposal chooses within
that allowlist; it cannot widen it.

### Execution parent

Choose exactly one:

- `baseline`
- one finalized candidate ID

This is the Git ancestry used by the current runtime.

### Idea parents

For refinement, use the execution parent.

For synthesis, list the assessed candidates whose lessons are being combined.
The execution parent must be among them.

For each idea parent, record:

```text
candidate ID | positive, negative, or contrast | exact contribution
```

Idea parents must come from closed earlier rounds under the same run-contract
hash. Same-round, unknown, duplicate, forward, and cyclic references are
invalid.

Idea lineage does not merge scores or commits. Construct one new patch on the
execution parent and evaluate the complete candidate independently.

Current limitation: only the execution parent is persisted structurally.
Additional idea parents must stay in the proposal rationale and redacted issue
evidence. Do not claim the runtime stored them.

### Model

Choose one model allowed by the narrowed repository policy.

### Implementation boundary

Name the intended files or component inside the allowed editable paths.

## Runtime mapping

The current CLI persists:

- candidate ID
- model
- hypothesis
- one execution parent

Encode the diagnosis and expected effect into a coherent hypothesis when
calling the current CLI. Do not pass unsupported structured or multi-parent
arguments. The current CLI also lacks native round allocation and batch
barriers; use a round width of one unless the selected runtime declares those
capabilities.
