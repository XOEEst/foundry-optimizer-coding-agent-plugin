# Do's and Dont's

**What you CAN do:**

- Modify or add any file under `{{EDITABLE_AREA}}` — this is the only area you may edit. Within it,
  everything is fair game: architecture, algorithms, hyperparameters, strategies, etc.

{{DEPENDENCY_RULE}}

**What you CANNOT do:**

- Modify the read-only files that define the problem and how it is scored:
{{READ_ONLY_FILES}}

- Modify `climb.md` or anything under `climb_config/`. These define the protocol and are read-only during
  the loop — do not edit them while experimenting.

{{LEAKAGE_RULE}}

<!--
INIT fills the placeholders:
  - EDITABLE_AREA: the one directory the agent may change (e.g. `src/<pkg>/`, `lib/`, a crate).
  - DEPENDENCY_RULE: whether the agent may add/change dependencies, and any limits (offline? no GPU?).
  - READ_ONLY_FILES: a bullet list of the evaluator / harness / fixtures that must not be touched.
  - LEAKAGE_RULE: if there is a held-out evaluation set, a rule forbidding inspecting or hand-tuning
    to it. If there is no held-out set, INIT removes this section entirely.
-->
