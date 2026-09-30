# Design — test-oracle-lane-hygiene

## D1 #2490: pure physical split

- **Keep the original path as one part.** Each of the three original paths stays as one part of its own split. The other parts are new `test_*.py` suites. Code shared between parts moves into a non-collectible helper module. This keeps the path pinned in `openspec/specs/ci-contract-baseline/spec.md:1039` valid, so no baseline spec delta is needed.
- **Naming.** Every part and helper split from either integration file must have `integration` in its file name, for example `tests/test_mvt_national_identity_probe_lineage_integration.py` or `tests/mvt_national_identity_probe_integration_helpers.py`. The existing `database` filter glob `tests/*integration*.py` (`.github/workflows/ci.yml:146-147`) then covers them, and neither `ci.yml` nor the #1688 registry needs to change. The retention helper is not an integration file; name it `tests/node27_mvt_cache_retention_helpers.py`. No test module may import another test module.
- **Markers and fixtures.** Every part keeps a **module-level** `pytestmark`, copied exactly; the two integration files carry `integration`. Per-function decorators are not allowed, because #1447's gating detection reads the module-level mark. The retention file's autouse fixture `_clean_env` (currently `:55`) must reach every retention part: define it in the helper and import it into each part with `# noqa: F401`.
- **Size.** Every file, new or remaining, stays strictly under 1000 lines.
- **Collected-set oracle.** Compare only nodes from the three old paths. Take master's `--collect-only -q` nodes whose path starts with one of the three old files. Take the branch's nodes from the union of all parts. Strip the file prefix and compare the multisets of `::<name>[params]`. They must be equal in three configurations: default, `NHMS_RUN_INTEGRATION=1` with a dummy URL, and `-m integration`. Other files, such as new tests or parametrised meta-tests, are not part of the comparison.
- **Run oracle.** `--collect-only` does not show conftest skip marks, so also run `uv run pytest -q -rs <old three>` on master and `<new file set>` on the branch locally, and compare the passed and skipped counts. The retention suite really runs locally; the two integration files skip entirely. On node-27, `-m integration` against a throwaway DB must give the same passed count on master and on the branch.
- **Why CI is not evidence.** The selector's `meta_guard_only` collapse can reduce the targeted run to a meta-guard-only run, so a green PR proves nothing about this split.
- **Selector changes.** Every literal reference to the old files in `scripts/select_ci_tests.py` switches to the full part set. Every owner path that selected an old file must select all of its parts; the guarded-module closure guard requires required ⊆ selected, so selecting more does not fail. Support-module rules must match what the closure guard can derive (`tests/test_select_ci_tests.py:12694-12723`; the importer derivation at :5425-5440 skips importers gated by a file-level `integration`/`e2e` mark):
  - Add `SUPPORT_MODULE_TEST_RULES` + `SUPPORT_MODULE_ROUTING_ANCHORS` entries for the retention helper (anchor: any retention part) and for `tests/object_store_forcing_real_disk_support.py` (anchor: `tests/test_object_store_forcing_real_disk_support.py`; the real_disk suite is e2e-gated and does not count).
  - The preflight test goes under the existing conftest rule.
  - Do **not** add entries for the two `*integration*` helpers. Their derived non-gated importer set is empty, so any entry would make the guard fail. In the PR lane they are covered by the meta-guard collapse plus the `tests/*integration*.py` database filter, which is why the naming rule above exists.
  - Proof: `uv run pytest -q tests/test_select_ci_tests.py -k support_module` is all green.
  #1447 stays enforced through the module-level `pytestmark` of each part, the selector comments at `scripts/select_ci_tests.py:3746-3749,3850-3853`, and the `tests/test_select_ci_tests.py:1865` assertion extended to every part.
- **Exact-set assertions to update.** In `tests/test_select_ci_tests.py`, update the exact-set assertions that name these files: `:245-253`, `:1865`, `:3996-4001` (the raw-retention template compares with `==`; the mutant test at `:4025` must also cover every part), `:11347-11349` and `:14259`, plus any others found. The #1913/#1948 partition guards stay green.
- **`.large-file-guard.json`.** Delete only the three exemption lines; change nothing else.
- **Hook check.** The hook fires on `git commit`. Committing all touched files in one real commit must exit 0.

## D2 #2595: derive the cycle at run time

- **Cycle selection lives in a support module.** Put it in a non-collectible support module, `tests/object_store_forcing_real_disk_support.py`, as `latest_complete_cycle(root, combos, lookup)`.
  - For each combo, `lookup` supplies the `basin_version_id` and `forcing_filename` needed by production path resolution (`_resolve_disk_path` / `_compute_cycle_compact`, `packages/common/object_store_forcing.py:254-276`). In production that lookup is `PsycopgStationLookup.from_env()`; the local tests pass a fake.
  - Candidate cycles are the cycle directories under the source's forcing root. A cycle qualifies only when all 4 combos resolve to an existing CSV whose mtime is at least 10 minutes old, so a cycle still being written is excluded.
  - The newest qualifying cycle is returned as an ISO `Z` string.
  - If no cycle qualifies, call `pytest.fail("real store has no cycle where all 4 combos are present (root=..., per-combo counts=...)")`. Never skip.
