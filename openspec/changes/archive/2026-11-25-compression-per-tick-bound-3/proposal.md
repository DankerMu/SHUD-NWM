# Compression per-tick bound 3 (#3380 slice 2)

## Why

`hypertable-compression` requires the committed env template to carry the decided per-tick bound, and
its scenario names the value. The value there is still 4: the re-derivation of 2026-09-18 (#2425, bound
2) changed the template, the pin and the runbook but not the scenario. #3380 slice 2 changes the bound
to 3 (owner decision 2026-11-26; derivation in the issue and in the runbook's "Per-tick capacity"), and
the requirement itself says that template, runbook and live env are re-pinned in the same change. The
scenario is brought to the value that ships.

## What changes

One MODIFIED requirement, "The compression per-tick bound MUST be a capacity-derived target consistent
across template, live env, and receipts":

- the template scenario names `PER_TICK_BOUND=3` and the inputs of today's derivation;
- the runbook scenario's cadence conclusion names bound 3;
- the sentence "The variable remains mandatory with no in-code default" is corrected: the variable is
  mandatory in an env declaration, and the in-code value for direct invocations without an env equals
  the template's and is pinned.

The code default, the template, the pins and the runbook are in the same PR (issue comment "Slice 2").

## Not in this change

- The requirement's sentences about a transitional `_legacy` sibling stay as they are: they describe how
  such a table is modelled when one is in the lane; none is today (the legacy store was dropped on
  2026-09-19).
- The selection order, the walls, the catch-up rule.
- The scenario "Chunk interval change without re-derivation is caught" is reproduced unchanged. No test
  implements it today (the template tests pin the assignment line and the absence of "one chunk width");
  that gap predates this change and is reported on the issue, not closed here.
- The narrative of "Raising the compress timeout above default MUST fail closed unless per_tick_bound is
  1" still speaks of bound 4 as the risk it was written for; its scenarios use 4 as an example input,
  which stays valid for any bound above 1.
