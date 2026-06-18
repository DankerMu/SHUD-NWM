# Design

## Row Filtering

The worker should separate expected coverage from stored result rows:

- expected coverage counts every peak/timestep evaluation opportunity for the
  run's q_down values;
- meaningful rows are rows with a usable `return_period` or `warning_level`;
- no-curve and no-usable-curve evaluations update quality counters/blockers but
  are not persisted to `flood.return_period_result`.

`_evaluate_q()` can keep returning quality flags for no-curve cases so summary
logic remains centralized, but `_compute_return_periods()` must filter rows
before batch upsert.

## Cleanup Order

For the current run/network, old peak and timestep result rows must be deleted
before writing replacement batches, regardless of whether the replacement batch
is empty. This prevents stale no-curve/null rows or stale meaningful rows from
surviving a recomputation that now produces no stored rows.

The existing single-run/single-duration invariant remains: the worker owns the
`return_period_result` rows it deletes for the current `(run_id,
river_network_version_id)` peak/timestep lanes. This issue does not introduce a
second curve duration.

## Quality Summary

`run_product_quality` remains the durable no-curve source:

- `expected_result_rows`, `expected_max_result_rows`, and
  `expected_timestep_result_rows` reflect all q_down coverage opportunities.
- `result_rows`, `return_period_rows`, `warning_rows`, and meaningful counters
  reflect stored/usable product rows.
- `no_frequency_curve_rows` and `no_usable_frequency_curve_rows` reflect skipped
  evaluations, not stored empty rows.
- all-no-curve runs are explicit `unavailable` with `frequency_curves` and
  `return_period_result` unavailable as appropriate.
- partial-curve runs are not `ready`.

## Tile Layer Boundary

Tile layer registration should continue only when the run has meaningful flood
return-period output and no non-frequency tile blockers. A no-curve run must not
register a flood tile layer. q_down/discharge publication is outside this
worker's flood tile layer registration and must not be blocked by missing
frequency curves.

## Tests

Required coverage:

- all-no-curve run writes zero result rows and one explicit unavailable quality
  row with no-curve counts/blockers;
- partial-curve run writes rows only for segments with usable curves and stores
  degraded quality with expected greater than meaningful rows;
- recomputing a run with stale null/no-curve rows deletes the stale rows even
  when no replacement rows are written;
- complete-curve run still writes peak/timestep rows normally;
- warning-threshold-unavailable run with usable curves still stores meaningful
  return-period rows while marking warning quality unavailable.
