## Why

The forcing producer reconstructed the precipitation rate from a *static* `native_time_resolution` (`_precip_to_timestep_factor` applied `24 / step_hours` for `mm` canonical, with an `ifs_precip_step_hours` default). That recomputation duplicated step logic the canonical converter already owns, and it could drift from the converter's actual frame-to-frame step (e.g. irregular GFS/IFS lead spacing), silently mis-scaling `PRCP` by `24 / step_hours`.

ERA5 already does the correct thing: its canonical converter (`convert_era5_precipitation_with_metadata`) divides each accumulation delta by the converter-computed `step_hours` and persists `mm/day`. GFS (`apcp`) and IFS (`tp`) instead persisted per-step `mm`, forcing the producer to re-derive the rate from a label rather than from the real step.

The follow-up (#266 / `forcing-prcp-unit-reconciliation`) reconciled the producer output unit to `mm/day` but left the per-step-`mm` → rate recomputation inside the producer. This change removes that recomputation at the root.

## What Changes

- GFS (`apcp`) and IFS (`tp`) canonical converters convert precipitation to `mm/day` *inside the converter*, using the converter's own actual step (`24 / step_hours`), exactly like ERA5. Canonical precip unit becomes `mm/day` for all three sources.
- `STANDARD_UNITS["prcp_rate_or_amount"]` and `IFS_STANDARD_UNITS["prcp_rate_or_amount"]` change from `mm` to `mm/day`; `ERA5_STANDARD_UNITS` is already `mm/day` (unchanged).
- The producer passes precipitation through unchanged (`_precip_to_timestep_factor` returns `1.0` for `mm/day`); the `mm` → `24 / step` branch, the `ifs_precip_step_hours` config, and the `_precip_step_hours` / `_parse_hour_resolution` helpers are removed as dead code.
- `EXPECTED_CANONICAL_UNITS["prcp_rate_or_amount"]` is narrowed from `("mm", "mm/day")` to `("mm/day",)` so any upstream per-step `mm` drift fails loud at the producer unit gate instead of being silently rebuilt.
- End-to-end `PRCP` magnitude is unchanged: the `24 / step_hours` scaling moves from the producer to the converter.

## Capabilities

### Modified Capabilities

- `canonical-conversion`: GFS and IFS precipitation are converted to `mm/day` inside the converter using the converter's actual step, aligning with the pre-existing ERA5 behavior.
- `fixed-station-forcing-production`: the producer treats all canonical precipitation as `mm/day` and passes it through unchanged; per-step `mm` is rejected at the unit gate.

## Impact

- `workers/canonical_converter/converter.py` — `apcp` branch in `convert_units_with_metadata`, `convert_ifs_precipitation_with_metadata`, `STANDARD_UNITS`, `IFS_STANDARD_UNITS`, `CONVERSION_PARAMS["apcp"]`, IFS lineage `operation`/`unit_conversion`.
- `workers/forcing_producer/producer.py` — `_precip_to_timestep_factor`, `EXPECTED_CANONICAL_UNITS`, removal of `ifs_precip_step_hours`, `_precip_step_hours`, `_parse_hour_resolution`.
- Tests: `tests/test_canonical_converter.py`, `tests/test_ifs_canonical.py`, `tests/test_e2e_ifs.py`, `tests/test_forcing_producer.py`, `tests/test_ifs_forecast_integration.py`.

## Non-Goals

- No change to the ERA5 precipitation conversion (already correct).
- No change to non-PRCP forcing variables, station selection/identity, packaging, or SHUD/Slurm/parse/publish behavior.
- No change to the end-to-end `PRCP` magnitude or to `OUTPUT_UNITS["PRCP"]` (`mm/day`).
