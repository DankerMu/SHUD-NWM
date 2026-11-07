## Design

**Change surface:**
- `workers/canonical_converter/converter.py`:
  - **Reader:** `_read_record_with_xarray`, which is the single injection point for `read_keys`, applied after
    `_cfgrib_backend_kwargs` returns. All three branches (explicit, bundle, default) therefore get it.
  - **Record:** `RawRecord` gains one defaulted field.
  - **IFS precipitation:** `convert_ifs_precipitation_with_metadata` and its call site in the IFS conversion loop
    (~`:2535`). The loop already holds `precipitation` and `previous_precipitation` records.
  - **Comment:** the constant comment at `:64-69`.
- **New module** `workers/canonical_converter/grib_packing.py`: step extraction from cfgrib attrs and the tolerance
  helper.

**Must preserve:**
- **No step known means today's behaviour, byte for byte.** This applies to:
  - synthetic `RawRecord`s;
  - the netcdf4 fallback (every `.nc` test fixture goes through it, `converter.py:1468-1476`);
  - ieee, constant-field and unknown packing.

  In all these cases thresholds, flags, anomaly dicts (keys and values), values, lineage and therefore checksums stay
  identical. A literal-equality test pins the full anomaly dicts for a no-step input.
- **Precipitation values are unchanged.** They are still `max(0, delta) * 24 / step_hours`.
- **No `converter_version` bump.** Values are unchanged, and a bump would make `expected_converter_version` mark the
  already-converted 2026-10-08..10-14 canonical stale and force reconversion in the middle of the node-22 catch-up.
- **`RawRecord.metadata` stays the manifest entry metadata.** Packing goes in a new field, so GFS
  `conversion_params["raw_metadata"]` is untouched.
- **GFS and ERA5 conversion are unchanged.** They read through the same reader, but the new field is ignored on their
  paths.
- **The one exact `backend_kwargs` assertion may change.** `tests/test_canonical_converter.py:1114` gains exactly the
  `read_keys` entry; this is a recorded deviation.

**Must add / change:**
- **D1 Reader.**
  - `backend_kwargs = {**_cfgrib_backend_kwargs(...), "read_keys": [...]}`. The keys are `binaryScaleFactor`,
    `decimalScaleFactor`, `bitsPerValue` and `packingType`.
  - After a successful cfgrib open, the reader derives `quantization_step` from the data array's `GRIB_*` attrs:
    `2.0**E / 10.0**D` in native units.
  - The step is `None` when:
    - the netcdf4 fallback was used;
    - any key is missing or not an int;
    - `bitsPerValue <= 0`, which marks a constant field (exact, no quantisation; for example IFS f000 `tp` = 0);
    - `packingType` is not in the measured allow-list `{"grid_simple"}`;
    - the result is not finite or not positive.
  - A missing or odd key never raises.
- **D2 Tolerance rule.** `negative_noise_tolerance_mm(floor_mm, *steps_mm)` returns
  `max(floor_mm, sum of the steps that are not None)`.
  - **Why the sum.** Each value carries a rounding error of at most step/2 (`packingError = step/2`, as measured).
    A difference between two values can therefore miss by up to `(s_prev + s_curr) / 2`, and the observed −1 step
    sits exactly on that bound.
  - **Why it must exceed one step.** Significance is `delta <= -tol`, so `tol == step` would still flag the −1-step
    artefact. The sum gives a 2× margin.
  - The floor is the existing `PRECIP_NEGATIVE_NOISE_TOLERANCE_MM`, read where it is read today.
- **D3 IFS call site.**
  - `steps_mm = (1000 * prev.quantization_step, 1000 * curr.quantization_step)`, since `tp` is in m.
  - For the first lead (`previous_precipitation is None`), the previous value is an exact 0, so only the current
    step counts.
  - `consecutive_negative_count` handling is unchanged.
- **D4 Audit fields.** These are written only when at least one step is known:
  - the small and significant IFS anomaly entries gain `tolerance_mm` and `quantization_step_mm`, a list of
    `[prev, curr]` with `None` for an unknown side;
  - nothing is added at the top level of `conversion_params`.

  As a result, real-GRIB products whose lead has no negative delta keep a byte-identical lineage and checksum.
- **D5 Comment.** Rewrite `converter.py:64-69` to describe the floor plus the packing-derived rule, and the 12-bit
  change at `2026102012`.

**Governing invariant:** an IFS precipitation lead is flagged significant-negative only if some delta reaches
`max(env floor, step_prev + step_curr)` in magnitude. A pure quantisation artefact (|delta| ≤ one step on equal
steps) never makes an IFS lead forcing-unusable.

