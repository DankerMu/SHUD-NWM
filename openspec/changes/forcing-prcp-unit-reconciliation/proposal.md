## Why

The forcing producer emits `PRCP` labeled `mm` (`OUTPUT_UNITS["PRCP"] = "mm"`, documented as "per-timestep accumulated mm"), but applies mutually inconsistent per-source factors in `_precip_to_timestep_factor`:

- GFS (`mm` canonical) → factor `1.0` → per-timestep mm
- ERA5 (`mm/day` canonical) → factor `step_hours / 24` → per-timestep mm
- IFS (`mm` per-step canonical) → factor `24 / step_hours` → daily total (`mm/day`)

GFS and ERA5 produce a per-timestep amount; the IFS branch produces a daily rate. They cannot both satisfy a single output convention. Separately, the authoritative SHUD forcing contract documents `PRCP` as **`mm/day`**:

- `SHUD/VersionUpdate.md:25` — "Forcing data: Precipitation (mm/day), ..."
- `AutoSHUD/Rfunction/LDAS_UnitConvert.R` — every adapter converts source precip to `mm/day (SHUD)`

So the producer's stated per-timestep-mm convention, the IFS branch, and the SHUD `mm/day` contract do not agree. At most one is correct, and the per-source PRCP magnitude can differ by a factor of `24 / step_hours` (8× at a 3h step). This was surfaced by the issue #256 Phase 6.5 cross-review (spec + correctness reviewers) as a pre-existing inconsistency outside the issue #256 GFS fixed-station scope.

## What Changes

- Establish the single authoritative `PRCP` unit the SHUD runtime actually consumes from `qhh.tsd.forc`, verified against the SHUD model parameters (`DT_QE_PRCP`, `TS_PRCP`) and the rSHUD/AutoSHUD contract — not assumed.
- Reconcile all three source branches (GFS / ERA5 / IFS) and `OUTPUT_UNITS["PRCP"]` so every source emits the verified unit; eliminate the IFS-vs-rest divergence.
- Add regression tests that pin the numeric PRCP magnitude per source against the verified SHUD unit, plus a contract test asserting every accepted canonical precip unit maps to exactly one documented output convention.

## Capabilities

### Modified Capabilities

- `fixed-station-forcing-production`: the `PRCP` output-unit convention is reconciled across GFS / ERA5 / IFS and aligned to the verified SHUD consumer contract, with per-source magnitude regression coverage.

## Impact

- `workers/forcing_producer/producer.py` — `OUTPUT_UNITS["PRCP"]`, `_precip_to_timestep_factor`, `_precip_step_hours`.
- `workers/canonical_converter/converter.py` — ERA5 (`mm/day`) and IFS (`mm` per-step) precip unit semantics referenced by the producer.
- `workers/shud_runtime/runtime.py` — `qhh.tsd.forc` PRCP column emission and any downstream interpretation.
- Tests: `tests/test_forcing_producer.py`, `tests/test_ifs_forecast_integration.py`, and any SHUD-runtime forcing-format test.

## Non-Goals

- No change to non-PRCP forcing variables (TEMP/RH/wind/Rn/Press).
- No change to fixed-station selection, identity binding, ready-state/idempotency, packaging, or path/resource contracts established by issue #256.
- No SHUD execution, Slurm submission, parse, or publish behavior change.
