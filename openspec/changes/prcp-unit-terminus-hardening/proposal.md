## Why

The #266 review surfaced that the SHUD forcing PRCP unit (`mm/day`, Decision A) is
asserted at the converter (`precip-mmday-at-converter`) and reconciled at the
producer (`forcing-prcp-unit-reconciliation`), but the **terminal consumer** —
SHUD staging in `workers/shud_runtime/runtime.py` — copies forcing values straight
into the SHUD `PRCP` column without ever checking the unit. If an upstream
regression re-introduced per-step `mm`, SHUD would silently ingest a physically
wrong precipitation amount with no error.

Separately, the producer's output semantics (`OUTPUT_UNITS`, the precip
conversion branch, and `rn_shortwave_factor`) are versioned only by a free-form
`producer_version` string. Nothing forces a developer who changes those semantics
to also bump the version, so the lineage currency check (#266) could be silently
defeated.

This change is pure regression hardening: assertions and tests only. Current code
is already correct; no numeric or output behavior changes.

## What Changes

- SHUD staging asserts the forcing package's declared `PRCP` unit is `mm/day`
  before staging. It reuses the package manifest already fetched and
  checksum-verified by `_verify_forcing_manifest_checksums` (no new network
  fetch). A new `SHUDRuntimeError("FORCING_PRCP_UNIT_MISMATCH", ...)` is raised
  only when the unit is **explicitly present and not `mm/day`**. Missing unit
  metadata (legacy packages) is tolerated for backward compatibility.
- A producer guard test fingerprints the output-semantics surface
  (`OUTPUT_UNITS` + precip-branch behavior + `rn_shortwave_factor` default) and
  pins it together with `producer_version`. Any change to those semantics flips
  the fingerprint and turns the test red, forcing a coordinated `producer_version`
  bump.
- A keyset-equality guard asserts `set(OUTPUT_UNITS) == set(REQUIRED_FORCING_VARIABLES)`
  and that every `package_manifest_unit(v)` is non-empty, catching a future
  OUTPUT_UNITS key added without a matching manifest unit.

## Capabilities

### Modified Capabilities

- `shud-runtime`: staging fails loud when the forcing package explicitly declares
  a non-`mm/day` PRCP unit; missing unit metadata is tolerated.
- `fixed-station-forcing-production`: producer output semantics are pinned to
  `producer_version` via a fingerprint regression gate, and the OUTPUT_UNITS /
  manifest-unit keysets are asserted to stay in lockstep.

## Impact

- `workers/shud_runtime/runtime.py` — new `_assert_forcing_prcp_unit`, invoked
  from `_verify_forcing_manifest_checksums`; new `EXPECTED_PRCP_UNIT` /
  `MAX_PACKAGE_MANIFEST_BYTES` constants; single `FORCING_PRCP_UNIT_MISMATCH`
  hard-failure error code (read/parse/size issues tolerate-skip, never fail).
- Tests: `tests/test_shud_runtime.py`, `tests/test_forcing_producer.py`,
  `tests/test_production_met_validation.py`.

## Non-Goals

- No change to the precipitation conversion numeric logic (#269 is settled).
- No change to producer `OUTPUT_UNITS` / `producer_version` values (only pinned).
- No new network fetch in staging (reuse the existing package manifest fetch).
- No hard failure on missing unit metadata (backward compatibility preserved).