- **Evaluation timing.** In the real_disk suite the cycle is evaluated lazily by a `scope="module"` fixture, after the `_real_object_store_root()` skip gate (`:206-217`). It is computed once per module, so all 4 tests use the same cycle, and the fixture prints it (`-s` / `-rA` puts it in the receipt). Nothing is computed at import time: the CI `--collect-only` smoke imports this module without `OBJECT_STORE_ROOT`.
- **Derived times.** All times come from that cycle: the `from`/`to` window, the equivalent +08:00 instant and the window assertions, keeping the original offsets from 06-20T12Z (cycle+3h, cycle+6h, and so on). The suite asserts the 3h step between the chosen CSV's first two data rows (`workers/forcing_producer/producer.py:2665-2699`) instead of assuming it. Alignment with the cycle itself is proven by the window assertions. `MISSING_CYCLE` is a negative case and stays unchanged.
- **Baseline fixture.** The baseline JSON is shape-only and cannot hold a comment: adding a key would break `_assert_station_series_shape`'s key-list equality. The note that the shape does not depend on the date goes next to `BASELINE_FIXTURE` in the test module. The file is not renamed.
- **Local tests.** A new file, `tests/test_object_store_forcing_real_disk_support.py`, has no `e2e`/`real_disk` markers. It exercises the support module with temporary `OBJECT_STORE_ROOT` trees and a fake lookup. Required cases:
  - the newest common cycle wins;
  - a newest cycle missing one combo is skipped over;
  - a too-fresh file is excluded;
  - an empty intersection fails with the diagnostic.

  Add selector routing for the support module and this test.

## D3 #2594: GRIB runtime wiring and preflight

- **Runbook lane.** Mirror `scripts/run_qhh_cycle.sbatch:45-51`:
  ```
  export NHMS_GRIB_ENV_ROOT=/home/nwm/nhms-grib
  export LD_LIBRARY_PATH=$NHMS_GRIB_ENV_ROOT/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
  export ECCODES_DIR=$NHMS_GRIB_ENV_ROOT
  export ECCODES_DEFINITION_PATH=$NHMS_GRIB_ENV_ROOT/share/eccodes/definitions
  ```
  Record the runtime-source fact in the runbook: `eccodes-2.47.0-ha1d8304_0`, the same conda build as the production compute GRIB env, checked on 2026-09-30. Existing guards on `ci-test-routing.md` still apply:
  - The first bash fence containing `nwm@210.77.77.27` and `uv run --no-sync pytest` is the node-27 lane (`tests/test_node22_entrypoint_invariant.py:180-186`). The GRIB exports go inside that fence, or into a fence placed after it.
  - The text must not contain `uv sync` or `/home/nwm/NWM/.venv/bin/python` (`:200-202`).
- **Conftest preflight.** It lives in `pytest_collection_finish(session)`, after `-m`/`-k` deselection, and acts only on `session.items`.
  - It runs only when `NHMS_RUN_GRIB=1` and at least one remaining item has the `grib` keyword.
  - The probe is `import eccodes; eccodes.codes_get_api_version()`, wrapped in a module-level function so tests can monkeypatch it.
  - On failure it raises `pytest.UsageError` with one message naming the missing ecCodes runtime and pointing at `docs/runbooks/ci-test-routing.md`. It never skips.
  - Without `NHMS_RUN_GRIB=1`, behaviour is unchanged.
  - The conftest must not contain the text `node-22` or `uv run pytest -m` (`tests/test_node22_entrypoint_invariant.py:375-378`).
  - The `pytest_collection_modifyitems` AST contract (`_conftest_auto_skip_markers`, `tests/test_select_ci_tests.py:5223-5258`) is not touched.
- **Preflight tests.** A new test pins the preflight: with the probe monkeypatched to fail it raises `UsageError`; with `NHMS_RUN_GRIB` unset, or with no grib item, it does nothing. If that test imports `tests.conftest`, add it to the conftest `SUPPORT_MODULE_TEST_RULES` tuple (`scripts/select_ci_tests.py:1915-1921`).

## D4 #2615: detached-worktree lane

- **Runbook recipe.** Place it after the node-27 lane fence.
  1. `git worktree add --detach <wt> <sha>`, then `cd <wt>`.
  2. Set `PATH=<active checkout>/.venv/bin:$PATH` (write it as `$HOME/NWM/.venv/bin`), `PYTHONPATH=<wt>` and `TMPDIR=/home/nwm/tmp`.
  3. Before pytest, assert `python -P -c 'import packages; assert packages.__file__.startswith("<wt>/"), packages.__file__'`. The `-P` is required: without it `-c` puts the cwd first on `sys.path`, and the check passes vacuously.
  4. Explain why `PYTHONPATH` must stay: the active venv's editable finder maps `packages` to the active checkout, so without it any subprocess started by path would silently import that checkout's code.
- **Recorded deviation.** The recipe calls the venv's `python` (found through `PATH`) directly instead of `uv run`. `uv run` in a worktree without its own venv would target a different environment; the recipe never creates or syncs one.
- **Test A** (`tests/test_canonical_precip_copyback_backfill.py` around `:1355-1371`): build a single `env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}` and pass it to both the outside and the inside subprocess calls, so the "difference is the cwd and nothing else" comment stays true.
- **Test B** (`tests/test_timeseries_storage_schemas.py:15-19`): if `shutil.which` finds nothing, fall back to `Path(sys.executable).parent / "check-jsonschema"`. Do not call `.resolve()`, which would jump to the uv-managed base interpreter's bin. Use the fallback only if it exists and is executable. If neither is found, the error message names both locations checked.
