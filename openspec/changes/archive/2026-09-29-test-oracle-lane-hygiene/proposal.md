# test-oracle-lane-hygiene

## Why

Four test-infrastructure defects make node-27, the declared oracle, produce receipts that are either blocked or full of false reds:

- **#2490**: three test files are over the 1000-line limit and are only tolerated through `.large-file-guard.json` exemptions: `tests/test_mvt_national_identity_probe_integration.py` (2370 lines), `tests/test_river_ts_read_path_surrogate_keys_integration.py` (1423) and `tests/test_node27_mvt_cache_retention.py` (1927). The two production files named in #2490 are already under 1000 lines on master (923 and 890), so this batch handles only the three test files.
- **#2595**: `tests/test_object_store_forcing_real_disk.py` hardcodes `LATEST_CYCLE = 2026-06-20T12Z`. node-27 retention has already removed that cycle (on 2026-09-30 `forcing/{ifs,gfs}` held 102 cycles, 2026080912..2026092900), so all 4 tests always return 404.
- **#2594**: the grib marker covers 8 tests, and all 8 fail on node-27 with `Cannot find the ecCodes library`. The runtime exists: `/home/nwm/nhms-grib` ships `libeccodes.so` from conda build `eccodes-2.47.0-ha1d8304_0`, the same build as node-22 production `/scratch/frd_muziyao/nhms-grib`. It is simply never wired into the pytest lane. The conftest opt-in also has no preflight, so a missing library shows up as 8 bare RuntimeErrors.
- **#2615**: the node-27 detached-worktree pytest lane has no written recipe. The venv's `bin` directory is not on `PATH`, so `check-jsonschema` is not found (11 false reds). `PYTHONPATH` leaks into a subprocess test that claims to differ only in cwd (1 false red).

## What Changes

- #2490: physically split the three test files, with no behaviour change, and remove their three exemptions from `.large-file-guard.json`. Update the literal routes in `scripts/select_ci_tests.py` and the meta-tests in the same change.
- #2595: pick the cycle at run time: the newest cycle for which all 4 COMBOS files are present in the real store. Derive every time value from that cycle. If no such cycle exists, fail with an explicit diagnostic.
- #2594: add a GRIB runtime injection step to the node-27 lane in `docs/runbooks/ci-test-routing.md`. When `NHMS_RUN_GRIB=1`, conftest checks that ecCodes can be loaded; if not, it fails loudly with an explicit diagnostic. It never skips.
- #2615: write the detached-worktree recipe into the same runbook. Decouple the two affected tests from the environment: the outside subprocess strips `PYTHONPATH` itself, and `_validator()` falls back to `Path(sys.executable).parent / "check-jsonschema"`.

## Impact

- Tests, `tests/conftest.py`, `scripts/select_ci_tests.py` + `tests/test_select_ci_tests.py`, `.large-file-guard.json`, `docs/runbooks/ci-test-routing.md`.
- No production code change. node-22 is not touched; running `codes_info -v` against its GRIB env once, read-only, is allowed.
