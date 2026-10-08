# node-27 grib lane receipt (#2700, PR #2776)

- Host: node-27, `/home/nwm/NWM` at `66c9c903` (merge commit of PR #2776), Python 3.11.15, node-27 date 2026-10-08.
- Environment: the lane environment of `docs/runbooks/ci-test-routing.md` (`NHMS_GRIB_ENV_ROOT=/home/nwm/nhms-grib`,
  `LD_LIBRARY_PATH`, `ECCODES_DIR`, `ECCODES_DEFINITION_PATH`), `TMPDIR=/home/nwm/tmp`, `DATABASE_URL` unset.
- Command: `NHMS_RUN_E2E=1 NHMS_RUN_GRIB=1 uv run --no-sync pytest -m grib -v -rA -p no:cacheprovider`
  (the `grib` selection only, not the runbook's `-m "e2e or grib"`: the `e2e` half was not run).
- Result: `3 passed, 23267 deselected in 75.00s`; each test printed `ecCodes 2.47.0`.
  - `tests/test_e2e_ifs.py::test_ifs_adapter_canonical_forcing_run_parse_e2e`
  - `tests/test_e2e_ifs.py::test_ifs_06z_144h_manifest_context_and_forcing_limit`
  - `tests/test_canonical_converter.py::test_gfs_grib2_bundles_decode_through_cfgrib_without_netcdf4_fallback`
- Log on node-27: `artifacts/ci-routing/grib-2700-2026-10-08.log`,
  sha256 `39d9cbc7ac165889275b10d18cb0ad0a469494c2dbe44421bb45619b7f4f17e3`.
- No database was opened and no file outside the temporary directory and the log was written; `md0` was
  `[2/2] [UU]` before and after.

CI (PR #2776, `48c94d7da`): Unit Tests `6330 passed, 23 skipped`; `tests/test_production_met_validation.py`
was in the targeted selection. The job log is `-q` output and does not name individual tests or attribute the
skips; the six validate-met tests carry no marker any more and the file runs 43 passed / 0 skipped locally
without any opt-in variable.

Review: 2 rounds (round 1 not clean on P2 items, one fix pass; round 2 clean).
