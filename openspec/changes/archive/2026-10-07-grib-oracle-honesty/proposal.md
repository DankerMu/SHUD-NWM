# The grib lane decodes GRIB2, and its eight red tests are fixed at their causes

Issue: #2700 (owner decision 2026-10-08: everything in this one issue, no split). Fixture level: **expanded**.
Risk packs: **Test-oracle honesty**, **Production validation tool behaviour (validate-met deterministic lane)**,
**Forcing producer fail-closed guard**.

## Why

All eight tests under `@pytest.mark.grib` fail (reproduced locally 2026-10-08 with ecCodes 2.41.0; identical
on node-27 with 2.47.0), and none of them decodes GRIB2: their fixtures are NetCDF4, cfgrib fails every time
and the converter falls back to its netcdf4 reader. Diagnosis (read-only, scripts under `/tmp/diag2700/`):

- **IFS e2e, 2 tests** (`tests/test_e2e_ifs.py:23,76`): the fixture loop (`:279-290`) writes eight
  single-variable payloads to the same per-forecast-hour bundle key; only `str` survives. Fixture defect.
- **validate-met, 6 tests** (`tests/test_production_met_validation.py:45,264,372,458,559,595`): the
  deterministic GFS fixture of `services/production_closure/met_validation.py` (`:682-685`) writes `apcp` and
  `dswrf` at f000. Real GFS has neither at f000 and the production manifest excludes them
  (`workers/data_adapters/gfs_adapter.py:107` `GFS_F000_UNAVAILABLE_VARIABLES`, `:1617-1622`). With an interval
  product ending at cycle time, `_gfs_interval_row_times` (`workers/forcing_producer/producer.py:3617-3626`)
  maps both interval ends to the row time `cycle_time`: two identical rows per variable, continuity fails,
  `status=blocked`. Second layer: `_expected_valid_times` (`met_validation.py:2023`, used at `:853`) expects
  one row per forecast hour, while GFS forcing rows sit at interval starts (hours {0,3} → one row at 00Z).
  **Not a production defect**: production never has interval products at f000. It is a defect of the
  validation tool's fixture and expectation — the tool has reported `blocked` on its default lane since
  before 2026-07-25 (history boundary), unseen because the tests were only ever collected where ecCodes
  failed to load.
- **Latent producer gap**: `_gfs_interval_row_times` has no guard for an interval ending at or before the
  cycle time; it silently emits duplicate row times. In production that would surface later as a primary-key
  violation at write time (inferred from the schema, not run).

## What changes

1. **validate-met deterministic lane** (`services/production_closure/met_validation.py`):
   - the deterministic GFS source manifest/raw fixture skips the variables of
     `gfs_adapter.GFS_F000_UNAVAILABLE_VARIABLES` at forecast hour 0 (imported, not re-listed);
   - the forcing-evidence continuity expectation (`_write_forcing_evidence`, `:811-856`, GFS only) is the
     interval-start row times: for forecast hours {0,3}, `[cycle 00Z]`. It is computed from the **configured**
     forecast hours, not from the products the producer saw (so a canonical product that went missing still
     fails continuity): the producer exposes its interval rule as a public function taking only times —
     `gfs_interval_row_times(interval_end_times, *, cycle_time)`, the existing `_gfs_interval_row_times`
     made public (private name kept as an alias only if other callers need it) — and validate-met calls it
     with `cycle_start + h` for every configured hour `h > 0`. One rule, two callers, independent inputs. The
     two negative checks at `:1437` / `:1467` keep `_expected_valid_times` (their rows come from
     `_forcing_rows_for_products`, one per product time);
   - the deterministic manifest writer (`:682-685`) and `_validate_deterministic_shape` (`:1169-1171`) share
     one `(forecast_hour, variable)` enumeration, so the shape check counts exactly what is written.
   The lane stays NetCDF4: validate-met also runs where ecCodes is absent, and its deterministic fixture is a
   closure smoke, not a decode oracle.
2. **The six validate-met tests** lose `@pytest.mark.grib` (they decode no GRIB2 and need no ecCodes) and
   keep every other marker they have; their expectations follow item 1 (`:102,105,276` 15 → 13 products;
   `:118` 14 → 12; `:142` `expected_valid_times` `["2026-05-07T00:00:00Z"]`). They must then actually run and
   pass in the lane that collects them (pure CI unit lane unless another marker gates them — the implementer
   states which lane and shows them executed, not skipped).
3. **Producer guard** (`workers/forcing_producer/producer.py`): the interval row-time function raises
   `ForcingProductionError` when an interval end is not strictly after the previous end (first: the cycle
   time), naming the cycle time and the offending valid time. No change for valid inputs.
