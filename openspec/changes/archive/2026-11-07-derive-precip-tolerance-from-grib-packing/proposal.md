## Why

ECMWF cut the GRIB packing of IFS `tp` from 16 to 12 bits starting with cycle `2026102012` (simple packing,
D=0, E from −18 to −13/−12). The quantisation step `2^E / 10^D` grew from 0.0038–0.0305 mm to 0.0153–0.2441 mm
and now increases with the accumulated amount.

De-accumulating a monotone accumulation then produces spurious deltas of exactly −1 step. These exceed the fixed
`PRECIP_NEGATIVE_NOISE_TOLERANCE_MM` (0.1, `workers/canonical_converter/converter.py:70`) and are classified as
significant:

- IFS precipitation is flagged `warning_negative_precip`, or `error_precip_accumulation` after three consecutive leads.
- Neither flag is in `FORCING_USABLE_CANONICAL_QUALITY_FLAGS` (`converter.py:338`), so canonical readiness returns
  `canonical_incomplete / missing_canonical_leads` and the forcing producer drops those leads (`producer.py:1344`).
- The IFS forecast for the cycle cannot be built, and the exact-required warm-start chain stalls behind it.
- The values themselves are unaffected: negatives are clamped to 0 in every case.

Measured on node-22 raw, NWM domain 225×329:

| Cycle | Flagged leads |
|---|---|
| 2026102000 (16-bit) | none |
| 2026102012 | f051, f063, f138, f144 |
| 2026102100 | f042, f150 |
| 2026102112 | f036, f045, f162, f168 |

Issue #3090.

## What Changes

- **Reader.** The raw GRIB reader requests the packing keys through cfgrib `read_keys`:
  - the keys are `binaryScaleFactor`, `decimalScaleFactor`, `bitsPerValue` and `packingType`;
  - the reader exposes the quantisation step `2^E / 10^D` (native units) on `RawRecord`.
- **Tolerance.** The IFS `tp` de-accumulation tolerance becomes
  `max(PRECIP_NEGATIVE_NOISE_TOLERANCE_MM, step_prev_mm + step_curr_mm)`.
  - When no step is known, the current env-only tolerance applies, so every existing caller is byte-identical.
  - GFS `apcp` is out of scope; see Non-goals in `design.md`.
- **Audit.** When a step is known, the applied tolerance and the steps are recorded in the IFS precipitation
  anomaly entries.
- **Docs.** Fix the stale "16-bit" comment at `converter.py:64-69`.

## Triage

```text
Issue type: bugfix
Fixture level: expanded
Upstream suggested level: absent
Blast radius: too-loose tolerance hides real IFS accumulation regressions (diagnostic only; values clamp either way); reader change could break raw decode for every source (gfs/IFS/ERA5) or alter lineage/checksum of canonical products
Selected risk packs: Schema (anomaly fields); Legacy compatibility (callers without packing info, netcdf4 fallback, the backend_kwargs assertion, canonical checksums); Error handling (missing/odd/constant-field packing keys); Config (env floor semantics); Documentation
Evidence floor: see tasks.md section 3 (ruff; reader/converter suites incl. adapters, ERA5, e2e; grib lane on node-27; real-raw replay receipt)
```

## Impact

- Code: `workers/canonical_converter/converter.py`; it is on the large-file exclude list, but new logic beyond a
  few lines goes in a small sibling module.
- Tests: `tests/test_ifs_canonical.py`, `tests/test_canonical_converter.py`, `tests/grib2_fixture_support.py`
  (a multi-point, configurable-bits encoder), plus a `grib`-marker test that encodes 12-bit GRIB at test time.
- Spec: `canonical-conversion`.
