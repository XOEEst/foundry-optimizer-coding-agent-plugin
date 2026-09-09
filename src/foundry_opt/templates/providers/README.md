# Provider bindings

The `agent-optimizer-v2` Skill is provider-neutral. It defines the atomic
operations the loop needs in `skills/agent-optimizer-v2/guides/operations.md`
and never names an API, CLI, SDK, or tool.

A **provider binding** lives here, outside the Skill, and connects those neutral
verbs to a concrete implementation. Each binding maps every operation in
`operations.md` to importable `module:qualname` entrypoints.

```
guides/operations.md            neutral verbs (inside the Skill)
        │  bound at run time
        ▼
providers/<name>/binding.yaml    verb -> foundry_opt entrypoint (outside the Skill)
        │  calls
        ▼
foundry_opt runtime + adapters   real deploy / invoke / evaluate / cleanup
```

At run time the executor picks a binding for the resolved `agent.kind`, records
its id and the chosen `isolation_mechanism` in the run's `provider_binding`
(see `skills/agent-optimizer-v2/templates/run-state.yaml`), and calls the named
entrypoints. Swapping providers means adding or editing a binding here; the
Skill itself never changes.

## Available bindings

| Binding | `agent.kind` | Isolation | Runtime |
|---|---|---|---|
| `foundry-hosted/binding.yaml` | `foundry_hosted` | git worktree + Foundry draft version | `foundry_opt.optimizer_runtime:FoundryOptRuntime` |
| `foundry-prompt/binding.yaml` | `foundry_prompt` | git worktree + Foundry prompt draft version | `foundry_opt.optimizer_runtime:FoundryOptRuntime` |

`tests/poc/test_provider_bindings.py` imports every entrypoint named in these
files, so a binding cannot silently drift from the `foundry_opt` code it binds.
