# CMFD P0.2 Synthetic Direct-Grid Evidence Package

Evidence fixture for change `cmfd-direct-grid-platform-readiness`, task 2.3.
This package is **evidence-only**: it lets the 2.4 real-object-store + real-DB
smoke exercise the direct-grid staging invariants against a byte-stable, fully
hand-derived input contract that mirrors the fixture shape used by
`tests/test_direct_grid_e2e.py`.

Pinned baseline: `readiness-manifest.v1.json` (baseline_commit
`5e518c151375b798c29ee3cafb3260413ac8905f`, manifest SHA-256
`bbbc4143d228dc36d6f0973a51060a9debe54b81f49505682de709ded88eeeaf`).

## 1. Purpose and non-goals

- Purpose: provide a minimal but §7.2/§7.3-conformant direct-grid contract plus
  the matching `.sp.att` / `.tsd.forc` / per-station CSVs, so `docs 2.4` can
  register it on node-27 as a dedicated evidence-only `core.model_instance`
  and exercise the runtime staging path (`_validate_direct_grid_sp_att_forcing_ids`
  et al.) without touching any production model.
- Non-goal: this is not the output of a mapping builder, not a substitute for
  a real basin package, and not part of the 13 production model instances.
  All values are hand-picked to keep the invariants trivially inspectable.

## 2. Package tree

```
synthetic-package/
  README.md                                 (this file)
  package/
    input_dir/
      synth-basin/
        synth-basin.sp.att                  (3 elements, FORC ∈ {1,2,3})
        synth-basin.sp.att.sha256
    forcing/
      qhh.tsd.forc                          (3 station header + rows for IDs 1,2,3)
      qhh.tsd.forc.sha256
      station-001.csv                       (5 timesteps, hand-derived)
      station-001.csv.sha256
      station-002.csv
      station-002.csv.sha256
      station-003.csv
      station-003.csv.sha256
    binding-manifest.json                   (§7.2 top-level + §7.3 per-binding)
    binding-manifest.json.sha256
    package.manifest.sha256                 (aggregate sha256 over sorted sidecars)
```

## 3. Per-value derivation table

### 3.1 Stations (chosen coordinates)

| station_id        | lon    | lat   | x | y | z   | forcing_filename  | grid_cell_id            |
|-------------------|-------:|------:|--:|--:|----:|-------------------|-------------------------|
| synth-station-001 | 100.0  | 30.0  | 1 | 1 | 100 | station-001.csv   | cell-0100.00-0030.00    |
| synth-station-002 | 100.5  | 30.0  | 2 | 1 | 150 | station-002.csv   | cell-0100.50-0030.00    |
| synth-station-003 | 100.0  | 30.5  | 1 | 2 | 200 | station-003.csv   | cell-0100.00-0030.50    |

Rationale:

- Lon/Lat: three deliberately-visible integer/half-integer grid centers, forming
  an L-shape on a 0.5° grid so any spatial-order regression is instantly visible.
- x / y: 1-based grid cell indices matching the (lon, lat) placement on the
  synthetic 0.5° grid.
- z: 100 / 150 / 200 — arbitrary but strictly non-zero and strictly ordered so
  a swap/reorder bug is immediately visible.
- forcing_filename: `station-<ID>.csv`, matching the 1-based ID column of
  `qhh.tsd.forc`; relative filename, same convention as the e2e fixture in
  `tests/test_direct_grid_e2e.py`.
- grid_cell_id: `cell-<lon×100 zero-padded to 4>.00-<lat×100 zero-padded to 4>.00`
  built from the chosen (lon, lat) as canonical cell-center identity. Format
  is stable and evidence-local; not a production grid registry ID.

### 3.2 `synth-basin.sp.att`

- Line 0: `3` — element count (spec: first line is element count).
- Line 1: `ID\tA\tB\tC\tFORC` — column header (spec).
- Lines 2..4: three element rows with IDs 1, 2, 3.
  - `A=100`, `B=200`, `C=300` — arbitrary but strictly non-zero placeholders,
    monotonically increasing so a column swap is obvious.
  - `FORC ∈ {1, 2, 3}` — 1:1 mapping element→station, so
    `FORC-set = {1,2,3} ⊆ tsd_forc IDs {1,2,3}` (equality holds trivially).

### 3.3 `qhh.tsd.forc`

- Line 0: `3 20260707` — `<station_count> <YYYYMMDD>`. Count 3 matches the
  element rows above; date `20260707` is the evidence-production date
  (2026-07-06 CST rounded to next UTC day as a stable synthetic anchor).
- Line 1: `shud` — spec literal.
- Line 2: `ID\tLon\tLat\tX\tY\tZ\tFilename` — header (spec).
- Lines 3..5: three station rows; values pulled directly from the station
  table above.

### 3.4 Per-station CSVs

