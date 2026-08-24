# Tasks

## 1. Red first

- [x] 1.1 Add a test that publishes a basin whose `cfg.calib` multiplier is
  over the previously enforced `SOIL_ALPHA` bound and asserts the published
  `cfg.calib` is byte-identical to source. It MUST fail on current `main`.
- [x] 1.2 Same for `GEOL_DMAC`.
- [x] 1.3 Add a test asserting the publication receipt's `repairs` list contains
  no `basins.calibration_repair.v1` entry. (The package manifest has no repair
  field for any repair kind, so the receipt is the only assertable seam.)
- [x] 1.4 Add a test that runs the real (non-mocked) publish with
  `repair_missing_radiation=True` on a basin missing only `*.tsd.rl` whose
  `cfg.calib` is over the deleted bound, and asserts all three bullets of the
  "missing radiation template" scenario together: the supplied `*.tsd.rl` is in
  the package, the run records the
  `basins.missing_tsd_rl_template_repair.v1` repair, and the published
  `cfg.calib` is byte-identical to source.

## 2. Delete

- [x] 2.1 Delete `workers/model_registry/basins_soil_alpha_repair.py`.
- [x] 2.2 Remove the four imports at `scripts/publish_scheduler_file_registry.py:62-71`.
- [x] 2.3 Remove `"repaired-basins-calibration"` and `"repaired-basins-soil-alpha"`
  from `REPAIR_STAGING_DIR_NAMES`; keep `"repaired-basins"`.
- [x] 2.4 Delete `_repair_calibrated_shud_contexts`, `_repair_calibrated_shud_context`,
  `_isolated_root_for_source_path`, `_merge_repairs`.
- [x] 2.5 Remove both call sites (`:206-210`, `:229-234`) so contexts flow through unchanged.
- [x] 2.6 Delete `tests/test_publish_scheduler_file_registry.py` soil-alpha tests
  (`:1246`, `:1272`), the import at `:25`, and the assertions at `:1393`, `:1447`.
  Preserve the radiation-repair assertions at `:856-857`, `:949`, `:1385-1391`.

## 3. Docs

- [x] 3.1 Rewrite `docs/runbooks/current-production-ops.md:308-310` so it records
  the repair as removed and points at #1816, rather than stating the bound as
  operative fact.

## 4. Verify

- [x] 4.1 `uv run pytest -q tests/test_publish_scheduler_file_registry.py tests/test_basins_package_publication.py`
- [x] 4.2 `uv run ruff check .`
- [x] 4.3 `grep -rn basins_soil_alpha_repair` returns nothing.
- [x] 4.4 `openspec validate delete-silent-calibration-bounds-repair --strict --no-interactive`

## Evidence Floor

- Red-first proof: the tests in §1 fail before §2 and pass after (paste both runs).
- `uv run pytest -q` clean on the two named test files.
- `uv run ruff check .` zero findings.
- Zero remaining references to the deleted module anywhere in the tree.
- Radiation-template repair still passes its existing tests unmodified.
- node-22 republish receipt (separate operation, tracked on #1816): for each of
  the 8 basins, `cmp` of published `cfg.calib` against
  `/volume/nwm/Basins/<slug>/input/*/[!.]*.cfg.calib` exits 0.
