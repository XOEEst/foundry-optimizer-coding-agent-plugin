# Producing evaluation results

This file is the **single source of truth** for how an experiment is scored. An experiment is not
done until it has been run through exactly these steps and produced a value for the primary metric.

## Produce a run

{{PRODUCE_RUN}}

<!--
INIT fills this in with the exact command(s) that take the current state of the editable area and
produce whatever artifact the scorer consumes (a run file, a benchmark output, a report). Include
the precise invocation, its inputs, and where the output lands under `experiment_metadata/`.
-->

## Score the run

{{SCORE_RUN}}

<!--
INIT fills this in with the exact command(s) that consume the produced artifact and emit the primary
metric (and any soft-constraint measurements). Show how to read the headline number off the output,
and capture that output to `experiment_metadata/` so the reported number is reproducible from the
branch.
-->

## Read off the metric

{{READ_METRIC}}

<!--
INIT fills this in: exactly how to extract the single primary-metric value from the scorer output
(e.g. a grep/jq expression, a printed line), so logging to results.tsv is unambiguous.
-->
