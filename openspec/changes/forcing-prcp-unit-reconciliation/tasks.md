## 1. Verify the authoritative SHUD PRCP unit

- [ ] 1.1 Determine empirically what unit the SHUD runtime reads from `qhh.tsd.forc` PRCP, using `DT_QE_PRCP`/`TS_PRCP` config plus a SHUD run on a known unit-probe forcing, or the authoritative rSHUD/AutoSHUD ingestion contract this pipeline must match.
- [ ] 1.2 Record the verified unit (`mm/day` vs per-timestep `mm`) with evidence in `design.md`, and select fix direction A or B accordingly.

## 2. Reconcile the producer PRCP unit across sources

- [ ] 2.1 Update `OUTPUT_UNITS["PRCP"]` and `_precip_to_timestep_factor` so GFS, ERA5, and IFS all emit the verified unit; remove the GFS/ERA5-vs-IFS divergence.
- [ ] 2.2 Keep the "unknown/zero/non-finite step is rejected" guard for any source whose factor depends on `step_hours`.
- [ ] 2.3 If direction A is chosen, re-examine issue #256's ERA5 `step/24` change and adjust; if direction B, fix only the IFS `24/step` branch.

## 3. Tests and regression locks

- [ ] 3.1 Pin per-source numeric PRCP magnitude against the verified unit for GFS, ERA5, and IFS at representative steps (1h/3h/6h).
- [ ] 3.2 Add a contract test asserting every unit in `EXPECTED_CANONICAL_UNITS["prcp_rate_or_amount"]` maps to exactly one documented output convention via `_precip_to_timestep_factor` (extends the issue #256 `mm/s`-rejection guard).
- [ ] 3.3 Update `tests/test_ifs_forecast_integration.py` precip assertion to the corrected magnitude.

## 4. Compatibility and verification

- [ ] 4.1 Record a data note: existing forcing snapshots / hydro results for any source whose magnitude changes may need regeneration (no silent value change).
- [ ] 4.2 Verify with `uv run pytest -q tests/test_forcing_producer.py tests/test_ifs_forecast_integration.py tests/test_production_met_validation.py`, `uv run ruff check .`, and `openspec validate forcing-prcp-unit-reconciliation --strict --no-interactive`.

### Evidence Floor

- Verified SHUD PRCP unit with empirical/contract evidence recorded in `design.md`.
- All three source branches and `OUTPUT_UNITS["PRCP"]` agree with that unit; per-source magnitude regression tests pass on the remote node-22 DB suite.
- IFS integration precip assertion reflects the corrected magnitude.
- Non-goal evidence: no change to non-PRCP variables, station selection/identity/packaging, SHUD/Slurm/parse/publish.
- Required commands:
  - `uv run pytest -q tests/test_forcing_producer.py tests/test_ifs_forecast_integration.py tests/test_production_met_validation.py`
  - `uv run ruff check .`
  - `openspec validate forcing-prcp-unit-reconciliation --strict --no-interactive`
