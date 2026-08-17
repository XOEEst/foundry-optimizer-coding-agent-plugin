# {{PROJECT_NAME}}

This is an autonomous **improvement loop**: an agent 1) brainstorms ideas, 2) sets up experiments
to implement them, 3) learns from the results, and 4) uses what it learned to generate new ideas —
climbing toward a better score, one experiment at a time.

> This file (`climb.md`) is the **steady-state loop**. It is filled in from a template by `INIT.md`.
> If you still see `{{PLACEHOLDER}}` markers below, the loop has not been initialized yet — run
> `INIT.md` first.

## Background

The background of the problem can be found in `climb_config/background.md`.

## Objective

The objective — the primary metric, its direction, and any soft constraints — is in
`climb_config/objective.md`.

## Data

{{DATA_SECTION}}

## Environment

Setup and development commands (toolchain, how to install and run things) are in
`climb_config/environment.md`.

## Do's and Dont's

What you can and absolutely cannot do is in `climb_config/dos-and-donts.md`.

## The Improvement Loop

1. **Obtain Background:** Read `climb_config/background.md` to familiarize yourself with the problem.

2. **Generate Ideas:** Generate 2-3 experimentation ideas. To generate them you can: 1/ look at
   previously tried ideas and their results (if any exist), 2/ search the internet and read
   relevant papers/blogs/docs, 3/ use your own knowledge. This is the time to be creative — let
   your creative juices flow! Write each idea to a separate markdown file under
   `experiment_tracking/ideas/`, following `climb_config/tracking-experiments.md`. For each idea,
   record its **lineage** — the parent experiment(s) it derives from: the current best or another
   prior experiment, the `baseline`, **several** parents when the idea combines earlier experiments,
   or **none** (`-`) when it's a wholly new line of thinking that starts its own island.

3. **Idea Implementation Loop:** For each idea from step 2:
   a. Pick a previously unimplemented idea and read its file under `experiment_tracking/ideas/<idea_file>.md`.
   b. **Refresh your memory** by re-reading:
      - The objective in `climb_config/objective.md`
      - The do's and don'ts in `climb_config/dos-and-donts.md`
      - How to track experiments and record results in `climb_config/tracking-experiments.md`
      - The idea file you are working on
   c. **Create a branch** named the same as the idea file.
   d. **Implement the idea:** Modify only the editable area declared in `climb_config/dos-and-donts.md`,
      then produce whatever inputs the evaluation needs.
   e. **Evaluate:** Follow `climb_config/evaluation.md` to produce and score a run.
   f. **Track results:** Record the outcome per `climb_config/tracking-experiments.md` — append the
      row (including the experiment's `parent`(s)) to `results.tsv` and regenerate the exploration
      DAG in `experiment_tracking/tree.md`.
   g. **Commit and continue:** Commit the idea branch and move on to the next idea.

4. **Continue per the termination condition (below).** When not terminating, go back to step 2 and
   generate another set of ideas.

## Termination Condition

{{TERMINATION_CONDITION}}

Regardless of the termination condition, **never pause mid-experiment to ask for permission.** Once
an experiment is underway, carry it through to a `keep`/`discard`/`crash` outcome and log it before
stopping. The one thing that ends the loop is the termination condition above (or a manual
interruption).

You are a completely autonomous researcher trying things out. If an idea works, keep it. If it
doesn't, move on. Brainstorm, implement, record progress, and iterate. If you feel stuck, you may
rewind — but do this very, very sparingly (if ever). If you run out of ideas, think harder: re-read
the referenced papers and the in-scope files for new angles, combine previous near-misses, or try
more radical changes.

## Operational Guidance

**Runtime & hangs:** Experiments vary widely in cost. Don't impose a single short timeout. Instead:
(1) estimate the expected runtime before launching and record it, so a long-but-expected run isn't
mistaken for a hang; (2) kill any run that makes **no observable progress** for ~15–30 minutes —
that's a hang, not slow work; (3) keep a generous absolute wall-clock backstop (e.g. a few hours,
adjustable) so nothing runs unbounded by accident.

**Crashes:** If a run crashes, use your judgment. If it's something dumb and easy to fix (a typo, a
missing import), fix it and re-run. If the idea itself is fundamentally broken, skip it, log `crash`
as the status, and move on.

## Protocol Is Read-Only

During the loop, treat `climb.md` and everything under `climb_config/` as **read-only**. They define the
protocol; do not edit them while experimenting.
