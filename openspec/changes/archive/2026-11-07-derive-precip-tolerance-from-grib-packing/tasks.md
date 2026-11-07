## 1. Implementation

- [x] 1.1 Add `workers/canonical_converter/grib_packing.py` with:
  - `quantization_step_from_attrs(attrs)`: returns `2^E/10^D` for `grid_simple`; returns `None` for a missing key or one that is not an integer (accept any `numbers.Integral`, numpy ints included, and reject bool), `bitsPerValue <= 0`, a packingType outside the allow-list, or a non-finite or non-positive result.
  - `negative_noise_tolerance_mm(floor_mm, *steps_mm)`.
- [x] 1.2 Reader:
  - inject `read_keys` once in `_read_record_with_xarray`, so all three `_cfgrib_backend_kwargs` branches get it;
  - add the `RawRecord.quantization_step: float | None = None` field; it is set only after a successful cfgrib open.
- [x] 1.3 IFS tp: `convert_ifs_precipitation_with_metadata` takes `quantization_step_m` / `previous_quantization_step_m` (default `None`). At the call site, pass the current record's step, and the previous record's step only when a previous record exists. When a step is known, add `tolerance_mm` and `quantization_step_mm` to the anomaly entries; when none is known, emit no new keys.
- [x] 1.4 Rewrite the comment at `converter.py:64-69`.

## 2. Tests

- [x] 2.1 Pure IFS tests (design "Pure IFS"): −1s → ok with `tolerance_mm == 0.244140625`; −3s → warning; the no-step case gives a warning and the full anomaly dict is literally equal to today's; first-lead tolerance.
- [x] 2.2 Pure `grib_packing` tests: each case in which the step is `None`, and the `grid_simple` value.
- [x] 2.3 Converter-level IFS manifest test with step-bearing `RawRecord`s: the −1s lead is ok with `tolerance_mm` in the anomaly, and readiness is `canonical_ready`.
- [x] 2.4 `grib`-marker test with a multi-point, configurable-`bitsPerValue` encoder added to `tests/grib2_fixture_support.py`. The existing `encode_grib2_bundle` stays unchanged. Cases:
  - a 12-bit non-constant `tp` gives a step equal to the eccodes-read `2^E/10^D`, and `bitsPerValue == 12` is asserted;
  - a constant field gives `None`, and eccodes reads back `bitsPerValue == 0` from the same file, so the test proves the `bitsPerValue <= 0` branch;
  - the netcdf4 fallback gives `None`.
- [x] 2.5 Update the single exact `backend_kwargs` assertion (`tests/test_canonical_converter.py:1114`) to include exactly the `read_keys` entry, and add one assertion for the default branch (recorded deviation).

## 3. Verification

- [x] 3.1 Local: `uv run ruff check .`
- [x] 3.2 Local: `uv run pytest -q tests/test_ifs_canonical.py tests/test_canonical_converter.py tests/test_ifs_adapter.py tests/test_gfs_adapter.py tests/test_era5_canonical.py tests/test_e2e.py tests/test_e2e_ifs.py tests/test_source_identity.py` plus the new test files.
- [x] 3.3 node-27 `grib` lane, in a temporary worktree so the live ingest checkout is not touched:
  - `git worktree add /home/nwm/NWM-wt-3090 <branch>`, then `mkdir -p /home/nwm/tmp && export TMPDIR=/home/nwm/tmp`, then `uv sync --all-extras --dev` inside the worktree;
  - run `uv run pytest -q -m grib tests/test_canonical_converter.py tests/test_ifs_canonical.py tests/test_e2e_ifs.py` plus the same 3.2 file list;
  - afterwards run `git worktree remove` and delete the worktree's `.venv`.
- [x] 3.4 Local real-raw replay as described in design "Real raw replay": `OBJECT_STORE_ROOT=<scratchpad>/os-fix NHMS_CANONICAL_DB_FREE=1 uv run python -m workers.canonical_converter.cli convert --source-id IFS --cycle-time <c>` for 2026102000 and 2026102012. The receipt compares per-lead flag and checksum against the master baseline (`os-base`).

## Risk packs

- Public API / CLI / script entry: not selected. No entrypoint changes; the CLI is used unchanged for the replay.
- Config / project setup: selected. The env var keeps its meaning as the floor (2.1, no-step and first-lead cases).
- File IO / path safety: not selected. The reader opens the same files, and the replay is isolated in the scratchpad.
- Schema / columns / units: selected. Additive anomaly keys, written only when a step is known, and the m→mm step conversion (2.1, 2.3, 3.4).
- Auth / permissions: not selected.
- Concurrency / shared state: not selected.
- Resource limits: not selected. Four extra keys are read per message.
- Legacy compatibility: selected. No-step callers, the netcdf4 fallback, the `backend_kwargs` assertion, and checksum stability on leads without negatives (2.1 literal equality, 2.4, 2.5, 3.2, 3.4 checksum diff).
- Error handling: selected. Missing, odd or constant-field keys give `None` and never raise (2.2, 2.4).
- Release / packaging: not selected. `read_keys` exists in the pinned cfgrib (`uv.lock` cfgrib 0.9.15.1; `pyproject.toml` lower bound `cfgrib>=0.9.10.4`).
- Documentation: selected (1.4).
