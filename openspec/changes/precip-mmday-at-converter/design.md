# Design: Convert GFS/IFS precipitation to mm/day at the converter

## Decision: Option B (root unification at the converter)

Move the per-step → daily-rate conversion (`24 / step_hours`) for GFS and IFS out of the producer and into the canonical converter, so all three sources persist `mm/day`, matching ERA5. The producer becomes a pure passthrough for precipitation. End-to-end `PRCP` magnitude is unchanged.

## Ground-truth evidence

### ERA5 already does this (reference implementation)

`convert_era5_precipitation_with_metadata` (`workers/canonical_converter/converter.py`):

```python
step_hours = _step_hours(forecast_hour, previous_forecast_hour)
...
mm_per_day = tuple(max(0.0, delta) * 1000.0 * 24.0 / step_hours for delta in deltas)
```

It uses the converter-computed `step_hours` and persists `mm/day` (`ERA5_STANDARD_UNITS["prcp_rate_or_amount"] == "mm/day"`). GFS/IFS now follow the same pattern; ERA5 is left untouched.

### Step semantics are owned by the converter

- `_step_hours(forecast_hour, previous_forecast_hour)` returns `1.0` when either hour is `None` (first frame / no predecessor) and otherwise `max(1, fh - prev_fh)`. GFS `apcp` uses this.
- `_ifs_step_hours(...)` returns `3.0` when `forecast_hour is None`, `forecast_hour` itself when there is no predecessor and `fh > 0`, else `max(1, fh - prev_fh)`. IFS `tp` uses this; the value is already computed at line 773 and returned for audit.

Because these come from the actual frame pair, they are correct even for irregular lead spacing — unlike the producer's previous reliance on a static `native_time_resolution` label and an `ifs_precip_step_hours` default.

### Magnitude invariance

The producer previously computed `PRCP = canonical_mm * 24 / step_hours`. Now the converter computes `canonical_mm_per_day = delta_mm * 24 / step_hours` and the producer multiplies by `1.0`. The product is identical:

| source | per-step delta | step | converter mm/day | producer factor | PRCP |
|--------|----------------|------|------------------|-----------------|------|
| IFS    | 2.0 mm         | 3h   | 16.0             | 1.0             | 16.0 |
| GFS    | 5.0 mm         | 3h   | 40.0             | 1.0             | 40.0 |
| GFS    | 4.0 mm         | 1h   | 96.0             | 1.0             | 96.0 |

`tests/test_ifs_forecast_integration.py` (`PRCP == 16.0`) and `tests/test_e2e_ifs.py` (`PRCP == 16.0`) both hold under B.

## Fail-loud on drift

`EXPECTED_CANONICAL_UNITS["prcp_rate_or_amount"]` is narrowed to `("mm/day",)`. If any upstream regression re-emits per-step `mm`, the producer's canonical unit gate raises a `unit mismatch` `ForcingProductionError` before writing any timeseries or `forcing_version`, instead of silently reconstructing a rate from a possibly-stale step label.

## Dead-code removal

With the `mm` branch gone from `_precip_to_timestep_factor`, these become unreferenced and are deleted:

- `ForcingProducerConfig.ifs_precip_step_hours`
- `_precip_step_hours`
- `_parse_hour_resolution` (its only caller was `_precip_step_hours`)

Negative-delta anomaly records keep the *raw* `mm` delta (`min_delta` / `min_delta_mm`) for audit; only the emitted values are rescaled to `mm/day`.

## Migration & Rollout

Pre-#269 canonical precip products were written with `unit="mm"`, and many lack a `converter_version`. The orchestrator self-heals them before the producer's unit gate can fail them terminally:

- **Version drift** — a product whose `converter_version` is recorded and differs from the source's current version triggers a demote (`canonical_ready` → `raw_complete`) and re-conversion. (Pre-existing, commit 8fd0b6e.)
- **Old `mm` unit (orthogonal criterion, new)** — a `prcp_rate_or_amount` product whose `unit` is explicitly recorded and is not `mm/day` triggers the same demote → re-conversion. This catches the common pre-#269 rows that carry `unit="mm"` but no `converter_version`, which would otherwise slip past the version check and die in `failed_forcing` with no self-heal path.
- **Missing version/unit is left untouched** — preserving fixture/seed safety; such rows fall through to the producer.
- **Producer `mm/day` unit gate** — the final backstop: anything that reaches production with a non-`mm/day` precip unit fails loud before writing a `forcing_version`.

Rollback: the bumped m1.1/m4.1 `mm/day` canonical rows pass through an old producer at factor `1.0` (magnitude-safe). If the demote logic is reverted, residual stale-version rows are silently retained, which is acceptable.