4. **Real GRIB2 for the grib lane**:
   - new test-only helper `tests/grib2_fixture_support.py`: encodes multi-message GRIB2 at test time with the
     installed ecCodes (`codes_grib_new_from_samples("regular_ll_sfc_grib2")`; keys `centre`, `Ni`/`Nj`,
     first/last grid point, increments, `dataDate`, `dataTime`, `shortName`, `step`, `bitsPerValue`, values),
     so fixtures always match the runtime's ecCodes version. After setting `shortName` it reads the key back
     and fails on a mismatch (concept resolution is the version-dependent part). It exposes the ecCodes
     version string and a reader that lists the shortNames of a file message by message with ecCodes. It
     imports `eccodes` inside its functions, and the tests import the helper **inside the test function
     bodies**: pure CI imports every test module without ecCodes (`uv.lock` has the `eccodes` python package
     but not the library, so a module-level import raises), and a module-level import of a `tests/` support
     module would need a `SUPPORT_MODULE_TEST_RULES` entry (`tests/test_select_ci_tests.py:7024-7054`);
   - the two IFS e2e tests write one GRIB2 bundle per forecast-hour key holding all eight shortNames
     (`2t 2d 10u 10v tp sp ssr str`) with the values `_ifs_raw_value` gives, assert that every bundle holds
     the eight shortNames (read message by message with ecCodes, not an unfiltered cfgrib open), and assert
     that the conversion used cfgrib. The converter has no positive engine indicator; it logs the fallback
     at WARNING on logger `workers.canonical_converter.converter` (`converter.py:1471-1475`), so the tests
     assert zero such records under `caplog.at_level(logging.WARNING, logger=...)`. The pre-written cells-layout grid
     definition (`tests/test_e2e_ifs.py:31,94`) goes or becomes rectilinear — GRIB decodes to a 2-D grid and
     `_ensure_grid_definition` (`converter.py:1818-1851`) rejects a different pre-existing layout;
   - one new grib-marked test in `tests/test_canonical_converter.py` (already routed by the selector;
     function-level `@pytest.mark.grib` only — a file-level `pytestmark = grib` would break the
     `GATING_MARKER_NAMES` anchoring, `tests/test_select_ci_tests.py:5405-5409`) decodes a GFS-style GRIB2
     through the converter's real read path with zero fallback; the shortNames come from
     `gfs_adapter.GFS_GRIB_SHORT_NAME` (`:631`), not a second list;
   - every grib-marked test prints the ecCodes version; the node-27 lane command gains `-rA` so passed tests'
     output reaches the receipt (`docs/runbooks/ci-test-routing.md:126-127`).
5. Marker text: `pyproject.toml:111`, `tests/conftest.py:80` and `docs/runbooks/ci-test-routing.md:19,141-145` say what the lane now is:
   GRIB2 encoded at test time with the runtime's ecCodes and decoded through cfgrib; no checked-in GRIB files.

6. `forecast_hours` without any hour above 0 is refused by `_validate_config` with
   `PRODUCTION_MET_FORECAST_HOURS_INVALID` and a message naming the reason (the GFS deterministic lane has no
   interval product at f000, so it cannot produce a forcing row). Before this change that configuration ended
   `ready` on the strength of the fake f000 interval products; without the refusal it would end `blocked`
   with a converter "missing canonical variables" error that does not point at the configuration, and the
   continuity expectation would be empty.
7. `.large-file-guard.json` gains an exclusion for `services/production_closure/met_validation.py` (2055 lines
   on master, over the 1000-line limit before this change; +~30 lines here). Splitting the module is not part
   of this issue.

## Must preserve

- Production forcing output for every valid input: the producer change only replaces a silent duplicate by an
  error.
- validate-met's CLI, evidence file names and schema; only the deterministic fixture content and the
  continuity expectation change. Any runbook or test that quotes the old counts is updated in the same PR.
- `tests/test_e2e_ifs.py` non-grib tests and `packages/common/test_netcdf4.py` behaviour for existing callers.
- Pure CI (`-m "not e2e and not grib and not integration"`) must import every test module without ecCodes.

## Out of scope

Accumulated-product GRIB templates (`stepType`/`stepRange` fidelity): the converter selects by shortName;
multi-point-grid interpolation oracles; checked-in GRIB samples; the `.nc` suffix of deterministic GFS keys.

## Evidence

- Red first: the eight tests fail on master (recorded); a unit test of `_gfs_interval_row_times` with an
  interval end equal to the cycle time fails before the guard (returns duplicates) and passes after.
- Local with ecCodes: `NHMS_RUN_E2E=1 NHMS_RUN_GRIB=1 uv run pytest -q -m grib -rA -s tests/test_e2e_ifs.py
  tests/test_canonical_converter.py` → all pass, the output shows the ecCodes version and no fallback record; breaking
  the encoder to omit one shortName makes the bundle assertion fail; forcing the fallback (monkeypatching the
  cfgrib open to raise) makes the no-fallback assertion fail.
- The six validate-met tests pass without `NHMS_RUN_GRIB`, in the lane named in the report, and are shown
  executed (not skipped) there; `tests/test_production_met_validation.py` passes in full.
- A validate-met unit assertion that the deterministic GFS manifest has no `apcp`/`dswrf` entry at f000 and
  that the continuity expectation equals the producer's planned row times.
- Every suite the selector maps to `workers/forcing_producer/**` passes (the guard must not fire on any
  existing GFS seed).
- node-27 probe **before** merge, done 2026-10-08 (files only, outside the deployed checkout): the prototype
  encoder under the documented lane environment (ecCodes 2.47.0, no `ECCODES_SAMPLES_PATH`) produced and
  cfgrib-decoded all eight IFS and seven GFS shortNames at steps 0 and 3, values round-tripped.
- `uv run ruff check .`; selector meta-guards after staging; pure-CI collection without ecCodes proven by
  running the touched test modules with `eccodes`/`cfgrib`/`gribapi` blocked in `sys.modules` (or equivalent).
- node-27 after merge, read-only files, no database: the grib lane of `docs/runbooks/ci-test-routing.md`
  (`NHMS_GRIB_ENV_ROOT=/home/nwm/nhms-grib`) with `-m grib` → all passed; output is the receipt.
