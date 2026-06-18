# Design

## Schema

Extend `flood.run_product_quality` in place instead of adding a second quality
table. The table already has the right run-level identity and existing readers
depend on its count columns.

Required explicit fields:

- `quality_state`: `ready`, `degraded`, or `unavailable`.
- `unavailable_products`: JSONB list of product identifiers such as
  `frequency_curves`, `return_period_result`, and `warning_thresholds`.
- `residual_blockers`: JSONB list of machine-readable reason entries. Each
  entry must preserve at least `code`, `state`, `quality_flag`,
  `residual_risk`, and `run_id` keys so future cleanup/API paths can audit why
  a flood product is not ready.
- Expected coverage counters for peak and timestep rows.
- Meaningful counters for rows that carry usable return period or warning data.
- Reason counters for `no_frequency_curve`, `no_usable_frequency_curve`, and
  warning-threshold unavailability.

The migration must be idempotent for local test databases and for production
databases that may already have `run_product_quality`. It must not create the
two NULL partial indexes on `flood.return_period_result`.

## Helper API

`packages/common/flood_quality.py` should support two modes:

- Explicit write mode for future workers: callers pass run-level quality stats
  and reasons, and the helper upserts the row even when source result rows are
  absent.
- Historical backfill mode: existing source rows are aggregated into compatible
  count fields. This path remains useful for existing runs until #488/#490.

Existing helper names should keep backward compatibility where possible. If a
new helper is needed, prefer a small dataclass for explicit quality input rather
than loose dictionaries.

## Read Compatibility

Existing count fields remain populated. New explicit fields should have safe
defaults so current readers do not break before #489:

- legacy rows can default to `quality_state=ready` only when existing counts
  indicate return-period/warning coverage, otherwise `degraded` or
  `unavailable`;
- empty explicit unavailable rows must not be removed by legacy refresh paths;
- missing table/schema behavior should fail closed for flood products without
  marking q_down unavailable. This can be verified at helper/read compatibility
  level in this issue, with route-level contract switching deferred to #489.

## Tests

Required coverage:

- migration creates/extends `run_product_quality` without NULL partial indexes;
- explicit all-no-curve quality can be written/read with zero meaningful rows;
- residual blocker entries round-trip with the minimum audit keys;
- partial-curve explicit quality remains non-ready;
- historical source-row backfill still produces compatible counts;
- empty-source refresh does not delete explicit unavailable rows.
- missing quality schema/table does not convert q_down/readiness helpers into a
  flood-product failure.