Line 0 is a provenance annotation (SHUD/wrapper convention skips line 1).
Line 1 is the header `Time_Day\tPrecip\tTemp\tRH\tWind\tRN` (no `Press`,
matching direct-grid CSV shape asserted at
`tests/test_direct_grid_e2e.py:147-149`).

Values are hand-picked deterministic ramps, chosen so:

- every column is inside its physical band (Precip 0–5 mm, Temp 5–25 °C,
  RH 0.3–0.9, Wind 1–10 m/s, RN 100–500 W/m²);
- values differ per station AND per timestep, so column mis-alignment
  (e.g. RH vs. RN swap) is trivially detectable.

| station | day 0                              | day 1                             | day 2                             | day 3                             | day 4                             |
|---------|------------------------------------|-----------------------------------|-----------------------------------|-----------------------------------|-----------------------------------|
| 001     | 1.0 / 10.0 / 0.50 / 3.0 / 200      | 1.5 / 11.0 / 0.55 / 3.5 / 210     | 2.0 / 12.0 / 0.60 / 4.0 / 220     | 2.5 / 13.0 / 0.65 / 4.5 / 230     | 3.0 / 14.0 / 0.70 / 5.0 / 240     |
| 002     | 2.5 / 15.0 / 0.65 / 5.0 / 300      | 3.0 / 16.0 / 0.70 / 5.5 / 310     | 3.5 / 17.0 / 0.75 / 6.0 / 320     | 4.0 / 18.0 / 0.80 / 6.5 / 330     | 4.5 / 19.0 / 0.85 / 7.0 / 340     |
| 003     | 0.5 / 20.0 / 0.80 / 7.0 / 400      | 1.0 / 21.0 / 0.82 / 7.5 / 410     | 1.5 / 22.0 / 0.84 / 8.0 / 420     | 2.0 / 23.0 / 0.86 / 8.5 / 430     | 2.5 / 24.0 / 0.88 / 9.0 / 440     |

Columns: `Precip / Temp / RH / Wind / RN`.

### 3.5 `binding-manifest.json` (§7.2 + §7.3)

| Field                     | Value                                                             | Derivation                                                                        |
|---------------------------|-------------------------------------------------------------------|-----------------------------------------------------------------------------------|
| `forcing_mapping_mode`    | `direct_grid`                                                     | Spec §7.2 constant for this readiness slice.                                      |
| `binding_uri`             | `synth://cmfd-p0.2-direct-grid-evidence/v1`                       | Synthetic scheme; makes it impossible to confuse with an object-store binding.    |
| `binding_checksum`        | `cdf0859b88828d5d4f16c22954b78bf0c36a9b838016b8a91174bdbf39a5dc07` | See §4.2 for the self-referential-hash construction.                              |
| `model_input_package_id`  | `synth-basin-v1`                                                  | Chosen; matches the `input_dir/synth-basin/` directory name.                      |
| `sp_att_path`             | `input_dir/synth-basin/synth-basin.sp.att`                        | Relative to the package root.                                                     |
| `sp_att_checksum`         | `74a64acaab43c7bc61ea9e0eccc83f1116e04b3f73905a39e8f7a4e47b517dde` | sha256 of `synth-basin.sp.att` file bytes verbatim.                               |
| `applicable_source_ids`   | `["cmfd"]`                                                        | Evidence-only; §7.2 allows any source_id subset — pinned to CMFD for this change. |
| `grid_id`                 | `synth-grid-p0.2-v1`                                              | Chosen synthetic grid identifier; not registered with any real grid registry.     |
| `grid_signature`          | `afafe1c814ad6b7a212455c2c8f25d6abaa22f3dd991f232d3749ff7f7d48449` | `sha256("synth-grid-p0.2-v1@cell=0.5deg")` — canonical string documented here.    |
| `station_bindings[i]`     | 3 entries                                                         | One §7.3 record per station; fields copy the §3.1 table verbatim.                 |

## 4. Invariant checks

### 4.1 `sp.att FORC ⊆ tsd.forc ID`

`.sp.att` FORC column: `{1, 2, 3}`
`.tsd.forc` ID column: `{1, 2, 3}`
Subset holds (equality in this synth). This is exactly the runtime
`DIRECT_GRID_FORCING_OWNERSHIP_RANGE` gate at
`workers/shud_runtime/runtime.py:2205-2213` (`_validate_direct_grid_sp_att_forcing_ids`
validator, raise at :2210); the reader helper `_read_sp_att_forcing_ids` at
:2216-2274 supplies the FORC id set that the validator checks against. The
2.4 smoke asserts the invariant directly by parsing both files.

### 4.2 `binding_checksum` — self-referential SHA-256 (verification recipe)

