# References

Store reviewed notes, migration pointers, and source references for the
bootstrap plugin here.

This task tracks the directory only. It does not copy runtime sources or
third-party assets into the plugin tree.

## Bootstrap bridge quick reference

- Start:
  - downloaded skill: `python scripts/bootstrap.py start --repository .`
  - source checkout: `python plugins/foundry-bootstrap/scripts/bootstrap.py start --repository .`
- Resume: rerun `status --operation-id <id>` with the same private state root,
  show the fresh owner markdown, and use the returned `next_question` or
  `available_actions`.
- Recovery:
  - use `status` after interruptions or stale-question errors to refresh the
    current `question_id`
  - use `rollback --operation-id <id> --step <repository|connection|commit|deployment>`
    only when the current machine envelope offers that rollback action
- The private state root defaults to `%APPDATA%\foundry-opt\bootstrap` on
  Windows and `~/.foundry-opt/bootstrap` elsewhere. `bootstrap.py` keeps runner
  state under `runner/` and uses `runtime/` for verified runtime reinstall.
