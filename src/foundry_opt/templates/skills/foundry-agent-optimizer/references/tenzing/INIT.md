# INIT — one-time setup for a Tenzing loop

**You are an agent. This file is a script for you to execute once, to turn this blank template into a
concrete improvement loop.** It is not documentation for a human to read. When you are done, this
loop will be ready to run via `climb.md`, and you will delete this file.

Do **not** run the improvement loop yet. Your only job here is setup.

---

## How this works

Every file in this template contains `{{PLACEHOLDER}}` markers. Your task is to interview the human,
then replace every placeholder with concrete content and remove the guidance HTML comments. After
that, the loop is defined entirely by `climb.md` + `climb_config/`.

Interview the human **one question at a time**. For each question, propose a recommended answer based
on anything you can already infer from the repo. Do not batch questions.

## Step 1 — Interview

Ask about, and record, each of the following:

1. **Project name** — a short name for what is being improved. → fills `{{PROJECT_NAME}}`.
2. **Background** — what system/artifact is being improved, why it matters, and what makes it hard.
   → fills `climb_config/background.md` (`{{BACKGROUND}}`).
3. **Primary metric** — the single number that decides whether one experiment beats another, and its
   **direction** (maximize or minimize). → fills `{{PRIMARY_METRIC}}`, `{{OBJECTIVE_DIRECTION}}`.
4. **Soft constraints** — secondary quantities that must not blow up (runtime, memory, cost, …), each
   with a tolerable-regression note. May be "none". → fills `{{SOFT_CONSTRAINTS}}`.
5. **Editable area** — the one directory the agent may modify. → fills `{{EDITABLE_AREA}}`.
6. **Dependency rule** — may the agent add/change dependencies? Any limits (offline, no GPU)?
   → fills `{{DEPENDENCY_RULE}}`.
7. **Read-only files** — the evaluator/harness/fixtures that must never be touched.
   → fills `{{READ_ONLY_FILES}}`.
8. **Held-out evaluation set?** — does one exist that would be invalidated by peeking?
   → decides `{{LEAKAGE_RULE}}` and whether `climb_config/data.md` + the `Data` section of `climb.md` stay.
9. **Data inputs** — if any, where they live and how to load them. → fills `climb_config/data.md`
   (`{{DATA_DETAILS}}`) and the `{{DATA_SECTION}}` reference in `climb.md`.
10. **Evaluation commands** — the exact command(s) to (a) produce a run/output and (b) score it and
    read off the primary metric. → fills `climb_config/evaluation.md`
    (`{{PRODUCE_RUN}}`, `{{SCORE_RUN}}`, `{{READ_METRIC}}`).
11. **Environment** — toolchain, setup commands, and common dev commands (test/lint/build).
    → fills `climb_config/environment.md` (`{{TOOLCHAIN}}`, `{{SETUP_COMMANDS}}`, `{{COMMON_COMMANDS}}`).
12. **Termination condition** — how the loop should end. Offer these choices and fill
    `{{TERMINATION_CONDITION}}` in `climb.md` with the corresponding text:
    - `forever` — never stop on your own; run until manually interrupted. Do NOT pause to ask "should
      I keep going?" — the human may be away and expects continuous progress.
    - `n-iterations` — stop after N completed experiments (ask for N).
    - `until-target` — stop when the primary metric reaches a target value (ask for the target).
    - `report-each` — stop and report to the human after each completed experiment.

## Step 2 — Derive the scoreboard columns

From the primary metric and soft constraints, construct the `results.tsv` schema and fill
`climb_config/tracking-experiments.md`:

- `{{RESULTS_TSV_HEADER}}` — tab-separated:
  `experiment_branch<TAB>idea_file<TAB>parent<TAB>status<TAB>{{PRIMARY_METRIC}}` followed by one
  column per soft constraint. (`parent` records which experiment(s) each one was derived from, so
  the run can be drawn as a multi-root DAG (a forest). It may name one parent, the `baseline`,
  **several** parents — comma-separated — when an idea combines earlier ones, or **nothing** (`-`)
  when it's a brand-new independent line that starts its own island.)
- `{{SOFT_CONSTRAINT_COLUMNS_DOC}}` — one bullet per soft-constraint column (omit if none).
- `{{RESULTS_TSV_EXAMPLE}}` — a header row plus 2-3 illustrative example rows (include a `baseline`
  row with parent `-`, and at least one `discard` and/or `crash`), using the real column set.

## Step 3 — Fill and clean up

1. Replace every `{{PLACEHOLDER}}` across `climb.md` and all `climb_config/*.md` files with the concrete
   content gathered above.
2. Remove every `<!-- ... -->` guidance comment.
3. If there is **no held-out set and no data**, delete `climb_config/data.md` and set `{{DATA_SECTION}}` in
   `climb.md` to state the loop has no external data. Otherwise fill both.
4. If there are **no soft constraints**, write "None." wherever soft constraints are referenced and
   omit their scoreboard columns.
5. Create the durable-memory scaffold: `experiment_tracking/ideas/` and an `experiment_tracking/`
   entry in `.gitignore` (already present in the template — verify it), create an empty
   `results.tsv` containing only the header row, and create an empty `experiment_tracking/tree.md`.
6. Update `README.md` so the "Getting started" section reflects the concrete project (keep the logo
   and tagline).
7. **Establish the baseline (recommended):** run the current editable area through
   `climb_config/evaluation.md` to get a starting value for the primary metric, and log it to
   `results.tsv` as the `baseline` row (parent `-`) on a `baseline` branch. This is the root of the
   exploration DAG and the loop's reference point; seed `tree.md` with the baseline node.

## Step 4 — Delete this file

Once every placeholder is filled and the scaffold exists, **delete `INIT.md`** and commit the
initialized repo. Then the loop is ready: an agent can be pointed at `climb.md` to begin climbing.

Do not start the loop yourself unless the human explicitly asks you to continue into `climb.md`.
