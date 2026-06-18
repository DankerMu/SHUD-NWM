# Proposal

## Summary

Add an operator-facing cleanup command for historical
`flood.return_period_result` rows that only encode no-curve unavailability:

```sql
return_period IS NULL
AND warning_level IS NULL
AND quality_flag IN ('no_frequency_curve', 'no_usable_frequency_curve')
```

The command must default to dry-run, emit an auditable manifest, and support
small resumable delete batches. It must not perform production cleanup by
default.

## Motivation

Issue #486 identified production-size growth in `flood.return_period_result`.
Issues #487, #488, and #489 moved flood product quality to explicit
`flood.run_product_quality`, stopped new no-curve empty rows, and updated API
read paths. Historical no-curve rows can now be cleaned as derived data, but
only after preserving quality summaries and producing execution evidence.

## Scope

- Add a CLI/script entrypoint for dry-run manifests and explicit apply mode.
- Support filters for `run_id`, `basin_version_id`, `source_id`, and
  `cycle_time` ranges.
- Report candidate counts by quality flag, run, max-over-window, and chunk/time
  bucket where available.
- Check affected runs have explicit `run_product_quality` summaries before
  deletion.
- Delete in bounded batches with per-batch commits and manifest records.
- Refresh or validate affected run quality after each run/batch without
  reintroducing large-table dependency for no-curve rows.

## Non-Goals

- Do not delete rows with non-null `return_period` or `warning_level`.
- Do not delete `hydro.river_timeseries` or object-store `/runs` artifacts.
- Do not drop, rebuild, or vacuum indexes; issue #491 owns index cleanup and
  space reclamation.
- Do not execute production cleanup as part of this implementation.
