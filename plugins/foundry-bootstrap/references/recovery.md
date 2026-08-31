# Interrupted or partial runs

This static process has no resumable bootstrap protocol. After an interruption
or partial result:

1. keep successful local and remote changes
2. read `.foundry-opt/bootstrap-report.md` and inspect the live repository and
   cloud resources
3. ask the user to confirm the folder scope, selected agent subset, and shared
   Foundry project endpoint again
4. begin a fresh read-only discovery for only that group
5. render a new exact diff and remote plan that preserves excluded and
   previously onboarded agents
6. obtain a new combined approval before any additional mutation

Follow [Failure handling](failure-handling.md) for the required handoff.
