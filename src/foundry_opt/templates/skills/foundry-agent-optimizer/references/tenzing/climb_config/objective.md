# Objective

**The goal is simple: {{OBJECTIVE_DIRECTION}} `{{PRIMARY_METRIC}}`.**

`{{PRIMARY_METRIC}}` is the **primary metric** and the single number that decides whether one
experiment beats another. Everything in the editable area is fair game in service of moving it in
the right direction.

## Soft constraints

{{SOFT_CONSTRAINTS}}

<!--
INIT fills this in. A soft constraint is a secondary quantity that should not blow up in pursuit of
the primary metric (e.g. runtime, memory, cost, latency, binary size). For each, state the quantity,
the acceptable direction, and roughly how much regression is tolerable for a meaningful primary gain.
If there are no soft constraints, write "None." here.
-->

## Simplicity criterion

All else being equal, **simpler is better**. A small improvement that adds ugly complexity is not
worth it. Conversely, removing something and getting equal-or-better results is a great outcome — a
simplification win. When deciding whether to keep a change, weigh the complexity cost against the
improvement magnitude:

- A tiny primary-metric gain that adds a pile of hacky code? Probably discard.
- The same tiny gain achieved by **deleting** code? Definitely keep.
- Roughly no change in the metric but much simpler code? Keep.
