# Tasks

- [x] Update `workers/flood_frequency/return_period.py` so no-curve and
  no-usable-curve evaluations do not produce stored `return_period_result` rows.
- [x] Delete the current run/network's prior peak and timestep rows before
  batch writing, even when the new peak/timestep batch is empty.
- [x] Keep expected coverage and no-curve counters in explicit
  `run_product_quality` even when zero result rows are stored.
- [x] Ensure all-no-curve quality is explicit unavailable and partial-curve
  quality is not ready.
- [x] Preserve complete-curve behavior: peak rows, timestep rows, warning
  levels, summary/ranking/timeline fields, and return stats remain compatible.
- [x] Preserve warning-threshold-unavailable behavior for usable curves: rows
  with return periods remain stored while warning quality is unavailable.
- [x] Keep no-curve flood tile layers unregistered and avoid blocking q_down.
- [x] Update old tests that expected null no-curve rows to expect skipped rows
  plus explicit quality summary.
- [x] Add recompute coverage proving stale null/no-curve rows for the current
  run are removed when replacement rows are empty.
- [x] Run focused verification:
  - `uv run --no-sync pytest -q tests/test_return_period.py`
  - `uv run --no-sync ruff check workers/flood_frequency/return_period.py tests/test_return_period.py`
  - `openspec validate issue-488-skip-empty-return-period-results --strict --no-interactive`

## Evidence Mapping

- Public entrypoint / legacy compatibility: complete-curve fixture
  `seg_001=[80,150,260]` with one usable curve -> `result.rows_written == 4`,
  one peak row, three timestep rows, warning levels still populated.
- Schema/field semantics: warning-threshold-unavailable fixture
  `seg_001=[260,300]` with a usable curve and quality contract
  `unavailable_products=["warning_thresholds"]` -> 3 stored rows, all
  `return_period IS NOT NULL`, all `warning_level IS NULL`, quality
  `quality_state="unavailable"`, `warning_threshold_unavailable_rows == 3`.
- DB state / stale output / idempotency: stale fixture inserts prior
  `quality_flag="no_frequency_curve"` rows with null return/warning for
  `forecast_run`; recompute with no usable curves -> final row count for that
  run/network is 0 and explicit quality remains available.
- Resource limits / large no-curve input: all-no-curve fixture
  `seg_001=[100]`, no frequency curves -> expected 2 evaluations
  (1 peak + 1 timestep), final `return_period_result` row count 0,
  `no_frequency_curve_rows == 2`, `meaningful_result_rows == 0`,
  `quality_state="unavailable"`, `unavailable_products` includes
  `frequency_curves` and `return_period_result`, tile layer count 0.
- Partial-output behavior: partial fixture `seg_001=[260]`, `seg_002=[300]`
  with a curve only for `seg_001` -> 2 stored rows only for `seg_001`,
  expected rows 4, meaningful rows 2, `no_frequency_curve_rows == 2`,
  `quality_state="degraded"`, no stored rows for `seg_002`.
- Published artifact / q_down boundary: same all-no-curve fixture
  `seg_001=[100]` -> after `compute_return_periods()`, flood tile layer count
  is 0, `flood.return_period_result` row count is 0, and
  `hydro.river_timeseries` still has the original one `q_down` row for
  `forecast_run`/`seg_001`/`q_down` with value `100.0`; no return-period
  filtering or cleanup touches q_down source rows.