**Sibling surfaces:**
- **Canonical checksum and forcing signature** (`producer.py:3094-3129` hashes canonical `checksum` and
  `quality_flag`; `_existing_product_is_current`, `converter.py:1929`). An IFS precipitation product whose lead has
  a negative delta and a known step gains `tolerance_mm` and `quantization_step_mm` in its lineage, so its checksum
  changes. This affects reconverting the already-converted 16-bit cycles too: their small-negative leads would be
  rewritten, and the forcing signatures that consume them would change.
  - **Accepted consequence:** the convert stage does not rerun already-ready cycles in normal operation.
  - **Replay expectation:** `2026102000` keeps every flag and value; its checksum changes only on the leads that
    carry small anomalies (f006, f009, f018, f072, f093, f099).
- **Readiness and producer usable-flag sets** (`converter.py:338`, `producer.py:1344`): unchanged. The fix works by
  not emitting the unusable flag.
- **Shortwave and net radiation** (ssr/str now 10-bit): out of scope. Shortwave only ever flags `warn`, which is
  forcing-usable (the baseline replay of 2026102012 shows 6 `warn`), and net radiation carries no flag, so there is
  no fail-class path.
- **GFS `apcp`** (cumulative and bucket): out of scope. Its significant flag is `warn`, which is forcing-usable in the
  DB-free production path, so there is no product loss, and its packing was not measured across all lead types.
- **ERA5 `total_precipitation`:** its own path; unchanged and not in production.

**Seams under test:**
- `convert_ifs_precipitation_with_metadata` (pure);
- `grib_packing` helpers (pure);
- `IFSCanonicalConverter.convert_manifest` with step-bearing records;
- `_read_record_with_xarray` on a real GRIB2 file encoded at test time (`grib` lane).

**Required evidence:**
- **Pure IFS, `s = 2^-13` m, both steps known.**
  - prev `0.002`, curr `0.002 - s` gives `ok` and a small anomaly with `tolerance_mm == 2000*s`
    (`0.244140625`).
  - curr `0.002 - 3*s` gives `warning_negative_precip`.
  - Steps `None` with the same −1s input gives `warning_negative_precip`, and the full anomaly dict is literally
    equal to today's (no new keys).
  - First lead (previous `None`) with step `s` gives `tolerance_mm == max(0.1, 1000*s)`.
- **Pure `grib_packing`.** The step is `None` for each of:
  - a missing key;
  - non-int E;
  - `bitsPerValue == 0`;
  - `packingType == "grid_ieee"`;
  - an unknown packingType.

  The step is `2^E/10^D` for `grid_simple`.
- **Converter level.** In an IFS manifest whose `tp` `RawRecord`s carry `quantization_step = s`, the −1s lead is
  written with `quality_flag == "ok"` and `tolerance_mm` in the anomaly, and readiness is `canonical_ready`.
- **`grib` lane.**
  - The test-time encoder writes a multi-point (≥ 4 points), non-constant `tp` field with `bitsPerValue=12`.
  - Reading it through the converter reader gives back `bitsPerValue == 12` (via eccodes), and `quantization_step`
    equals `2^E/10^D` with E and D read by eccodes from the same file.
  - A constant field (all 0) gives `quantization_step is None`.
  - A `.nc` netcdf4 fallback record gives `None`.
- **Real raw replay.** The replay runs on the local Mac with the branch checkout and `uv run`, with
  `OBJECT_STORE_ROOT=<scratchpad>/os-<label>` and `NHMS_CANONICAL_DB_FREE=1`, so there is no DB and no production
  object store. Input is node-22 raw `raw/IFS/{2026102000,2026102012}/` plus its `manifest.json`, copied into that
  root. The receipt is the per-lead `quality_flag` and `checksum` from `_catalog/catalog.json`.
  - **Baseline on master (measured, red):** 2026102012 is `canonical_incomplete`, with f051, f063, f138 and f144
    flagged `warning_negative_precip`; 2026102000 is `canonical_ready` with all 53 prcp leads `ok`.
  - **Expected on the branch:** 2026102012 is `canonical_ready`, all 53 prcp leads `ok`, and those four leads carry
    a small anomaly with `tolerance_mm`. 2026102000 keeps every flag and value; its checksum changes only on the six
    small-anomaly leads.

**Non-goals:**
- Changing the usable-flag sets.
- GFS `apcp`, radiation and temperature precision.
- A `converter_version` bump.
- Deploying to node-22, which follows the separate master sync.

**Review focus:**
- No-step paths are byte-identical, including the anomaly dicts.
- `RawRecord.metadata` is untouched.
- The rule uses the sum of the two steps.
- The m→mm conversion of the IFS step.
- Constant fields and the allow-list.
- A missing key never raises.
