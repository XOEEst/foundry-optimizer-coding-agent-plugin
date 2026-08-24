# Interrupted or partial runs

This static process has no resumable bootstrap protocol. After an interruption
or partial result:

1. keep successful local and remote changes
2. read `.foundry-opt/bootstrap-report.md` and inspect the live repository and
   cloud resources
3. begin a fresh read-only discovery
4. render a new exact diff and remote plan
5. obtain a new combined approval before any additional mutation

Follow [Failure handling](failure-handling.md) for the required handoff.