The manifest's `binding_checksum` field is a self-referential SHA-256 covering
the manifest bytes themselves with the checksum field's own 64-hex value
replaced by the empty string. It is NOT a re-serialization from parsed JSON —
`json.dumps(manifest, indent=2)+'\n'` produces a DIFFERENT byte layout
(1512 vs the on-disk 1504 bytes) and would not reproduce the recorded value.

Recipe to reproduce the recorded value:

```python
import hashlib, pathlib

path = pathlib.Path("openspec/changes/cmfd-direct-grid-platform-readiness/evidence/synthetic-package/package/binding-manifest.json")
raw = path.read_bytes()
recorded_hex = b"cdf0859b88828d5d4f16c22954b78bf0c36a9b838016b8a91174bdbf39a5dc07"
target_bytes = raw.replace(recorded_hex, b"")
assert hashlib.sha256(target_bytes).hexdigest() == recorded_hex.decode()
```

This is byte-for-byte reproducible against the on-disk `binding-manifest.json`.
Any editor reformatting of the JSON (indent change, key reorder, trailing-newline
change) will invalidate the checksum; regenerate by (a) resetting `binding_checksum`
to `""`, (b) saving the file with your editor, (c) reading the raw bytes,
(d) sha256, (e) pasting the digest back into the field.

### 4.3 `sp_att_checksum`

`sha256(bytes(synth-basin.sp.att))` verbatim — no normalization, no strip.
Expected: `74a64acaab43c7bc61ea9e0eccc83f1116e04b3f73905a39e8f7a4e47b517dde`.

### 4.4 Per-station CSV row count

Each `station-00N.csv` has 2 preamble lines + 5 data rows = 7 lines total.
The 5-timestep count matches the day range `[0, 4]` used across all three
stations, so `timestep_count = 5` is a per-package invariant.

### 4.5 `grid_signature`

Canonical string: `synth-grid-p0.2-v1@cell=0.5deg` (bytes, no newline).
`sha256` of that string:
`afafe1c814ad6b7a212455c2c8f25d6abaa22f3dd991f232d3749ff7f7d48449`.

## 5. `package.manifest.sha256` aggregate

Definition: `sha256` over the alphabetically-sorted concatenation of every
per-file `*.sha256` sidecar's on-disk bytes (each ending in `\n`), taken by
`sort` on POSIX-standard byte-lexicographic order of relative paths from the
`package/` root.

Reproduction from `package/`:

```
find . -name '*.sha256' ! -name 'package.manifest.sha256' | sort | xargs cat | shasum -a 256
```

Sorted sidecar order (as recorded in the aggregate):

1. `./binding-manifest.json.sha256`
2. `./forcing/qhh.tsd.forc.sha256`
3. `./forcing/station-001.csv.sha256`
4. `./forcing/station-002.csv.sha256`
5. `./forcing/station-003.csv.sha256`
6. `./input_dir/synth-basin/synth-basin.sp.att.sha256`

Expected aggregate:
`0baeaf810241b4bc06b129acc0785b1840b79fb32a664b9d42817d3a33aadae5`.

Recorded in `package/package.manifest.sha256` with `package.manifest` as the
symbolic file label (the aggregate itself is not a real file).

## 7. Operator runbook (§2.4 consumers)

The env-gated smoke carrier `tests/test_direct_grid_evidence_smoke.py` consumes
this fixture package. It is a skeleton pre-flight (structural checks only) —
§2.4's actual real-object-store + real-DB smoke will extend it.

To run the current structural checks on node-27
(`ssh -p 32099 nwm@210.77.77.27`, `cd /home/nwm/NWM`):

```bash
NHMS_RUN_E2E=1 \
NHMS_RUN_CMFD_P02_SMOKE=1 \
DATABASE_URL="${DATABASE_URL:?set via infra/env/node27-ingest.env}" \
NHMS_CMFD_P02_SYNTHETIC_PACKAGE_ROOT="$PWD/openspec/changes/cmfd-direct-grid-platform-readiness/evidence/synthetic-package/package" \
uv run pytest -q tests/test_direct_grid_evidence_smoke.py
```

Environment contract:

| variable | required | value |
|---|---|---|
| `NHMS_RUN_E2E` | yes | `1` (bypasses `tests/conftest.py:22-36` auto-skip for the `e2e` marker; without it, all tests skip at collection time before `_gate_or_skip` runs) |
| `NHMS_RUN_CMFD_P02_SMOKE` | yes | `1` |
| `DATABASE_URL` | yes | inherit from `infra/env/node27-ingest.env` (writable `nhms` role) |
| `NHMS_CMFD_P02_SYNTHETIC_PACKAGE_ROOT` | yes | absolute path to the `package/` directory containing `binding-manifest.json`, `forcing/`, and `input_dir/` |

If any of the four variables is unset, tests skip cleanly (default CI behavior). Expected PASS output on node-27: `4 passed in ~0.02s`; any `skipped` count means at least one env var above is unset.
