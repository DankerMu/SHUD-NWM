## 1. Converter: convert GFS/IFS precip to mm/day

- [x] 1.1 GFS `apcp` branch in `convert_units_with_metadata` rescales per-step delta by the converter step (`* 24 / _step_hours(...)`) and returns `mm/day`.
- [x] 1.2 `convert_ifs_precipitation_with_metadata` rescales `deltas_mm` by `* 24 / step_hours` (the already-computed `_ifs_step_hours`) and returns `mm/day`.
- [x] 1.3 `STANDARD_UNITS["prcp_rate_or_amount"]` and `IFS_STANDARD_UNITS["prcp_rate_or_amount"]` set to `mm/day`; `ERA5_STANDARD_UNITS` left unchanged.
- [x] 1.4 ERA5 precip path left unchanged.
- [x] 1.5 Update lineage labels (`CONVERSION_PARAMS["apcp"]`, IFS `operation`/`unit_conversion`) to describe mm/day; keep `step_hours` for audit.

## 2. Producer: passthrough only

- [x] 2.1 `_precip_to_timestep_factor` returns `1.0` for `mm/day` and raises for any other unit; `mm` → `24 / step` branch removed.
- [x] 2.2 `EXPECTED_CANONICAL_UNITS["prcp_rate_or_amount"]` narrowed to `("mm/day",)`.
- [x] 2.3 Remove dead code: `ifs_precip_step_hours` config, `_precip_step_hours`, `_parse_hour_resolution`.
- [x] 2.4 `OUTPUT_UNITS["PRCP"]` kept as `mm/day`.

## 3. Tests and regression locks

- [x] 3.1 Canonical converter tests assert `mm/day` unit and the `× 24 / step` magnitude for GFS and IFS.
- [x] 3.2 Producer tests feed `mm/day` canonical and assert step-independent passthrough; per-step `mm` is rejected at the unit gate; `mm/s` rejection retained.
- [x] 3.3 IFS integration / e2e precip assertions confirm unchanged end-to-end `PRCP` magnitude (16.0).
- [x] 3.4 ERA5 canonical test confirmed unchanged.

## 4. Verification

- [x] 4.1 `uv run ruff check .`
- [x] 4.2 `uv run pytest -q tests/test_forcing_producer.py tests/test_ifs_forecast_integration.py tests/test_ifs_canonical.py tests/test_e2e_ifs.py tests/test_production_met_validation.py tests/test_canonical_converter.py tests/test_era5_canonical.py`
- [ ] 4.3 `openspec validate precip-mmday-at-converter --strict --no-interactive`

### Evidence Floor

- GFS and IFS canonical precip persist `mm/day` using the converter's actual step; ERA5 unchanged.
- Producer passes `mm/day` through unchanged; per-step `mm` and `mm/s` are rejected before any write.
- End-to-end `PRCP` magnitude unchanged (IFS integration and e2e both 16.0).
- Dead code (`ifs_precip_step_hours`, `_precip_step_hours`, `_parse_hour_resolution`) removed with no remaining references.
- Required commands:
  - `uv run ruff check .`
  - `uv run pytest -q tests/test_forcing_producer.py tests/test_ifs_forecast_integration.py tests/test_ifs_canonical.py tests/test_e2e_ifs.py tests/test_production_met_validation.py tests/test_canonical_converter.py tests/test_era5_canonical.py`
  - `openspec validate precip-mmday-at-converter --strict --no-interactive`
